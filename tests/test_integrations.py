import json
from pathlib import Path
import tempfile
import unittest

from agentbridge.integrations import build_plan, install, registered, uninstall


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="Agent Bridge ' 测试 ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_preview_has_no_side_effects_and_paths_are_absolute(self):
        before = list(self.root.iterdir())
        result = install(self.root, preview=True)
        self.assertEqual(list(self.root.iterdir()), before)
        self.assertTrue(result["preview"])
        _, files, commands = build_plan(self.root, with_mcp=True)
        self.assertIn(str(self.root), files[".mcp.json"])
        self.assertTrue(all("--project" in command for command in commands))

    def test_install_preserves_settings_is_idempotent_and_uninstalls(self):
        (self.root / ".claude").mkdir()
        settings = self.root / ".claude/settings.local.json"
        before = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}]}}
        original = json.dumps(before)
        settings.write_text(original)
        (self.root / "CLAUDE.md").write_text("Existing project instructions.\n")
        install(self.root, ("claude",), with_mcp=True)
        self.assertEqual(registered(self.root), self.root.resolve())
        self.assertEqual(install(self.root, ("claude",), with_mcp=True)["files"], [])
        current = json.loads(settings.read_text())
        self.assertEqual(current["permissions"], before["permissions"])
        self.assertEqual(len(current["hooks"]["Stop"]), 2)
        result = uninstall(self.root)
        self.assertEqual(result["skipped_modified_files"], [])
        self.assertEqual(settings.read_text(), original)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "Existing project instructions.\n")
        self.assertFalse((self.root / ".mcp.json").exists())

    def test_reinstall_then_uninstall_never_deletes_user_edits(self):
        install(self.root, ("claude",), with_mcp=True)
        settings = self.root / ".claude/settings.local.json"
        data = json.loads(settings.read_text())
        data["permissions"] = {"allow": ["Bash(git status)"]}
        settings.write_text(json.dumps(data))
        instructions = self.root / "CLAUDE.md"
        instructions.write_text(instructions.read_text() + "\nUser added this later.\n")
        install(self.root, ("claude",), with_mcp=True)
        result = uninstall(self.root)
        self.assertIn(".claude/settings.local.json", result["skipped_modified_files"])
        self.assertEqual(json.loads(settings.read_text())["permissions"], data["permissions"])
        self.assertIn("User added this later", instructions.read_text())

    def test_malformed_existing_settings_fail_before_any_write(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude/settings.local.json").write_text("broken json")
        with self.assertRaises(ValueError):
            install(self.root)
        self.assertFalse((self.root / ".agentbridge").exists())
        self.assertFalse((self.root / ".codex").exists())

    def test_symlinked_config_is_rejected_without_modifying_target(self):
        external = self.root / "external"
        external.mkdir()
        project = self.root / "project"
        project.mkdir()
        (project / ".claude").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            install(project, ("claude",))
        self.assertEqual(list(external.iterdir()), [])

    def test_path_change_requires_registration(self):
        install(self.root, ("claude",))
        marker = self.root / ".agentbridge/project.json"
        data = json.loads(marker.read_text())
        data["project_root"] = "/another/project"
        marker.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            registered(self.root)
        install(self.root, ("claude",))
        self.assertEqual(registered(self.root), self.root.resolve())

    def test_default_is_manual_without_mcp_or_rule_token_overhead(self):
        install(self.root)
        data = json.loads((self.root / ".agentbridge/project.json").read_text())
        self.assertEqual(data["sync_mode"], "manual")
        for path in (".mcp.json", ".codex/config.toml", "AGENTS.md", "CLAUDE.md", "CODEBUDDY.md"):
            self.assertFalse((self.root / path).exists())
        install(self.root, mode="interval")
        install(self.root)
        data = json.loads((self.root / ".agentbridge/project.json").read_text())
        self.assertEqual(data["sync_mode"], "interval")


if __name__ == "__main__":
    unittest.main()
