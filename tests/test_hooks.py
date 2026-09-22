import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbridge.hooks import MAX_INPUT_BYTES, handle_hook, run_hook
from agentbridge.store import Store


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.register(self.base / "project")
        self.other = self.register(self.base / "project-other")

    def register(self, path):
        marker = path / ".agentbridge" / "project.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"version": 1, "project_root": str(path), "name": path.name}))
        return path

    def event(self, event_name="Stop", **kwargs):
        event = {"hook_event_name": event_name, "cwd": str(self.project), "session_id": "test-session"}
        event.update(kwargs)
        return event

    def configure(self, mode, interval_seconds=900):
        marker = self.project / ".agentbridge" / "project.json"
        registration = json.loads(marker.read_text())
        registration.update(sync_mode=mode, sync_interval_seconds=interval_seconds)
        marker.write_text(json.dumps(registration))

    def manual_event(self, **kwargs):
        return self.event("UserPromptSubmit", prompt="同步进展", **kwargs)

    def test_context_includes_other_agent_only_in_same_project(self):
        self.configure("interval")
        Store(self.project).publish("claude", "a", "shared-project-summary")
        Store(self.project).publish("codex", "b", "own-agent-summary")
        Store(self.other).publish("claude", "c", "different-project-summary")
        for event_name in ("SessionStart", "UserPromptSubmit"):
            output = handle_hook(self.project, "codex", self.event(event_name, session_id="recipient-" + event_name))
            specific = output["hookSpecificOutput"]
            self.assertEqual(specific["hookEventName"], event_name)
            self.assertIn("shared-project-summary", specific["additionalContext"])
            self.assertNotIn("own-agent-summary", specific["additionalContext"])
            self.assertNotIn("different-project-summary", specific["additionalContext"])

    def test_context_is_incremental_per_session_with_short_history_for_new_session(self):
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "first-peer-summary")
        self.assertEqual(handle_hook(self.project, "codex", self.event("SessionStart")), {})
        first = handle_hook(self.project, "codex", self.manual_event())
        self.assertIn("first-peer-summary", first["hookSpecificOutput"]["additionalContext"])
        for _ in range(3):
            self.assertEqual(handle_hook(self.project, "codex", self.event("UserPromptSubmit")), {})
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "second-peer-summary")
        second = handle_hook(self.project, "codex", self.manual_event())
        context = second["hookSpecificOutput"]["additionalContext"]
        self.assertIn("second-peer-summary", context)
        self.assertNotIn("first-peer-summary", context)
        self.assertLessEqual(len(context), 1000)
        self.assertEqual(handle_hook(self.project, "codex", self.event("UserPromptSubmit")), {})
        empty = handle_hook(self.project, "codex", self.manual_event())
        self.assertEqual(empty["hookSpecificOutput"]["additionalContext"], "本项目没有新的重要共享进展。")
        history = handle_hook(self.project, "codex", self.manual_event(session_id="new-recipient-session"))
        context = history["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("first-peer-summary", context)
        self.assertIn("second-peer-summary", context)

    def test_activity_alone_does_not_inject_context(self):
        self.configure("session")
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "file-operation-activity", kind="activity", files=["main.py"])
        self.assertEqual(handle_hook(self.project, "codex", self.event("SessionStart")), {})
        self.assertEqual(handle_hook(self.project, "codex", self.event("UserPromptSubmit")), {})

    def test_context_requires_session_and_requests_small_incremental_budget(self):
        for session_id in (None, "", "bad\nsession"):
            with self.subTest(session_id=session_id), patch("agentbridge.hooks.Store") as store:
                self.assertEqual(handle_hook(self.project, "codex", self.manual_event(session_id=session_id)), {})
                store.assert_not_called()
        with patch("agentbridge.hooks.Store") as store:
            store.return_value.deliver_context.return_value = ""
            result = handle_hook(self.project, "codex", self.manual_event())
            self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "本项目没有新的重要共享进展。")
            store.return_value.deliver_context.assert_called_once_with("codex", "test-session", limit=3, max_chars=1000, min_interval_seconds=0)
            store.return_value.context.assert_not_called()

    def test_default_manual_mode_does_not_deliver_for_ordinary_events(self):
        with patch("agentbridge.hooks.Store") as store:
            self.assertEqual(handle_hook(self.project, "codex", self.event("SessionStart")), {})
            store.assert_not_called()
        for event in (self.event("UserPromptSubmit", prompt="继续工作"), self.event("UserPromptSubmit", prompt="请在完成后同步进展")):
            with patch("agentbridge.hooks.Store") as store:
                self.assertEqual(handle_hook(self.project, "codex", event), {})
                store.return_value.deliver_context.assert_not_called()
                store.return_value.mark_sync_turn.assert_called_once_with("codex", "test-session", False)

    def test_manual_sync_aliases_are_exact_and_do_not_store_prompt(self):
        for phrase in ("同步进展", " 同步进度\n"):
            with patch("agentbridge.hooks.Store") as store:
                store.return_value.deliver_context.return_value = "new-shared-report"
                result = handle_hook(self.project, "codex", self.event("UserPromptSubmit", prompt=phrase))
                self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "new-shared-report")
                store.return_value.deliver_context.assert_called_once_with("codex", "test-session", limit=3, max_chars=1000, min_interval_seconds=0)
                store.return_value.publish.assert_not_called()

    def test_sync_response_and_stop_retries_do_not_echo_peer_reports(self):
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "peer-implemented-feature")
        synced = handle_hook(self.project, "codex", self.manual_event())
        self.assertIn("peer-implemented-feature", synced["hookSpecificOutput"]["additionalContext"])
        sync_stop = self.event("Stop", last_assistant_message="已同步，Claude说peer-implemented-feature", turn_id="sync-turn")
        for _ in range(2):
            self.assertEqual(handle_hook(self.project, "codex", sync_stop), {})
        with Store(self.project) as store:
            self.assertEqual(store.list_events(agent="codex"), [])
        self.assertEqual(handle_hook(self.project, "codex", self.event("UserPromptSubmit", prompt="现在实现下一项功能")), {})
        self.assertEqual(handle_hook(self.project, "codex", self.event("Stop", last_assistant_message="implemented-next-feature", turn_id="work-turn")), {})
        with Store(self.project) as store:
            reports = store.list_events(agent="codex")
        self.assertEqual(len(reports), 1)
        self.assertIn("implemented-next-feature", reports[0]["summary"])

    def test_empty_manual_sync_response_is_not_published_either(self):
        result = handle_hook(self.project, "codex", self.manual_event())
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "本项目没有新的重要共享进展。")
        handle_hook(self.project, "codex", self.event("Stop", last_assistant_message="本项目没有新的重要共享进展。"))
        with Store(self.project) as store:
            self.assertEqual(store.list_events(), [])

    def test_session_mode_only_automatically_injects_at_session_start(self):
        self.configure("session")
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "session-initial-report")
        first = handle_hook(self.project, "codex", self.event("SessionStart"))
        self.assertIn("session-initial-report", first["hookSpecificOutput"]["additionalContext"])
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "session-later-report")
        self.assertEqual(handle_hook(self.project, "codex", self.event("UserPromptSubmit", prompt="继续")), {})
        manual = handle_hook(self.project, "codex", self.manual_event())
        self.assertIn("session-later-report", manual["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("session-initial-report", manual["hookSpecificOutput"]["additionalContext"])

    def test_interval_mode_rate_limits_but_manual_sync_bypasses_interval(self):
        self.configure("interval", interval_seconds=900)
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "interval-initial-report")
        first = handle_hook(self.project, "codex", self.event("SessionStart"))
        self.assertIn("interval-initial-report", first["hookSpecificOutput"]["additionalContext"])
        with Store(self.project) as store:
            store.publish("claude", "peer-session", "interval-later-report")
        self.assertEqual(handle_hook(self.project, "codex", self.event("UserPromptSubmit", prompt="继续")), {})
        manual = handle_hook(self.project, "codex", self.manual_event())
        self.assertIn("interval-later-report", manual["hookSpecificOutput"]["additionalContext"])

    def test_interval_mode_passes_registered_interval(self):
        self.configure("interval", interval_seconds=1200)
        for event_name in ("SessionStart", "UserPromptSubmit"):
            with patch("agentbridge.hooks.Store") as store:
                store.return_value.deliver_context.return_value = ""
                self.assertEqual(handle_hook(self.project, "codex", self.event(event_name)), {})
                store.return_value.deliver_context.assert_called_once_with("codex", "test-session", limit=3, max_chars=1000, min_interval_seconds=1200)

    def test_invalid_scope_never_opens_store(self):
        nested = self.register(self.project / "nested")
        nested_child = nested / "src"
        nested_child.mkdir()
        for cwd in (None, "", ".", str(self.other), str(nested), str(nested_child)):
            with self.subTest(cwd=cwd), patch("agentbridge.hooks.Store") as store, contextlib.redirect_stderr(io.StringIO()):
                result = handle_hook(self.project, "codex", self.manual_event(cwd=cwd))
                self.assertEqual(result, {})
                store.assert_not_called()

    def test_project_marker_is_required_valid_and_bound_to_root(self):
        marker = self.project / ".agentbridge" / "project.json"
        for contents in ("{}", "invalid-json", json.dumps({"version": 1, "project_root": str(self.other)})):
            marker.write_text(contents)
            with patch("agentbridge.hooks.Store") as store, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(handle_hook(self.project, "claude", self.manual_event()), {})
                store.assert_not_called()
        marker.unlink()
        with patch("agentbridge.hooks.Store") as store, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(handle_hook(self.project, "claude", self.manual_event()), {})
            store.assert_not_called()

    def test_symlink_cwd_cannot_escape_project(self):
        link = self.project / "elsewhere"
        link.symlink_to(self.other, target_is_directory=True)
        with patch("agentbridge.hooks.Store") as store, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(handle_hook(self.project, "codex", self.manual_event(cwd=str(link))), {})
            store.assert_not_called()

    def test_stop_uses_only_assistant_summary_and_deduplicates(self):
        event = self.event(last_assistant_message="implemented-project-feature", turn_id="turn-1", prompt="PRIVATE USER PROMPT", transcript_path="/nonexistent/private-transcript")
        for _ in range(2):
            self.assertEqual(handle_hook(self.project, "codex", event), {})
        context = Store(self.project).context("claude")
        self.assertEqual(context.count("implemented-project-feature"), 1)
        self.assertNotIn("PRIVATE USER PROMPT", context)
        self.assertNotIn("private-transcript", context)
        self.assertIn("尚未独立验证", context)

    def test_stop_without_turn_id_uses_content_deduplication(self):
        event = self.event(last_assistant_message="fallback-dedupe-summary")
        handle_hook(self.project, "claude", event)
        handle_hook(self.project, "claude", event)
        self.assertEqual(Store(self.project).context("codex").count("fallback-dedupe-summary"), 1)

    def test_stop_bounds_summary_and_does_not_claim_verification(self):
        with patch("agentbridge.hooks.Store") as store:
            store.return_value.is_sync_turn.return_value = False
            handle_hook(self.project, "codex", self.event(last_assistant_message="x" * 8000))
            summary = store.return_value.publish.call_args.args[2]
            self.assertLessEqual(len(summary), 4000)
            self.assertIn("尚未独立验证", summary)
            self.assertIn("已截断", summary)

    def test_write_and_edit_record_only_project_file_paths(self):
        child = self.project / "src"
        child.mkdir()
        for tool_name in ("Write", "Edit"):
            with self.subTest(tool_name=tool_name), patch("agentbridge.hooks.Store") as store:
                event = self.event("PostToolUse", cwd=str(child), tool_name=tool_name, tool_input={"file_path": "main.py", "content": "PRIVATE CODE"}, tool_response={"output": "PRIVATE TOOL OUTPUT"}, tool_use_id="tool-1")
                self.assertEqual(handle_hook(self.project, "claude", event), {})
                call = store.return_value.publish.call_args
                self.assertEqual(call.kwargs["files"], ["src/main.py"])
                self.assertEqual(call.kwargs["kind"], "activity")
                self.assertIn("工具报告文件操作", call.args[2])
                self.assertNotIn("PRIVATE", repr(call))

    def test_patch_tracks_explicit_headers_and_move_destination(self):
        command = "*** Begin Patch\n*** Update File: src/old.py\n*** Move to: src/new.py\n@@\n-SECRET OLD CODE\n+SECRET NEW CODE\n*** Add File: README.md\n+hello\n*** Delete File: unused.py\n*** End Patch"
        with patch("agentbridge.hooks.Store") as store:
            handle_hook(self.project, "codex", self.event("PostToolUse", tool_name="functions.apply_patch", tool_input={"command": command}, tool_response={"success": True}))
            call = store.return_value.publish.call_args
            self.assertEqual(call.kwargs["files"], ["src/old.py", "src/new.py", "README.md", "unused.py"])
            self.assertNotIn("SECRET", repr(call))
            self.assertNotIn("Begin Patch", repr(call))

    def test_sensitive_file_names_are_not_repeated_in_summary(self):
        handle_hook(self.project, "claude", self.event("PostToolUse", tool_name="Write", tool_input={"file_path": ".env.production", "content": "SECRET"}))
        context = Store(self.project).context("codex")
        self.assertNotIn(".env.production", context)
        self.assertNotIn("SECRET", context)

    def test_repeated_file_operation_in_different_turns_is_preserved(self):
        for turn_id in ("turn-a", "turn-b"):
            event = self.event("PostToolUse", turn_id=turn_id, tool_name="Write", tool_input={"file_path": "main.py"})
            handle_hook(self.project, "claude", event)
            handle_hook(self.project, "claude", event)
        with Store(self.project) as store:
            self.assertEqual(len(store.list_events()), 2)

    def test_store_is_closed_after_context_or_publication(self):
        self.configure("session")
        for event in (self.event("SessionStart"), self.event(last_assistant_message="summary")):
            with patch("agentbridge.hooks.Store") as store:
                handle_hook(self.project, "codex", event)
                store.return_value.close.assert_called_once_with()

    def test_unknown_tools_and_shell_wrapped_patch_are_not_interpreted(self):
        examples = [
            ("Bash", {"command": "touch secret.py"}),
            ("exec_command", {"cmd": "rm other.py"}),
            ("apply_patch", {"command": "echo start\n*** Begin Patch\n*** Add File: a.py\n*** End Patch"}),
        ]
        for tool_name, tool_input in examples:
            with self.subTest(tool=tool_name), patch("agentbridge.hooks.Store") as store:
                handle_hook(self.project, "codex", self.event("PostToolUse", tool_name=tool_name, tool_input=tool_input))
                store.return_value.publish.assert_not_called()

    def test_file_paths_cannot_enter_other_or_nested_projects(self):
        nested = self.register(self.project / "nested")
        link = self.project / "elsewhere"
        link.symlink_to(self.other, target_is_directory=True)
        invalid = [str(self.other / "a.py"), "../project-other/a.py", str(nested / "a.py"), str(link / "a.py"), ".agentbridge/project.json"]
        for file_path in invalid:
            with self.subTest(file_path=file_path), patch("agentbridge.hooks.Store") as store:
                handle_hook(self.project, "claude", self.event("PostToolUse", tool_name="Write", tool_input={"file_path": file_path}))
                store.return_value.publish.assert_not_called()

    def test_known_tool_errors_do_not_publish_file_activity(self):
        errors = [
            {"isError": True}, {"is_error": True}, {"success": False},
            {"status": "failed"}, {"error": "PRIVATE ERROR"}, {"exit_code": 1},
            {"result": {"exitCode": 2}}, '{"success":false}', "Error: PRIVATE ERROR",
        ]
        for response in errors:
            with self.subTest(response=response), patch("agentbridge.hooks.Store") as store:
                handle_hook(self.project, "claude", self.event("PostToolUse", tool_name="Edit", tool_input={"file_path": "main.py"}, tool_response=response))
                store.return_value.publish.assert_not_called()

    def test_failures_emit_empty_json_without_sensitive_diagnostics(self):
        output = io.StringIO()
        error = io.StringIO()
        with patch("agentbridge.hooks.Store", side_effect=RuntimeError("PRIVATE PAYLOAD")), contextlib.redirect_stderr(error):
            result = run_hook(self.project, "codex", io.StringIO(json.dumps(self.manual_event())), output)
        self.assertEqual(result, {})
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertNotIn("PRIVATE", error.getvalue())
        self.assertNotIn("decision", output.getvalue())

    def test_oversized_malformed_and_nonobject_input_fail_open(self):
        examples = ["x" * (MAX_INPUT_BYTES + 1), '"' + "汉" * (MAX_INPUT_BYTES // 3 + 1) + '"', "{broken", "[]", "null"]
        for raw in examples:
            with self.subTest(length=len(raw)), patch("agentbridge.hooks.Store") as store, contextlib.redirect_stderr(io.StringIO()):
                output = io.StringIO()
                self.assertEqual(run_hook(self.project, "codex", io.StringIO(raw), output), {})
                self.assertEqual(json.loads(output.getvalue()), {})
                store.assert_not_called()

    def test_unsupported_agents_cannot_read_or_write(self):
        with patch("agentbridge.hooks.Store") as store, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(handle_hook(self.project, "unknown", self.manual_event()), {})
            store.assert_not_called()

    def test_missing_stop_message_or_session_does_not_publish(self):
        for event in (self.event(), self.event(last_assistant_message="hello", session_id=None)):
            with patch("agentbridge.hooks.Store") as store:
                store.return_value.is_sync_turn.return_value = False
                self.assertEqual(handle_hook(self.project, "codex", event), {})
                store.return_value.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
