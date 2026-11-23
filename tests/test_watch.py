"""Fleet desired state and watch service through the host CLI and fake AX."""

import fcntl
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_ax_orbi import AGENT, CLI, CONFIG, FAKES, Sandbox, cli, wait_until


class FleetTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ax-orbi-fleet-")))
        self.fleet = self.root / "fleet"
        self.home = self.root / "state"
        self.desired = self.home / "deployments" / "demo" / "desired.json"
        self.env = dict(os.environ, AX_ORBI_FLEET=str(self.fleet), AX_ORBI_HOME=str(self.home),
                        FAKE_AX_DIR=str(self.root), FAKE_AX_AGENT=str(AGENT))

    def config(self, path, name="demo", ax=FAKES / "ax"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG.replace('name = "demo"', f'name = "{name}"') +
                        f'[ax]\nbin = {json.dumps(str(ax))}\n')
        return path

    def process(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=15)

    def command(self, *args, code=0):
        proc = self.process(*args, "--json")
        self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["exitCode"], code, result)
        return result

    def status(self, name="demo"):
        result = self.command("status", name)
        self.assertEqual(set(result), {"deployments", "exitCode"})
        entry, = result["deployments"]
        return entry


class WatchTests(FleetTestCase):
    def deployment(self, name="demo", failing=False):
        sb = Sandbox(self, with_config=False)
        wrapper = self.root / f"ax-{name}"
        env = {key: sb.env[key] for key in ("PATH", "AX_ORBI_ORBI_BIN", "AX_ORBI_VERSIONS_FILE",
                                           "FAKE_ORBI_MODE_FILE", "FAKE_ORBI_LOG")}
        env.update(FAKE_AX_DIR=str(sb.tmp), FAKE_AX_AGENT=str(AGENT), FAKE_AX_TASK=f"orbi-{name}",
                   FAKE_AX_REPO_DIR=str(sb.repo), FAKE_AX_DEPLOY_HOME=str(sb.tmp / "deploy"))
        exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())
        sb.ax = wrapper
        sb.ax_env = "#!/bin/sh\n" + exports
        self.wrap_ax(sb, "exit 1\n" if failing else "")
        self.config(self.fleet / f"{name}.toml", name, wrapper)
        self.command("enable", name)
        self.addCleanup(self.fake_ax, sb, "suspend", "task", f"orbi-{name}")
        self.addCleanup(self.wrap_ax, sb)  # Restore the fake even after a failure injection.
        return sb

    def wrap_ax(self, sb, before=""):
        sb.ax.write_text(sb.ax_env + before + f'exec {shlex.quote(str(FAKES / "ax"))} "$@"\n')
        sb.ax.chmod(0o755)

    def fake_ax(self, sb, *args):
        return subprocess.run([str(sb.ax), *args], env=self.env, cwd=self.root, text=True,
                              capture_output=True, timeout=12, check=True)

    def watch_path(self, name="demo"):
        return self.home / "deployments" / name / "watch.json"

    def watch_state(self, name="demo"):
        return json.loads(self.watch_path(name).read_text())

    def watch_once(self, *args):
        proc = self.process("watch", "--once", *args)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def reach_tick(self, name="demo"):
        phases = []
        for _ in range(10):
            self.watch_once()
            phases.append(self.watch_state(name)["phase"])
            if phases[-1] == "ticking":
                return phases
        self.fail(f"{name} never reached ticking: {phases}; {self.watch_state(name)}")

    def make_ready(self, sb):
        self.watch_once()
        self.watch_once()
        sb.wait_ready()

    def calls(self, sb):
        return [json.loads(line) for line in (sb.tmp / "ax-calls.jsonl").read_text().splitlines()]

    def requests(self, sb):
        return [args[-1] for args in self.calls(sb) if "request-tick" in args]

    def make_due(self):
        state = self.watch_state()
        state["nextActionAt"] = "2000-01-01T00:00:00+00:00"
        self.watch_path().write_text(json.dumps(state))

    def test_paused_deployment_is_left_alone(self):
        sb = self.deployment()
        self.command("pause", "demo", "--reason", "maintenance")
        before = self.desired.read_bytes()
        for _ in range(3):
            self.watch_once()
        self.assertEqual(self.watch_state()["phase"], "paused")
        self.assertEqual(self.desired.read_bytes(), before)
        self.assertFalse((sb.tmp / "ax-calls.jsonl").exists())

    def test_pause_during_ax_lookup_prevents_a_change(self):
        sb = self.deployment()
        lookup = self.root / "in-get"
        release = self.root / "release-get"
        self.wrap_ax(sb, f'if [ "$3" = get ]; then\n  : > {shlex.quote(str(lookup))}\n'
                     f'  timeout --foreground 5s sh -c \'while [ ! -f "$1" ]; do sleep 0.05; done\' sh '
                     f'{shlex.quote(str(release))} || exit "$?"\nfi\n')
        proc = subprocess.Popen([sys.executable, str(CLI), "watch", "--once"], env=self.env,
                                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lookup.exists, timeout=4, what="watch to start the AX lookup")
            self.command("pause", "demo")
            release.touch()
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, out + err)
        finally:
            release.touch()
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        state = self.watch_state()
        self.assertEqual((state["desired"], state["phase"]), ("paused", "paused"))
        self.assertEqual(self.calls(sb), [["get", "tasks"]])

    def test_ax_timeout_stops_wrapper_children(self):
        sb = self.deployment()
        worker_pid = self.root / "worker.pid"
        self.wrap_ax(sb, f'if [ "$3" = apply ]; then\n  sleep 4 &\n  echo "$!" > '
                     f'{shlex.quote(str(worker_pid))}\n  wait\nfi\n')
        ax = cli.Ax(cli.load_config(self.fleet / "demo.toml"), start_new_session=True)
        with patch.dict(os.environ, self.env):
            code, _, error = ax.run("apply", "-f", "-", stdin="", timeout=0.5)
        self.assertEqual(code, 124, error)
        worker = Path("/proc") / worker_pid.read_text().strip() / "stat"

        def stopped():
            try:
                return worker.read_text().rsplit(") ", 1)[1].startswith("Z ")
            except FileNotFoundError:
                return True

        wait_until(stopped, timeout=1, what="the timed out AX wrapper's worker to exit")

    def test_enabled_deployment_reaches_a_tick(self):
        sb = self.deployment()
        phases = self.reach_tick()
        self.assertEqual(phases[:2], ["preparing", "resuming"])
        state = self.watch_state()
        run_id = state["pendingTick"]["id"]
        self.assertRegex(run_id, r"^watch-\d{8}T\d{6}Z-[a-f0-9]{6}$")
        sb.wait_status(run_id, "succeeded")
        self.assertEqual(sb.ticks(), 1)
        before_status = self.calls(sb)
        status = self.status()
        self.assertEqual(status["watch"], state)
        self.assertGreaterEqual(status["watchAgeSeconds"], 0)
        proc = self.process("status", "demo")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f'ticking: {state["detail"]}', proc.stdout)
        self.assertRegex(proc.stdout, r"\b\d+s ago\b")
        self.assertEqual(self.calls(sb), before_status)

    def test_next_tick_waits_for_the_interval(self):
        sb = self.deployment()
        config = self.fleet / "demo.toml"
        config.write_text(config.read_text() + "[watch]\ntick_interval = 75\n")
        self.reach_tick()
        first = self.watch_state()["pendingTick"]["id"]
        sb.wait_status(first, "succeeded")
        self.watch_once()
        state = self.watch_state()
        self.assertEqual(state["phase"], "idle", state)
        self.assertEqual(state["lastTick"]["id"], first)
        seconds = (datetime.fromisoformat(state["nextActionAt"]) - datetime.fromisoformat(state["at"])).total_seconds()
        self.assertAlmostEqual(seconds, 75, delta=1)
        self.watch_once()
        self.assertEqual(self.watch_state()["phase"], "idle")
        self.assertEqual(self.requests(sb), [first])

        self.make_due()
        self.watch_once()
        second = self.watch_state()["pendingTick"]["id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.requests(sb), [first, second])
        sb.wait_status(second, "succeeded")
        self.assertEqual(sb.ticks(), 2)

    def test_pending_tick_is_not_started_twice(self):
        sb = self.deployment()
        self.make_ready(sb)
        # Deliver the request, then kill watch before it can process the AX response.
        self.wrap_ax(sb, f'if [ "${{7-}}" = request-tick ]; then\n'
                         f'  {shlex.quote(str(FAKES / "ax"))} "$@" || exit "$?"\n'
                         '  kill -KILL "$PPID"\n  exit 0\nfi\n')
        proc = self.process("watch", "--once")
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stdout + proc.stderr)
        run_id = self.watch_state()["pendingTick"]["id"]
        self.wrap_ax(sb)
        sb.wait_status(run_id, "succeeded")
        self.watch_once()
        state = self.watch_state()
        self.assertIsNone(state["pendingTick"])
        self.assertEqual(state["lastTick"]["id"], run_id)
        self.assertEqual(sb.ticks(), 1)
        self.assertEqual(set(self.requests(sb)), {run_id})

    def test_failed_request_reuses_the_pending_id(self):
        sb = self.deployment()
        self.make_ready(sb)
        self.wrap_ax(sb, 'if [ "${7-}" = request-tick ]; then echo "request lost" >&2; exit 1; fi\n')
        self.watch_once()
        state = self.watch_state()
        run_id = state["pendingTick"]["id"]
        self.assertEqual(state["phase"], "blocked")
        self.assertIn("request lost", state["detail"])
        self.assertEqual(sb.ticks(), 0)
        self.wrap_ax(sb)
        self.watch_once()
        self.assertEqual(self.watch_state()["pendingTick"], state["pendingTick"])
        self.assertEqual(self.requests(sb), [run_id])
        sb.wait_status(run_id, "succeeded")
        self.assertEqual(sb.ticks(), 1)

    def test_pending_tick_is_synced_before_retrying_a_failed_write(self):
        sb = self.deployment()
        self.make_ready(sb)
        sync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("simulated watch state directory sync failure")
            sync(fd)

        # The rename was visible, but the pending id was not yet durable.
        with (patch.dict(os.environ, self.env), patch.object(cli.os, "fsync", fail_directory_sync),
              patch("sys.stderr")):
            self.assertEqual(cli.main(["watch", "--once"]), 0)
            pending = self.watch_state()["pendingTick"]
            self.assertEqual(cli.main(["watch", "--once"]), 0)
            self.assertEqual(self.watch_state()["pendingTick"], pending)
        self.assertEqual(self.requests(sb), [])
        self.watch_once()
        self.assertEqual(self.requests(sb), [pending["id"]])
        sb.wait_status(pending["id"], "succeeded")

    def test_one_failing_deployment_does_not_stop_the_other(self):
        broken = self.deployment("broken", failing=True)
        good = self.deployment()
        self.reach_tick()
        entries = {entry["name"]: entry for entry in self.command("status")["deployments"]}
        self.assertEqual(entries["broken"]["watch"]["phase"], "waiting")
        self.assertEqual(entries["broken"]["watch"]["detail"], "Waiting for AX to answer")
        run_id = entries["demo"]["watch"]["pendingTick"]["id"]
        good.wait_status(run_id, "succeeded")
        self.wrap_ax(broken)
        self.watch_once()
        self.assertEqual(self.status("broken")["watch"]["phase"], "preparing")

    def test_interrupted_tick_is_kept_and_work_continues(self):
        sb = self.deployment()
        sb.set_mode("hang")
        self.reach_tick()
        first = self.watch_state()["pendingTick"]["id"]
        wait_until(sb.ticks, what="the first tick to start")
        self.fake_ax(sb, "suspend", "task", "orbi-demo")
        self.assertEqual(json.loads(self.desired.read_text())["mode"], "enabled")
        sb.set_mode("ok")
        self.watch_once()
        self.assertEqual(self.watch_state()["phase"], "resuming")
        sb.wait_ready()
        self.watch_once()
        state = self.watch_state()
        self.assertEqual(state["phase"], "idle", state)
        self.assertEqual(state["lastTick"]["id"], first)
        self.assertEqual(state["lastTick"]["status"], "interrupted")
        self.make_due()
        self.watch_once()
        second = self.watch_state()["pendingTick"]["id"]
        self.assertNotEqual(first, second)
        sb.wait_status(second, "succeeded")
        self.assertEqual(sb.run_record(first)["status"], "interrupted")
        self.assertEqual(self.requests(sb), [first, second])
        for run_id in (first, second):
            self.assertEqual((sb.state / "runs" / run_id / "tick.log").read_text().count("fake orbi tick:"), 1)
        self.assertEqual(sb.ticks(), 2)

    def test_operator_tick_is_active_but_not_recorded_by_watch(self):
        sb = self.deployment()
        self.make_ready(sb)
        sb.set_mode("hang")
        self.command("run", "--config", str(self.fleet / "demo.toml"), "--id", "operator", "--no-wait")
        wait_until(sb.ticks, what="the operator tick to start")
        self.watch_once()
        state = self.watch_state()
        self.assertEqual(state["phase"], "ticking", state)
        self.assertIn("operator", state["detail"])
        self.assertIsNone(state["pendingTick"])
        self.assertIsNone(state["lastTick"])
        self.assertEqual(self.requests(sb), ["operator"])

    def test_starting_since_clears_when_agent_starts_and_stale_running_is_ignored(self):
        sb = self.deployment()
        self.make_ready(sb)
        self.env["FAKE_AX_STATUS"] = '{"started": false}'
        self.watch_once()
        state = self.watch_state()
        self.assertEqual(state["phase"], "starting")
        since = state["startingSince"]
        self.assertIsNotNone(since)
        self.watch_once()
        self.assertEqual(self.watch_state()["startingSince"], since)

        status = sb.status()
        status["prepare"]["ok"] = False
        self.env["FAKE_AX_STATUS"] = json.dumps(status)
        self.watch_once()
        state = self.watch_state()
        self.assertEqual(state["phase"], "blocked")
        self.assertIsNone(state["startingSince"])
        self.assertEqual(self.requests(sb), [])

        status["prepare"]["ok"] = True
        status["counts"] = {"running": 1}
        self.assertIsNone(status["health"]["currentRun"])
        self.env["FAKE_AX_STATUS"] = json.dumps(status)
        self.watch_once()
        run_id = self.watch_state()["pendingTick"]["id"]
        self.assertEqual(self.requests(sb), [run_id])
        sb.wait_status(run_id, "succeeded")

    def test_second_watch_refuses_to_start(self):
        self.home.mkdir()
        with (self.home / "watch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = self.process("watch", "--once")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("Another ax-orbi watch holds", proc.stdout)
            self.assertIn("watch.lock", proc.stdout)
        self.watch_once()

    def test_watch_finishes_the_current_step_on_sigint(self):
        sb = self.deployment()
        self.deployment("next")
        marker = self.root / "in-apply"
        self.wrap_ax(sb, f'if [ "$3" = apply ]; then : > {shlex.quote(str(marker))}; sleep 0.5; fi\n')
        proc = subprocess.Popen([sys.executable, str(CLI), "watch"], env=self.env, cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            wait_until(marker.exists, timeout=5, what="watch to start apply")
            os.killpg(proc.pid, signal.SIGINT)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, out + err)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        self.assertEqual(self.watch_state()["phase"], "preparing")
        self.assertFalse(self.watch_path("next").exists())
        self.watch_once()
        self.assertEqual(self.watch_state("next")["phase"], "preparing")

    def test_watch_loops_over_an_override_fleet_until_sigterm(self):
        self.deployment()
        alternate = self.root / "alternate"
        self.fleet.rename(alternate)
        proc = subprocess.Popen([sys.executable, str(CLI), "watch", "--interval", "0.05", "--fleet", str(alternate)],
                                env=self.env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lambda: self.watch_path().exists() and self.watch_state()["phase"] == "ticking",
                       timeout=8, what="watch to start a tick in the override fleet")
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, out + err)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        self.watch_once("--fleet", str(alternate))

    def test_watch_table_is_not_rendered(self):
        config = self.config(self.root / "demo.toml")
        original = config.read_text()
        render_args = ("render", "--config", str(config))
        without = self.process(*render_args)
        config.write_text(original + "[watch]\ntick_interval = 60\n")
        with_watch = self.process(*render_args)
        self.assertEqual(without.returncode, 0, without.stdout + without.stderr)
        self.assertEqual(with_watch.returncode, 0, with_watch.stdout + with_watch.stderr)
        self.assertEqual(without.stdout, with_watch.stdout)
        for invalid in ("10", "60.0", "true"):
            with self.subTest(interval=invalid):
                config.write_text(original + f"[watch]\ntick_interval = {invalid}\n")
                proc = self.process(*render_args)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("watch.tick_interval must be an integer of at least 60 seconds", proc.stdout)

    def test_config_error_keeps_identity_and_the_last_notify_command(self):
        sb = self.deployment()
        self.command("pause", "demo")
        config = self.fleet / "demo.toml"
        valid = config.read_text() + '[watch]\nnotify_command = ["never-run-this"]\n'
        config.write_text(valid)
        self.watch_once()
        self.assertEqual(self.watch_state()["notifyCommand"], ["never-run-this"])
        config.write_text(valid.replace('["never-run-this"]', "2026-11-23"))
        self.watch_once()
        self.assertEqual(self.watch_state()["phase"], "config-error")
        self.assertIn("watch.notify_command", self.watch_state()["detail"])
        self.assertEqual(self.watch_state()["notifyCommand"], ["never-run-this"])

        config.write_text(valid + "tick_interval = 10\n")
        (self.fleet / "bad.toml").write_text("not valid TOML")
        self.config(self.fleet / "unregistered.toml", "unregistered")
        self.watch_once()
        entries = {entry["name"]: entry for entry in self.command("status")["deployments"]}
        self.assertEqual(entries["demo"]["watch"]["phase"], "config-error")
        self.assertEqual(entries["demo"]["watch"]["notifyCommand"], ["never-run-this"])
        self.assertIn("configError", entries[None])
        self.assertNotIn("watch", entries[None])
        self.assertNotIn("watch", entries["unregistered"])
        self.assertEqual(list(self.home.glob("deployments/*/watch.json")), [self.watch_path()])
        self.assertFalse((sb.tmp / "ax-calls.jsonl").exists())

    def test_fixed_config_without_a_record_is_not_watched(self):
        config = self.config(self.fleet / "demo.toml")
        original = config.read_text()
        config.write_text(original + "[watch]\ntick_interval = 10\n")
        self.watch_once()
        self.assertEqual(self.status()["watch"]["phase"], "config-error")
        config.write_text(original)
        self.watch_once()
        entry = self.status()
        self.assertEqual(entry["desired"], "not-enabled")
        self.assertNotIn("watch", entry)

    def test_print_unit_names_this_checkout(self):
        proc = self.process("watch", "--print-unit")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        start, = [line for line in proc.stdout.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(shlex.split(start.removeprefix("ExecStart=")), [str(CLI.absolute()), "watch"])
        self.assertIn("Restart=on-failure", proc.stdout.splitlines())
        self.assertFalse(self.home.exists())


class FleetTests(FleetTestCase):
    def test_enable_pause_resume_round_trip(self):
        config = self.config(self.fleet / "arbitrary-filename.toml")
        self.assertEqual(self.status(), {"name": "demo", "repository": "example/demo",
                                         "configPath": str(config), "desired": "not-enabled",
                                         "reason": None, "changedAt": None})
        self.command("enable", "demo")
        self.assertEqual(self.status()["desired"], "enabled")
        self.command("pause", "demo", "--reason", "x")
        paused = self.status()
        self.assertEqual((paused["desired"], paused["reason"]), ("paused", "x"))
        self.assertEqual(datetime.fromisoformat(paused["changedAt"]).tzinfo, timezone.utc)
        self.assertEqual(json.loads(self.desired.read_text()),
                         {"version": 1, "mode": "paused", "reason": "x", "changedAt": paused["changedAt"]})
        self.command("resume", "demo")
        self.assertEqual(self.status()["desired"], "enabled")
        self.assertFalse((self.root / "ax-calls.jsonl").exists())

    def test_pause_survives_a_new_process(self):
        self.config(self.fleet / "demo.toml")
        self.command("enable", "demo")
        self.command("pause", "demo", "--reason", "maintenance")
        before = self.desired.read_bytes()
        # Every call launches bin/ax-orbi again, with no in-process state.
        proc = self.process("status")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("demo", proc.stdout)
        self.assertIn("example/demo", proc.stdout)
        self.assertIn("paused", proc.stdout)
        self.assertIn("maintenance", proc.stdout)
        self.assertIn(json.loads(before)["changedAt"], proc.stdout)
        self.assertEqual(self.desired.read_bytes(), before)

    def test_enable_by_config_path_links_the_file(self):
        config = self.config(self.root / "external.toml")
        self.command("enable", "--config", config.name)
        link = self.fleet / "demo.toml"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), config)
        self.assertEqual(self.status()["desired"], "enabled")
        self.command("enable", "--config", config.name)  # reuse the same registration
        before = self.desired.read_bytes()
        other = self.config(self.root / "different.toml")
        self.command("enable", "--config", str(other), code=2)
        self.assertEqual(link.readlink(), config)
        self.assertEqual(self.desired.read_bytes(), before)

    def test_unknown_or_invalid_names_are_refused(self):
        for command in ("enable", "pause", "resume"):
            self.command(command, "unknown", code=2)
        self.assertEqual(self.command("status", "unknown", code=2)["deployments"], [])
        proc = self.process("enable", "unknown")
        self.assertIn("enable --config", proc.stdout)  # tell the operator how to register it

        config = self.config(self.fleet / "renamed.toml", "invalid")
        config.write_text(config.read_text().replace("[image]", "[not-image]"))
        self.command("enable", "invalid", code=2)
        self.command("enable", "--config", str(config), code=2)
        self.assertIn("image.ref", self.status("invalid")["configError"])
        self.assertFalse(self.home.exists())

    def test_duplicate_names_are_reported_not_acted_on(self):
        self.config(self.fleet / "one.toml")
        self.command("enable", "demo")
        before = self.desired.read_bytes()
        self.config(self.fleet / "two.toml")
        deployments = self.command("status")["deployments"]
        self.assertEqual([entry["name"] for entry in deployments], ["demo", "demo"])
        self.assertTrue(all("duplicate" in entry["configError"] for entry in deployments))
        for command in ("enable", "pause", "resume"):
            self.command(command, "demo", code=2)
        self.assertEqual(self.desired.read_bytes(), before)

    def test_unreadable_fleet_links_do_not_hide_valid_configs(self):
        self.config(self.fleet / "demo.toml")
        self.command("enable", "demo")
        pipe = self.root / "pipe"
        os.mkfifo(pipe)
        missing = self.fleet / "missing.toml"
        missing.symlink_to(self.root / "missing")
        blocked = self.fleet / "pipe.toml"
        blocked.symlink_to(pipe)

        entries = {entry["configPath"]: entry for entry in self.command("status")["deployments"]}
        self.assertEqual(entries[str(self.fleet / "demo.toml")]["desired"], "enabled")
        self.assertIn("configError", entries[str(missing)])
        self.assertIn("regular file", entries[str(blocked)]["configError"])
        self.command("enable", "--config", str(pipe), code=2)

    def test_status_runs_no_ax_command(self):
        self.config(self.fleet / "demo.toml")
        self.assertEqual(self.status()["desired"], "not-enabled")
        self.assertFalse((self.root / "ax-calls.jsonl").exists())
        self.assertFalse(self.home.exists())
        watch = self.desired.with_name("watch.json")
        watch.parent.mkdir(parents=True)
        watch.write_text('{"version": 1, "lastTick": "example"}')
        self.assertEqual(self.status()["watch"], json.loads(watch.read_text()))
        self.assertFalse((self.root / "ax-calls.jsonl").exists())

    def test_fleet_flag_overrides_environment(self):
        alternate = self.root / "other-fleet"
        config = self.config(alternate / "demo.toml")
        self.assertEqual(self.command("status")["deployments"], [])
        self.command("enable", "demo", "--fleet", str(alternate))
        entry, = self.command("status", "--fleet", str(alternate))["deployments"]
        self.assertEqual(entry["configPath"], str(config))
        self.assertEqual(entry["desired"], "enabled")

    def test_default_host_layout(self):
        config_home = self.root / "config"
        state_home = self.root / "state"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home),
                                     "XDG_STATE_HOME": str(state_home)}, clear=True):
            self.assertEqual(cli.fleet_dir(None), config_home / "ax-orbi" / "deployments")
            self.assertEqual(cli.desired_path("demo"), state_home / "ax-orbi/deployments/demo/desired.json")
        with patch.dict(os.environ, {"HOME": str(self.root)}, clear=True):
            self.assertEqual(cli.fleet_dir(None), self.root / ".config/ax-orbi/deployments")
            self.assertEqual(cli.desired_path("demo"),
                             self.root / ".local/state/ax-orbi/deployments/demo/desired.json")

    def test_stop_pauses_an_enabled_deployment(self):
        sb = Sandbox(self)
        self.env = dict(sb.env, **{key: self.env[key] for key in ("AX_ORBI_HOME", "AX_ORBI_FLEET")},
                        FAKE_AX_DIR=str(sb.tmp), FAKE_AX_AGENT=str(AGENT))
        self.env.pop("AX_ORBI_CONFIG")
        config = self.config(self.fleet / "demo.toml")

        def fake_ax(*args, **kwargs):
            return subprocess.run([str(FAKES / "ax"), *args], env=self.env, text=True,
                                  capture_output=True, timeout=15, **kwargs)

        self.addCleanup(fake_ax, "suspend", "task", "orbi-demo")
        fake_ax("apply", "-f", "-", input=cli.render(cli.load_config(config)), check=True)
        state_path = sb.tmp / "ax-state.json"
        state = json.loads(state_path.read_text())
        agent_cfg = json.loads(state["env"]["AX_ORBI_CONFIG"])
        agent_cfg.update(repo_dir=str(sb.repo), deploy_home=str(sb.tmp / "deploy"))
        state["env"]["AX_ORBI_CONFIG"] = json.dumps(agent_cfg)
        state_path.write_text(json.dumps(state))
        self.command("prepare", "--config", str(config), "--timeout", "10")

        def stop(*args, code=0):
            return self.command("stop", "--config", str(config), *args, code=code)

        self.command("enable", "demo")
        enabled = self.desired.read_bytes()
        stop("--delete", code=2)
        self.assertEqual(self.desired.read_bytes(), enabled)
        self.env["FAKE_AX_STATUS"] = json.dumps({"supervisorAlive": True,
                                                 "health": {"currentRun": "busy", "state": "running"},
                                                 "counts": {"running": 1}})
        stop(code=1)
        self.assertEqual(self.desired.read_bytes(), enabled)
        del self.env["FAKE_AX_STATUS"]
        self.env["FAKE_AX_FAIL_SSH"] = "1"
        refusal = stop(code=3)["checks"][-1]
        self.assertIn("cannot confirm the sandbox is idle", refusal["detail"])
        self.assertIn("--force", refusal["fix"])
        self.assertEqual(self.desired.read_bytes(), enabled)
        del self.env["FAKE_AX_FAIL_SSH"]

        for expected_state, args, next_check in (
            ("Working", (), "suspend"),
            ("Suspended", (), "suspend"),
            ("Suspended", ("--delete", "--yes-delete-state"), "delete:task"),
            ("NotFound", (), None),
        ):
            with self.subTest(state=expected_state, args=args):
                self.command("resume", "demo")
                result = stop(*args)
                self.assertEqual(result["taskState"], expected_state)
                checks = [check["name"] for check in result["checks"]]
                self.assertIn("pause", checks)
                if next_check:
                    self.assertLess(checks.index("pause"), checks.index(next_check))
                paused = self.status()
                self.assertEqual((paused["desired"], paused["reason"]),
                                 ("paused", "stopped with ax-orbi stop"))

        self.command("resume", "demo")
        config.write_text(config.read_text().replace(str(FAKES / "ax"), str(self.root / "missing-ax")))
        stop("--delete", "--yes-delete-state", code=1)
        self.assertEqual(self.status()["desired"], "paused", "keep the pause after an AX failure")
        config.write_text(config.read_text().replace(str(self.root / "missing-ax"), str(FAKES / "ax")))
        self.desired.unlink()
        self.assertFalse(any(check["name"] == "pause" for check in stop()["checks"]))
        self.assertFalse(self.desired.exists())
