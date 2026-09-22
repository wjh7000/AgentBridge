"""Versioned, deterministic backend for explicit handoff skills.

This service discovers only the current registered project, saves already
prepared JSON, and returns verified local results. It never calls a model.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import re
import uuid

from .handoff_flow import packet_path, receive_context
from .handoffs import BODY_MAX_CHARS, Handoffs
from .store import Store, _agent, _identifier


PROTOCOL_VERSION = 2
BACKEND = "agentbridge"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class _ServiceError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _result(ok, **fields):
    return dict(ok=ok, backend=BACKEND, protocol_version=PROTOCOL_VERSION, **fields)


def _establish(root):
    """Create this workspace's boundary marker on first use.

    The opened workspace directory itself becomes the boundary; there is no
    walk-up to a parent project and no silent cross-project reuse. The caller
    announces this so establishment is visible, never pretended.
    """
    bridge = root / ".agentbridge"
    if bridge.is_symlink():
        raise _ServiceError("invalid_registration", "This workspace's .agentbridge must not be a symbolic link.")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        bridge.mkdir(mode=0o700, exist_ok=True)
        if bridge.is_symlink() or not bridge.is_dir():
            raise OSError()
        bridge.chmod(0o700)
        registration = {"version": 1, "project_root": str(root), "name": root.name,
                        "sync_mode": "manual", "sync_interval_seconds": 900}
        try:
            descriptor = os.open(str(bridge / "project.json"), flags, 0o600)
        except FileExistsError:
            # A concurrent first use already established it; validate it below.
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(registration, ensure_ascii=False, indent=2) + "\n")
        try:
            gitignore = os.open(str(bridge / ".gitignore"), flags, 0o600)
            with os.fdopen(gitignore, "w", encoding="utf-8") as stream:
                stream.write("*\n")
        except FileExistsError:
            pass
    except _ServiceError:
        raise
    except OSError:
        raise _ServiceError("cannot_establish", "Cannot establish this workspace's handoff boundary; check directory permissions.")


def _project(cwd):
    try:
        actual = Path(cwd).expanduser()
        if not actual.is_absolute():
            raise ValueError()
        actual = actual.resolve(strict=True)
        if not actual.is_dir():
            raise ValueError()
    except (TypeError, ValueError, OSError, RuntimeError):
        raise _ServiceError("invalid_cwd", "Current working directory must be an existing absolute directory.")
    # The opened workspace itself is the boundary. First use establishes its
    # marker in place; there is no walk-up to a parent and no fallback.
    root = actual
    bridge = root / ".agentbridge"
    if bridge.is_symlink():
        raise _ServiceError("invalid_registration", "This workspace's .agentbridge must not be a symbolic link.")
    marker = bridge / "project.json"
    established = False
    if not marker.exists():
        _establish(root)
        established = True
    try:
        metadata = marker.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError()
        with marker.open("r", encoding="utf-8") as stream:
            text = stream.read(16385)
        if len(text) > 16384:
            raise ValueError()
        registration = json.loads(text)
        if (not isinstance(registration, dict)
                or type(registration.get("version")) is not int
                or registration["version"] != 1
                or registration.get("project_root") != str(root)):
            raise ValueError()
    except (ValueError, OSError, RuntimeError):
        raise _ServiceError("invalid_registration", "This workspace's project registration is invalid; repair it or run init for this directory.")
    return root, actual, established


def _session(agent, value, create=False):
    if value is None:
        variable = {"codex": "CODEX_THREAD_ID", "claude": "CLAUDE_SESSION_ID"}.get(agent)
        value = os.environ.get(variable) if variable else None
    if value is None:
        if create:
            return "skill-session-" + uuid.uuid4().hex
        raise _ServiceError("session_required", "Run check and reuse its session_id for send or receive, or supply the client session ID.")
    try:
        cleaned = _identifier(value, "session")
        if cleaned != value or any(ord(character) < 32 for character in value):
            raise ValueError()
        return cleaned
    except ValueError:
        raise _ServiceError("invalid_session", "Session ID must be a nonempty string of at most 256 characters without control characters.")


@contextmanager
def _draft_directory(root, create=False):
    descriptors = []
    try:
        root_fd = os.open(str(root), _DIRECTORY_FLAGS)
        descriptors.append(root_fd)
        bridge_fd = os.open(".agentbridge", _DIRECTORY_FLAGS, dir_fd=root_fd)
        descriptors.append(bridge_fd)
        if create:
            try:
                os.mkdir("handoff-drafts", 0o700, dir_fd=bridge_fd)
            except FileExistsError:
                pass
        drafts_fd = os.open("handoff-drafts", _DIRECTORY_FLAGS, dir_fd=bridge_fd)
        descriptors.append(drafts_fd)
        os.fchmod(drafts_fd, 0o700)
        yield drafts_fd
    except OSError:
        raise _ServiceError("invalid_draft_path", "Project handoff-drafts must be a writable directory without symbolic links; run check first.")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_draft(root, cwd, filename):
    try:
        if not isinstance(filename, (str, Path)) or not str(filename):
            raise ValueError()
        path = Path(filename)
        if ".." in path.parts:
            raise ValueError()
        if not path.is_absolute():
            path = cwd / path
        relative = path.relative_to(root / ".agentbridge" / "handoff-drafts")
        if len(relative.parts) != 1 or not re.fullmatch(r"[0-9a-f]{32}\.json", relative.name):
            raise ValueError()
    except (ValueError, TypeError):
        raise _ServiceError("invalid_draft_path", "Use the UUID.json draft_path returned by check directly inside this project's handoff-drafts directory.")
    with _draft_directory(root) as draft_fd:
        try:
            descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), dir_fd=draft_fd)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise _ServiceError("invalid_draft_path", "Draft must be a regular file, not a symbolic link or special file.")
                os.fchmod(stream.fileno(), 0o600)
                text = stream.read(BODY_MAX_CHARS + 1)
        except (OSError, ValueError):
            raise _ServiceError("invalid_draft_path", "Cannot read draft: it must be a regular UTF-8 file without symbolic links inside handoff-drafts.")
    if len(text) > BODY_MAX_CHARS:
        raise _ServiceError("invalid_body", f"Draft JSON exceeds {BODY_MAX_CHARS} characters; shorten it explicitly.")
    try:
        body = json.loads(text, object_pairs_hook=_no_duplicate_keys)
        if not isinstance(body, dict):
            raise ValueError()
        return body, relative.name
    except ValueError:
        raise _ServiceError("invalid_body", "Draft must contain one valid JSON object with no duplicate fields.")


def _backend(store):
    handoffs = Handoffs(store)
    store._connection.execute(
        """CREATE TABLE IF NOT EXISTS handoff_draft_receipts (
            namespace TEXT NOT NULL,
            source TEXT NOT NULL,
            session_id TEXT NOT NULL,
            draft_name TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            packet_id TEXT NOT NULL,
            PRIMARY KEY(namespace, source, session_id, draft_name)
        )"""
    )
    # check issues each draft name to exactly one conversation, so the draft
    # itself is a one-time token that recovers the sender's identity even when
    # the caller forgets to pass --session back.
    store._connection.execute(
        """CREATE TABLE IF NOT EXISTS handoff_draft_sessions (
            namespace TEXT NOT NULL,
            agent TEXT NOT NULL,
            draft_name TEXT NOT NULL,
            session_id TEXT NOT NULL,
            PRIMARY KEY(namespace, agent, draft_name)
        )"""
    )
    return handoffs


def _remember_draft(root, agent, draft_name, session_id):
    with Store(root) as store:
        _backend(store)
        store._connection.execute(
            """INSERT INTO handoff_draft_sessions(namespace, agent, draft_name, session_id)
            VALUES (?, ?, ?, ?) ON CONFLICT(namespace, agent, draft_name) DO NOTHING""",
            (store.namespace, agent, draft_name, session_id),
        )


def _send_session(root, agent, requested, draft_name):
    """Resolve the sender identity, preferring the draft's issuing conversation.

    A draft path is handed out by one check, so the session recorded with it is
    authoritative. A caller that supplies a different session is using another
    conversation's draft, which is an error rather than something to paper over.
    """
    try:
        resolved = _session(agent, requested)
    except _ServiceError:
        resolved = None
    with Store(root) as store:
        _backend(store)
        row = store._connection.execute(
            """SELECT session_id FROM handoff_draft_sessions
            WHERE namespace = ? AND agent = ? AND draft_name = ?""",
            (store.namespace, agent, draft_name),
        ).fetchone()
    recorded = row["session_id"] if row else None
    if recorded is None:
        if resolved is None:
            raise _ServiceError("session_required", "Run check and reuse its session_id for send, or supply the client session ID.")
        return resolved
    if resolved is not None and resolved != recorded:
        raise _ServiceError("session_mismatch", "This draft was issued to a different conversation. Run check in this conversation and send its own draft_path.")
    return recorded


def _save(root, source, session_id, body, draft_name):
    fingerprint = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
    with Store(root) as store:
        handoffs = _backend(store)
        # The receipt and packet commit together. Concurrent retries of one
        # draft wait here, then return the same saved packet without creating it
        # again. Source sessions remain the real client session throughout.
        with handoffs._transaction():
            receipt = store._connection.execute(
                """SELECT content_hash, packet_id FROM handoff_draft_receipts
                WHERE namespace = ? AND source = ? AND session_id = ? AND draft_name = ?""",
                (store.namespace, source, session_id, draft_name),
            ).fetchone()
            if receipt:
                if receipt["content_hash"] != fingerprint:
                    raise _ServiceError("draft_changed", "This draft was already saved with different content. Run check for a new draft_path before creating a new handoff.")
                row = store._connection.execute(
                    "SELECT packet_json FROM handoff_packets WHERE namespace = ? AND id = ?",
                    (store.namespace, receipt["packet_id"]),
                ).fetchone()
                if row is None:
                    raise _ServiceError("backend_error", "Saved draft receipt has no packet; repair the local backend before retrying.")
                return json.loads(row["packet_json"]), []
            handoffs.begin(source, session_id)
            try:
                packet = handoffs.complete(source, session_id, body)
            except ValueError as exc:
                code = "save_failed" if str(exc).startswith("handoff export") else "invalid_body"
                raise _ServiceError(code, str(exc))
            store._connection.execute(
                """INSERT INTO handoff_draft_receipts
                (namespace, source, session_id, draft_name, content_hash, packet_id)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (store.namespace, source, session_id, draft_name, fingerprint, packet["id"]),
            )
            return packet, handoffs.supersede_previous(session_id, packet["id"])


