"""Explicit, bounded handoffs with one recipient and no model calls.

Packets contain agent-authored, unverified context. They never grant authority
to execute their contents. Ordinary activity reports use a separate table.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Optional
import uuid

from .store import Store, _agent, _identifier, _integer, redact_summary


BODY_MAX_CHARS = 8000
LIST_FIELDS = (
    "constraints", "completed", "in_progress", "decisions", "findings", "verification",
    "next_steps", "blockers",
)
BODY_FIELDS = frozenset(("goal", "files") + LIST_FIELDS)
_PACKET_ID = re.compile(r"^[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Handoffs:
    """A handoff queue scoped to the canonical root of an existing Store."""

    def __init__(self, store: Store):
        self.store = store
        self.connection = store._connection
        self.namespace = store.namespace
        self.project = store.project
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS handoff_intents (
                namespace TEXT NOT NULL,
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                id TEXT NOT NULL,
                target TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('preparing','ready','failed')),
                error_code TEXT,
                PRIMARY KEY(namespace, source, session_id)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS handoff_packets (
                namespace TEXT NOT NULL,
                id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_session TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL,
                created_at TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                received_session TEXT,
                received_agent TEXT,
                received_at TEXT,
                superseded_by TEXT,
                PRIMARY KEY(namespace, id)
            )"""
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(handoff_packets)")}
        for name, definition in (("source_session", "TEXT NOT NULL DEFAULT ''"), ("received_agent", "TEXT"),
                                 ("superseded_by", "TEXT")):
            if name not in columns:
                try:
                    self.connection.execute(f"ALTER TABLE handoff_packets ADD COLUMN {name} {definition}")
                except Exception as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS handoff_queue
            ON handoff_packets(namespace, target, received_session, created_at)"""
        )

    @contextmanager
    def _transaction(self):
        nested = self.connection.in_transaction
        savepoint = "handoff_" + uuid.uuid4().hex
        self.connection.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("RELEASE SAVEPOINT " + savepoint if nested else "COMMIT")
        except BaseException:
            if nested:
                self.connection.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                self.connection.execute("RELEASE SAVEPOINT " + savepoint)
            else:
                self.connection.execute("ROLLBACK")
            raise

    def intent(self, source: str, session_id: str) -> Optional[dict]:
        source = _agent(source)
        session_id = _identifier(session_id, "session_id")
        row = self.connection.execute(
            """SELECT id, source, session_id, target, state, error_code
            FROM handoff_intents WHERE namespace = ? AND source = ? AND session_id = ?""",
            (self.namespace, source, session_id),
        ).fetchone()
        return dict(row) if row else None

    def begin(self, source: str, session_id: str, target: Optional[str] = None) -> dict:
        source = _agent(source)
        target = "any" if target is None else _agent(target)
        session_id = _identifier(session_id, "session_id")
        if source == target:
            raise ValueError("handoff source and target must be different")
        with self._transaction():
            current = self.intent(source, session_id)
            if current and current["target"] == target and current["state"] != "ready":
                packet_id = current["id"]
            else:
                packet_id = uuid.uuid4().hex
            self.connection.execute(
                """INSERT INTO handoff_intents(namespace, source, session_id, id, target, state)
                VALUES (?, ?, ?, ?, ?, 'preparing')
                ON CONFLICT(namespace, source, session_id) DO UPDATE SET
                id=excluded.id, target=excluded.target, state='preparing', error_code=NULL""",
                (self.namespace, source, session_id, packet_id, target),
            )
            return self.intent(source, session_id)

    def cancel(self, source: str, session_id: str) -> None:
        source = _agent(source)
        session_id = _identifier(session_id, "session_id")
        self.connection.execute(
            """DELETE FROM handoff_intents WHERE namespace = ? AND source = ?
            AND session_id = ? AND state != 'ready'""",
            (self.namespace, source, session_id),
        )

    def mark_failed(self, source: str, session_id: str, errorcode: str) -> None:
        source = _agent(source)
        session_id = _identifier(session_id, "session_id")
        # Only machine error codes belong in this field, never arbitrary output.
        if not isinstance(errorcode, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", errorcode):
            raise ValueError("errorcode must be a short lowercase machine code")
        self.connection.execute(
            """UPDATE handoff_intents SET state='failed', error_code=?
            WHERE namespace = ? AND source = ? AND session_id = ? AND state != 'ready'""",
            (errorcode, self.namespace, source, session_id),
        )

    def _body(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != BODY_FIELDS:
            raise ValueError("handoff body must contain exactly: " + ", ".join(sorted(BODY_FIELDS)))
        if not isinstance(body["goal"], str) or not body["goal"].strip() or len(body["goal"]) > 300:
            raise ValueError("goal must be a nonempty string of at most 300 characters")
        for key in LIST_FIELDS + ("files",):
            values = body[key]
            count, length = (20, 160) if key == "files" else (12, 500)
            if not isinstance(values, list) or len(values) > count:
                raise ValueError(f"{key} must be a list of at most {count} strings")
            if any(not isinstance(item, str) or len(item) > length for item in values):
                raise ValueError(f"each {key} entry must be a string of at most {length} characters")
        if len(_json(body)) > BODY_MAX_CHARS:
            raise ValueError(f"handoff body exceeds {BODY_MAX_CHARS} characters; shorten it explicitly")
        files = self.store._files(body["files"])
        for relative in files:
            resolved = (self.project / relative).resolve()
            cursor = resolved if resolved.is_dir() else resolved.parent
            while cursor != self.project:
                marker = cursor / ".agentbridge" / "project.json"
                if marker.exists() or marker.is_symlink():
                    raise ValueError("handoff file belongs to a nested registered project")
                cursor = cursor.parent
        cleaned = {
            key: redact_summary(value) if isinstance(value, str)
            else [redact_summary(item) for item in (files if key == "files" else value)]
            for key, value in body.items()
        }
        if not cleaned["goal"]:
            raise ValueError("goal must contain visible characters")
        # Redaction can expand short values (e.g. token=x -> token=[REDACTED]).
        for key, values in cleaned.items():
            length = 300 if key == "goal" else 160 if key == "files" else 500
            if any(len(item) > length for item in ([values] if key == "goal" else values)):
                raise ValueError(f"{key} exceeds its size limit after redaction")
        if len(_json(cleaned)) > BODY_MAX_CHARS:
            raise ValueError(f"handoff body exceeds {BODY_MAX_CHARS} characters after redaction")
        return cleaned

    def _export(self, packet: dict) -> None:
        """Atomic private export using directory descriptors to reject symlinks."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptors = []
        temporary = None
        export_fd = None
        try:
            root_fd = os.open(str(self.project), flags)
            descriptors.append(root_fd)
            bridge_fd = os.open(".agentbridge", flags, dir_fd=root_fd)
            descriptors.append(bridge_fd)
            try:
                os.mkdir("handoffs", 0o700, dir_fd=bridge_fd)
            except FileExistsError:
                pass
            export_fd = os.open("handoffs", flags, dir_fd=bridge_fd)
            descriptors.append(export_fd)
            os.fchmod(export_fd, 0o700)
            filename = packet["id"] + ".json"
            try:
                existing = os.stat(filename, dir_fd=export_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ValueError("handoff export must not be a symbolic link or nonregular file")
            temporary = "." + uuid.uuid4().hex + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600, dir_fd=export_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(_json(packet) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, filename, src_dir_fd=export_fd, dst_dir_fd=export_fd)
            temporary = None
        except OSError as exc:
            raise ValueError("handoff export directory/file must be writable and must not use symlinks") from exc
        finally:
            if temporary is not None and export_fd is not None:
                try:
                    os.unlink(temporary, dir_fd=export_fd)
                except OSError:
                    pass
            for fd in reversed(descriptors):
                os.close(fd)

    def complete(self, source: str, session_id: str, body: dict) -> dict:
        source = _agent(source)
        session_id = _identifier(session_id, "session_id")
        with self._transaction():
            current = self.intent(source, session_id)
            if current is None:
                raise ValueError("no active handoff intent for this source session")
            if current["state"] == "ready":
                row = self.connection.execute(
                    "SELECT packet_json FROM handoff_packets WHERE namespace = ? AND id = ?",
                    (self.namespace, current["id"]),
                ).fetchone()
                if row is None:
                    raise ValueError("ready handoff packet is missing")
                return json.loads(row["packet_json"])
            packet = {
                "id": current["id"], "source": source, "target": current["target"],
                "created_at": _now(), "project": str(self.project), "body": self._body(body),
            }
            # Do not mark a packet ready unless its complete export succeeded.
            self._export(packet)
            self.connection.execute(
                """INSERT INTO handoff_packets(namespace, id, source, source_session, target, created_at, packet_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.namespace, packet["id"], source, session_id, packet["target"], packet["created_at"], _json(packet)),
            )
            self.connection.execute(
                """UPDATE handoff_intents SET state='ready', error_code=NULL
                WHERE namespace = ? AND source = ? AND session_id = ? AND id = ?""",
                (self.namespace, source, session_id, packet["id"]),
            )
            return packet

    def supersede_previous(self, source: str, session_id: str, keep_id: str) -> list:
        """Void this conversation's earlier handoffs that nobody has claimed.

        Sending again after more work should replace the stale packet rather
        than leave the receiver choosing between two versions of one task. A
        handoff someone already claimed is never voided: that session may
        already be acting on it.
        """
        source = _agent(source)
        session_id = _identifier(session_id, "session_id")
        rows = self.connection.execute(
            """SELECT id FROM handoff_packets WHERE namespace = ? AND source = ?
            AND source_session = ? AND id != ? AND received_session IS NULL
            AND superseded_by IS NULL""",
            (self.namespace, source, session_id, keep_id),
        ).fetchall()
        superseded = [row["id"] for row in rows]
        if superseded:
            self.connection.execute(
                "UPDATE handoff_packets SET superseded_by = ? WHERE namespace = ? AND id IN (%s)"
                % ",".join("?" * len(superseded)),
                (keep_id, self.namespace, *superseded),
            )
        return superseded

    @staticmethod
    def _choice(row) -> dict:
        packet = json.loads(row["packet_json"])
        return {"id": packet["id"], "source": packet["source"],
                "created_at": packet["created_at"], "goal": packet["body"]["goal"][:100]}

    def receive(self, target: str, session_id: str, packet_id: Optional[str] = None) -> dict:
        target = _agent(target)
        session_id = _identifier(session_id, "session_id")
        if packet_id is not None and (not isinstance(packet_id, str) or not _PACKET_ID.fullmatch(packet_id)):
            raise ValueError("packet_id must be a complete 32-character lowercase hexadecimal ID")
        with self._transaction():
            if packet_id is not None:
                row = self.connection.execute(
                    """SELECT * FROM handoff_packets WHERE namespace = ? AND target IN (?, 'any') AND id = ?
                    AND superseded_by IS NULL AND NOT (source = ? AND source_session = ?)""",
                    (self.namespace, target, packet_id, target, session_id),
                ).fetchone()
                if row is None or (row["received_session"] is not None and
                                   (row["received_session"] != session_id or row["received_agent"] != target)):
                    return {"status": "empty"}
                if row["received_session"] == session_id:
                    return {"status": "already_received", "packet": json.loads(row["packet_json"])}
            else:
                rows = self.connection.execute(
                    """SELECT * FROM handoff_packets WHERE namespace = ? AND target IN (?, 'any')
                    AND received_session IS NULL AND superseded_by IS NULL
                    AND NOT (source = ? AND source_session = ?)
                    ORDER BY created_at, id""",
                    (self.namespace, target, target, session_id),
                ).fetchall()
                if not rows:
                    # Distinguish "nothing here" from "the only one is yours",
                    # which would otherwise read as if the send had failed.
                    own = self.connection.execute(
                        """SELECT 1 FROM handoff_packets WHERE namespace = ? AND target IN (?, 'any')
                        AND received_session IS NULL AND superseded_by IS NULL
                        AND source = ? AND source_session = ? LIMIT 1""",
                        (self.namespace, target, target, session_id),
                    ).fetchone()
                    return {"status": "empty", "only_own": own is not None}
                if len(rows) > 1:
                    return {"status": "choose", "items": [self._choice(item) for item in rows]}
                row = rows[0]
            self.connection.execute(
                """UPDATE handoff_packets SET received_session = ?, received_agent = ?, received_at = ?
                WHERE namespace = ? AND target IN (?, 'any') AND id = ? AND received_session IS NULL""",
                (session_id, target, _now(), self.namespace, target, row["id"]),
            )
            return {"status": "received", "packet": json.loads(row["packet_json"])}

    def list_packets(self, target: Optional[str] = None, limit: int = 10) -> list:
        limit = _integer(limit, "limit", 1, 200)
        parameters = [self.namespace]
        query = "SELECT * FROM handoff_packets WHERE namespace = ?"
        if target is not None:
            query += " AND target IN (?, 'any')"
            parameters.append(_agent(target))
        parameters.append(limit)
        rows = self.connection.execute(query + " ORDER BY created_at DESC, id DESC LIMIT ?", parameters).fetchall()
        return [dict(self._choice(row), target=row["target"],
                     status="received" if row["received_session"] is not None
                     else "superseded" if row["superseded_by"] else "ready") for row in rows]
