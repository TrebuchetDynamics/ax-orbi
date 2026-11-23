"""Tests for the in-sandbox agent and the host CLI.

No cluster and no network: Orbi, Pi, gh and `ax` are the stand-ins in
tests/fakes. What is real is every line of ax-orbi-agent and bin/ax-orbi,
including the process handling: supervisors are started, signalled and killed
as separate processes.

    python3 -m unittest discover -s tests -v
"""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import suppress
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes"
AGENT = ROOT / "image" / "ax-orbi-agent"
CLI = ROOT / "bin" / "ax-orbi"


def load(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load(CLI, "ax_orbi_cli")
agent = load(AGENT, "ax_orbi_agent")

CONFIG = """
name = "demo"
[repository]
source = "example/demo"
[image]
ref = "localhost:5001/ax-orbi@sha256:%s"
[model]
provider = "deepseek"
model = "deepseek-flash"
base_url = "https://api.deepseek.com"
[credentials.github]
secret = "ax-system/orbi-demo-github"
[credentials.model]
secret = "ax-system/orbi-demo-model"
[network]
allow = ["pypi.org"]
[orbi]
message_language = "en"
""" % ("a" * 64)


def wait_until(predicate, timeout=15, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Sandbox:
    """A temp directory standing in for /workspace, plus helpers to drive it."""

    def __init__(self, test: unittest.TestCase, with_config: bool = True):
        self.tmp = Path(tempfile.mkdtemp(prefix="ax-orbi-test-"))
        test.addCleanup(self.cleanup)
        self.state = self.tmp / "workspace" / ".ax-orbi"
        self.repo = self.tmp / "workspace" / "repo"
        self.repo.mkdir(parents=True)
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run(git + ["remote", "add", "origin", "https://github.com/example/demo.git"], check=True)
        (self.tmp / "deploy").mkdir()
        self.mode_file = self.tmp / "mode"
        self.tick_log = self.tmp / "ticks.log"
        self.versions_file = self.tmp / "versions.json"
        self.set_mode("ok")
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{FAKES}:{os.environ['PATH']}",
            "AX_ORBI_HOME": str(self.tmp / "host-state"),
            "AX_ORBI_FLEET": str(self.tmp / "fleet"),
            "AX_ORBI_STATE_DIR": str(self.state),
            "AX_ORBI_ORBI_BIN": str(FAKES / "orbi"),
            "AX_ORBI_VERSIONS_FILE": str(self.versions_file),
            "FAKE_ORBI_MODE_FILE": str(self.mode_file),
            "FAKE_ORBI_LOG": str(self.tick_log),
            "GH_TOKEN": "placeholder",
        })
        pins = cli.load_versions()
        self.versions_file.write_text(json.dumps({
            "ax_ref": pins["ax"]["ref"], "orbi_tag": pins["orbi"]["tag"],
            "orbi_commit": pins["orbi"]["commit"], "pi_version": pins["pi"]["version"],
            "gh_version": pins["gh"]["version"],
        }))
        if with_config:
            self.env["AX_ORBI_CONFIG"] = json.dumps({
                "source_repo": "example/demo", "repo_dir": str(self.repo),
                "deploy_home": str(self.tmp / "deploy"),
                "pi": {"provider": "p", "model": "m", "base_url": "https://api.example.com"}})
        self.procs: list[subprocess.Popen] = []

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout:
                proc.stdout.close()
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def set_mode(self, mode: str):
        self.mode_file.write_text(mode)

    def configure_project(self, *, check=True):
        (self.repo / "setup.sh").write_text('set -eu\n: > dependency\nprintf "setup\\n" >> setup-count\n')
        project = {"setup": "setup.sh", "setup_timeout": 60}
        if check:
            script = self.repo / "check.sh"
            script.write_text('#!/bin/sh\ntest -f dependency || { echo dependency-missing; exit 1; }\n')
            script.chmod(0o755)
            project["check"] = "check.sh"
        cfg = json.loads(self.env["AX_ORBI_CONFIG"])
        self.env["AX_ORBI_CONFIG"] = json.dumps({**cfg, "project": project})

    def ticks(self) -> int:
        return len(self.tick_log.read_text().splitlines()) if self.tick_log.exists() else 0

    def serve(self) -> subprocess.Popen:
        proc = subprocess.Popen([str(AGENT), "serve"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        return proc

    def agent(self, *args: str):
        proc = subprocess.run([str(AGENT), *args], env=self.env, capture_output=True, text=True,
                              timeout=5)
        return proc.returncode, proc.stdout

    def status(self, run: str | None = None) -> dict:
        _, out = self.agent("status", "--json", "--limit", "50", *(["--run", run] if run else []))
        return json.loads(out)

    def run_record(self, run_id: str) -> dict:
        return self.status(run_id)["runs"][0]

    def request(self, run_id: str) -> dict:
        code, out = self.agent("request-tick", "--id", run_id)
        assert code == 0, out
        return json.loads(out)

    def wait_ready(self):
        wait_until(lambda: self.status()["started"], what="supervisor")

    def wait_status(self, run_id: str, *statuses: str) -> dict:
        return wait_until(lambda: (r := self.run_record(run_id))["status"] in statuses and r,
                          what=f"run {run_id} to be {statuses}")


class AgentTests(unittest.TestCase):
    def test_stop_before_launch_starts_nothing(self):
        sb = Sandbox(self)
        sb.request("t-queued")
        supervisor = agent.Supervisor(sb.state)
        supervisor.on_signal(signal.SIGINT, None)
        child = Mock(pid=12345)
        child.wait.return_value = 0
        with (patch.dict(os.environ, sb.env),
              patch.object(agent.subprocess, "Popen", return_value=child) as launch):
            supervisor.execute("t-queued")
        self.assertEqual(launch.call_count, 0)
        self.assertFalse((sb.state / "runs" / "t-queued" / "run.json").exists())
        self.assertEqual(sb.run_record("t-queued")["status"], "queued")

    def test_stop_during_launch_is_recorded_interrupted(self):
        sb = Sandbox(self)
        run_id = "t-launch"
        sb.request(run_id)
        supervisor = agent.Supervisor(sb.state)
        stop_signals = {signal.SIGTERM, signal.SIGINT}
        for signum in stop_signals:
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        old_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, stop_signals)
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, old_mask)

        heartbeat_started = threading.Event()
        heartbeat_mask = set()
        self.addCleanup(supervisor.stopping.set)

        def heartbeat():
            heartbeat_mask.update(signal.pthread_sigmask(signal.SIG_BLOCK, ()))
            heartbeat_started.set()
            supervisor.stopping.wait(5)

        run_file = sb.state / "runs" / run_id / "run.json"
        statuses = []
        write_json = agent.write_json

        def write_and_stop(path, data):
            write_json(path, data)
            if path == run_file:
                statuses.append(agent.read_json(path)["status"])
                if len(statuses) == 1:
                    self.assertEqual(data["status"], "running")
                    self.assertTrue(heartbeat_started.wait(5), "heartbeat did not start")
                    os.kill(os.getpid(), signal.SIGTERM)

        # serve is normally a process entry point; close its workspace lock ourselves.
        flock = agent.fcntl.flock

        def lock_workspace(fd, op):
            self.addCleanup(os.close, fd)
            flock(fd, op)

        child = Mock(pid=12345)
        child.poll.return_value = None
        child.wait.return_value = -signal.SIGTERM
        with (patch.dict(os.environ, sb.env),
              patch.object(supervisor, "heartbeat_loop", heartbeat),
              patch.object(supervisor, "next_request", side_effect=[run_id]),
              patch.object(agent.fcntl, "flock", lock_workspace),
              patch.object(agent, "prepare", return_value={"ok": True, "checks": []}),
              patch.object(agent, "write_json", write_and_stop),
              patch.object(agent.subprocess, "Popen", return_value=child),
              patch.object(agent.os, "killpg") as terminate):
            self.assertEqual(supervisor.serve(), 0)

        record = sb.run_record(run_id)
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["signal"], "SIGTERM")
        self.assertEqual(record["pid"], child.pid)
        self.assertEqual(record["exitCode"], -signal.SIGTERM)
        terminate.assert_called_once_with(child.pid, signal.SIGTERM)
        self.assertNotIn("running", statuses[statuses.index("interrupted"):])
        self.assertTrue(stop_signals <= heartbeat_mask)

    def test_pending_child_signal_does_not_interrupt_supervisor_record(self):
        sb = Sandbox(self)
        run_id = "t-child-signal"
        sb.request(run_id)
        supervisor = agent.Supervisor(sb.state)
        old_handler = signal.signal(signal.SIGTERM, supervisor.on_signal)
        self.addCleanup(signal.signal, signal.SIGTERM, old_handler)
        popen = subprocess.Popen

        def launch(*args, preexec_fn, **kwargs):
            def before_exec():
                # Model a process-group stop pending in the child from before setsid().
                os.kill(os.getpid(), signal.SIGTERM)
                preexec_fn()

            return popen(*args, preexec_fn=before_exec, **kwargs)

        with (patch.dict(os.environ, sb.env),
              patch.object(agent.subprocess, "Popen", side_effect=launch)):
            supervisor.execute(run_id)

        record = sb.run_record(run_id)
        self.assertEqual((record["status"], record["exitCode"]), ("succeeded", 0))

    def test_terminal_record_is_never_downgraded(self):
        sb = Sandbox(self)
        run_id = "t-terminal"
        rdir = sb.state / "runs" / run_id
        rdir.mkdir(parents=True)
        for status in agent.TERMINAL:
            with self.subTest(status=status):
                agent.write_json(rdir / "run.json", {"id": run_id, "status": status})
                agent.update_run(sb.state, run_id, status="running")
                self.assertEqual(agent.load_run(sb.state, run_id)["status"], status)

    def test_prepare_does_not_start_a_delivery(self):
        sb = Sandbox(self)
        sb.serve()
        sb.wait_ready()
        status = sb.status()
        self.assertTrue(status["prepare"]["ok"], status["prepare"])
        self.assertEqual(status["health"]["state"], "idle")
        self.assertEqual(sb.ticks(), 0)
        toml = (sb.state / "orbi.toml").read_text()
        self.assertIn('source_repos = ["example/demo"]', toml)
        tag = cli.load_versions()["orbi"]["tag"]
        self.assertIn(f'engine_source_track = "tag:{tag}"', toml)
        self.assertIn('git_transport = "https"', toml)
        providers = json.loads((sb.state / "pi-providers.json").read_text())
        self.assertEqual(providers["providers"]["p"]["apiKey"], "$PI_API_KEY")
        self.assertIn(".orbi/", (sb.repo / ".git/info/exclude").read_text())

    def test_tick_success_and_failure_are_recorded(self):
        sb = Sandbox(self)
        sb.serve()
        sb.wait_ready()
        sb.request("t-ok")
        ok = sb.wait_status("t-ok", "succeeded", "failed")
        self.assertEqual((ok["status"], ok["exitCode"]), ("succeeded", 0))
        sb.set_mode("fail")
        sb.request("t-fail")
        bad = sb.wait_status("t-fail", "succeeded", "failed")
        self.assertEqual((bad["status"], bad["exitCode"]), ("failed", 1))
        code, log = sb.agent("logs", "--run", "t-fail")
        self.assertEqual(code, 0)
        self.assertIn("runner failed: simulated", log)
        # The tick ran with the generated config and a HOME on the durable volume.
        line = sb.tick_log.read_text().splitlines()[0]
        self.assertIn(f"config={sb.state}/orbi.toml", line)
        self.assertIn(f"home={sb.state}/home", line)

    def test_same_request_id_never_runs_twice(self):
        sb = Sandbox(self)
        sb.serve()
        sb.wait_ready()
        first = sb.request("t-1")
        self.assertFalse(first["duplicate"])
        sb.wait_status("t-1", "succeeded")
        again = sb.request("t-1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["run"]["status"], "succeeded")
        time.sleep(1.5)  # longer than the supervisor's poll interval
        self.assertEqual(sb.ticks(), 1)

    def test_second_supervisor_refuses_to_start(self):
        sb = Sandbox(self)
        sb.serve()
        sb.wait_ready()
        second = sb.serve()
        self.assertEqual(second.wait(timeout=10), 75)
        self.assertIn("another supervisor", second.stdout.read())

    def test_graceful_stop_preserves_work_and_does_not_rerun(self):
        sb = Sandbox(self)
        sb.set_mode("hang")
        first = sb.serve()
        sb.wait_ready()
        sb.request("t-long")
        sb.wait_status("t-long", "running")
        wait_until(sb.ticks, what="the tick to start")
        (sb.repo / "work-in-progress.txt").write_text("unpushed work")
        first.send_signal(signal.SIGTERM)  # what AX's runner sends on suspend
        self.assertEqual(first.wait(timeout=15), 0)
        run = sb.run_record("t-long")
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["signal"], "SIGTERM")
        self.assertFalse(sb.status()["supervisorAlive"])
        self.assertIn("got-sigterm", sb.agent("logs", "--run", "t-long")[1])

        sb.request("t-queued")  # asked for while the sandbox is down
        sb.set_mode("ok")
        sb.serve()  # resume: same files, new process tree
        sb.wait_status("t-queued", "succeeded")
        self.assertEqual(sb.run_record("t-long")["status"], "interrupted")
        self.assertEqual(sb.ticks(), 2)  # t-long once, t-queued once
        self.assertEqual((sb.repo / "work-in-progress.txt").read_text(), "unpushed work")

    def test_hard_kill_is_recovered_at_next_start(self):
        sb = Sandbox(self)
        sb.set_mode("hang")
        first = sb.serve()
        sb.wait_ready()
        sb.request("t-killed")
        run = sb.wait_status("t-killed", "running")
        wait_until(lambda: sb.run_record("t-killed").get("pid"), what="the tick pid")
        tick_pid = sb.run_record("t-killed")["pid"]
        first.kill()  # no chance to write anything
        first.wait()
        os.killpg(tick_pid, signal.SIGKILL)
        self.assertEqual(sb.run_record("t-killed")["status"], "running")  # stale record
        self.assertFalse(sb.status()["supervisorAlive"])  # ...which status does not trust

        sb.set_mode("ok")
        sb.serve()
        sb.wait_ready()
        recovered = sb.wait_status("t-killed", "interrupted")
        self.assertNotEqual(recovered["recoveredAtBoot"], run["bootId"])
        time.sleep(1.5)
        self.assertEqual(sb.ticks(), 1)

    def test_unprepared_environment_refuses_ticks(self):
        sb = Sandbox(self, with_config=False)
        sb.serve()
        sb.wait_ready()
        self.assertFalse(sb.status()["prepare"]["ok"])
        sb.request("t-x")
        run = sb.wait_status("t-x", "failed")
        self.assertIn("not prepared", run["detail"])
        self.assertEqual(sb.ticks(), 0)

    def test_project_setup_runs_when_check_fails(self):
        sb = Sandbox(self)
        sb.configure_project()
        sb.serve()
        sb.wait_ready()
        prep = sb.status()["prepare"]
        checks = {c["name"]: c for c in prep["checks"]}
        self.assertIn("project:ready", checks)
        self.assertTrue(checks["project:ready"]["ok"], prep)
        self.assertEqual(prep["project"], {
            "ranSetup": True,
            "setupSha256": hashlib.sha256((sb.repo / "setup.sh").read_bytes()).hexdigest(),
        })
        sb.request("t-project")
        self.assertEqual(sb.wait_status("t-project", "succeeded", "failed")["status"], "succeeded")
        self.assertEqual((sb.repo / "setup-count").read_text().splitlines(), ["setup"])

    def test_ready_project_does_not_run_setup(self):
        sb = Sandbox(self)
        sb.configure_project()
        # An executable check with a non-sh shebang verifies direct execution.
        (sb.repo / "check.sh").write_text(
            '#!/usr/bin/env python3\nimport os, pathlib, sys\n'
            'state = pathlib.Path(os.environ["AX_ORBI_STATE_DIR"])\n'
            'assert os.environ["HOME"] == str(state / "home")\n'
            'assert os.environ["ORBI_CONFIG"] == str(state / "orbi.toml")\n'
            'assert "AX_ORBI_CONFIG" not in os.environ\n'
            'sys.exit(not pathlib.Path("dependency").is_file())\n')
        code, out = sb.agent("prepare")
        self.assertEqual(code, 1, out)
        self.assertFalse(json.loads(out)["project"]["ranSetup"])
        self.assertFalse((sb.state / "project-setup.log").exists())
        (sb.repo / "dependency").touch()
        code, out = sb.agent("prepare")
        self.assertEqual(code, 0, out)
        self.assertFalse((sb.state / "prepare.json").exists())
        sb.serve()
        sb.wait_ready()
        prep = sb.status()["prepare"]
        self.assertTrue(prep["ok"], prep)
        self.assertFalse(prep["project"]["ranSetup"])
        self.assertFalse((sb.repo / "setup-count").exists())

    def test_removed_dependency_is_restored_before_next_tick(self):
        sb = Sandbox(self)
        sb.configure_project()
        sb.serve()
        sb.wait_ready()
        sb.request("t-first")
        self.assertEqual(sb.wait_status("t-first", "succeeded", "failed")["status"], "succeeded")
        (sb.repo / "dependency").unlink()
        sb.request("t-repaired")
        self.assertEqual(sb.wait_status("t-repaired", "succeeded", "failed")["status"], "succeeded")
        self.assertTrue((sb.repo / "dependency").is_file())
        self.assertEqual((sb.repo / "setup-count").read_text().splitlines(), ["setup", "setup"])
        self.assertTrue(sb.status()["prepare"]["project"]["ranSetup"])
        self.assertEqual(sb.ticks(), 2)

    def test_failed_project_setup_refuses_ticks(self):
        sb = Sandbox(self)
        sb.configure_project()
        setup = sb.repo / "setup.sh"
        setup.write_text("echo old-error; exit 1\n")
        sb.serve()
        sb.wait_ready()
        setup.write_text("echo installing; echo setup-broke >&2; exit 1\n")
        sb.request("t-blocked")
        run = sb.wait_status("t-blocked", "failed", "succeeded")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["detail"], "not started: the environment is not prepared (see prepare.json)")
        prep = sb.status()["prepare"]
        ready = next(c for c in prep["checks"] if c["name"] == "project:ready")
        self.assertFalse(ready["ok"])
        log = sb.state / "project-setup.log"
        for evidence in ("exit 1: setup-broke", "check exit 1: dependency-missing", str(log)):
            self.assertIn(evidence, ready["detail"])
        self.assertEqual(log.read_text(), "installing\nsetup-broke\n")
        self.assertEqual(sb.ticks(), 0)
        sb.configure_project()
        sb.request("t-retry")
        self.assertEqual(sb.wait_status("t-retry", "succeeded", "failed")["status"], "succeeded")

    def test_setup_removing_check_fails_the_project_path_check(self):
        sb = Sandbox(self)
        sb.configure_project()
        (sb.repo / "setup.sh").write_text("rm check.sh\n")
        sb.serve()
        sb.wait_ready()
        prep = sb.status()["prepare"]
        failure = next(c for c in prep["checks"] if not c["ok"])
        self.assertEqual(failure["name"], "project:setup", prep)
        self.assertIn("project.check", failure["fix"])
        self.assertEqual(sb.ticks(), 0)

    def test_setup_without_check_runs_once_per_start(self):
        sb = Sandbox(self)
        sb.configure_project(check=False)
        code, out = sb.agent("prepare")
        self.assertEqual(code, 1, out)
        self.assertFalse((sb.repo / "setup-count").exists())
        first = sb.serve()
        sb.wait_ready()
        for run_id in ("t-first", "t-second"):
            sb.request(run_id)
            self.assertEqual(sb.wait_status(run_id, "succeeded", "failed")["status"], "succeeded")
        prep = sb.status()["prepare"]
        self.assertFalse(prep["project"]["ranSetup"])
        cache = sb.state / "project-setup.json"
        self.assertEqual(json.loads(cache.read_text())["bootId"], prep["bootId"])
        self.assertEqual((sb.repo / "setup-count").read_text().splitlines(), ["setup"])
        first.terminate()
        self.assertEqual(first.wait(timeout=5), 0)
        sb.serve()
        sb.wait_ready()
        self.assertNotEqual(json.loads(cache.read_text())["bootId"], prep["bootId"])
        self.assertEqual((sb.repo / "setup-count").read_text().splitlines(), ["setup", "setup"])

    def test_stop_during_setup_ends_the_script(self):
        for at_start, script, sig in ((True, "setup.sh", signal.SIGTERM),
                                      (False, "setup.sh", signal.SIGTERM),
                                      (False, "check.sh", signal.SIGINT)):
            with self.subTest(at_start=at_start, script=script, signal=sig):
                sb = Sandbox(self)
                sb.configure_project()
                if not at_start:
                    (sb.repo / "dependency").touch()
                pidfile = sb.repo / "script-pids"
                # Check both the script and a child in its process group.
                hanging = ('#!/bin/sh\nsleep 120 &\nchild=$!\n'
                           'trap \'wait "$child"; exit 0\' TERM\n'
                           'printf "%s %s\\n" "$$" "$child" > script-pids\nwait "$child"\n')
                if at_start:
                    (sb.repo / script).write_text(hanging)
                proc = sb.serve()
                if not at_start:
                    sb.wait_ready()
                    (sb.repo / "dependency").unlink()
                    (sb.repo / script).write_text(hanging)
                sb.request("t-stopped")
                pids = wait_until(lambda: pidfile.exists() and pidfile.read_text().split(),
                                  timeout=5, what="project script to start")
                leader, sleeper = map(int, pids)
                try:
                    os.kill(sleeper, 0)
                    proc.send_signal(sig)
                    self.assertEqual(proc.wait(timeout=5), 0)
                    for pid in (leader, sleeper):
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    with suppress(ProcessLookupError):
                        os.killpg(leader, signal.SIGKILL)
                prep = json.loads((sb.state / "prepare.json").read_text())
                ready = next(c for c in prep["checks"] if c["name"] == "project:ready")
                self.assertFalse(ready["ok"])
                self.assertEqual(ready["detail"], "interrupted by stop")
                self.assertEqual(sb.run_record("t-stopped")["status"], "queued")
                self.assertEqual(sb.ticks(), 0)

    def test_wrong_origin_fails_prepare(self):
        sb = Sandbox(self)
        subprocess.run(["git", "-C", str(sb.repo), "remote", "set-url", "origin",
                        "https://github.com/example/demo-two.git"], check=True)
        code, out = sb.agent("prepare")
        self.assertEqual(code, 1)
        failed = [c["name"] for c in json.loads(out)["checks"] if not c["ok"]]
        self.assertEqual(failed, ["origin"])


