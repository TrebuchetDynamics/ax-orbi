"""Owner demonstrations through the fleet CLI, real agent and offline AX/Orbi.

One repair for several PRs blocked by shared CI belongs to Orbi and is not
part of this milestone.
"""

import argparse
import json
import re
import shlex
import subprocess
import time
import unittest
from datetime import datetime

from test_ax_orbi import AGENT, FAKES, ROOT, Sandbox, cli, wait_until
from test_watch import FleetTestCase


class UnattendedTests(FleetTestCase):
    def enable(self, name="demo", *, settings=""):
        sb = Sandbox(self, with_config=False)
        wrapper = self.root / f"ax-{name}"
        env = {key: sb.env[key] for key in ("PATH", "AX_ORBI_ORBI_BIN", "AX_ORBI_VERSIONS_FILE",
                                           "FAKE_ORBI_MODE_FILE", "FAKE_ORBI_LOG")}
        env.update(FAKE_AX_DIR=str(sb.tmp), FAKE_AX_AGENT=str(AGENT), FAKE_AX_TASK=f"orbi-{name}",
                   FAKE_AX_REPO_DIR=str(sb.repo), FAKE_AX_DEPLOY_HOME=str(sb.tmp / "deploy"))
        wrapper.write_text("#!/bin/sh\n" +
                           "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items()) +
                           f'exec {shlex.quote(str(FAKES / "ax"))} "$@"\n')
        wrapper.chmod(0o755)
        config = self.config(self.root / f"{name}.toml", name, wrapper)
        text = config.read_text().replace('"example/demo"', f'"example/{name}"')
        if name != "demo":
            subprocess.run(["git", "-C", str(sb.repo), "remote", "set-url", "origin",
                            f"https://github.com/example/{name}.git"], check=True, timeout=5)
        config.write_text(text + settings)
        result = self.command("enable", "--no-install", "--config", str(config))
        self.assertEqual(result["checks"][-1]["detail"], f"{name} enabled")
        self.addCleanup(self.ax_side_stop, wrapper, name)
        return sb, wrapper

    def ax_side_stop(self, wrapper, name):
        # Only used to inject an AX-side interruption or to clean up the fixture.
        subprocess.run([str(wrapper), "suspend", "task", f"orbi-{name}"], env=self.env,
                       capture_output=True, text=True, check=True, timeout=12)

    def watch_once(self):
        self.command("watch", "--once")

    def make_due(self, name="demo"):
        path = self.home / "deployments" / name / "watch.json"
        state = json.loads(path.read_text())
        state["nextActionAt"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(state))

    def drive_until(self, predicate, what, name="demo"):
        deadline = time.monotonic() + 10
        while True:
            self.watch_once()
            entry = self.status(name)
            if predicate(entry["watch"]):
                return entry["watch"]
            if time.monotonic() >= deadline:
                self.fail(f"{name} did not reach {what}: {entry}")

    def completed_tick(self, outcome="succeeded", *, after=None, name="demo"):
        def completed(watch):
            tick = watch["lastTick"]
            return tick and tick["id"] != after

        watch = self.drive_until(completed, f"a new terminal tick after {after}", name)
        self.assertEqual(watch["lastTick"]["status"], outcome, watch)
        return watch

    def ax_calls(self, sb):
        return [json.loads(line) for line in (sb.tmp / "ax-calls.jsonl").read_text().splitlines()]

    def requested_ids(self, sb):
        return [args[-1] for args in self.ax_calls(sb) if "request-tick" in args]

    def test_pause_survives_service_restart(self):
        sb, _ = self.enable()
        first = self.completed_tick()["lastTick"]
        self.make_due()
        calls = sb.tmp / "ax-calls.jsonl"
        before = calls.read_bytes()
        self.command("pause", "demo", "--reason", "maintenance")
        paused = self.status()
        self.assertEqual((paused["desired"], paused["reason"]), ("paused", "maintenance"))

        for _ in range(3):
            self.watch_once()  # A new service process every time.
            entry = self.status()
            self.assertEqual(entry["desired"], "paused")
            self.assertEqual(entry["reason"], "maintenance")
            self.assertEqual(entry["changedAt"], paused["changedAt"])
            self.assertEqual(entry["watch"]["phase"], "paused")
            self.assertEqual(entry["watch"]["lastTick"], first)
        self.assertEqual(calls.read_bytes(), before)  # Not even a read-only AX call.
        self.assertEqual(sb.ticks(), 1)

    def test_removed_dependency_is_restored(self):
        sb, _ = self.enable(settings='[project]\nsetup = "setup.sh"\ncheck = "check.sh"\n')
        dependency = sb.repo / "dependency"
        setups = sb.repo / "setup-count"
        (sb.repo / "setup.sh").write_text('set -eu\n: > dependency\nprintf "setup\\n" >> setup-count\n')
        (sb.repo / "check.sh").write_text('test -f dependency || { echo dependency-missing; exit 1; }\n')
        first = self.completed_tick()["lastTick"]["id"]
        self.assertTrue(dependency.is_file())
        self.assertEqual(setups.read_text().splitlines(), ["setup"])

        dependency.unlink()
        self.make_due()
        second = self.completed_tick(after=first)["lastTick"]["id"]
        self.assertTrue(dependency.is_file())
        self.assertEqual(setups.read_text().splitlines(), ["setup", "setup"])
        self.assertIn("fake orbi tick: mode=ok", (sb.state / "runs" / second / "tick.log").read_text())
        self.assertEqual(sb.ticks(), 2)

    def test_interrupted_worker_continues_without_duplicate(self):
        sb, wrapper = self.enable()
        sb.set_mode("hang")
        active = self.drive_until(lambda watch: watch["pendingTick"], "a tick request")
        first = active["pendingTick"]["id"]
        wait_until(sb.ticks, timeout=5, what=f"fake Orbi to start {first}")
        old_run = sb.state / "runs" / first / "run.json"
        started = json.loads(old_run.read_text())
        self.assertEqual(started["status"], "running", started)

        self.ax_side_stop(wrapper, "demo")
        sb.set_mode("ok")
        self.assertEqual(self.status()["desired"], "enabled")
        self.watch_once()
        resuming = self.status()["watch"]
        self.assertEqual((resuming["taskState"], resuming["phase"]), ("Suspended", "resuming"), resuming)
        restored = self.completed_tick("interrupted")
        self.assertEqual(restored["lastTick"]["id"], first)
        kept = json.loads(old_run.read_text())
        self.assertEqual(kept["status"], "interrupted", kept)
        for field in ("startedAt", "bootId"):
            self.assertEqual(kept[field], started[field])

        self.make_due()
        second = self.completed_tick(after=first)["lastTick"]["id"]
        self.assertEqual(json.loads(old_run.read_text()), kept)
        self.assertEqual(self.requested_ids(sb), [first, second])
        for tick_id in (first, second):
            log = (sb.state / "runs" / tick_id / "tick.log").read_text()
            self.assertEqual(log.count("fake orbi tick:"), 1, log)
        self.assertEqual(sb.ticks(), 2)

    def test_outage_then_recovery(self):
        notices = self.root / "notifications.jsonl"
        notify = ("import json, os, sys; data = json.load(sys.stdin); "
                  "assert data['deployment'] == os.environ['AX_ORBI_DEPLOYMENT']; "
                  "assert data['question']['id'] == os.environ['AX_ORBI_QUESTION_ID']; "
                  "print(json.dumps(data), file=open(sys.argv[1], 'a'))")
        argv = json.dumps(["python3", "-c", notify, str(notices)])
        sb, _ = self.enable(settings=f"[watch]\nnotify_command = {argv}\n")
        last = self.completed_tick()["lastTick"]["id"]
        sb.set_mode("fail")
        for attempt, delay in enumerate((60, 300, 900, 1800), 1):
            self.make_due()
            state = self.completed_tick("failed", after=last)
            last = state["lastTick"]["id"]
            self.assertEqual((state["cause"], state["attempts"]), ("tick:failed", attempt), state)
            wait = datetime.fromisoformat(state["nextActionAt"]) - datetime.fromisoformat(state["at"])
            self.assertAlmostEqual(wait.total_seconds(), delay, delta=1)
            self.watch_once()  # Not due: neither another tick nor another question.
            self.assertEqual(len(self.requested_ids(sb)), attempt + 1)
            self.assertEqual(self.status()["watch"]["attempts"], attempt)
            if attempt < 3:
                self.assertEqual(state["phase"], "repairing", state)
                self.assertIsNone(state["question"])
                self.assertFalse(notices.exists())
            elif attempt == 3:
                question = state["question"]
                self.assertIsNotNone(question, state)
                self.assertEqual(state["phase"], "needs-owner", state)
                self.assertEqual(question["text"],
                                 "Orbi ticks for demo keep failing; can you inspect the run evidence?")
                self.assertIn("Read recent tick logs", [option["label"] for option in question["options"]])
                self.assertIsNotNone(question["notifiedAt"])
            else:
                self.assertEqual(state["question"], question)
        self.assertIn("runner failed: simulated", (sb.state / "runs" / last / "tick.log").read_text())

        sb.set_mode("ok")
        self.make_due()
        recovered = self.completed_tick(after=last)
        self.assertEqual((recovered["phase"], recovered["cause"], recovered["attempts"]), ("idle", None, 0))
        self.assertIsNone(recovered["question"])
        notice, = map(json.loads, notices.read_text().splitlines())
        self.assertEqual((notice["deployment"], notice["question"]["id"]), ("demo", question["id"]))
        events = self.home / "deployments" / "demo" / "events.jsonl"
        self.assertEqual([json.loads(line)["action"] for line in events.read_text().splitlines()],
                         ["question-opened", "notify", "question-closed"])
        self.assertEqual(sb.ticks(), 6)

    def test_other_deployment_keeps_working(self):
        blocked, _ = self.enable("blocked")
        working, _ = self.enable()
        self.watch_once()  # Apply both deployments.
        path = blocked.tmp / "ax-state.json"
        state = json.loads(path.read_text())
        path.write_text(json.dumps(dict(state, phase="Failed")))
        self.watch_once()
        waiting = self.status("blocked")["watch"]
        self.assertEqual(waiting["phase"], "needs-owner", waiting)
        self.assertEqual(waiting["question"]["cause"], "task:Failed")

        first = self.completed_tick()["lastTick"]["id"]
        self.make_due()
        second = self.completed_tick(after=first)["lastTick"]["id"]
        fleet = {entry["name"]: entry["watch"] for entry in self.command("status")["deployments"]}
        self.assertEqual(fleet["blocked"]["question"], waiting["question"])
        self.assertEqual(fleet["blocked"]["phase"], "needs-owner")
        self.assertEqual(fleet["demo"]["lastTick"]["id"], second)
        self.assertEqual([args[0] for args in self.ax_calls(blocked) if args[0] != "get"], ["apply"])
        self.assertEqual(self.requested_ids(working), [first, second])
        self.assertEqual(working.ticks(), 2)


