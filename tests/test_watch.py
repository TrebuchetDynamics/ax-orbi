"""Fleet desired state and watch service through the host CLI and fake AX."""

import fcntl
import json
import os
import shlex
import shutil
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


class EnableTests(FleetTestCase):
    def setUp(self):
        super().setUp()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.git = shutil.which("git")
        for tool in ("python3", "sh"):
            (self.tools / tool).symlink_to(shutil.which(tool))
        self.env.update(PATH=str(self.tools), HOME=str(self.root), GIT_CONFIG_NOSYSTEM="1")
        self.program("git", f'''import os, sys
with open(os.path.join(os.environ["HOME"], "git.log"), "a") as log:
    print(sys.argv[1], file=log)
os.execv({self.git!r}, [{self.git!r}, *sys.argv[1:]])
''')
        self.source = self.root / "upstream"
        self.source.mkdir()
        self.git_run(self.source, "init", "-q", "-b", "main")
        self.git_run(self.source, "commit", "-qm", "pinned", "--allow-empty")
        self.pin = self.git_run(self.source, "rev-parse", "HEAD")
        self.git_run(self.source, "commit", "-qm", "newer", "--allow-empty")
        versions = self.root / "versions.toml"
        default_pins = (CLI.parent.parent / "versions.toml").read_text()
        ax_pin = cli.load_versions(CLI.parent.parent / "versions.toml")["ax"]
        versions.write_text(default_pins.replace(ax_pin["repo"], str(self.source))
                            .replace(ax_pin["ref"], self.pin))
        self.env["AX_ORBI_VERSIONS_TOML"] = str(versions)
        self.ax_source = self.home / "ax-source"
        self.ax_bin = self.home / "bin" / "ax"
        self.ax_script = f'''import os, sys
from pathlib import Path
if sys.argv[1:] == ["version"]:
    print("ax version dev+REVISION")
    sys.exit(0)
if os.environ.get("FAKE_AX_FAIL_GET") and Path(os.environ["HOME"], "cluster-up").exists():
    del os.environ["FAKE_AX_FAIL_GET"]
os.execv({str(FAKES / 'ax')!r}, [{str(FAKES / 'ax')!r}, *sys.argv[1:]])
'''
        self.program("go", f'''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["HOME"], "go.log").open("a") as log:
    print(json.dumps(args), file=log)
assert args == ["build", "-o", {str(self.ax_bin)!r}, "./cmd/ax"], args
assert os.environ["GOTOOLCHAIN"] == "auto"
rev = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=5).strip()
binary = Path(args[2])
binary.write_text("#!/usr/bin/env python3\\n" + {self.ax_script!r}.replace("REVISION", rev[:12]))
binary.chmod(0o755)
print("fake go built AX")
''')
        self.program("make", f'''import os, subprocess, sys
from pathlib import Path
with Path(os.environ["HOME"], "make.log").open("a") as log:
    print(" ".join(sys.argv[1:]), file=log)
assert Path.cwd() == Path({str(self.ax_source)!r})
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=5).strip() == {self.pin!r}
assert os.environ["GOTOOLCHAIN"] == "auto"
Path(os.environ["HOME"], "cluster-up").touch()
print("fake make started AX")
''')
        self.deployment = self.fleet / "demo.toml"
        self.deployment.parent.mkdir()
        self.deployment.write_text(CONFIG)

    def git_run(self, path, *args):
        return subprocess.run([self.git, "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                              cwd=path, env=self.env, check=True, capture_output=True, text=True,
                              timeout=5).stdout.strip()

    def program(self, name, script):
        path = self.tools / name
        path.write_text("#!/usr/bin/env python3\n" + script)
        path.chmod(0o755)
        return path

    def test_enable_builds_the_pinned_ax_when_missing(self):
        self.assertIsNone(shutil.which("ax", path=self.env["PATH"]))
        config = self.deployment.rename(self.root / "ax-orbi.toml")
        proc = subprocess.run([str(CLI), "enable", "--config", config.name, "--json"],
                              env=self.env, cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["exitCode"], 0)
        self.assertIn("fake go built AX", proc.stderr)
        self.assertTrue(os.access(self.ax_bin, os.X_OK))
        self.assertEqual(self.git_run(self.ax_source, "rev-parse", "HEAD"), self.pin)
        self.assertEqual(self.git_run(self.ax_source, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(self.status()["desired"], "enabled")
        self.assertEqual(self.deployment.readlink(), config)
        checks = {c["name"]: c for c in self.command("check", "-c", str(config))["checks"]}
        self.assertEqual(checks["ax"]["detail"], str(self.ax_bin))
        self.assertEqual(checks["ax-version"]["detail"], f"ax version dev+{self.pin[:12]}")
        self.assertIs(checks["ax-version"]["ok"], True)
        self.assertIs(checks["ax-cluster"]["ok"], True)

    def matching_ax(self):
        return self.program("ax", self.ax_script.replace("REVISION", self.pin[:12]))

    def assert_no_install_commands(self):
        for name in ("git", "go", "make"):
            self.assertFalse((self.root / f"{name}.log").exists(), f"unexpected {name} invocation")

    def test_enable_skips_the_build_when_ax_matches(self):
        self.matching_ax()
        self.command("enable", "demo")
        self.assert_no_install_commands()
        self.assertFalse(self.ax_bin.exists())
        self.assertEqual(self.status()["desired"], "enabled")

    def test_enable_fetches_existing_source_when_default_ax_does_not_match(self):
        self.home.mkdir()
        self.git_run(self.home, "clone", "-q", str(self.source), str(self.ax_source))
        newer = self.git_run(self.source, "rev-parse", "HEAD")
        self.program("ax", self.ax_script.replace("REVISION", newer[:12]))
        self.command("enable", "demo")
        self.assertEqual((self.root / "git.log").read_text().splitlines(),
                         ["rev-parse", "status", "fetch", "checkout", "rev-parse"])
        self.assertTrue(self.ax_bin.exists())
        self.assertEqual(self.git_run(self.ax_source, "rev-parse", "HEAD"), self.pin)

    def test_enable_does_not_checkout_in_an_enclosing_repo(self):
        self.git_run(self.root, "init", "-q", "-b", "owner")
        self.git_run(self.root, "commit", "-qm", "owner", "--allow-empty")
        head = self.git_run(self.root, "rev-parse", "HEAD")
        self.ax_source.mkdir(parents=True)
        failure = self.command("enable", "demo", code=1)["checks"][-1]
        self.assertIn("not an AX checkout", failure["detail"])
        self.assertIn(str(self.ax_source), failure["fix"])
        self.assertEqual(self.git_run(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git_run(self.root, "rev-parse", "--abbrev-ref", "HEAD"), "owner")
        self.assertEqual((self.root / "git.log").read_text().splitlines(), ["rev-parse"])
        self.assertFalse((self.root / "go.log").exists())
        self.assertFalse(self.desired.exists())

    def test_enable_does_not_start_from_modified_ax_source(self):
        self.matching_ax()
        self.env["FAKE_AX_FAIL_GET"] = "1"
        self.deployment.write_text(CONFIG + "\n[ax]\nlocal = true\n")
        self.home.mkdir()
        self.git_run(self.home, "clone", "-q", str(self.source), str(self.ax_source))
        modified = self.ax_source / "Makefile"
        modified.write_text("local changes\n")
        self.git_run(self.ax_source, "add", modified.name)
        failure = self.command("enable", "demo", code=1)["checks"][-1]
        self.assertIn("local changes", failure["detail"])
        self.assertIn(str(self.ax_source), failure["fix"])
        self.assertEqual(modified.read_text(), "local changes\n")
        self.assertFalse((self.root / "make.log").exists())
        self.assertFalse(self.desired.exists())

    def test_enable_does_not_replace_a_configured_binary(self):
        self.matching_ax()  # A valid default on PATH must not mask a custom mismatch.
        newer = self.git_run(self.source, "rev-parse", "HEAD")
        custom = self.program("owner-ax", self.ax_script.replace("REVISION", newer[:12]))
        before = custom.read_bytes()
        self.config(self.deployment, ax=custom)
        failure = self.command("enable", "demo", code=1)["checks"][-1]
        self.assertIn("different commit", failure["detail"])
        self.assertIn('bin = "ax"', failure["fix"])
        self.assertEqual(custom.read_bytes(), before)
        self.assert_no_install_commands()
        self.assertFalse(self.ax_bin.exists())
        self.assertFalse(self.desired.exists())

    def test_cluster_is_started_only_when_local_is_set(self):
        self.matching_ax()
        self.env["FAKE_AX_FAIL_GET"] = "1"
        proc = self.process("enable", "demo")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ax-cluster: cannot connect to AX", proc.stdout)
        self.assertIn("fix: start it with `ax local start` or select a cluster with `ax ctx`", proc.stdout)
        self.assertFalse(self.desired.exists())
        self.assert_no_install_commands()

        self.deployment.write_text(CONFIG + "\n[ax]\nlocal = true\n")
        proc = self.process("enable", "demo", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["exitCode"], 0)
        self.assertIn("fake make started AX", proc.stderr)
        self.command("enable", "demo")  # Once reachable, do not start it again.
        self.assertEqual((self.root / "make.log").read_text().splitlines(),
                         ["build-local-cluster CONTAINER_CLI=docker"])
        self.assertFalse((self.root / "go.log").exists())
        self.assertEqual(self.status()["desired"], "enabled")

    def test_failed_check_leaves_the_deployment_not_enabled(self):
        self.matching_ax()
        # Use the documented fallback path, keeping kubectl off PATH.
        kubectl = self.root / ".ax/bin/kubectl"
        kubectl.parent.mkdir(parents=True)
        self.program("kubectl", '''import json, os, sys
with open(os.path.join(os.environ["HOME"], "kubectl.log"), "a") as log:
    print(json.dumps(sys.argv[1:]), file=log)
sys.exit(1)
''').rename(kubectl)
        result = self.command("enable", "demo", code=1)
        failures = [c for c in result["checks"] if c["ok"] is False]
        self.assertEqual([c["name"] for c in failures], ["secret:github", "secret:model"])
        self.assertIn("create it as docs/SETUP.md describes", failures[0]["fix"])
        calls = [json.loads(line) for line in (self.root / "kubectl.log").read_text().splitlines()]
        self.assertEqual([args[:5] for args in calls], [
            ["-n", "ax-system", "get", "secret", "orbi-demo-github"],
            ["-n", "ax-system", "get", "secret", "orbi-demo-model"],
        ])
        self.assertFalse(self.desired.exists())
        self.assertEqual(self.status()["desired"], "not-enabled")

    def test_missing_build_tool_has_a_fix(self):
        for name in ("git", "go"):
            with self.subTest(tool=name):
                tool = self.tools / name
                hidden = tool.rename(tool.with_suffix(".hidden"))
                try:
                    failure = self.command("enable", "demo", code=1)["checks"][-1]
                    self.assertIn(f"{name} not found", failure["detail"])
                    self.assertIn("install", failure["fix"])
                    self.assertFalse(self.desired.exists())
                    self.assert_no_install_commands()
                finally:
                    hidden.rename(tool)

    def test_no_install_keeps_the_plain_enable(self):
        self.deployment.write_text(CONFIG + "\n[ax]\nlocal = true\n")
        self.command("enable", "demo", "--no-install")
        self.assert_no_install_commands()
        self.assertFalse(self.ax_bin.exists())
        self.assertEqual(json.loads(self.desired.read_text())["mode"], "enabled")

    def test_local_key_is_not_rendered(self):
        def render():
            proc = self.process("render", "-c", str(self.deployment))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return proc.stdout

        default = render()
        self.deployment.write_text(CONFIG + "\n[ax]\nlocal = true\n")
        self.assertEqual(render(), default)
        self.deployment.write_text(CONFIG + '\n[ax]\nlocal = "true"\n')
        proc = self.process("render", "-c", str(self.deployment))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("ax.local must be a boolean", proc.stdout)

    def test_watch_never_installs(self):
        self.deployment.write_text(CONFIG + "\n[ax]\nlocal = true\n")
        self.command("enable", "demo", "--no-install")
        self.command("watch", "--once")
        self.assertEqual(self.status()["watch"]["phase"], "waiting")
        self.assert_no_install_commands()

    def test_second_enable_does_not_rebuild(self):
        self.command("enable", "demo")
        self.command("enable", "demo")
        calls = (self.root / "go.log").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.status()["desired"], "enabled")

    def test_build_image_does_not_enable_if_no_digest_was_reported(self):
        self.program("docker", '''import sys
if sys.argv[1:3] == ["image", "inspect"]:
    print("null")
''')
        failure = self.command("enable", "demo", "--build-image", code=1)["checks"][-1]
        self.assertEqual(failure["name"], "push")
        self.assertIn("no digest was reported", failure["detail"])
        self.assertIn("imagetools inspect", failure["fix"])
        self.assert_no_install_commands()
        self.assertFalse(self.desired.exists())

    def test_build_image_requires_the_owner_to_update_the_digest(self):
        ref = "localhost:5001/ax-orbi@sha256:" + "b" * 64
        self.program("docker", f'''import json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.environ["HOME"], "docker.log"), "a") as log:
    print(args[0], file=log)
if args[0] == "image":
    assert args[1:4] == ["inspect", "--format", "{{{{json .RepoDigests}}}}"], args
    print(json.dumps([{ref!r}]))
else:
    print("fake docker " + args[0])
''')
        proc = self.process("enable", "demo", "--build-image")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f'[image]\nref = "{ref}"', proc.stdout)
        self.assertEqual(self.deployment.read_text(), CONFIG)
        self.assertFalse(self.desired.exists())
        self.assert_no_install_commands()
        self.assertEqual((self.root / "docker.log").read_text().splitlines(), ["build", "push", "image"])

        updated = CONFIG.replace("a" * 64, "b" * 64)
        self.deployment.write_text(updated)
        proc = self.process("enable", "demo", "--build-image", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["ref"], ref)
        self.assertIn("fake docker push", proc.stderr)
        self.assertEqual(self.deployment.read_text(), updated)
        self.assertEqual(self.status()["desired"], "enabled")


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
        self.command("enable", "--no-install", name)
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

    def configure_watch(self, content):
        config = self.fleet / "demo.toml"
        config.write_text(config.read_text() + "[watch]\n" + content)
        return config

    def configure_notify(self, exit_code=0):
        output = self.root / "notifications.jsonl"
        script = self.root / "notify.py"
        script.write_text(
            "import json, os, sys\n"
            "data = json.load(sys.stdin)\n"
            "assert data['deployment'] == os.environ['AX_ORBI_DEPLOYMENT']\n"
            "assert data['question']['id'] == os.environ['AX_ORBI_QUESTION_ID']\n"
            "with open(sys.argv[1], 'a') as log:\n"
            "    print(json.dumps(data), file=log)\n"
            f"sys.exit({exit_code})\n"
        )
        config = self.configure_watch(f'notify_command = {json.dumps(["python3", str(script), str(output)])}\n')
        return config, output

    def assert_backoff(self, seconds):
        state = self.watch_state()
        delay = (datetime.fromisoformat(state["nextActionAt"]) - datetime.fromisoformat(state["at"])).total_seconds()
        self.assertAlmostEqual(delay, seconds, delta=1)
        return state

    def test_failed_ticks_back_off_then_ask_once(self):
        sb = self.deployment()
        _, output = self.configure_notify()
        sb.set_mode("fail")
        self.reach_tick()
        for attempt, delay in enumerate((60, 300, 900, 1800), 1):
            run_id = self.watch_state()["pendingTick"]["id"]
            sb.wait_status(run_id, "failed")
            self.watch_once()
            state = self.assert_backoff(delay)
            self.assertEqual((state["cause"], state["attempts"]), ("tick:failed", attempt))
            self.assertEqual(state["phase"], "repairing" if attempt < 3 else "needs-owner")
            self.watch_once()  # No extra tick or notification before the deadline.
            self.assertEqual(len(self.requests(sb)), attempt)
            self.assertEqual(self.watch_state()["attempts"], attempt)
            if attempt < 4:
                self.make_due()
                self.watch_once()
        question = state["question"]
        self.assertRegex(question["id"], r"^[a-f0-9]{12}$")
        self.assertEqual(question["cause"], "tick:failed")
        self.assertTrue(question["text"])
        self.assertTrue(question["options"])
        self.assertIsNotNone(question["notifiedAt"])
        for _ in range(5):
            self.watch_once()
        notice, = map(json.loads, output.read_text().splitlines())
        self.assertEqual(notice["question"]["id"], question["id"])
        self.assertEqual(len(self.requests(sb)), 4)

    def events(self):
        return [json.loads(line) for line in self.watch_path().with_name("events.jsonl").read_text().splitlines()]

    def set_task_phase(self, sb, phase):
        path = sb.tmp / "ax-state.json"
        fake_state = json.loads(path.read_text())
        path.write_text(json.dumps(dict(fake_state, phase=phase)))

    def fail_ticks(self, sb, count):
        sb.set_mode("fail")
        self.reach_tick()
        for index in range(count):
            if index:
                self.make_due()
                self.watch_once()
            sb.wait_status(self.watch_state()["pendingTick"]["id"], "failed")
            self.watch_once()

    def failed_setup_status(self, sb):
        status = sb.status()
        status["prepare"]["ok"] = False
        status["prepare"]["checks"].extend([
            dict(name="project-setup", ok=False, detail="setup recipe failed"),
            dict(name="another-check", ok=False, detail="another check failed"),
        ])
        return status

    def test_dead_supervisor_is_restarted(self):
        sb = self.deployment()
        self.make_ready(sb)
        pid = json.loads((sb.tmp / "ax-state.json").read_text())["pid"]
        os.kill(pid, signal.SIGKILL)
        wait_until(lambda: not sb.status()["supervisorAlive"], what="supervisor to die")
        state = self.watch_state()
        state["startingSince"] = "2000-01-01T00:00:00+00:00"
        self.watch_path().write_text(json.dumps(state))
        stale = sb.status()
        stale["health"]["currentRun"] = "stale"
        stale["counts"] = {"running": 1}
        self.env["FAKE_AX_STATUS"] = json.dumps(stale)
        self.watch_once()
        del self.env["FAKE_AX_STATUS"]
        self.assertEqual(self.watch_state()["phase"], "repairing")
        self.assertEqual(self.watch_state()["cause"], "agent:down")
        self.watch_once()
        sb.wait_ready()
        self.reach_tick()
        sb.wait_status(self.watch_state()["pendingTick"]["id"], "succeeded")
        changes = [args[0] for args in self.calls(sb) if args[0] in ("suspend", "resume")]
        self.assertEqual(changes, ["resume", "suspend", "resume"])
        self.assertEqual(self.watch_state()["attempts"], 0)
        self.assertEqual(self.watch_state()["desired"], "enabled")
        repair, = self.events()
        self.assertEqual((repair["cause"], repair["action"], repair["attempt"]), ("agent:down", "repair", 1))

    def test_recovery_closes_the_question(self):
        sb = self.deployment()
        self.fail_ticks(sb, 4)
        question = self.watch_state()["question"]
        sb.set_mode("ok")
        self.make_due()
        self.watch_once()
        sb.wait_status(self.watch_state()["pendingTick"]["id"], "succeeded")
        self.watch_once()
        state = self.watch_state()
        self.assertIsNone(state["question"])
        self.assertIsNone(state["cause"])
        self.assertEqual((state["attempts"], state["phase"], state["lastTick"]["status"]), (0, "idle", "succeeded"))
        opened, closed = self.events()
        self.assertEqual((opened["cause"], opened["action"]), (question["cause"], "question-opened"))
        self.assertEqual((closed["cause"], closed["action"]), (question["cause"], "question-closed"))

    def test_interrupted_tick_does_not_reset_failed_tick_backoff(self):
        sb = self.deployment()
        self.fail_ticks(sb, 1)
        sb.set_mode("hang")
        self.make_due()
        self.watch_once()
        run_id = self.watch_state()["pendingTick"]["id"]
        sb.wait_status(run_id, "running")
        self.fake_ax(sb, "suspend", "task", "orbi-demo")
        self.watch_once()
        sb.wait_ready()
        self.watch_once()
        state = self.assert_backoff(60)
        self.assertEqual(state["lastTick"]["status"], "interrupted")
        self.assertEqual((state["cause"], state["attempts"], state["phase"]), ("tick:failed", 1, "repairing"))

    def test_changed_cause_resets_the_count(self):
        sb = self.deployment()
        self.fail_ticks(sb, 2)
        self.assertEqual(self.watch_state()["attempts"], 2)
        self.env["FAKE_AX_FAIL_GET"] = "1"
        self.watch_once()
        state = self.assert_backoff(60)
        self.assertEqual((state["cause"], state["attempts"], state["phase"]), ("ax:unreachable", 1, "waiting"))

    def test_failed_task_asks_and_is_not_deleted(self):
        sb = self.deployment()
        self.watch_once()  # Create the task before injecting a failed state.
        self.set_task_phase(sb, "Failed")
        self.watch_once()
        state = self.assert_backoff(900)
        self.assertEqual((state["cause"], state["attempts"], state["phase"]), ("task:Failed", 1, "needs-owner"))
        self.assertIn("unpushed work", state["question"]["text"])
        before = self.calls(sb)
        self.watch_once()
        self.assertEqual(self.calls(sb), before)
        self.make_due()
        self.watch_once()
        self.assert_backoff(1800)
        self.assertEqual(state["question"], self.watch_state()["question"])
        self.assertFalse(any(args[0] == "delete" for args in self.calls(sb)))

    def test_events_rotate_and_unrecognised_state_never_recreates(self):
        sb = self.deployment()
        self.configure_watch("recreate_failed = true\n")
        self.watch_once()
        log = self.watch_path().with_name("events.jsonl")
        rotated = log.with_suffix(".jsonl.1")
        old_events = (b'{"previousEvent":true}\n' * 50_000)  # More than 1 MiB.
        log.write_bytes(old_events)
        rotated.write_text("an older log")
        self.set_task_phase(sb, "RequiresAction")
        self.watch_once()
        state = self.watch_state()
        self.assertEqual((state["phase"], state["attempts"]), ("needs-owner", 1))
        self.assertIn("unrecognised state 'RequiresAction'", state["question"]["text"])
        self.assertFalse(any(args[0] == "delete" for args in self.calls(sb)))
        self.assertEqual(rotated.read_bytes(), old_events)
        event, = self.events()
        self.assertEqual((event["cause"], event["action"]), ("task:RequiresAction", "question-opened"))
        self.set_task_phase(sb, "Suspended")
        self.make_due()
        self.watch_once()
        self.assertIsNone(self.watch_state()["question"])

    def test_image_pin_mismatch_asks_without_restarting(self):
        sb = self.deployment()
        self.make_ready(sb)
        status = sb.status()
        status["prepare"]["image"]["ax_ref"] = "wrong-ref"
        self.env["FAKE_AX_STATUS"] = json.dumps(status)
        self.watch_once()
        state = self.watch_state()
        self.assertEqual((state["cause"], state["phase"], state["attempts"]), ("image-pins", "needs-owner", 1))
        self.assertFalse(any(args[0] in ("delete", "suspend") for args in self.calls(sb)))
        del self.env["FAKE_AX_STATUS"]
        self.make_due()
        self.reach_tick()
        self.assertIsNone(self.watch_state()["question"])

    def test_failed_task_is_recreated_only_when_allowed(self):
        sb = self.deployment()
        self.configure_watch("recreate_failed = true\n")
        self.watch_once()
        self.set_task_phase(sb, "Failed")
        self.watch_once()
        state = self.watch_state()
        self.assertEqual((state["cause"], state["phase"]), ("task:Failed", "repairing"))
        self.assertIsNone(state["question"])
        self.reach_tick()
        sb.wait_status(self.watch_state()["pendingTick"]["id"], "succeeded")
        changes = [args for args in self.calls(sb) if args[0] in ("apply", "delete", "resume")]
        self.assertEqual(changes, [["apply", "-f", "-"], ["delete", "task", "orbi-demo"],
                                   ["delete", "workspace", "orbi-demo-ws"], ["delete", "gateway", "orbi-demo-gw"],
                                   ["apply", "-f", "-"], ["resume", "task", "orbi-demo"]])
        self.assertEqual(self.watch_state()["attempts"], 0)
        self.assertEqual(json.loads(self.desired.read_text())["mode"], "enabled")

    def test_failing_notify_command_does_not_stop_the_pass(self):
        sb = self.deployment()
        _, output = self.configure_notify(exit_code=1)
        self.watch_once()
        self.set_task_phase(sb, "Failed")
        for _ in range(6):
            self.watch_once()
        attempts = [event for event in self.events() if event["action"] == "notify"]
        self.assertEqual([(event["attempt"], event["result"]) for event in attempts],
                         [(1, "exited 1"), (2, "exited 1"), (3, "exited 1")])
        self.assertEqual(len(output.read_text().splitlines()), 3)
        self.assertIsNone(self.watch_state()["question"]["notifiedAt"])

    def test_no_repair_while_a_tick_runs(self):
        sb = self.deployment()
        sb.set_mode("hang")
        self.reach_tick()
        run_id = self.watch_state()["pendingTick"]["id"]
        sb.wait_status(run_id, "running")
        status = self.failed_setup_status(sb)
        self.assertEqual(status["health"]["currentRun"], run_id)
        self.env["FAKE_AX_STATUS"] = json.dumps(status)
        self.watch_once()
        self.assertFalse(any(args[0] == "suspend" for args in self.calls(sb)))

    def test_sandbox_repair_is_bounded_across_restarts(self):
        sb = self.deployment()
        self.make_ready(sb)
        self.env["FAKE_AX_STATUS"] = json.dumps(self.failed_setup_status(sb))
        for attempt, delay in enumerate((60, 300), 1):
            self.watch_once()
            state = self.assert_backoff(delay)
            self.assertEqual((state["cause"], state["attempts"], state["phase"]),
                             ("sandbox:project-setup", attempt, "repairing"))
            self.watch_once()  # Resume the task on a separate pass, without losing the cause.
            sb.wait_ready()
            self.watch_once()
            self.assertEqual(self.watch_state()["attempts"], attempt)
            self.make_due()
        self.watch_once()
        self.assertEqual(self.assert_backoff(900)["phase"], "needs-owner")
        self.make_due()
        self.watch_once()
        self.assert_backoff(1800)
        self.assertEqual(sum(args[0] == "suspend" for args in self.calls(sb)), 2)
        self.assertEqual([event["action"] for event in self.events()], ["repair", "repair", "question-opened"])
        del self.env["FAKE_AX_STATUS"]
        self.make_due()
        self.reach_tick()
        self.assertIsNone(self.watch_state()["question"])

    def test_fresh_start_is_not_treated_as_down(self):
        sb = self.deployment()
        self.watch_once()
        self.watch_once()
        since = self.watch_state()["startingSince"]
        self.assertIsNotNone(since)
        self.env["FAKE_AX_FAIL_SSH"] = "1"
        for _ in range(3):
            self.watch_once()
            state = self.watch_state()
            self.assertEqual((state["phase"], state["startingSince"], state["cause"]), ("starting", since, None))
        self.assertFalse(any(args[0] == "suspend" for args in self.calls(sb)))

    def test_unreachable_ax_stays_waiting_until_the_question(self):
        sb = self.deployment()
        self.env["FAKE_AX_FAIL_GET"] = "1"
        for attempt, delay in enumerate((60, 300, 900), 1):
            self.watch_once()
            state = self.assert_backoff(delay)
            self.assertEqual(state["attempts"], attempt)
            self.assertEqual(state["phase"], "waiting" if attempt < 3 else "needs-owner")
            self.assertIn("next check at", state["detail"])
            if attempt < 3:
                self.make_due()
        before = self.calls(sb)
        self.watch_once()
        self.assertEqual(self.calls(sb), before)
        del self.env["FAKE_AX_FAIL_GET"]
        self.make_due()
        self.watch_once()
        self.assertEqual((self.watch_state()["phase"], self.watch_state()["question"]), ("preparing", None))

    def test_unloadable_config_asks_once(self):
        sb = self.deployment()
        config, output = self.configure_notify()
        self.watch_once()  # Save the notification command while the config still loads.
        valid = config.read_text()
        config.write_text(valid + "tick_interval = 1\n")
        before = self.calls(sb)
        for _ in range(4):
            self.watch_once()
        state = self.watch_state()
        self.assertEqual((state["cause"], state["phase"]), ("config", "needs-owner"))
        self.assertIn("watch.tick_interval", state["detail"])
        notice, = map(json.loads, output.read_text().splitlines())
        self.assertEqual(notice["question"]["id"], state["question"]["id"])
        self.assertEqual(self.calls(sb), before)
        config.write_text(valid)
        self.watch_once()
        self.assertIsNone(self.watch_state()["question"])
        self.assertEqual(self.watch_state()["phase"], "resuming")

    def test_status_shows_the_question(self):
        sb = self.deployment()
        self.watch_once()
        self.set_task_phase(sb, "Failed")
        self.watch_once()
        question = self.watch_state()["question"]
        self.assertEqual(self.status()["watch"]["question"], question)
        proc = self.process("status", "demo")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("The sandbox for demo is in AX state Failed", proc.stdout)
        self.assertIn(question["text"], proc.stdout)
        self.assertEqual(len(question["options"]), 3)
        for option in question["options"]:
            self.assertIn(option["command"], proc.stdout)
        recreate = question["options"][1]["command"]
        # Actually follow the displayed recovery option against fake AX.
        for command in recreate.split(" && "):
            tool, *args = shlex.split(command)
            self.assertEqual(tool, "ax-orbi")
            self.command(*args)
        self.make_due()
        self.reach_tick()
        sb.wait_status(self.watch_state()["pendingTick"]["id"], "succeeded")
        self.assertIsNone(self.watch_state()["question"])

    def test_paused_deployment_gets_no_repair_or_question(self):
        sb = self.deployment()
        _, output = self.configure_notify()
        self.env["FAKE_AX_FAIL_GET"] = "1"
        self.command("pause", "demo", "--reason", "maintenance")
        before = self.desired.read_bytes()
        for _ in range(3):
            self.watch_once()
        state = self.watch_state()
        self.assertEqual(state["phase"], "paused")
        self.assertIsNone(state["question"])
        self.assertIsNone(state["cause"])
        self.assertFalse(output.exists())
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
        self.assertEqual(state["phase"], "waiting")
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
        self.assertRegex(entries["broken"]["watch"]["detail"], r"^Waiting for AX to answer; next check at \d\d:\d\d UTC$")
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
        self.assertEqual(state["phase"], "waiting")
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
        config.write_text(original + '[watch]\ntick_interval = 60\nnotify_command = ["sh", "notify.sh"]\n'
                          'recreate_failed = true\n')
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
        for field, invalid, expected in (
            ("notify_command", '"script"', "a non-empty list of strings"),
            ("notify_command", "[]", "a non-empty list of strings"),
            ("notify_command", '["sh", 1]', "a non-empty list of strings"),
            ("recreate_failed", '"true"', "a boolean"),
        ):
            with self.subTest(field=field, invalid=invalid):
                config.write_text(original + f"[watch]\n{field} = {invalid}\n")
                proc = self.process(*render_args)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(f"watch.{field} must be {expected}", proc.stdout)

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
        self.command("enable", "--no-install", "demo")
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
        self.command("enable", "--no-install", "demo")
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
        self.command("enable", "--no-install", "--config", config.name)
        link = self.fleet / "demo.toml"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), config)
        self.assertEqual(self.status()["desired"], "enabled")
        self.command("enable", "--no-install", "--config", config.name)  # reuse the same registration
        before = self.desired.read_bytes()
        other = self.config(self.root / "different.toml")
        self.command("enable", "--no-install", "--config", str(other), code=2)
        self.assertEqual(link.readlink(), config)
        self.assertEqual(self.desired.read_bytes(), before)

    def test_unknown_or_invalid_names_are_refused(self):
        for command in (("enable", "--no-install"), ("pause",), ("resume",)):
            self.command(*command, "unknown", code=2)
        self.assertEqual(self.command("status", "unknown", code=2)["deployments"], [])
        proc = self.process("enable", "--no-install", "unknown")
        self.assertIn("enable --config", proc.stdout)  # tell the operator how to register it

        config = self.config(self.fleet / "renamed.toml", "invalid")
        config.write_text(config.read_text().replace("[image]", "[not-image]"))
        self.command("enable", "--no-install", "invalid", code=2)
        self.command("enable", "--no-install", "--config", str(config), code=2)
        self.assertIn("image.ref", self.status("invalid")["configError"])
        self.assertFalse(self.home.exists())

    def test_duplicate_names_are_reported_not_acted_on(self):
        self.config(self.fleet / "one.toml")
        self.command("enable", "--no-install", "demo")
        before = self.desired.read_bytes()
        self.config(self.fleet / "two.toml")
        deployments = self.command("status")["deployments"]
        self.assertEqual([entry["name"] for entry in deployments], ["demo", "demo"])
        self.assertTrue(all("duplicate" in entry["configError"] for entry in deployments))
        for command in (("enable", "--no-install"), ("pause",), ("resume",)):
            self.command(*command, "demo", code=2)
        self.assertEqual(self.desired.read_bytes(), before)

    def test_unreadable_fleet_links_do_not_hide_valid_configs(self):
        self.config(self.fleet / "demo.toml")
        self.command("enable", "--no-install", "demo")
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
        self.command("enable", "--no-install", "--config", str(pipe), code=2)

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
        self.command("enable", "--no-install", "demo", "--fleet", str(alternate))
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

        self.command("enable", "--no-install", "demo")
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
