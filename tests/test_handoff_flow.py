from datetime import datetime, timedelta, timezone
import json
import unittest

from agentbridge.handoff_flow import _age, receive_context


def _packet(body_extra=None, drop=(), created_at=None):
    body = {
        "goal": "Finish the serializer migration",
        "constraints": ["Keep the public API"],
        "completed": ["Parser rewritten"],
        "in_progress": ["Serializer half migrated; the tree does not build"],
        "decisions": [], "findings": [], "files": [],
        "verification": ["not verified"],
        "next_steps": ["Finish serializer.py"], "blockers": [],
    }
    body.update(body_extra or {})
    for key in drop:
        body.pop(key)
    return {"id": "a" * 32, "source": "claude", "target": "any", "project": "/tmp/ws",
            "created_at": created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "body": body}


class AgeTests(unittest.TestCase):
    def test_relative_ages(self):
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        def at(**delta):
            return _age((now - timedelta(**delta)).isoformat().replace("+00:00", "Z"), now=now)
        self.assertEqual(at(seconds=5), "刚刚")
        self.assertEqual(at(minutes=7), "7分钟前")
        self.assertEqual(at(hours=3), "3小时前")
        self.assertEqual(at(days=2), "2天前")

    def test_unusable_timestamps_make_no_claim(self):
        for value in (None, "", "not a date", 12345, "2026-13-45T99:99:99Z"):
            with self.subTest(value=value):
                self.assertIsNone(_age(value))
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertIsNone(_age(future.isoformat().replace("+00:00", "Z")))


class ReceiveCardTests(unittest.TestCase):
    def card(self, packet):
        text = receive_context({"status": "received", "packet": packet}, max_chars=1000)
        self.assertLessEqual(len(text), 1000)
        return json.loads(text[text.index("{"):text.rindex("}") + 1])

    def test_card_surfaces_in_progress_and_age(self):
        card = self.card(_packet())
        self.assertIn("Serializer half migrated", card["in_progress"][0])
        self.assertEqual(card["written"], "刚刚")
        self.assertFalse(card["read_full_constraints"])

    def test_packet_without_in_progress_still_renders(self):
        # Packets saved before the field existed must keep working.
        card = self.card(_packet(drop=("in_progress",)))
        self.assertEqual(card["in_progress"], [])
        self.assertFalse(card["read_full_constraints"])

    def test_omitted_in_progress_sets_read_full_constraints(self):
        packet = _packet({"in_progress": ["one", "two", "three"]})
        self.assertTrue(self.card(packet)["read_full_constraints"])

    def test_unreadable_timestamp_omits_the_age_instead_of_guessing(self):
        self.assertNotIn("written", self.card(_packet(created_at="unknown")))


if __name__ == "__main__":
    unittest.main()