class SkillTests(unittest.TestCase):
    skill = ROOT / "skills" / "ax-orbi" / "SKILL.md"

    def test_skill_frontmatter(self):
        # The bundled file uses a plain name and a single-line quoted YAML scalar.
        text = self.skill.read_text(encoding="utf-8")
        header = re.match(r"\A---\nname: ([a-z0-9-]+)\ndescription: '([^'\n]*)'\n---\n", text)
        self.assertIsNotNone(header, "expected YAML frontmatter with name and description")
        name, description = header.groups()
        self.assertEqual(name, "ax-orbi")
        self.assertTrue(description.strip())
        self.assertLessEqual(len(description), 1024)

    def test_skill_commands_exist(self):
        text = self.skill.read_text(encoding="utf-8")
        parser = cli.build_parser()
        commands = next(a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        options = {name: {option for action in sub._actions for option in action.option_strings}
                   for name, sub in commands.items()}
        long_option = r"(?<![\w-])--\w[\w-]*"
        # Check standalone option mentions and command examples, even in prose.
        self.assertEqual(set(re.findall(long_option, text)) - set().union(*options.values()), set())
        examples = re.findall(r"(?<![\w-])ax-orbi[ \t]+([\w-]+)([^`\n;&|]*)", text)
        self.assertTrue(examples, "no ax-orbi commands found in the skill")
        for name, args in examples:
            with self.subTest(command=name, args=args):
                self.assertIn(name, commands)
                for option in re.findall(long_option, args):
                    self.assertIn(option, options[name])


class CliConfigTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ax-orbi-cfg-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        path = tmp / "ax-orbi.toml"
        path.write_text(text)
        return path

    def render_config(self, path: Path, *args: str):
        home, fleet = path.parent / "home", path.parent / "fleet"
        home.mkdir(exist_ok=True)
        fleet.mkdir(exist_ok=True)
        env = {**os.environ, "AX_ORBI_HOME": str(home), "AX_ORBI_FLEET": str(fleet)}
        return subprocess.run([str(CLI), "render", "-c", str(path), *args], env=env,
                              capture_output=True, timeout=5)

    def test_project_paths_must_stay_in_the_repository(self):
        for settings, key in (
            ("", "setup"), ('setup = ""', "setup"), ('setup = "/setup.sh"', "setup"),
            ('setup = "scripts/../setup.sh"', "setup"),
            ('setup = "setup.sh"\ncheck = "../check.sh"', "check"),
            ('setup = "setup.sh"\ncheck = "/check.sh"', "check"),
            ('setup = "setup.sh"\ncheck = ""', "check"),
            *[(f'setup = "setup.sh"\nsetup_timeout = {value}', "setup_timeout")
              for value in (59, 7201, "true", "60.0")],
        ):
            with self.subTest(settings=settings):
                result = self.render_config(self.write(CONFIG + "\n[project]\n" + settings), "--json")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(f"project.{key}", json.loads(result.stdout)["checks"][0]["detail"])

        sb = Sandbox(self)
        sb.configure_project()
        script = sb.repo / "setup.sh"
        outside = sb.tmp / "outside.sh"
        script.rename(outside)
        for missing in (False, True):
            if missing:
                script.unlink()
            else:
                script.symlink_to(outside)
            code, out = sb.agent("prepare")
            self.assertEqual(code, 1, out)
            prep = json.loads(out)
            failure = next(c for c in prep["checks"] if not c["ok"])
            self.assertEqual(failure["name"], "project:setup")
            self.assertIn("inside the repository", failure["fix"])

    def test_project_table_is_optional_in_manifests(self):
        project = '\n[project]\nsetup = "setup.sh"\ncheck = "check.sh"\nsetup_timeout = 7200\n'
        path = self.write(CONFIG + project)
        result = self.render_config(path, "--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        task = next(doc for doc in json.loads(result.stdout)["manifests"] if doc["kind"] == "Task")
        payload = json.loads(next(e["value"] for e in task["spec"]["env"]
                                  if e["name"] == "AX_ORBI_CONFIG"))
        self.assertEqual(payload.pop("project"), {
            "setup": "setup.sh", "check": "check.sh", "setup_timeout": 7200})

        path.write_text(CONFIG)
        baseline = self.render_config(path)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        # SHA-256 of `ax-orbi render -c` using this CONFIG at 94044bb.
        self.assertEqual(hashlib.sha256(baseline.stdout).hexdigest(),
                         "cc9b60322e477940dbb3dc896503af154dd5dab1b45c54167ff7166f21afcc0e")
        self.assertEqual(cli.agent_config(cli.load_config(path)), payload)

    def test_example_config_and_referenced_docs_exist(self):
        cfg = cli.load_config(ROOT / "examples" / "minimal" / "ax-orbi.toml")
        docs = cli.render(cfg).split("\n---\n")
        expected_kinds = ("Gateway", "Workspace", "Task")
        self.assertEqual(len(docs), len(expected_kinds))
        for doc, kind in zip(docs, expected_kinds):
            self.assertIn(f'kind: "{kind}"', doc.splitlines())
        self.assertTrue((ROOT / "docs" / "SETUP.md").is_file())

    def test_render_has_no_secrets_and_binds_everything(self):
        cfg = cli.load_config(self.write(CONFIG))
        gateway, workspace, task = cli.manifests(cfg)
        self.assertEqual(task["metadata"]["name"], "orbi-demo")
        self.assertEqual(task["spec"]["workspace"]["name"], workspace["metadata"]["name"])
        self.assertEqual(task["spec"]["gateway"]["name"], gateway["metadata"]["name"])
        self.assertEqual(workspace["spec"]["setup"]["git"][0]["dir"], "repo")
        env = {e["name"]: e["value"] for e in task["spec"]["env"]}
        self.assertEqual(env["GH_TOKEN"], "placeholder")
        self.assertEqual(env["PI_API_KEY"], "placeholder")
        agent_cfg = json.loads(env["AX_ORBI_CONFIG"])
        self.assertEqual(agent_cfg["source_repo"], "example/demo")
        self.assertEqual(agent_cfg["orbi"], {"message_language": "en"})
        hosts = [h for rule in gateway["spec"]["egress"] for h in rule["https"]["hostnames"]]
        for host in ("github.com", "api.github.com", "api.deepseek.com", "pypi.org"):
            self.assertIn(host, hosts)
        uris = [h["credentialUri"] for rule in gateway["spec"]["egress"]
                for h in rule["https"].get("effects", {}).get("replaceHeaders", [])]
        self.assertIn("ate-secret://k8s.io/default/ax-system/orbi-demo-github/token", uris)
        self.assertIn("ate-secret://k8s.io/default/ax-system/orbi-demo-model/api-key", uris)
        # AX only injects credentials for exact hostnames.
        for rule in gateway["spec"]["egress"]:
            if "effects" in rule["https"]:
                self.assertFalse(any(h.startswith("*") for h in rule["https"]["hostnames"]))

    def test_rendered_yaml_is_stable_and_parseable(self):
        cfg = cli.load_config(self.write(CONFIG))
        text = cli.render(cfg)
        self.assertEqual(text, cli.render(cfg))
        self.assertEqual(text.count("\n---\n"), 2)
        self.assertIn('image: "localhost:5001/ax-orbi@sha256:', text)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed; structure is covered by the fake-ax tests")
        self.assertEqual(list(yaml.safe_load_all(text)), cli.manifests(cfg))

    def test_config_errors_are_specific(self):
        cases = {
            CONFIG.replace('@sha256:' + 'a' * 64, ':latest'): "pinned by digest",
            CONFIG.replace('source = "example/demo"', 'source = "nope"'): "repository.source",
            CONFIG.replace('name = "demo"', 'name = "Demo_1"'): "name =",
            CONFIG.replace('secret = "ax-system/orbi-demo-github"', 'secret = "x"'): "credentials.github.secret",
            CONFIG.replace('allow = ["pypi.org"]', 'allow = ["https://pypi.org"]'): "network.allow",
            CONFIG + '\n# ghp_' + 'A' * 36 + '\n': "appears to contain a credential",
        }
        for text, expected in cases.items():
            with self.assertRaises(cli.ConfigError) as ctx:
                cli.load_config(self.write(text))
            self.assertIn(expected, str(ctx.exception))

    def test_check_distinguishes_ax_revision_warnings(self):
        versions = cli.load_versions()
        pin = versions["ax"]["ref"][:12]
        other = f"{pin[:-1]}{'1' if pin.endswith('0') else '0'}"
        cases = (
            (f"dev+{pin}", 0, True, pin),
            (f"dev+{other}", 0, None, "different commit"),
            (f"dev+{pin}-dirty", 0, None, "-dirty build"),
            ("v1.2.3", 0, None, "release build cannot be compared with a commit pin"),
            ("dev", 0, None, "did not report a comparable revision"),
            (f"dev+{pin}", 1, None, "exited 1"),
        )
        for version, code, ok, detail in cases:
            with self.subTest(version=version, code=code):
                report = cli.Report(as_json=True)
                result = subprocess.CompletedProcess(
                    ["ax", "version"], code, f"ax version {version} (API v1alpha1)\n", "")
                with patch.object(cli.shutil, "which", return_value="/fake/ax"), \
                     patch.object(cli.subprocess, "run", return_value=result):
                    cli.host_checks(report, None, versions, cluster=False)
                check = next(c for c in report.data["checks"] if c["name"] == "ax-version")
                self.assertIs(check["ok"], ok)
                self.assertIn(detail, check["detail"])

    def test_model_is_optional(self):
        text = CONFIG.split("[model]")[0] + '[credentials.github]\nsecret = "ax-system/s"\n'
        cfg = cli.load_config(self.write(text))
        _, _, task = cli.manifests(cfg)
        self.assertNotIn("PI_API_KEY", [e["name"] for e in task["spec"]["env"]])


class CliFlowTests(unittest.TestCase):
    """bin/ax-orbi against the fake `ax`, which runs the real agent."""

    def setUp(self):
        self.sb = Sandbox(self)
        self.config = self.sb.tmp / "ax-orbi.toml"
        self.config.write_text(CONFIG.replace('message_language = "en"', "") +
                               f'[ax]\nbin = "{FAKES / "ax"}"\n')
        self.env = dict(self.sb.env)
        self.env.pop("AX_ORBI_CONFIG")  # must arrive through the rendered Task
        self.env.update(FAKE_AX_DIR=str(self.sb.tmp), FAKE_AX_AGENT=str(AGENT))
        self.addCleanup(self.suspend)

    def suspend(self):
        subprocess.run([str(FAKES / "ax"), "suspend", "task", "orbi-demo"], env=self.env,
                       capture_output=True)

    def cli_process(self, *args: str):
        return subprocess.run([sys.executable, str(CLI), *args, "-c", str(self.config)],
                              env=self.env, capture_output=True, text=True, timeout=120)

    def cli(self, *args: str):
        proc = self.cli_process(*args, "--json")
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            self.fail(f"not JSON (exit {proc.returncode}): {proc.stdout}\n{proc.stderr}")

    def task_state(self):
        return json.loads((self.sb.tmp / "ax-state.json").read_text())

    def set_phase(self, phase):
        state = self.task_state()
        state["phase"] = phase
        (self.sb.tmp / "ax-state.json").write_text(json.dumps(state))

    def ax_calls(self):
        return [json.loads(line) for line in (self.sb.tmp / "ax-calls.jsonl").read_text().splitlines()]

    def fix_repo_path(self):
        # The agent's default repo_dir is /workspace/repo; point the fake
        # sandbox at the temp checkout instead.
        state = json.loads((self.sb.tmp / "ax-state.json").read_text())
        cfg = json.loads(state["env"]["AX_ORBI_CONFIG"])
        cfg.update(repo_dir=str(self.sb.repo), deploy_home=str(self.sb.tmp / "deploy"))
        state["env"]["AX_ORBI_CONFIG"] = json.dumps(cfg)
        (self.sb.tmp / "ax-state.json").write_text(json.dumps(state))

    def prepare(self):
        subprocess.run([str(FAKES / "ax"), "apply", "-f", "-"], env=self.env, check=True,
                       input=cli.render(cli.load_config(self.config)), text=True, capture_output=True)
        self.fix_repo_path()
        return self.cli("prepare", "--timeout", "30")

    def test_check_lists_unverified_versions(self):
        code, out = self.cli("check", "--offline")
        self.assertEqual(code, 0, out)
        self.assertCountEqual(out["unverified"], ["ax-version", "ax-server"])
        checks = {c["name"]: c for c in out["checks"]}
        self.assertIsNone(checks["ax-version"]["ok"])
        self.assertIn("ax fake", checks["ax-version"]["detail"])
        self.assertIsNone(checks["ax-server"]["ok"])
        self.assertEqual(checks["ax-server"]["detail"],
                         "server revision not verified: AX at the pinned commit does not report it")

        proc = self.cli_process("check", "--offline")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.splitlines()[-1], "ready, with 2 items not verified")
        self.assertEqual(self.ax_calls(), [["version"], ["version"]])

    def test_image_pin_mismatch_fails_readiness(self):
        versions_file = self.sb.versions_file
        matching = json.loads(versions_file.read_text())
        tag = matching["orbi_tag"]
        different_tag = f"{tag}-mismatch"
        mismatched = dict(matching, orbi_tag=different_tag)
        del mismatched["pi_version"]
        versions_file.write_text(json.dumps(mismatched))
        prepare_code, prepare_out = self.prepare()

        code, out = self.cli("inspect")
        self.assertEqual(code, 1, out)
        pins = next(c for c in out["checks"] if c["name"] == "image-pins")
        self.assertIs(pins["ok"], False)
        self.assertIn(f"orbi_tag: image {different_tag!r}, expected {tag!r}", pins["detail"])
        self.assertIn(f"pi_version: image <missing>, expected {matching['pi_version']!r}", pins["detail"])
        self.assertIn("ax-orbi build --push", pins["fix"])
        self.assertIn("new `name`", pins["fix"])
        self.assertEqual(prepare_code, 1, prepare_out)
        proc = self.cli_process("inspect")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(f"[FAIL] image-pins: {pins['detail']}", proc.stdout)
        self.assertIn(pins["fix"], proc.stdout)

        code, out = self.cli("run", "--no-wait", "--start-timeout", "3")
        self.assertEqual(code, 1, out)
        self.assertFalse(any("request-tick" in call for call in self.ax_calls()))

        self.assertEqual(self.cli("stop")[0], 0)
        versions_file.write_text(json.dumps(matching))
        code, out = self.cli("recover", "--timeout", "30")
        self.assertEqual(code, 0, out)
        code, out = self.cli("inspect")
        self.assertEqual(code, 0, out)
        self.assertIs(next(c for c in out["checks"] if c["name"] == "image-pins")["ok"], True)

    def test_inspect_reports_unavailable_prepare_metadata(self):
        self.assertEqual(self.prepare()[0], 0)
        # A new boot exposes no prepare report until its supervisor finishes prepare.
        (self.sb.state / "prepare.json").unlink()

        code, out = self.cli("inspect")
        self.assertEqual(code, 1, out)
        self.assertIsNone(out["status"]["prepare"])
        pins = next(c for c in out["checks"] if c["name"] == "image-pins")
        self.assertIs(pins["ok"], False)
        self.assertIn("no prepare report for this boot", pins["detail"])
        self.assertIn("retry `ax-orbi inspect`", pins["fix"])
        self.assertNotIn("build --push", pins["fix"])

    def test_prepare_run_inspect_stop_recover(self):
        code, out = self.prepare()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sb.ticks(), 0, "prepare must not start a delivery")

        code, out = self.cli("run", "--id", "first", "--poll", "1")
        self.assertEqual((code, out["run"]["status"]), (0, "succeeded"), out)
        code, out = self.cli("run", "--id", "first", "--poll", "1")  # a retry of the same request
        self.assertEqual(code, 0)
        self.assertEqual(self.sb.ticks(), 1)

        self.sb.set_mode("fail")
        code, out = self.cli("run", "--id", "second", "--poll", "1")
        self.assertEqual((code, out["run"]["status"]), (1, "failed"), out)

        self.sb.set_mode("hang")
        code, out = self.cli("run", "--id", "third", "--no-wait")
        self.assertEqual(code, 0)
        wait_until(lambda: self.sb.run_record("third")["status"] == "running", what="third to run")
        code, out = self.cli("stop")
        self.assertEqual(code, 1, "stop must refuse while a tick is running")
        self.assertIn("--force", out["checks"][-1]["fix"])
        code, out = self.cli("stop", "--force")
        self.assertEqual((code, out["interruptedRun"]), (0, "third"), out)

        code, out = self.cli("inspect")
        self.assertEqual((code, out["taskState"]), (0, "Suspended"))

        self.sb.set_mode("ok")
        code, out = self.cli("recover", "--timeout", "30")
        self.assertEqual((code, out["interrupted"]), (0, ["third"]), out)
        self.assertEqual(self.sb.ticks(), 3, "the interrupted tick must not be re-run")

        code, out = self.cli("inspect", "--run", "third", "--logs")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"]["runs"][0]["status"], "interrupted")
        self.assertIn("got-sigterm", out["logs"])
        self.assertEqual(out["status"]["counts"], {"failed": 1, "interrupted": 1, "succeeded": 1})

    def test_stop_requires_idle_observation(self):
        self.assertEqual(self.prepare()[0], 0)
        self.env["FAKE_AX_FAIL_SSH"] = "1"

        idle = {"supervisorAlive": True, "health": {"state": "idle", "currentRun": None}, "counts": {}}
        for stdout in ("", json.dumps(idle)):
            with self.subTest(stdout=stdout):
                self.env["FAKE_AX_STATUS"] = stdout
                code, out = self.cli("stop")
                self.assertEqual(code, 3, out)
                self.assertEqual(self.task_state()["phase"], "Working")
                self.assertIn("--force", out["checks"][-1]["fix"])
                self.assertIn("simulated SSH failure", out["checks"][-1]["detail"])
        self.assertNotIn("suspend", [call[0] for call in self.ax_calls()])

        code, out = self.cli("stop", "--force")
        self.assertEqual(code, 0, out)
        self.assertIs(out["executionUnknown"], True)
        self.assertEqual(self.task_state()["phase"], "Suspended")

    def test_inspect_and_run_report_failed_task(self):
        self.assertEqual(self.prepare()[0], 0)
        self.set_phase("Failed")
        calls_before = len(self.ax_calls())

        code, out = self.cli("inspect")
        self.assertEqual((code, out["taskState"]), (1, "Failed"), out)
        self.assertNotIn("checkpointed", json.dumps(out["checks"]))
        self.assertIn("ax describe task orbi-demo", out["checks"][-1]["fix"])

        code, out = self.cli("run", "--start-timeout", "1")
        self.assertEqual(code, 1, out)
        self.assertNotIn("resume", [call[0] for call in self.ax_calls()[calls_before:]])

        code, out = self.cli("stop", "--delete", "--yes-delete-state")
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["checks"][0]["ok"])
        self.assertIn("Failed", out["checks"][0]["detail"])
        self.assertNotIn("suspend", [call[0] for call in self.ax_calls()[calls_before:]])

    def test_inspect_distinguishes_suspending_from_suspended(self):
        self.assertEqual(self.prepare()[0], 0)
        self.set_phase("Suspending")
        code, out = self.cli("inspect")
        self.assertEqual((code, out["taskState"]), (3, "Suspending"), out)
        self.assertNotIn("checkpointed", json.dumps(out["checks"]))

        self.set_phase("Suspended")
        code, out = self.cli("inspect")
        self.assertEqual((code, out["taskState"]), (0, "Suspended"), out)
        self.assertIn("checkpointed", out["checks"][-1]["detail"])

    def test_stop_observes_idle_then_recovers(self):
        self.assertEqual(self.prepare()[0], 0)
        code, out = self.cli("stop")
        self.assertEqual(code, 0, out)
        self.assertNotIn("executionUnknown", out)
        code, out = self.cli("inspect")
        self.assertEqual((code, out["taskState"]), (0, "Suspended"), out)
        code, out = self.cli("recover", "--timeout", "30")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.task_state()["phase"], "Working")
        self.assertEqual(self.sb.ticks(), 0)

    def test_stop_rejects_incomplete_or_nonidle_status(self):
        self.assertEqual(self.prepare()[0], 0)
        idle = {"supervisorAlive": True, "health": {"state": "idle", "currentRun": None}, "counts": {}}
        for status in (
            "{invalid JSON",
            json.dumps(dict(idle, supervisorAlive=False)),
            json.dumps(dict(idle, health={"state": "stopping", "currentRun": None})),
            json.dumps(dict(idle, health={"state": "idle"})),
            json.dumps(dict(idle, counts=None)),
            json.dumps(dict(idle, counts={"queued": 1})),
            json.dumps(dict(idle, counts={"running": 1})),
        ):
            with self.subTest(status=status):
                self.env["FAKE_AX_STATUS"] = status
                code, out = self.cli("stop")
                self.assertEqual(code, 3, out)
                self.assertIn("--force", out["checks"][-1]["fix"])
        self.assertNotIn("suspend", [call[0] for call in self.ax_calls()])

    def test_nonworking_states_are_not_resumed_or_suspended(self):
        self.assertEqual(self.prepare()[0], 0)
        calls_before = len(self.ax_calls())
        for phase, expected in (("Cancelled", 1), ("Completed", 1), ("Terminating", 1),
                                ("Idle", 3), ("RequiresAction", 3), ("Unknown", 3),
                                ("Running", 3), ("Other", 3), ("Queued", 3), ("Suspending", 3)):
            self.set_phase(phase)
            for args in (("inspect",), ("stop",), ("prepare", "--timeout", "0"),
                         ("recover", "--timeout", "0"), ("run", "--start-timeout", "0")):
                with self.subTest(phase=phase, command=args[0]):
                    code, out = self.cli(*args)
                    self.assertEqual((code, out["taskState"]), (expected, phase), out)
                    self.assertNotIn("checkpointed", json.dumps(out["checks"]))
        self.assertTrue({"resume", "suspend", "ssh"}.isdisjoint(
            call[0] for call in self.ax_calls()[calls_before:]))

    def test_recover_polls_a_transitional_state(self):
        self.assertEqual(self.prepare()[0], 0)
        self.set_phase("Queued")
        calls_before = len(self.ax_calls())
        code, out = self.cli("recover", "--timeout", "2")
        self.assertEqual((code, out["taskState"]), (3, "Queued"), out)
        self.assertGreater(self.ax_calls()[calls_before:].count(["get", "tasks"]), 1)

        calls_before = len(self.ax_calls())

        def finish_transition():
            wait_until(lambda: ["get", "tasks"] in self.ax_calls()[calls_before:],
                       timeout=4, what="recover to observe Queued")
            self.set_phase("Working")

        transition = threading.Thread(target=finish_transition)
        transition.start()
        try:
            code, out = self.cli("recover", "--timeout", "5")
            self.assertEqual((code, out["taskState"]), (0, "Working"), out)
            self.assertNotIn("resume", [call[0] for call in self.ax_calls()[calls_before:]])
        finally:
            transition.join(timeout=5)

    def test_unreachable_is_distinct_from_unknown_and_can_be_deleted(self):
        self.assertEqual(self.prepare()[0], 0)
        self.set_phase("Unknown")
        code, out = self.cli("inspect")
        self.assertEqual((code, out["taskState"]), (3, "Unknown"), out)
        self.assertIn("'Unknown'", out["checks"][-1]["detail"])

        self.env["FAKE_AX_FAIL_GET"] = "1"
        for command in ("inspect", "stop", "recover", "run"):
            with self.subTest(command=command):
                code, out = self.cli(command)
                self.assertEqual(code, 1, out)
                self.assertIsNone(out["taskState"])
                self.assertIn("cannot reach the AX control plane", out["checks"][-1]["detail"])
        code, out = self.cli("prepare")
        self.assertEqual(code, 1, out)
        cluster = next(check for check in out["checks"] if check["name"] == "ax-cluster")
        self.assertFalse(cluster["ok"])
        self.assertIn("cannot connect to AX", cluster["detail"])

        calls_before = len(self.ax_calls())
        code, out = self.cli("stop", "--delete", "--yes-delete-state")
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["checks"][0]["ok"])
        self.assertEqual(self.ax_calls()[calls_before:], [
            ["get", "tasks"], ["delete", "task", "orbi-demo"],
            ["delete", "workspace", "orbi-demo-ws"], ["delete", "gateway", "orbi-demo-gw"],
        ])
        self.assertNotIn("applied", self.task_state())

    def test_changed_config_explains_immutability(self):
        self.assertEqual(self.prepare()[0], 0)
        self.config.write_text(self.config.read_text().replace('allow = ["pypi.org"]', "allow = []"))
        code, out = self.cli("prepare", "--timeout", "30")
        self.assertEqual(code, 1)
        apply = next(c for c in out["checks"] if c["name"] == "apply")
        self.assertIn("immutable", apply["fix"])

    def test_inspect_before_prepare(self):
        code, out = self.cli("inspect")
        self.assertEqual(code, 1)
        self.assertIn("ax-orbi prepare", out["checks"][0]["fix"])

    def test_inspect_reports_missing_run_from_agent(self):
        self.assertEqual(self.prepare()[0], 0)
        code, out = self.cli("inspect", "--run", "not-requested")
        self.assertEqual(code, 1, out)
        self.assertIn({"name": "run", "ok": False, "detail": "no such run not-requested"},
                      out["checks"])
        self.assertIs(out["status"]["supervisorAlive"], True)

    def test_delete_accepts_an_already_absent_task(self):
        code, out = self.cli("stop", "--delete", "--yes-delete-state")
        self.assertEqual(code, 0, out)
        self.assertIn({"name": "delete:task", "ok": True, "detail": "orbi-demo already absent"},
                      out["checks"])

    def test_delete_does_not_treat_a_missing_ax_binary_as_deleted(self):
        self.assertEqual(self.prepare()[0], 0)
        self.config.write_text(self.config.read_text().replace(str(FAKES / "ax"),
                                                               str(self.sb.tmp / "missing-ax")))
        code, out = self.cli("stop", "--delete", "--yes-delete-state")
        self.assertEqual(code, 1, out)
        for kind in ("task", "workspace", "gateway"):
            check = next(c for c in out["checks"] if c["name"] == f"delete:{kind}")
            self.assertIs(check["ok"], False)
            self.assertIn("not found on PATH", check["detail"])
        self.assertEqual(self.task_state()["phase"], "Working")

    def test_delete_needs_explicit_confirmation(self):
        self.assertEqual(self.prepare()[0], 0)
        code, out = self.cli("stop", "--delete")
        self.assertEqual(code, 2)
        self.assertTrue((self.sb.tmp / "ax-state.json").read_text().count("applied"))


if __name__ == "__main__":
    unittest.main()
