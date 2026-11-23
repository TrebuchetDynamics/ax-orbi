"""Fleet desired state through the host CLI, using only local fixtures."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_ax_orbi import AGENT, CLI, CONFIG, FAKES, Sandbox, cli


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ax-orbi-fleet-")))
        self.fleet = self.root / "fleet"
        self.home = self.root / "state"
        self.desired = self.home / "deployments" / "demo" / "desired.json"
        self.env = dict(os.environ, AX_ORBI_FLEET=str(self.fleet), AX_ORBI_HOME=str(self.home),
                        FAKE_AX_DIR=str(self.root), FAKE_AX_AGENT=str(AGENT))

    def config(self, path, name="demo"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG.replace('name = "demo"', f'name = "{name}"') +
                        f'[ax]\nbin = "{FAKES / "ax"}"\n')
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
