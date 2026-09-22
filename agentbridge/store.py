"""Workspace-scoped SQLite storage backing the handoff backend.

The namespace is derived from the resolved workspace path, so two directories
never share handoffs. Secret scrubbing is a best-effort filter for common
formats, not comprehensive DLP. Handoff content is unverified external data;
it is never an instruction for an agent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from typing import Any


MAX_FILES = 40
MAX_FILE_LENGTH = 160

AGENTS = frozenset(("codex", "claude", "workbuddy"))

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_ASSIGNED_SECRET = re.compile(
    r"\b(api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|token|"
    r"password|passwd|secret|client[-_ ]?secret)\b([\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;\]}]+)",
    re.IGNORECASE,
)
_SENSITIVE_NAME = re.compile(
    r"^(?:\.env(?:\..*)?|\.netrc|\.npmrc|\.pypirc|"
    r"(?:credentials?|secrets?|passwords?)(?:[._-].*)?|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|"
    r".*\.(?:pem|key|p12|pfx|keystore))$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(Bearer\s+)[A-Za-z0-9_.~+/-]+=*", re.IGNORECASE)
_URL_PASSWORD = re.compile(r"(https?://)[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)


def _clean_text(value: str) -> str:
    value = _ANSI.sub("", value)
    return "".join(
        char for char in value
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Cs")
    )


def redact_summary(summary: str) -> str:
    """Scrub common secrets; this deliberately does not promise complete DLP."""
    summary = _clean_text(summary)
    summary = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", summary)
    for pattern in _SECRET_PATTERNS:
        summary = pattern.sub("[REDACTED]", summary)
    summary = _ASSIGNED_SECRET.sub(r"\1\2[REDACTED]", summary)
    summary = _BEARER.sub(r"\1[REDACTED]", summary)
    return _URL_PASSWORD.sub(r"\1[REDACTED]@", summary).strip()


def _agent(value: str) -> str:
    if not isinstance(value, str) or value not in AGENTS:
        raise ValueError("agent must be codex, claude, or workbuddy")
    return value


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 characters")
    value = _clean_text(value).strip()
    if not value:
        raise ValueError(f"{name} must contain visible characters")
    return value


class Store:
    """A report store for exactly one canonical project directory.

    Copying memory.sqlite3 to a different project does not expose its reports
    through this API: every query includes the hash of the canonical root.
    Moving a project intentionally creates a new scope until explicitly migrated.
    """

    def __init__(self, project: Any):
        try:
            self.project = Path(project).expanduser().resolve(strict=True)
        except (TypeError, OSError, RuntimeError) as exc:
            raise ValueError("project must be an existing directory") from exc
        if not self.project.is_dir():
            raise ValueError("project must be an existing directory")
        self.namespace = hashlib.sha256(os.fsencode(str(self.project))).hexdigest()
        directory = self.project / ".agentbridge"
        if directory.is_symlink():
            raise ValueError(".agentbridge must not be a symbolic link")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir():
            raise ValueError(".agentbridge must be a directory")
        directory.chmod(0o700)
        self.db_path = directory / "memory.sqlite3"
        if self.db_path.is_symlink():
            raise ValueError("memory.sqlite3 must not be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(self.db_path), flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self._connection = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA busy_timeout = 10000")
            # The WAL switch can race another first-time constructor before
            # SQLite's ordinary busy handler is active for that operation.
            for attempt in range(6):
                try:
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.025 * (attempt + 1))
            self._connection.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            self._connection.close()
            raise

    def _files(self, files: Any) -> list:
        if files is None:
            return []
        if not isinstance(files, (list, tuple)) or len(files) > MAX_FILES:
            raise ValueError(f"files must be a list of at most {MAX_FILES} paths")
        cleaned = []
        for item in files:
            if not isinstance(item, (str, Path)):
                raise ValueError("each file must be a string or Path")
            raw = str(item)
            if not raw or _clean_text(raw) != raw or "\n" in raw or "\t" in raw or "\\" in raw:
                raise ValueError("file paths must be nonempty and contain no control characters or backslashes")
            path = Path(raw)
            if ".." in path.parts:
                raise ValueError("file paths must not contain '..'")
            absolute = path if path.is_absolute() else self.project / path
            try:
                relative = absolute.resolve().relative_to(self.project)
            except (OSError, ValueError, RuntimeError) as exc:
                raise ValueError("file paths must remain inside the project") from exc
            normalized = relative.as_posix()
            if normalized == "." or len(normalized) > MAX_FILE_LENGTH:
                raise ValueError(f"file paths must name a file and be at most {MAX_FILE_LENGTH} characters")
            if any(_SENSITIVE_NAME.match(part) for part in relative.parts):
                continue
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
