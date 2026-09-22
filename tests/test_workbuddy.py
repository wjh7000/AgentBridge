import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from agentbridge.hooks import MAX_INPUT_BYTES
from agentbridge.store import Store
from agentbridge.workbuddy import dispatch_hook, install_workbuddy, uninstall_workbuddy


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.settings = self.base / "home/workbuddy/settings.json"
        self.registry = self.base / "home/bridge/workbuddy-projects.json"
        self.one = self.register(self.base / "project one")
        self.two = self.register(self.base / "project two")

    def register(self, root):
        marker = root / ".agentbridge/project.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"version": 1, "project_root": str(root), "name": root.name}))
        return root

    def install(self, projects, **kwargs):
        return install_workbuddy(projects, self.settings, self.registry, **kwargs)

    def dispatch(self, cwd, **fields):
        event = dict(hook_event_name="UserPromptSubmit", cwd=str(cwd), session_id="session")
        event.update(fields)
        out = io.StringIO()
        value = dispatch_hook(self.registry, stdin=io.StringIO(json.dumps(event)), stdout=out)
        self.assertEqual(json.loads(out.getvalue()), value)
        return value

    def test_preview_writes_nothing(self):
        result = self.install([self.one], preview=True)
        self.assertTrue(result["preview"])
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.registry.parent.exists())

    def test_install_preserves_settings_merges_projects_and_is_idempotent(self):
        self.settings.parent.mkdir(parents=True)
        original = {"model": "keep-me", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-command"}]}]}}
        self.settings.write_text(json.dumps(original))
        self.install([self.one])
        self.install([self.two, self.one])
        before = self.settings.read_bytes()
        self.assertEqual(self.install([self.two])["files"], [])
        self.assertEqual(self.settings.read_bytes(), before)
        data = json.loads(before)
        self.assertEqual(data["model"], "keep-me")
        self.assertEqual(data["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        for name in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            handlers = [h for group in data["hooks"][name] for h in group["hooks"] if "dispatch" in h.get("command", "")]
            self.assertEqual(len(handlers), 1)
            args = shlex.split(handlers[0]["command"])
            self.assertEqual(args[2:], ["dispatch", "--registry", str(self.registry), "--agent", "workbuddy"])
        self.assertEqual(json.loads(self.registry.read_text())["projects"], sorted([str(self.one), str(self.two)]))

    def test_uninstall_restores_original_bytes(self):
        self.settings.parent.mkdir(parents=True)
        original = b'{ "model": "preserve formatting" }\n'
        self.settings.write_bytes(original)
        self.install([self.one])
        self.install([self.two])
        result = uninstall_workbuddy(self.settings, self.registry)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(self.registry.exists())
        self.assertEqual(result["skipped_modified_files"], [])

    def test_uninstall_never_overwrites_user_changes_even_after_reinstall(self):
        for reinstall in (False, True):
            with self.subTest(reinstall=reinstall):
                self.install([self.one])
                data = json.loads(self.settings.read_text())
                data["user_change"] = True
                self.settings.write_text(json.dumps(data))
                if reinstall:
                    self.install([self.two])
                before = self.settings.read_bytes()
                result = uninstall_workbuddy(self.settings, self.registry)
                self.assertIn(str(self.settings), result["skipped_modified_files"])
                self.assertEqual(self.settings.read_bytes(), before)

    def test_routes_between_projects_without_cross_contamination(self):
        self.install([self.one, self.two])
        Store(self.one).publish("claude", "a", "ONLY-PROJECT-ONE")
        Store(self.two).publish("codex", "b", "ONLY-PROJECT-TWO")
        for project, yes, no in ((self.one, "ONLY-PROJECT-ONE", "ONLY-PROJECT-TWO"), (self.two, "ONLY-PROJECT-TWO", "ONLY-PROJECT-ONE")):
            child = project / "src"
            child.mkdir()
            self.assertEqual(self.dispatch(child, prompt="继续工作"), {})
            context = self.dispatch(child, prompt="同步进展")["hookSpecificOutput"]["additionalContext"]
            self.assertIn(yes, context)
            self.assertNotIn(no, context)
        self.dispatch(self.one, prompt="执行项目任务")
        self.dispatch(self.one, hook_event_name="Stop", last_assistant_message="WORKBUDDY-ONE-RESULT")
        self.assertIn("WORKBUDDY-ONE-RESULT", Store(self.one).context("codex"))
        self.assertNotIn("WORKBUDDY-ONE-RESULT", Store(self.two).context("codex"))

    def test_user_permission_and_hook_survive_reenable_then_disable(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"model": "original-model"}))
        self.install([self.one])
        settings = json.loads(self.settings.read_text())
        settings["permissions"] = {"deny": ["Bash(rm *)"]}
        user_hook = {"hooks": [{"type": "command", "command": "user-added-validation"}]}
        settings["hooks"]["Stop"].append(user_hook)
        self.settings.write_text(json.dumps(settings))
        self.install([self.two])
        # Further unchanged reinstalls must not clear the user-modified flag.
        self.install([self.one, self.two])
        expected = self.settings.read_bytes()
        result = uninstall_workbuddy(self.settings, self.registry)
        self.assertIn(str(self.settings), result["skipped_modified_files"])
        self.assertEqual(self.settings.read_bytes(), expected)
        remaining = json.loads(self.settings.read_text())
        self.assertEqual(remaining["permissions"], {"deny": ["Bash(rm *)"]})
        self.assertIn(user_hook, remaining["hooks"]["Stop"])
        self.assertFalse(self.registry.exists())

    def test_deleted_settings_before_reenable_do_not_resurrect_old_backup(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"model": "intentionally-deleted"}))
        self.install([self.one])
        self.settings.unlink()
        self.install([self.two])
        expected = self.settings.read_bytes()
        result = uninstall_workbuddy(self.settings, self.registry)
        self.assertIn(str(self.settings), result["skipped_modified_files"])
        self.assertEqual(self.settings.read_bytes(), expected)
        self.assertNotIn("intentionally-deleted", self.settings.read_text())

    def test_unlisted_or_malformed_nested_marker_blocks_parent(self):
        self.install([self.one])
        nested = self.register(self.one / "nested")
        for marker_content in (None, "{}", "not-json"):
            if marker_content is not None:
                (nested / ".agentbridge/project.json").write_text(marker_content)
            with patch("agentbridge.workbuddy.handle_hook") as handle:
                self.assertEqual(self.dispatch(nested), {})
                handle.assert_not_called()
        with patch("agentbridge.workbuddy.handle_hook") as handle:
            self.assertEqual(self.dispatch(self.two), {})
            handle.assert_not_called()

    def test_oversize_or_invalid_input_fails_open(self):
        self.install([self.one])
        for raw in ("x" * (MAX_INPUT_BYTES + 1), "null", "{", '{"cwd":"."}'):
            out = io.StringIO()
            with patch("agentbridge.workbuddy.handle_hook") as handle:
                self.assertEqual(dispatch_hook(self.registry, stdin=io.StringIO(raw), stdout=out), {})
                self.assertEqual(out.getvalue(), "{}\n")
                handle.assert_not_called()

    def test_invalid_registration_rejected_before_writes(self):
        (self.one / ".agentbridge/project.json").write_text(json.dumps({"version": 1, "project_root": str(self.two)}))
        with self.assertRaises(ValueError):
            self.install([self.one])
        self.assertFalse(self.settings.exists())

    def test_symlink_destination_rejected(self):
        target = self.base / "actual.json"
        target.write_text("{}")
        self.settings.parent.mkdir(parents=True)
        self.settings.symlink_to(target)
        with self.assertRaises(ValueError):
            self.install([self.one])
        self.assertEqual(target.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