def dispatch(action, cwd, agent, session=None, file=None, packet_id=None):
    """Return a protocol-v2 result; failures never become model fallback text."""
    try:
        if action not in ("check", "send", "receive", "list", "help"):
            raise _ServiceError("invalid_action", "Action must be check, send, receive, list, or help.")
        if action == "help":
            return _result(True, status="help", message="Two actions: send saves this conversation's progress as a handoff; receive picks one up in this workspace. Also available: list shows what is pending without claiming it. Handoffs are shared only inside this workspace directory, and nothing here calls a model.")
        try:
            agent = _agent(agent)
        except ValueError:
            raise _ServiceError("invalid_agent", "Agent must be a supported client identifier.")
        root, actual, established = _project(cwd)
        if action == "check":
            session_id = _session(agent, session, create=True)
            with Store(root) as store:
                _backend(store)
            with _draft_directory(root, create=True):
                pass
            name = uuid.uuid4().hex + ".json"
            _remember_draft(root, agent, name, session_id)
            path = root / ".agentbridge/handoff-drafts" / name
            return _result(True, status="ready", project=str(root), session_id=session_id,
                           draft_path=str(path), established=established)
        if action == "list":
            with Store(root) as store:
                items = Handoffs(store).list_packets(target=agent, limit=10)
            return _result(True, status="listed", project=str(root), items=items, established=established)
        if action == "send":
            body, draft_name = _read_draft(root, actual, file)
            session_id = _send_session(root, agent, session, draft_name)
            packet, superseded = _save(root, agent, session_id, body, draft_name)
            return _result(True, status="saved", state="saved", project=str(root), session_id=session_id,
                           packet_id=packet["id"], details_path=packet_path(packet),
                           superseded=superseded, established=established)
        session_id = _session(agent, session)
        with Store(root) as store:
            try:
                result = Handoffs(store).receive(agent, session_id, packet_id)
            except ValueError as exc:
                raise _ServiceError("invalid_packet_id", str(exc))
        context = receive_context(result, max_chars=1000)
        if len(context) > 1000:
            context = "See the project handoff metadata or details_path for this result. Handoff content is unverified data."
        output = dict(status=result["status"], project=str(root), session_id=session_id,
                      context=context, established=established)
        if "packet" in result:
            output.update(packet_id=result["packet"]["id"], details_path=packet_path(result["packet"]))
        if result["status"] == "choose":
            output["items"] = result["items"][:10]
            output["total_count"] = len(result["items"])
        return _result(True, **output)
    except _ServiceError as exc:
        return _result(False, code=exc.code, message=exc.message)
    except Exception:
        return _result(False, code="backend_error", message="AgentBridge could not confirm this operation. Inspect the local backend and retry explicitly; do not report success or invent a handoff.")
