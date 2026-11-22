"""Tests for the in-sandbox agent and the host CLI.

No cluster and no network: Orbi, Pi, gh and `ax` are the stand-ins in
tests/fakes. What is real is every line of ax-orbi-agent and bin/ax-orbi,
including the process handling: supervisors are started, signalled and killed
as separate processes.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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
        self.set_mode("ok")
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{FAKES}:{os.environ['PATH']}",
            "AX_ORBI_STATE_DIR": str(self.state),
            "AX_ORBI_ORBI_BIN": str(FAKES / "orbi"),
            "AX_ORBI_VERSIONS_FILE": str(self.tmp / "versions.json"),
            "FAKE_ORBI_MODE_FILE": str(self.mode_file),
            "FAKE_ORBI_LOG": str(self.tick_log),
            "GH_TOKEN": "placeholder",
        })
        (self.tmp / "versions.json").write_text('{"orbi_tag": "v9.9.9"}')
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

    def ticks(self) -> int:
        return len(self.tick_log.read_text().splitlines()) if self.tick_log.exists() else 0

    def serve(self) -> subprocess.Popen:
        proc = subprocess.Popen([str(AGENT), "serve"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        return proc

    def agent(self, *args: str):
        proc = subprocess.run([str(AGENT), *args], env=self.env, capture_output=True, text=True)
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
        self.assertIn('engine_source_track = "tag:v9.9.9"', toml)
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

    def test_wrong_origin_fails_prepare(self):
        sb = Sandbox(self)
        subprocess.run(["git", "-C", str(sb.repo), "remote", "set-url", "origin",
                        "https://github.com/example/demo-two.git"], check=True)
        code, out = sb.agent("prepare")
        self.assertEqual(code, 1)
        failed = [c["name"] for c in json.loads(out)["checks"] if not c["ok"]]
        self.assertEqual(failed, ["origin"])


class CliConfigTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ax-orbi-cfg-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        path = tmp / "ax-orbi.toml"
        path.write_text(text)
        return path

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

    def cli(self, *args: str):
        proc = subprocess.run([sys.executable, str(CLI), *args, "-c", str(self.config), "--json"],
                              env=self.env, capture_output=True, text=True, timeout=120)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            self.fail(f"not JSON (exit {proc.returncode}): {proc.stdout}\n{proc.stderr}")

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

    def test_delete_needs_explicit_confirmation(self):
        self.assertEqual(self.prepare()[0], 0)
        code, out = self.cli("stop", "--delete")
        self.assertEqual(code, 2)
        self.assertTrue((self.sb.tmp / "ax-state.json").read_text().count("applied"))


if __name__ == "__main__":
    unittest.main()
