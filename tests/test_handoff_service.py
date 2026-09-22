import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from agentbridge.handoff_service import dispatch
from agentbridge.handoffs import Handoffs
from agentbridge.store import Store


def _body(goal="Resume the parser task"):
    return dict(goal=goal, constraints=["Preserve the public API"], completed=["Parser implemented"],
                decisions=[], findings=[], files=["src/parser.py"], verification=["Integration unverified"],
                next_steps=["Verify with the actual client"], blockers=[])


def _send_worker(project, filename, results):
    results.put(dispatch("send", project, "claude", session="source", file=filename))


class HandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self._register(self.project)
        self.environment = patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_SESSION_ID": ""})
        self.environment.start()
        # Missing variables, rather than empty IDs, exercise check's fallback.
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("CLAUDE_SESSION_ID", None)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _register(self, root):
        (root / ".agentbridge").mkdir(parents=True)
        (root / ".agentbridge/project.json").write_text(json.dumps({"version": 1, "project_root": str(root)}))

    def _call(self, action, **kwargs):
        return dispatch(action, kwargs.pop("cwd", self.project), kwargs.pop("agent", "claude"), **kwargs)

    def _assert_protocol(self, result, ok=True):
        self.assertIs(result["ok"], ok)
        self.assertEqual(result["backend"], "agentbridge")
        self.assertEqual(result["protocol_version"], 2)
        if not ok:
            self.assertIn("code", result)
            self.assertIn("message", result)
            self.assertNotIn("packet_id", result)
            self.assertNotIn("details_path", result)
            self.assertNotIn("state", result)

    def _prepare(self, body=None, session="source"):
        checked = self._call("check", session=session)
        self._assert_protocol(checked)
        path = Path(checked["draft_path"])
        path.write_text(json.dumps(_body() if body is None else body, ensure_ascii=False), encoding="utf-8")
        return path

    def test_check_at_registered_project_does_not_re_establish(self):
        result = self._call("check")
        self._assert_protocol(result)
        self.assertEqual(result["project"], str(self.project))
        self.assertIs(result["established"], False)
        self.assertTrue(result["session_id"].startswith("skill-session-"))
        draft = Path(result["draft_path"])
        self.assertEqual(draft.parent, self.project / ".agentbridge/handoff-drafts")
        self.assertFalse(draft.exists())
        self.assertEqual(stat.S_IMODE(draft.parent.stat().st_mode), 0o700)
        self.assertTrue((self.project / ".agentbridge/memory.sqlite3").exists())
        self.assertNotEqual(self._call("check")["session_id"], result["session_id"])

    def test_boundary_is_the_opened_workspace_not_a_parent(self):
        # The opened subdirectory becomes its own boundary; it never walks up
        # to reuse the parent project's namespace.
        cwd = self.project / "src/deep"
        cwd.mkdir(parents=True)
        result = self._call("check", cwd=cwd)
        self._assert_protocol(result)
        self.assertEqual(result["project"], str(cwd))
        self.assertIs(result["established"], True)
        marker = json.loads((cwd / ".agentbridge/project.json").read_text())
        self.assertEqual(marker["project_root"], str(cwd))
        self.assertEqual(marker["version"], 1)
        self.assertEqual(Path(result["draft_path"]).parent, cwd / ".agentbridge/handoff-drafts")
        self.assertEqual(stat.S_IMODE((cwd / ".agentbridge").stat().st_mode), 0o700)
        self.assertEqual((cwd / ".agentbridge/.gitignore").read_text(), "*\n")
        # A second call in the same directory reuses it silently.
        self.assertIs(self._call("check", cwd=cwd)["established"], False)

    def test_full_temporary_project_send_receive_and_metadata(self):
        checked = self._call("check")
        draft = Path(checked["draft_path"])
        original = _body()
        draft.write_text(json.dumps(original), encoding="utf-8")
        sent = self._call("send", session=checked["session_id"], file=draft)
        self._assert_protocol(sent)
        self.assertEqual(sent["state"], "saved")
        self.assertEqual(sent["status"], "saved")
        self.assertNotIn("received", json.dumps(sent))
        packet = json.loads(Path(sent["details_path"]).read_text())
        self.assertEqual(packet["id"], sent["packet_id"])
        self.assertEqual(packet["body"], original)
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o600)
        listed = self._call("list", agent="codex")
        self._assert_protocol(listed)
        self.assertEqual(listed["items"][0]["status"], "ready")
        self.assertNotIn("body", listed["items"][0])
        received = self._call("receive", agent="codex", session="receiver")
        self._assert_protocol(received)
        self.assertEqual(received["status"], "received")
        self.assertEqual(received["packet_id"], sent["packet_id"])
        self.assertEqual(received["details_path"], sent["details_path"])
        self.assertLessEqual(len(received["context"]), 1000)
        self.assertNotIn("packet", received)
        self.assertNotIn("body", received)
        self.assertEqual(self._call("receive", agent="codex", session="another")["status"], "empty")
        reread = self._call("receive", agent="codex", session="receiver", packet_id=sent["packet_id"])
        self.assertEqual(reread["status"], "already_received")
        self.assertNotIn(original["goal"], reread["context"])

    def test_first_use_establishes_the_boundary_and_announces_it(self):
        outside = self.root / "fresh workspace"
        outside.mkdir()
        checked = self._call("check", cwd=outside, session="session")
        self._assert_protocol(checked)
        self.assertIs(checked["established"], True)
        self.assertEqual(checked["project"], str(outside))
        marker = json.loads((outside / ".agentbridge/project.json").read_text())
        self.assertEqual(marker, {"version": 1, "project_root": str(outside), "name": outside.name,
                                  "sync_mode": "manual", "sync_interval_seconds": 900})
        # Every later action reuses the same established boundary.
        listed = self._call("list", cwd=outside)
        self._assert_protocol(listed)
        self.assertIs(listed["established"], False)

    def test_first_use_never_reaches_into_a_parent_boundary(self):
        child = self.project / "child"
        child.mkdir()
        result = self._call("check", cwd=child)
        self.assertEqual(result["project"], str(child))
        self.assertIs(result["established"], True)
        self.assertTrue((child / ".agentbridge/project.json").exists())

    def test_nearest_invalid_marker_never_falls_back_to_parent(self):
        nested = self.project / "nested"
        (nested / ".agentbridge").mkdir(parents=True)
        marker = nested / ".agentbridge/project.json"
        for text in ("broken", "{}", json.dumps({"version": True, "project_root": str(nested)}),
                     json.dumps({"version": 1, "project_root": str(self.project)}), "x" * 16385):
            marker.write_text(text)
            result = self._call("check", cwd=nested)
            self._assert_protocol(result, False)
            self.assertEqual(result["code"], "invalid_registration")
        self.assertFalse((self.project / ".agentbridge/handoff-drafts").exists())
        self.assertFalse((nested / ".agentbridge/handoff-drafts").exists())

    def test_nearest_valid_project_is_used(self):
        nested = self.project / "nested"
        self._register(nested)
        result = self._call("check", cwd=nested)
        self._assert_protocol(result)
        self.assertEqual(result["project"], str(nested))

    def test_symbolic_registration_and_drafts_fail(self):
        marker = self.project / ".agentbridge/project.json"
        outside_marker = self.root / "registration.json"
        marker.rename(outside_marker)
        marker.symlink_to(outside_marker)
        result = self._call("check")
        self._assert_protocol(result, False)
        self.assertEqual(result["code"], "invalid_registration")
        marker.unlink()
        outside_marker.rename(marker)
        drafts = self.project / ".agentbridge/handoff-drafts"
        drafts.symlink_to(self.root)
        result = self._call("check")
        self._assert_protocol(result, False)
        self.assertEqual(result["code"], "invalid_draft_path")

    def test_send_rejects_external_paths_and_symlinks_and_special_files(self):
        draft = self._prepare()
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(_body()))
        alias = draft.parent / ("a" * 32 + ".json")
        alias.symlink_to(outside)
        directory_alias = draft.parent / "external"
        directory_alias.symlink_to(self.root)
        fifo = draft.parent / ("b" * 32 + ".json")
        os.mkfifo(fifo)
        invalid = (None, "", outside, alias, directory_alias / "outside.json", fifo, draft.parent,
                   draft.parent / "missing.json", draft.parent / "../project.json")
        for filename in invalid:
            result = self._call("send", session="source", file=filename)
            self._assert_protocol(result, False)
            self.assertEqual(result["code"], "invalid_draft_path")
        self.assertEqual(self._call("list")["items"], [])
        self.assertEqual(json.loads(outside.read_text()), _body())

    def test_nested_registered_project_inside_drafts_cannot_be_read(self):
        draft = self._prepare()
        nested = draft.parent / "nested"
        self._register(nested)
        subdirectory = nested / "subdirectory"
        subdirectory.mkdir()
        nested_draft = subdirectory / "draft.json"
        nested_draft.write_text(json.dumps(_body()))
        result = self._call("send", session="source", file=nested_draft)
        self._assert_protocol(result, False)
        self.assertEqual(result["code"], "invalid_draft_path")

    def test_only_direct_uuid_drafts_are_supported_with_relative_path_allowed(self):
        draft = self._prepare()
        nested = draft.parent / "drafts"
        nested.mkdir()
        target = nested / draft.name
        draft.rename(target)
        result = self._call("send", session="source", file=target.relative_to(self.project))
        self._assert_protocol(result, False)
        self.assertEqual(result["code"], "invalid_draft_path")
        target.rename(draft)
        result = self._call("send", session="source", file=draft.relative_to(self.project))
        self._assert_protocol(result)
        self.assertEqual(result["state"], "saved")

    def test_draft_body_failures_are_structured_and_never_publish(self):
        draft = self._prepare()
        for body in ("broken", "[]", '{"goal":"one","goal":"two"}', " " * 6001,
                     json.dumps(dict(_body(), goal="")), json.dumps(dict(_body(), extra="unknown"))):
            draft.write_text(body)
            result = self._call("send", session="source", file=draft)
            self._assert_protocol(result, False)
            self.assertEqual(result["code"], "invalid_body")
        self.assertEqual(self._call("list")["items"], [])

    def test_save_export_failure_cannot_claim_saved(self):
        draft = self._prepare()
        export = self.project / ".agentbridge/handoffs"
        export.symlink_to(self.root)
        result = self._call("send", session="source", file=draft)
        self._assert_protocol(result, False)
        self.assertEqual(self._call("list")["items"], [])
        with Store(self.project) as store:
            self.assertIsNone(Handoffs(store).intent("claude", "source"))

    def test_send_retry_same_draft_is_idempotent_and_new_draft_is_new_packet(self):
        draft = self._prepare()
        first = self._call("send", session="source", file=draft)
        second = self._call("send", session="source", file=draft)
        self._assert_protocol(first)
        self.assertEqual(first, second)
        new_draft = self._prepare()
        third = self._call("send", session="source", file=new_draft)
        self._assert_protocol(third)
        self.assertNotEqual(first["packet_id"], third["packet_id"])
        self.assertEqual(self._call("send", session="source", file=draft), first)
        self.assertEqual(len(self._call("list")["items"]), 2)
        self.assertEqual(self._call("receive", session="source")["status"], "empty")
        draft.write_text(json.dumps(_body("Changed after saving")))
        changed = self._call("send", session="source", file=draft)
        self._assert_protocol(changed, False)
        self.assertEqual(changed["code"], "draft_changed")
        self.assertEqual(len(self._call("list")["items"]), 2)

    def test_concurrent_send_retries_commit_one_packet_and_receipt(self):
        draft = self._prepare()
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes = [context.Process(target=_send_worker, args=(str(self.project), str(draft), results))
                     for _ in range(3)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        outputs = [results.get(timeout=2) for _ in processes]
        for output in outputs:
            self._assert_protocol(output)
        self.assertEqual(len({output["packet_id"] for output in outputs}), 1)
        self.assertEqual(len(self._call("list")["items"]), 1)
        results.close()

    def test_check_fails_on_bad_database_before_requesting_draft_content(self):
        (self.project / ".agentbridge/memory.sqlite3").write_text("broken database")
        result = self._call("check")
        self._assert_protocol(result, False)
        self.assertEqual(result["code"], "backend_error")
        self.assertNotIn("draft_path", result)
        self.assertFalse((self.project / ".agentbridge/handoff-drafts").exists())

    def test_receive_context_is_bounded_and_marks_omitted_constraints(self):
        body = _body()
        body["constraints"] = ["constraint " + str(index) + "x" * 90 for index in range(8)]
        body["blockers"] = ["blocker " + str(index) + "y" * 100 for index in range(4)]
        draft = self._prepare(body)
        self._assert_protocol(self._call("send", session="source", file=draft))
        result = self._call("receive", agent="codex", session="receiver")
        self._assert_protocol(result)
        self.assertLessEqual(len(result["context"]), 1000)
        cards = [json.loads(line) for line in result["context"].splitlines() if line.startswith("{")]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["read_full_constraints"])
        saved = json.loads(Path(result["details_path"]).read_text())
        self.assertEqual(saved["body"]["constraints"], body["constraints"])
        self.assertEqual(saved["body"]["blockers"], body["blockers"])

    def test_session_ids_from_client_environment_or_explicit_check(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-env", "CLAUDE_SESSION_ID": "claude-env"}):
            self.assertEqual(self._call("check", agent="codex")["session_id"], "codex-env")
            self.assertEqual(self._call("check", agent="claude")["session_id"], "claude-env")
            self.assertEqual(self._call("check", session="explicit")["session_id"], "explicit")
            self.assertTrue(self._call("check", agent="workbuddy")["session_id"].startswith("skill-session-"))
        for action in ("send", "receive"):
            result = self._call(action)
            self._assert_protocol(result, False)
            self.assertEqual(result["code"], "session_required")
        for session in ("", "a\nb", "a\tb", "\x00", "x" * 257, [], 9):
            result = self._call("check", session=session)
            self._assert_protocol(result, False)
            self.assertEqual(result["code"], "invalid_session")
        self._assert_protocol(self._call("list"))

    def test_multiple_packets_require_choice_without_returning_full_body(self):
        draft = self._prepare()
        first = self._call("send", session="first", file=draft)
        second = self._call("send", session="second", file=draft)
        result = self._call("receive", agent="workbuddy", session="receiver")
        self._assert_protocol(result)
        self.assertEqual(result["status"], "choose")
        self.assertEqual(result["total_count"], 2)
        self.assertNotIn("packet_id", result)
        self.assertEqual({item["id"] for item in result["items"]}, {first["packet_id"], second["packet_id"]})
        self.assertLessEqual(len(result["context"]), 1000)
        for item in result["items"]:
            self.assertEqual(set(item), {"id", "source", "created_at", "goal"})
        selected = self._call("receive", agent="workbuddy", session="receiver", packet_id=first["packet_id"])
        self.assertEqual(selected["packet_id"], first["packet_id"])

    def test_cross_project_draft_and_packet_are_isolated(self):
        other = self.root / "other"
        self._register(other)
        draft = self._prepare()
        packet = self._call("send", session="source", file=draft)
        received = self._call("receive", cwd=other, agent="codex", session="receiver", packet_id=packet["packet_id"])
        self._assert_protocol(received)
        self.assertEqual(received["status"], "empty")
        self.assertNotIn(_body()["goal"], json.dumps(received))
        sent = self._call("send", cwd=other, session="source", file=draft)
        self._assert_protocol(sent, False)
        self.assertEqual(sent["code"], "invalid_draft_path")

    def test_invalid_request_and_backend_exception_are_structured(self):
        for arguments, code in (({"action": "unknown"}, "invalid_action"),
                                ({"action": "check", "agent": "unknown"}, "invalid_agent"),
                                ({"action": "check", "cwd": "relative"}, "invalid_cwd"),
                                ({"action": "check", "cwd": self.root / "missing"}, "invalid_cwd"),
                                ({"action": "receive", "session": "s", "packet_id": "bad"}, "invalid_packet_id")):
            result = self._call(**arguments)
            self._assert_protocol(result, False)
            self.assertEqual(result["code"], code)
        with patch("agentbridge.handoff_service.Store", side_effect=RuntimeError("secret backend diagnostic")):
            result = self._call("list")
        self._assert_protocol(result, False)
        self.assertEqual(result["code"], "backend_error")
        self.assertNotIn("secret backend diagnostic", json.dumps(result))
        self._assert_protocol(dispatch("help", None, None))


if __name__ == "__main__":
    unittest.main()
