import copy
import json
import multiprocessing
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from agentbridge.handoffs import BODY_MAX_CHARS, Handoffs
from agentbridge.store import Store


def _body(goal="Continue the project"):
    return {
        "goal": goal,
        "constraints": ["Keep the current API"],
        "completed": ["Implemented the parser"],
        "decisions": ["Use a project-local database"],
        "findings": ["Legacy configurations lack a version field"],
        "files": ["src/parser.py", "tests/test_parser.py"],
        "verification": ["Unit tests ran; integration tests have not run"],
        "next_steps": ["Run the integration test against a real client"],
        "blockers": [],
    }


def _receive_worker(project, number, results):
    with Store(project) as store:
        results.put(Handoffs(store).receive("codex", "receiver-" + str(number)))


def _shared_receive_worker(project, agent, results):
    with Store(project) as store:
        results.put(Handoffs(store).receive(agent, "same-session-id"))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.project)
        self.handoffs = Handoffs(self.store)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _complete(self, source="claude", session="source", target="codex", body=None):
        self.handoffs.begin(source, session, target)
        return self.handoffs.complete(source, session, _body() if body is None else body)

    def test_complete_exports_all_fields_without_creating_ordinary_logs(self):
        original = _body()
        packet = self._complete(body=original)
        self.assertEqual(packet["body"], original)
        self.assertEqual(packet["project"], str(self.project.resolve()))
        self.assertEqual(packet["source"], "claude")
        self.assertEqual(packet["target"], "codex")
        self.assertEqual(len(packet["id"]), 32)
        export = self.project / ".agentbridge" / "handoffs" / (packet["id"] + ".json")
        self.assertEqual(json.loads(export.read_text()), packet)
        self.assertEqual(stat.S_IMODE(export.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(export.parent.stat().st_mode), 0o700)

    def test_begin_pending_is_idempotent_and_completion_never_overwrites(self):
        first = self.handoffs.begin("claude", "source", "codex")
        second = self.handoffs.begin("claude", "source", "codex")
        self.assertEqual(first, second)
        packet = self.handoffs.complete("claude", "source", _body("Original goal"))
        self.assertEqual(self.handoffs.intent("claude", "source")["state"], "ready")
        self.assertEqual(self.handoffs.complete("claude", "source", _body("New goal")), packet)
        self.assertEqual(self.handoffs.complete("claude", "source", None), packet)
        self.assertEqual(len(self.handoffs.list_packets()), 1)
        new = self.handoffs.begin("claude", "source", "codex")
        self.assertNotEqual(new["id"], packet["id"])

    def test_changed_target_replaces_pending_intent(self):
        first = self.handoffs.begin("claude", "source", "codex")
        second = self.handoffs.begin("claude", "source", "workbuddy")
        self.assertNotEqual(first["id"], second["id"])
        packet = self.handoffs.complete("claude", "source", _body())
        self.assertEqual(packet["target"], "workbuddy")
        self.assertEqual(self.handoffs.receive("codex", "session"), {"status": "empty"})

    def test_cancel_and_failed_intents(self):
        self.handoffs.cancel("claude", "missing")
        self.handoffs.begin("claude", "source", "codex")
        self.handoffs.mark_failed("claude", "source", "invalid_body")
        failed = self.handoffs.intent("claude", "source")
        self.assertEqual(failed["state"], "failed")
        resumed = self.handoffs.begin("claude", "source", "codex")
        self.assertEqual(resumed["id"], failed["id"])
        self.assertEqual(resumed["state"], "preparing")
        self.handoffs.cancel("claude", "source")
        self.assertIsNone(self.handoffs.intent("claude", "source"))
        with self.assertRaises(ValueError):
            self.handoffs.complete("claude", "source", _body())
        self._complete()
        self.handoffs.cancel("claude", "source")
        self.handoffs.mark_failed("claude", "source", "invalid_body")
        self.assertEqual(self.handoffs.intent("claude", "source")["state"], "ready")

    def test_single_receive_once_and_explicit_same_session_reread(self):
        packet = self._complete()
        self.assertEqual(self.handoffs.receive("codex", "receiver"), {"status": "received", "packet": packet})
        self.assertEqual(self.handoffs.receive("codex", "receiver"), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("codex", "receiver-2"), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("codex", "receiver-2", packet["id"]), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("codex", "receiver", packet["id"]),
                         {"status": "already_received", "packet": packet})

    def test_wrong_target_and_unknown_id_do_not_reveal_content(self):
        packet = self._complete(body=_body("private project goal"))
        self.assertEqual(self.handoffs.receive("workbuddy", "receiver", packet["id"]), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("claude", "source", packet["id"]), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("codex", "receiver", "0" * 32), {"status": "empty"})
        self.assertEqual(self.handoffs.list_packets(target="workbuddy"), [])
        self.assertEqual(self.handoffs.receive("codex", "receiver")["status"], "received")

    def test_multiple_pending_requires_selection_and_returns_only_small_metadata(self):
        first = self._complete(body=_body("a" * 300))
        second = self._complete(source="workbuddy", session="second")
        choice = self.handoffs.receive("codex", "receiver")
        self.assertEqual(choice["status"], "choose")
        self.assertEqual({item["id"] for item in choice["items"]}, {first["id"], second["id"]})
        for item in choice["items"]:
            self.assertEqual(set(item), {"id", "source", "created_at", "goal"})
            self.assertLessEqual(len(item["goal"]), 100)
        self.assertEqual(self.handoffs.receive("codex", "receiver"), choice)
        self.assertEqual(self.handoffs.receive("codex", "receiver", first["id"])["packet"], first)
        self.assertEqual(self.handoffs.receive("codex", "another-receiver")["packet"], second)

    def test_metadata_lists_status_without_body(self):
        first = self._complete()
        self._complete(target="workbuddy", session="second")
        items = self.handoffs.list_packets("codex")
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]), {"id", "source", "target", "created_at", "goal", "status"})
        self.assertEqual(items[0]["status"], "ready")
        self.handoffs.receive("codex", "one", first["id"])
        self.assertEqual(self.handoffs.list_packets("codex")[0]["status"], "received")
        self.assertEqual(len(self.handoffs.list_packets(limit=1)), 1)

    def test_project_namespaces_survive_copied_database(self):
        packet = self._complete(body=_body("Project A data"))
        sibling = self.root / "other" / "project"
        sibling.mkdir(parents=True)
        self.store.close()
        (sibling / ".agentbridge").mkdir()
        shutil.copy2(self.project / ".agentbridge/memory.sqlite3", sibling / ".agentbridge/memory.sqlite3")
        self.store = Store(self.project)
        self.handoffs = Handoffs(self.store)
        with Store(sibling) as store:
            other = Handoffs(store)
            self.assertIsNone(other.intent("claude", "source"))
            self.assertEqual(other.list_packets(), [])
            self.assertEqual(other.receive("codex", "receiver", packet["id"]), {"status": "empty"})
            other.begin("claude", "source", "codex")
            other.complete("claude", "source", _body("Project B data"))
            self.assertEqual(len(other.list_packets()), 1)
        self.assertEqual(len(self.handoffs.list_packets()), 1)
        self.assertEqual(self.handoffs.receive("codex", "receiver")["packet"], packet)

    def test_concurrent_receivers_claim_only_once(self):
        self._complete()
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes = [context.Process(target=_receive_worker, args=(str(self.project), index, results))
                     for index in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        statuses = [results.get(timeout=2)["status"] for _ in processes]
        self.assertEqual(statuses.count("received"), 1)
        self.assertEqual(statuses.count("empty"), 3)
        results.close()

    def test_shared_packet_allows_any_other_session_but_not_source_session(self):
        intent = self.handoffs.begin("claude", "source")
        self.assertEqual(intent["target"], "any")
        packet = self.handoffs.complete("claude", "source", _body())
        self.assertEqual(self.handoffs.receive("claude", "source"), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("claude", "source", packet["id"]), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("claude", "new-session")["packet"], packet)
        self.assertEqual(self.handoffs.receive("codex", "new-session", packet["id"]), {"status": "empty"})
        self.assertEqual(self.handoffs.receive("claude", "new-session", packet["id"])["status"], "already_received")

    def test_shared_metadata_and_named_packets_can_require_choice(self):
        shared = self._complete(target=None)
        named = self._complete(source="workbuddy", target="codex")
        self.assertEqual({row["id"] for row in self.handoffs.list_packets("codex")}, {shared["id"], named["id"]})
        self.assertEqual([row["id"] for row in self.handoffs.list_packets("workbuddy")], [shared["id"]])
        self.assertEqual(self.handoffs.receive("codex", "receiver")["status"], "choose")
        self.assertEqual(self.handoffs.receive("workbuddy", "receiver")["packet"], shared)
        self.assertEqual(self.handoffs.receive("codex", "receiver")["packet"], named)

    def test_shared_concurrent_different_agents_receive_exactly_once(self):
        packet = self._complete(target=None)
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes = [context.Process(target=_shared_receive_worker, args=(str(self.project), agent, results))
                     for agent in ("codex", "claude", "workbuddy")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        statuses = [results.get(timeout=2)["status"] for _ in processes]
        self.assertEqual(statuses.count("received"), 1)
        rereads = [self.handoffs.receive(agent, "same-session-id", packet["id"])["status"]
                   for agent in ("codex", "claude", "workbuddy")]
        self.assertEqual(rereads.count("already_received"), 1)
        self.assertEqual(rereads.count("empty"), 2)
        results.close()

    def test_large_body_rejected_without_truncation_or_ready_state(self):
        self.handoffs.begin("claude", "source", "codex")
        body = _body()
        body["findings"] = ["a" * 500] * 8
        body["decisions"] = ["b" * 500] * 8
        before = copy.deepcopy(body)
        with self.assertRaisesRegex(ValueError, str(BODY_MAX_CHARS)):
            self.handoffs.complete("claude", "source", body)
        self.assertEqual(body, before)
        self.assertEqual(self.handoffs.intent("claude", "source")["state"], "preparing")
        self.assertEqual(self.handoffs.list_packets(), [])
        self.assertFalse((self.project / ".agentbridge/handoffs").exists())

    def test_body_exact_fields_and_types(self):
        self.handoffs.begin("claude", "source", "codex")
        invalid = [None, [], {}, dict(_body(), extra="field")]
        for key in _body():
            missing = _body()
            del missing[key]
            invalid.append(missing)
        for goal in ("", "  ", "\x00", "a" * 301, 3, True, []):
            invalid.append(dict(_body(), goal=goal))
        for key in _body():
            if key == "goal":
                continue
            for value in ("plain string", ("tuple",), {}, None, [3], [False], [[]]):
                invalid.append(dict(_body(), **{key: value}))
            invalid.append(dict(_body(), **{key: ["a"] * (21 if key == "files" else 9)}))
            invalid.append(dict(_body(), **{key: ["a" * (161 if key == "files" else 501)]}))
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.handoffs.complete("claude", "source", body)
        empty_arrays = {key: [] for key in _body() if key != "goal"}
        self.assertEqual(self.handoffs.complete("claude", "source", dict(empty_arrays, goal="Minimal"))["body"],
                         dict(empty_arrays, goal="Minimal"))

    def test_redaction_recursively_cleans_text_without_mutating_input(self):
        body = _body("goal sk-proj-" + "a" * 32)
        for key in body:
            if key not in ("goal", "files"):
                body[key] = ["password=secret-value", "Keep useful context\x00"]
        body["files"] = [".env", "src/normal.py", "credentials.json"]
        original = copy.deepcopy(body)
        packet = self._complete(body=body)
        self.assertEqual(body, original)
        self.assertNotIn("secret-value", json.dumps(packet))
        self.assertNotIn("a" * 32, json.dumps(packet))
        self.assertEqual(packet["body"]["files"], ["src/normal.py"])
        self.assertEqual(packet["body"]["next_steps"][1], "Keep useful context")

    def test_paths_must_stay_in_project_and_outside_nested_registration(self):
        nested = self.project / "nested"
        (nested / ".agentbridge").mkdir(parents=True)
        (nested / ".agentbridge/project.json").write_text("not valid json; still a boundary")
        (self.project / "escape").symlink_to(self.root)
        (self.project / "nested-alias").symlink_to(nested)
        invalid = ["../outside.py", str(self.root / "outside.py"), "src/../file.py", "escape/outside.py",
                   ".", "bad\npath", "bad\\path", "nested", "nested/file.py", "nested-alias/file.py"]
        self.handoffs.begin("claude", "source", "codex")
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.handoffs.complete("claude", "source", dict(_body(), files=[path]))
        packet = self.handoffs.complete("claude", "source", dict(_body(), files=[str(self.project / "src/file.py")]))
        self.assertEqual(packet["body"]["files"], ["src/file.py"])

    def test_export_rejects_directory_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".agentbridge/handoffs").symlink_to(outside)
        self.handoffs.begin("claude", "source", "codex")
        with self.assertRaises(ValueError):
            self.handoffs.complete("claude", "source", _body())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.handoffs.intent("claude", "source")["state"], "preparing")
        self.assertEqual(self.handoffs.list_packets(), [])

    def test_export_rejects_packet_symlink(self):
        intent = self.handoffs.begin("claude", "source", "codex")
        directory = self.project / ".agentbridge/handoffs"
        directory.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("do not replace")
        (directory / (intent["id"] + ".json")).symlink_to(outside)
        with self.assertRaises(ValueError):
            self.handoffs.complete("claude", "source", _body())
        self.assertEqual(outside.read_text(), "do not replace")
        self.assertEqual(self.handoffs.list_packets(), [])

    def test_export_failure_does_not_publish_or_mark_ready(self):
        self.handoffs.begin("claude", "source", "codex")
        with patch("agentbridge.handoffs.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(ValueError):
                self.handoffs.complete("claude", "source", _body())
        self.assertEqual(self.handoffs.intent("claude", "source")["state"], "preparing")
        self.assertEqual(self.handoffs.list_packets(), [])
        self.assertEqual(list((self.project / ".agentbridge/handoffs").iterdir()), [])

    def test_invalid_agents_identifiers_and_limits(self):
        for agent in ("unknown", [], None, 3):
            for call in (
                lambda: self.handoffs.begin(agent, "one", "codex"),
                lambda: self.handoffs.intent(agent, "one"),
                lambda: self.handoffs.complete(agent, "one", _body()),
                lambda: self.handoffs.receive(agent, "one"),
                lambda: self.handoffs.cancel(agent, "one"),
            ):
                with self.subTest(agent=agent), self.assertRaises(ValueError):
                    call()
        for agent in ("unknown", [], 3):
            with self.assertRaises(ValueError):
                self.handoffs.begin("codex", "one", agent)
        with self.assertRaises(ValueError):
            self.handoffs.begin("codex", "one", "codex")
        for session in ("", "\x00", "x" * 257, [], None):
            with self.subTest(session=session), self.assertRaises(ValueError):
                self.handoffs.begin("claude", session, "codex")
        for packet_id in ("", "a" * 31, "a" * 33, "../bad", [], 3, "A" * 32):
            with self.subTest(packet_id=packet_id), self.assertRaises(ValueError):
                self.handoffs.receive("codex", "one", packet_id)
        for limit in (0, -1, True, 201):
            with self.assertRaises(ValueError):
                self.handoffs.list_packets(limit=limit)
        with self.assertRaises(ValueError):
            self.handoffs.mark_failed("claude", "one", "arbitrary error contains secrets")


if __name__ == "__main__":
    unittest.main()
