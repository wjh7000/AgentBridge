import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentbridge import skill_install

from agentbridge.skill_install import CLIENTS, DIRECTORIES, MANIFEST, install_skills, uninstall_skills


class TemporarySkillCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="AgentBridge skills ' 测试 ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()

    def skill(self, agent="codex"):
        return self.home / DIRECTORIES[agent] / "skills/handoff"

    def snapshot(self, directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}


class SkillInstallationTests(TemporarySkillCase):
    def test_preview_does_not_create_client_directories(self):
        result = install_skills(home=self.home, preview=True)
        self.assertTrue(result["preview"])
        self.assertEqual({item["agent"] for item in result["skills"]}, set(CLIENTS))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_installs_three_clients_with_fixed_identity_and_explicit_policies(self):
        result = install_skills(home=self.home)
        self.assertEqual(len(result["skills"]), 3)
        for agent in CLIENTS:
            with self.subTest(agent=agent):
                folder = self.skill(agent)
                config = json.loads((folder / "bridge.json").read_text())
                self.assertEqual(config["agent"], agent)
                self.assertEqual(config["backend"], "agentbridge")
                self.assertEqual(config["protocol_version"], 2)
                self.assertIn(len(config["command"]), (1, 2))
                self.assertTrue(all(Path(part).is_absolute() and Path(part).is_file()
                                    for part in config["command"]))
                self.assertTrue((folder / "scripts/handoff.py").is_file())
                manifest = json.loads((folder / MANIFEST).read_text())
                self.assertEqual(manifest["agent"], agent)
                self.assertEqual(set(manifest["files"]),
                                 {"SKILL.md", "agents/openai.yaml", "scripts/handoff.py", "bridge.json"})
                skill = (folder / "SKILL.md").read_text()
                self.assertEqual("disable-model-invocation: true" in skill, agent != "codex")
                self.assertIn("allow_implicit_invocation: false", (folder / "agents/openai.yaml").read_text())
        self.assertFalse((self.home / ".agents").exists())

    def test_repeat_install_is_idempotent_and_preserves_other_configuration(self):
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"existing": true}\n')
        install_skills(home=self.home)
        before = self.snapshot(self.home)
        install_skills(home=self.home)
        self.assertEqual(self.snapshot(self.home), before)

    def test_existing_unmanaged_name_blocks_all_installs_before_writing(self):
        existing = self.skill("workbuddy") / "SKILL.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("A different handoff skill\n")
        before = self.snapshot(self.home)
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            install_skills(home=self.home)
        self.assertEqual(self.snapshot(self.home), before)
        self.assertFalse(self.skill("codex").exists())

    def test_edited_managed_file_is_preserved_by_upgrade_and_uninstall(self):
        install_skills(("codex",), home=self.home)
        path = self.skill() / "SKILL.md"
        path.write_text(path.read_text() + "\nUser customization.\n")
        before = self.snapshot(self.home)
        with self.assertRaisesRegex(ValueError, "local edits"):
            install_skills(("codex",), home=self.home)
        removed = uninstall_skills(("codex",), home=self.home)
        self.assertEqual(removed["removed"], [])
        self.assertEqual(removed["skipped_modified_or_unmanaged"], [str(self.skill())])
        self.assertEqual(self.snapshot(self.home), before)

    def test_uninstall_removes_managed_files_but_keeps_extra_user_file(self):
        install_skills(("claude",), home=self.home)
        extra = self.skill("claude") / "notes.txt"
        extra.write_text("Keep me")
        result = uninstall_skills(("claude",), home=self.home)
        self.assertEqual(result["removed"], [str(self.skill("claude"))])
        self.assertEqual(extra.read_text(), "Keep me")
        self.assertEqual(self.snapshot(self.skill("claude")), {"notes.txt": b"Keep me"})

    def test_complete_uninstall_is_repeatable(self):
        install_skills(home=self.home)
        result = uninstall_skills(home=self.home)
        self.assertEqual(len(result["removed"]), 3)
        self.assertTrue(all(not self.skill(agent).exists() for agent in CLIENTS))
        self.assertEqual(uninstall_skills(home=self.home)["removed"], [])

    def test_symlinked_client_directory_does_not_touch_target(self):
        external = self.root / "external"
        external.mkdir()
        (self.home / ".codex").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlinked"):
            install_skills(("codex",), home=self.home)
        self.assertEqual(list(external.iterdir()), [])

    def test_wrong_manifest_shape_is_rejected_and_preserved(self):
        self.skill().mkdir(parents=True)
        manifest = self.skill() / MANIFEST
        for value in ([], 12, None, {"owner": "another-tool"}):
            with self.subTest(value=value):
                manifest.write_text(json.dumps(value))
                before = self.snapshot(self.home)
                with self.assertRaises(ValueError):
                    install_skills(("codex",), home=self.home)
                result = uninstall_skills(("codex",), home=self.home)
                self.assertEqual(result["removed"], [])
                self.assertEqual(self.snapshot(self.home), before)

    def test_invalid_client_has_no_side_effects(self):
        with self.assertRaises(ValueError):
            install_skills(("codex", "unknown"), home=self.home)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_backend_argv_matches_the_helpers_accepted_shapes(self):
        from agentbridge.skill_install import backend_argv
        argv = backend_argv()
        self.assertIn(len(argv), (1, 2, 3))
        if len(argv) == 3:
            self.assertEqual(argv[1:], ["-m", "agentbridge"])
            file_parts = argv[:1]
        else:
            file_parts = argv
        for part in file_parts:
            self.assertTrue(Path(part).is_absolute() and Path(part).is_file(), part)

    def test_uninstall_validates_all_clients_before_removing_any(self):
        install_skills(home=self.home)
        before = self.snapshot(self.home)
        with self.assertRaisesRegex(ValueError, "codex,claude,workbuddy"):
            uninstall_skills(("codex", "unknown"), home=self.home)
        self.assertEqual(self.snapshot(self.home), before)
        self.assertTrue((self.skill("codex") / MANIFEST).is_file())
        with self.assertRaises(ValueError):
            uninstall_skills((), home=self.home)

    def test_interrupted_first_install_rolls_back_and_can_retry(self):
        original_write = skill_install._write
        calls = 0

        def interrupt_once(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated write interruption")
            return original_write(path, content)

        with patch.object(skill_install, "_write", side_effect=interrupt_once):
            with self.assertRaises(OSError):
                install_skills(("codex",), home=self.home)
        self.assertFalse(self.skill().exists())
        install_skills(("codex",), home=self.home)
        self.assertTrue((self.skill() / MANIFEST).is_file())

    def test_interrupted_upgrade_restores_previous_managed_bytes(self):
        install_skills(("codex",), home=self.home)
        before = self.snapshot(self.home)
        original_files, original_write = skill_install._files, skill_install._write
        calls = 0

        def changed_files(agent):
            files = original_files(agent)
            files["SKILL.md"] += b"\nAn updated workflow.\n"
            return files

        def interrupt_once(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated write interruption")
            return original_write(path, content)

        with patch.object(skill_install, "_files", side_effect=changed_files), \
                patch.object(skill_install, "_write", side_effect=interrupt_once):
            with self.assertRaises(OSError):
                install_skills(("codex",), home=self.home)
        self.assertEqual(self.snapshot(self.home), before)
        install_skills(("codex",), home=self.home)


class SkillAdapterTests(TemporarySkillCase):
    def setUp(self):
        super().setUp()
        install_skills(home=self.home)
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        self.environment = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CODEBUDDY_SESSION_ID"):
            self.environment.pop(key, None)

    def call(self, action="check", agent="codex", cwd=None, declared_cwd=None, extra=()):
        process_cwd = self.cwd if cwd is None else cwd
        declared_cwd = process_cwd if declared_cwd is None else declared_cwd
        result = subprocess.run(
            [sys.executable, str(self.skill(agent) / "scripts/handoff.py"), action,
             "--cwd", str(declared_cwd), *extra],
            cwd=process_cwd, env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertNotIn("Traceback", result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if value["ok"] else 1)
        return value

    def rewrite_config(self, agent="codex", **updates):
        path = self.skill(agent) / "bridge.json"
        value = json.loads(path.read_text())
        value.update(updates)
        path.write_text(json.dumps(value))

    def fake_backend(self, payload=None, raw=None, returncode=0):
        runner = self.root / "fake backend.py"
        source = "import sys\n"
        source += "print(" + repr(raw if raw is not None else json.dumps(payload)) + ")\n"
        source += "raise SystemExit(" + repr(returncode) + ")\n"
        runner.write_text(source)
        self.rewrite_config(command=[sys.executable, str(runner)])

    @staticmethod
    def envelope(**fields):
        return dict(ok=True, backend="agentbridge", protocol_version=2, **fields)

    def test_missing_configuration_is_failure(self):
        (self.skill() / "bridge.json").unlink()
        self.assertEqual(self.call()["code"], "not_configured")
        self.assertFalse((self.cwd / ".agentbridge").exists())

    def test_missing_backend_is_failure(self):
        self.rewrite_config(command=[sys.executable, str(self.root / "missing.py")])
        self.assertEqual(self.call()["code"], "backend_missing")

    def test_malformed_backend_command_is_invalid_config(self):
        for value in ("agentbridge", [], [""], [str(self.root / "a"), "b", "c"], ["relative/path"]):
            with self.subTest(value=value):
                self.rewrite_config(command=value)
                self.assertIn(self.call()["code"], ("invalid_config", "backend_missing"))

    def test_invalid_configuration_protocol_is_failure(self):
        self.rewrite_config(protocol_version=1)
        self.assertEqual(self.call()["code"], "invalid_config")

    def test_another_actual_directory_is_not_selected_via_cwd_argument(self):
        other = self.root / "another-project"
        other.mkdir()
        self.assertEqual(self.call(declared_cwd=other)["code"], "cwd_mismatch")
        self.assertEqual(self.call(declared_cwd=".")["code"], "cwd_mismatch")
        self.assertFalse((other / ".agentbridge").exists())

    def test_first_use_establishes_boundary_at_the_workspace(self):
        result = self.call()
        self.assertTrue(result["ok"], result)
        self.assertIs(result["established"], True)
        self.assertEqual(result["project"], str(self.cwd))
        marker = json.loads((self.cwd / ".agentbridge/project.json").read_text())
        self.assertEqual(marker["project_root"], str(self.cwd))
        # A second invocation in the same workspace reuses it silently.
        self.assertIs(self.call()["established"], False)

    def test_non_json_backend_output_is_not_a_receipt(self):
        self.fake_backend(raw="I saved the handoff successfully.")
        self.assertEqual(self.call()["code"], "invalid_response")

    def test_incompatible_response_protocol_is_rejected(self):
        for fields in ({"protocol_version": 1}, {"backend": "another"}, {"ok": "true"}):
            with self.subTest(fields=fields):
                payload = self.envelope()
                payload.update(fields)
                self.fake_backend(payload)
                self.assertEqual(self.call()["code"], "protocol_mismatch")

    def test_nonzero_backend_cannot_claim_success(self):
        self.fake_backend(self.envelope(project=str(self.cwd), session_id="s", draft_path="draft"), returncode=7)
        self.assertEqual(self.call()["code"], "backend_failed")

    def test_backend_failure_is_passed_through_honestly(self):
        payload = dict(ok=False, backend="agentbridge", protocol_version=2,
                       code="invalid_body", message="Draft was not saved")
        self.fake_backend(payload, returncode=1)
        self.assertEqual(self.call("send"), payload)

    def test_partial_check_or_send_success_is_rejected(self):
        for action, payload in (("check", self.envelope(project=str(self.cwd))),
                                ("send", self.envelope(state="saved")),
                                ("send", self.envelope(state="prepared", packet_id="a" * 32, details_path="x"))):
            with self.subTest(action=action, payload=payload):
                self.fake_backend(payload)
                self.assertEqual(self.call(action)["code"], "invalid_receipt")

    def test_partial_receive_success_is_rejected(self):
        for status in ("received", "already_received"):
            with self.subTest(status=status):
                self.fake_backend(self.envelope(status=status))
                result = self.call("receive")
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid_receipt")

    def test_unknown_receive_status_is_failure(self):
        self.fake_backend(self.envelope(status="probably_received"))
        self.assertEqual(self.call("receive")["code"], "invalid_receipt")

    def test_receive_cannot_inject_an_unbounded_card(self):
        self.fake_backend(self.envelope(status="received", packet_id="a" * 32,
                                        details_path="details.json", context="x" * 1001))
        self.assertFalse(self.call("receive")["ok"])

    def test_oversized_backend_response_is_rejected(self):
        self.fake_backend(raw="x" * 32001)
        self.assertEqual(self.call()["code"], "invalid_response")

    def _path(self, subdir, name):
        return str(self.cwd / ".agentbridge" / subdir / name)

    def test_truthy_but_malformed_receipt_fields_are_rejected(self):
        drafts, handoffs = "handoff-drafts", "handoffs"
        ident = "a" * 32
        other = self.root / "another-project"
        cases = [
            # A non-hex or non-string packet id must not pass as truthy.
            ("send", self.envelope(status="saved", state="saved", project=str(self.cwd),
                                    packet_id=True, details_path=self._path(handoffs, ident + ".json"))),
            ("send", self.envelope(status="saved", state="saved", project=str(self.cwd),
                                    packet_id="A" * 32, details_path=self._path(handoffs, "A" * 32 + ".json"))),
            # A details path must live inside THIS project's handoffs directory.
            ("send", self.envelope(status="saved", state="saved", project=str(self.cwd), packet_id=ident,
                                    details_path=str(other / ".agentbridge/handoffs" / (ident + ".json")))),
            # The details filename must match the reported packet id.
            ("send", self.envelope(status="saved", state="saved", project=str(self.cwd), packet_id=ident,
                                    details_path=self._path(handoffs, "b" * 32 + ".json"))),
            # A receipt cannot claim a project that is not this task's directory tree.
            ("check", self.envelope(status="ready", project=str(other), session_id="s",
                                     draft_path=self._path(drafts, ident + ".json"))),
            # The draft path must be a UUID.json inside handoff-drafts.
            ("check", self.envelope(status="ready", project=str(self.cwd), session_id="s",
                                     draft_path=self._path(drafts, "notes.txt"))),
            # List metadata must be a list of well-formed items.
            ("list", self.envelope(status="listed", project=str(self.cwd), items="all of them")),
            ("list", self.envelope(status="listed", project=str(self.cwd), items=[{"id": "short", "source": "codex"}])),
            # A choose receipt needs valid options and an honest total count.
            ("receive", self.envelope(status="choose", project=str(self.cwd), session_id="s", context="pick",
                                       items=[{"id": ident, "source": "codex"}], total_count=0)),
        ]
        for action, payload in cases:
            with self.subTest(action=action, packet=payload.get("packet_id"), items=payload.get("items")):
                self.fake_backend(payload)
                result = self.call(action)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["code"], "invalid_receipt")

    def test_receipt_session_must_match_the_request(self):
        self.fake_backend(self.envelope(status="ready", project=str(self.cwd), session_id="theirs",
                                        draft_path=self._path("handoff-drafts", "a" * 32 + ".json")))
        result = self.call("check", extra=("--session", "mine"))
        self.assertEqual(result["code"], "invalid_receipt")

    def test_well_formed_receipts_for_each_action_are_accepted(self):
        ident = "a" * 32
        payloads = {
            "check": self.envelope(status="ready", project=str(self.cwd), session_id="s",
                                    draft_path=self._path("handoff-drafts", ident + ".json")),
            "send": self.envelope(status="saved", state="saved", project=str(self.cwd), session_id="s",
                                   packet_id=ident, details_path=self._path("handoffs", ident + ".json")),
            "list": self.envelope(status="listed", project=str(self.cwd),
                                   items=[dict(id=ident, source="codex", created_at=1, goal="g")]),
            "receive": self.envelope(status="received", project=str(self.cwd), session_id="s", context="card",
                                      packet_id=ident, details_path=self._path("handoffs", ident + ".json")),
        }
        for action, payload in payloads.items():
            with self.subTest(action=action):
                self.fake_backend(payload)
                self.assertTrue(self.call(action)["ok"], payload)

    def test_temporary_project_handoff_crosses_installed_client_adapters(self):
        body = {"goal": "Finish the parser integration", "constraints": ["Keep compatibility"],
                "completed": ["Wrote the parser"], "in_progress": [], "decisions": ["Use JSON"], "findings": [],
                "files": ["parser.py"], "verification": ["Integration not run"],
                "next_steps": ["Check the parser and run integration"], "blockers": []}
        (self.cwd / "parser.py").write_text("# project artifact\n")
        source = self.call(agent="workbuddy")
        self.assertTrue(source["ok"], source)
        draft = Path(source["draft_path"])
        draft.write_text(json.dumps(body))
        saved = self.call("send", agent="workbuddy", extra=("--session", source["session_id"], "--file", str(draft)))
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["state"], "saved")
        export = json.loads(Path(saved["details_path"]).read_text())
        self.assertEqual(export["source"], "workbuddy")
        self.assertEqual(export["target"], "any")
        self.assertEqual(export["body"], body)
        listed = self.call("list", agent="claude")
        self.assertEqual(listed["items"][0]["id"], saved["packet_id"])
        receiver = self.call(agent="codex")
        received = self.call("receive", agent="codex", extra=("--session", receiver["session_id"]))
        self.assertTrue(received["ok"], received)
        self.assertEqual(received["status"], "received")
        self.assertEqual(received["packet_id"], saved["packet_id"])
        self.assertLessEqual(len(received["context"]), 1000)
        self.assertEqual(received["details_path"], saved["details_path"])
        reread = self.call("receive", extra=("--session", receiver["session_id"], "--id", saved["packet_id"]))
        self.assertEqual(reread["status"], "already_received")
        other_receiver = self.call(agent="claude")
        empty = self.call("receive", agent="claude", extra=("--session", other_receiver["session_id"]))
        self.assertEqual(empty["status"], "empty")

    def test_reusing_workbuddy_session_across_invocations_keeps_ownership(self):
        body = {"goal": "Continue the current project", "constraints": [], "completed": [], "in_progress": [],
                "decisions": [], "findings": [], "files": [], "verification": [],
                "next_steps": ["Inspect the remaining work"], "blockers": []}
        source = self.call(agent="workbuddy")
        draft = Path(source["draft_path"])
        draft.write_text(json.dumps(body))
        saved = self.call("send", agent="workbuddy", extra=("--session", source["session_id"], "--file", str(draft)))
        self.assertTrue(saved["ok"], saved)
        next_invocation = self.call(agent="workbuddy", extra=("--session", source["session_id"]))
        self.assertEqual(next_invocation["session_id"], source["session_id"])
        own = self.call("receive", agent="workbuddy", extra=("--session", next_invocation["session_id"], "--id", saved["packet_id"]))
        self.assertEqual(own["status"], "empty")
        different_chat = self.call(agent="workbuddy")
        self.assertNotEqual(different_chat["session_id"], source["session_id"])
        received = self.call("receive", agent="workbuddy", extra=("--session", different_chat["session_id"], "--id", saved["packet_id"]))
        self.assertEqual(received["status"], "received")
        later_invocation = self.call(agent="workbuddy", extra=("--session", different_chat["session_id"]))
        reread = self.call("receive", agent="workbuddy", extra=("--session", later_invocation["session_id"], "--id", saved["packet_id"]))
        self.assertEqual(reread["status"], "already_received")


class BootstrapInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="AgentBridge bootstrap ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.installer = Path(__file__).resolve().parents[1] / "install.py"
        self.env = dict(os.environ, HOME=str(self.home))
        self.env.pop("USERPROFILE", None)

    def run_installer(self, *extra):
        return subprocess.run([sys.executable, str(self.installer), *extra],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def handoff(self, agent):
        return self.home / DIRECTORIES[agent] / "skills/handoff"

    def test_detect_clients_reports_only_present_directories(self):
        (self.home / ".workbuddy-ai").mkdir()
        self.assertEqual(skill_install.detect_clients(self.home), ("workbuddy",))
        (self.home / ".codex").mkdir()
        self.assertEqual(skill_install.detect_clients(self.home), ("codex", "workbuddy"))

    def test_bootstrap_auto_installs_present_clients_only(self):
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.handoff("claude") / "bridge.json").is_file())
        self.assertTrue((self.handoff("codex") / "bridge.json").is_file())
        self.assertFalse(self.handoff("workbuddy").exists())
        self.assertIn("handoff", result.stdout)

    def test_bootstrap_without_any_client_reports_and_writes_nothing(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No Codex", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_bootstrap_preview_writes_nothing(self):
        (self.home / ".claude").mkdir()
        result = self.run_installer("--preview")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.handoff("claude").exists())
        self.assertIn("Preview only", result.stdout)

    def test_bootstrap_explicit_clients_override_detection(self):
        result = self.run_installer("--clients", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.handoff("codex") / "bridge.json").is_file())
        self.assertFalse(self.handoff("claude").exists())

    def test_bootstrap_invalid_client_stops_without_changes(self):
        result = self.run_installer("--clients", "codex,unknown")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Install stopped without changes", result.stderr)
        self.assertFalse(self.handoff("codex").exists())


if __name__ == "__main__":
    unittest.main()