class DocumentedCommandTests(unittest.TestCase):
    def test_readme_and_setup_ax_orbi_examples_exist(self):
        parser = cli.build_parser()
        commands = next(a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        executable = r"(?:timeout (?:-s INT )?\d+[smhd]? )?(?:\./)?(?:bin/)?ax-orbi[ \t]+"
        inline = re.compile(r"`" + executable + r"([^`\n]+)`")
        shell = re.compile(r"^[ \t]*" + executable + r"([^\n;&|<>]+)", re.MULTILINE)
        for path in (ROOT / "README.md", ROOT / "docs" / "SETUP.md"):
            prose_and_fences = re.split(r"^```[^\n]*\n", path.read_text(), flags=re.MULTILINE)
            examples = [sample for prose in prose_and_fences[::2] for sample in inline.findall(prose)]
            for block in prose_and_fences[1::2]:
                examples.extend(shell.findall(block.replace("\\\n", " ")))
            self.assertTrue(examples, f"no ax-orbi examples in {path}")
            for sample in examples:
                with self.subTest(path=path, example=sample):
                    argv = shlex.split(sample, comments=True)
                    self.assertTrue(argv, "expected an ax-orbi subcommand or option")
                    if argv[0].startswith("-"):
                        command = parser  # e.g. the top-level --help example
                    else:
                        self.assertIn(argv[0], commands)
                        command = commands[argv.pop(0)]
                    options = {option for action in command._actions for option in action.option_strings}
                    for arg in argv:
                        if arg.startswith("-"):
                            self.assertIn(arg.split("=", 1)[0], options)
