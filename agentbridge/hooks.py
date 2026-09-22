"""Small, fail-open lifecycle adapters for project-scoped agent context.

Hook payloads are untrusted. Only the supplied working directory, session identity,
explicit file paths, final assistant message, and exact manual sync phrases are
considered. Transcripts, user prompts, commands, and tool output are never persisted.
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import Store


MAX_INPUT_BYTES = 1024 * 1024
MAX_SUMMARY_CHARS = 4000
AGENTS = frozenset(("codex", "claude", "workbuddy"))
_CONTEXT_EVENTS = frozenset(("SessionStart", "UserPromptSubmit"))
_MANUAL_SYNC_PHRASES = frozenset(("同步进展", "同步进度"))
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$")
_SELF_REPORT = "代理自述（尚未独立验证，不代表检查或测试已通过）：\n"


class _InvalidHook(ValueError):
    """A payload or project registration cannot safely be used."""


def _diagnostic(reason: str) -> None:
    # Do not print exception messages: they may contain a path or hook payload.
    try:
        print("agentbridge hook: skipped (" + reason + ")", file=sys.stderr)
    except Exception:
        pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _crosses_project(path: Path, root: Path) -> bool:
    """Even malformed nested registrations are treated as a boundary."""
    cursor = path
    while cursor != root:
        marker = cursor / ".agentbridge" / "project.json"
        if marker.exists() or marker.is_symlink():
            return True
        parent = cursor.parent
        if parent == cursor:
            return True
        cursor = parent
    return False


def _validate_scope(project: Path, event: Dict[str, Any]) -> tuple:
    root = Path(project).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise _InvalidHook()
    marker = root / ".agentbridge" / "project.json"
    with marker.open("r", encoding="utf-8") as stream:
        registration_text = stream.read(16385)
    if len(registration_text) > 16384:
        raise _InvalidHook()
    registration = json.loads(registration_text)
    if (
        not isinstance(registration, dict)
        or type(registration.get("version")) is not int
        or registration["version"] != 1
        or registration.get("project_root") != str(root)
    ):
        raise _InvalidHook()
    raw_cwd = event.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd or not Path(raw_cwd).is_absolute():
        raise _InvalidHook()
    cwd = Path(raw_cwd).resolve(strict=True)
    if not cwd.is_dir() or not _inside(cwd, root) or _crosses_project(cwd, root):
        raise _InvalidHook()
    return root, cwd, registration


def _delivery_interval(registration: Dict[str, Any], event: Dict[str, Any], manual: bool) -> Optional[int]:
    """None means no context request; zero explicitly bypasses rate limiting."""
    if manual:
        return 0
    mode = registration.get("sync_mode", "manual")
    if mode == "manual":
        return None
    if mode == "session":
        return 0 if event["hook_event_name"] == "SessionStart" else None
    if mode == "interval":
        interval = registration.get("sync_interval_seconds", 900)
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 2592000:
            raise _InvalidHook()
        return interval
    raise _InvalidHook()


def _session_id(event: Dict[str, Any]) -> Optional[str]:
    value = event.get("session_id")
    if not isinstance(value, str) or not value.strip():
        return None
    if any(ord(character) < 32 for character in value):
        return None
    if len(value) > 256:
        return "hook-" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    return value


def _event_key(event: Dict[str, Any], session_id: str, content: str) -> str:
    # A stable turn/tool id deduplicates repeated callbacks. Older clients can
    # fall back to a session + event + content hash without retaining raw input.
    event_name = event.get("hook_event_name", "")
    unique_id = event.get("tool_use_id") if event_name == "PostToolUse" else event.get("turn_id")
    if not isinstance(unique_id, str) or not unique_id:
        unique_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if event_name == "PostToolUse" and isinstance(event.get("turn_id"), str):
            unique_id = event["turn_id"] + ":" + unique_id
    key = json.dumps([session_id, event_name, unique_id], ensure_ascii=True)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _bounded_summary(text: str) -> str:
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    suffix = "\n…（已截断）"
    return text[: MAX_SUMMARY_CHARS - len(suffix)] + suffix


def _known_tool_error(response: Any) -> bool:
    if isinstance(response, str):
        stripped = response.strip()
        if stripped.startswith("{"):
            try:
                return _known_tool_error(json.loads(stripped))
            except (ValueError, TypeError):
                pass
        return bool(re.match(r"^(?:error\b|failed\b|failure\b|interrupted\b)", stripped, re.I))
    if not isinstance(response, dict):
        return False
    if response.get("isError") is True or response.get("is_error") is True or response.get("success") is False:
        return True
    if str(response.get("status", "")).lower() in ("error", "failed", "failure", "rejected", "interrupted"):
        return True
    if response.get("error"):
        return True
    for key in ("exit_code", "exitCode", "returncode"):
        if isinstance(response.get(key), int) and response[key] != 0:
            return True
    # Some clients wrap tool responses in a result envelope.
    return isinstance(response.get("result"), dict) and _known_tool_error(response["result"])


def _relative_file(raw_path: str, cwd: Path, root: Path) -> Optional[str]:
    if not raw_path or any(ord(character) < 32 for character in raw_path):
        return None
    target = Path(raw_path)
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve(strict=False)
    if target == root or not _inside(target, root) or _crosses_project(target.parent, root):
        return None
    relative = target.relative_to(root).as_posix()
    if len(relative) > 160 or ".agentbridge" in target.relative_to(root).parts:
        return None
    return relative


def _tool_files(event: Dict[str, Any], cwd: Path, root: Path) -> List[str]:
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")
    if not isinstance(tool_name, str):
        return []
    tool_name = tool_name.rsplit(".", 1)[-1]
    paths = []
    if tool_name in ("Write", "Edit") and isinstance(tool_input, dict):
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str):
            paths.append(file_path)
    elif tool_name == "apply_patch":
        command = tool_input.get("command") if isinstance(tool_input, dict) else tool_input
        if isinstance(command, str):
            # Parse only patch headers. Never execute or retain the command.
            # Requiring an actual patch wrapper avoids treating shell scripts
            # or file contents that resemble headers as file operations.
            lines = command.strip().splitlines()
            if lines and lines[0] == "*** Begin Patch" and lines[-1] == "*** End Patch":
                for line in lines[1:-1]:
                    match = _PATCH_PATH.match(line)
                    if match:
                        paths.append(match.group(1))
    result = []
    for raw_path in paths:
        relative = _relative_file(raw_path, cwd, root)
        if relative and relative not in result:
            result.append(relative)
        if len(result) >= 40:
            break
    return result


def handle_hook(project: Path, agent: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Return valid hook JSON, skipping all failures without blocking the agent."""
    store = None
    try:
        if agent not in AGENTS or not isinstance(event, dict):
            raise _InvalidHook()
        event_name = event.get("hook_event_name")
        if event_name not in _CONTEXT_EVENTS and event_name not in ("Stop", "PostToolUse", "Interrupt"):
            return {}
        root, cwd, registration = _validate_scope(project, event)
        session_id = _session_id(event)
        if not session_id:
            return {}
        prompt = event.get("prompt")
        manual = event_name == "UserPromptSubmit" and isinstance(prompt, str) and prompt.strip() in _MANUAL_SYNC_PHRASES
        if event_name == "UserPromptSubmit":
            store = Store(root)
            # A sync-only response must not become another "new" report. Keep
            # this flag across Stop retries until a normal prompt clears it.
            store.mark_sync_turn(agent, session_id, manual)
        if event_name in _CONTEXT_EVENTS:
            interval = _delivery_interval(registration, event, manual)
            if interval is None:
                return {}
            # Local incremental delivery is manual by default. Optional modes
            # request session-start or interval-limited updates. No model or
            # network calls are used, and the prompt is never persisted.
            if store is None:
                store = Store(root)
            context = store.deliver_context(agent, session_id, limit=3, max_chars=1000, min_interval_seconds=interval)
            if not context:
                if not manual:
                    return {}
                context = "本项目没有新的重要共享进展。"
            return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}
        if store is None:
            store = Store(root)
        if event_name == "Stop":
            if store.is_sync_turn(agent, session_id):
                return {}
            message = event.get("last_assistant_message")
            if not isinstance(message, str) or not message.strip():
                return {}
            summary = _bounded_summary(_SELF_REPORT + message.strip())
            store.publish(agent, session_id, summary, kind="summary", event_key=_event_key(event, session_id, message))
        elif event_name == "PostToolUse":
            if _known_tool_error(event.get("tool_response")):
                return {}
            files = _tool_files(event, cwd, root)
            if files:
                # Keep names solely in the structured files field, so the
                # store's sensitive-path filter applies to every visible name.
                summary = "工具报告文件操作（尚未独立验证）：涉及 {} 项文件。".format(len(files))
                store.publish(agent, session_id, summary, kind="activity", files=files, event_key=_event_key(event, session_id, "\n".join(files)))
        elif event_name == "Interrupt":
            summary = "代理会话已中断（来自生命周期事件，状态尚未独立验证）。"
            store.publish(agent, session_id, summary, kind="interrupted", event_key=_event_key(event, session_id, summary))
        return {}
    except _InvalidHook:
        _diagnostic("invalid project scope or payload")
    except Exception:
        _diagnostic("adapter unavailable")
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                _diagnostic("adapter cleanup unavailable")
    return {}


def run_hook(project: Path, agent: str, stdin=None, stdout=None) -> Dict[str, Any]:
    """Read one bounded JSON payload and emit only protocol JSON on stdout."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    output = {}
    try:
        raw = stdin.read(MAX_INPUT_BYTES + 1)
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(raw_bytes) > MAX_INPUT_BYTES:
            raise _InvalidHook()
        event = json.loads(raw_bytes)
        output = handle_hook(project, agent, event)
    except _InvalidHook:
        _diagnostic("input exceeds size limit")
    except Exception:
        _diagnostic("invalid input")
    try:
        stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
        stdout.flush()
    except Exception:
        _diagnostic("output unavailable")
    return output
