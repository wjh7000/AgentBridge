#!/usr/bin/env python3
"""Explicit skill adapter. Missing configuration is an error, never a fallback."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX32_JSON = re.compile(r"[0-9a-f]{32}\.json")
_AGENT = re.compile(r"[a-z][a-z0-9-]{1,31}")


def failure(code, message):
    return {"ok": False, "backend": "agentbridge", "protocol_version": 2,
            "code": code, "message": message}


def _hex32(value):
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


def _project_of(value, cwd):
    """The receipt project must be this task's directory or one of its parents."""
    if not isinstance(value, str) or not value:
        return None
    try:
        root = Path(value)
    except (TypeError, ValueError):
        return None
    if not root.is_absolute():
        return None
    return root if root == cwd or root in cwd.parents else None


def _named_under(project, value, subdir, expected_name=None):
    """A receipt path must be <project>/.agentbridge/<subdir>/<32hex>.json."""
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        if not path.is_absolute():
            return False
        relative = path.relative_to(project / ".agentbridge" / subdir)
    except (TypeError, ValueError):
        return False
    if len(relative.parts) != 1 or _HEX32_JSON.fullmatch(relative.name) is None:
        return False
    return expected_name is None or relative.name == expected_name + ".json"


def _items(value):
    if not isinstance(value, list):
        return False
    return all(isinstance(item, dict) and _hex32(item.get("id"))
               and isinstance(item.get("source"), str) and item["source"] for item in value)


def _validate(action, value, args, cwd):
    """Confirm a success receipt matches this exact request; otherwise reject it."""
    project = _project_of(value.get("project"), cwd)
    if project is None:
        return "Receipt project is not this task's project; stop."
    if action in ("check", "send", "receive"):
        session_id = value.get("session_id")
        if (not isinstance(session_id, str) or not session_id
                or (args.session is not None and session_id != args.session)):
            return "Receipt session does not match this request; stop."
    if action == "check":
        if value.get("status") != "ready" or not _named_under(project, value.get("draft_path"), "handoff-drafts"):
            return "Preflight did not return a valid draft path in this project; stop."
    elif action == "send":
        packet_id = value.get("packet_id")
        if (value.get("status") != "saved" or value.get("state") != "saved" or not _hex32(packet_id)
                or not _named_under(project, value.get("details_path"), "handoffs", packet_id)):
            return "No saved packet receipt; do not claim handoff was saved."
    elif action == "list":
        if value.get("status") != "listed" or not _items(value.get("items")):
            return "List did not return valid handoff metadata; stop."
    elif action == "receive":
        status = value.get("status")
        if status not in ("empty", "choose", "received", "already_received"):
            return "No valid receive status; stop."
        context = value.get("context")
        if not isinstance(context, str) or not context or len(context) > 1000:
            return "Receive receipt is missing its bounded task card; stop."
        if status in ("received", "already_received"):
            packet_id = value.get("packet_id")
            if not _hex32(packet_id) or not _named_under(project, value.get("details_path"), "handoffs", packet_id):
                return "Receive receipt is missing its packet or details path; stop."
        if status == "choose":
            total = value.get("total_count")
            if not _items(value.get("items")) or type(total) is not int or total < len(value["items"]):
                return "Choose receipt has invalid handoff options; stop."
    return None


def run(argv=None):
    parser = argparse.ArgumentParser(description="AgentBridge handoff skill adapter")
    parser.add_argument("action", choices=("check", "send", "receive", "list", "help"))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--session")
    parser.add_argument("--file")
    parser.add_argument("--id", dest="packet_id")
    args = parser.parse_args(argv)
    try:
        config_path = Path(__file__).resolve().parents[1] / "bridge.json"
        if not config_path.is_file():
            return failure("not_configured", "Skill backend is not configured. Stop; do not simulate handoff or auto-install.")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        # The caller identity is checked for shape only; which identifiers exist
        # is the backend's business, so adding a client never edits this helper.
        if (not isinstance(config, dict) or config.get("protocol_version") != 2
                or config.get("backend") != "agentbridge"
                or not isinstance(config.get("agent"), str)
                or _AGENT.fullmatch(config["agent"]) is None):
            return failure("invalid_config", "Skill backend configuration is invalid; stop.")
        backend = config.get("command")
        if (not isinstance(backend, list) or not (1 <= len(backend) <= 3)
                or not all(isinstance(part, str) and part for part in backend)):
            return failure("invalid_config", "Skill backend configuration is invalid; stop.")
        # Accept only the shapes backend_argv produces: [console], [python, script],
        # or [python, "-m", "agentbridge"]. The launcher is always an absolute file.
        if len(backend) == 3 and backend[1:] != ["-m", "agentbridge"]:
            return failure("invalid_config", "Skill backend configuration is invalid; stop.")
        file_parts = backend[:1] if len(backend) == 3 else backend
        parts = [Path(part) for part in file_parts]
        if not all(part.is_absolute() and part.is_file() for part in parts):
            return failure("backend_missing", "Configured AgentBridge backend is missing; stop.")
        cwd = Path(args.cwd)
        if not cwd.is_absolute() or cwd.resolve(strict=True) != Path.cwd().resolve():
            return failure("cwd_mismatch", "--cwd must be this task's actual process working directory; do not switch projects to bypass this check.")
        cwd = cwd.resolve(strict=True)
        command = list(backend) + ["handoff", args.action,
                   "--cwd", str(cwd), "--agent", config["agent"]]
        for name, value in (("--session", args.session), ("--file", args.file), ("--id", args.packet_id)):
            if value is not None:
                command.extend((name, value))
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        if len(result.stdout) > 32000:
            return failure("invalid_response", "Backend returned excessive output; no valid receipt.")
        try:
            value = json.loads(result.stdout)
        except ValueError:
            return failure("invalid_response", "Backend returned no valid JSON receipt; do not claim success.")
        if (not isinstance(value, dict) or value.get("backend") != "agentbridge"
                or value.get("protocol_version") != 2 or type(value.get("ok")) is not bool):
            return failure("protocol_mismatch", "Backend protocol is incompatible; stop.")
        if result.returncode != 0 and value["ok"]:
            return failure("backend_failed", "Backend failed without a valid success receipt; stop.")
        if value["ok"] and args.action != "help":
            problem = _validate(args.action, value, args, cwd)
            if problem is not None:
                return failure("invalid_receipt", problem)
        return value
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return failure("adapter_unavailable", "Skill adapter could not complete the operation; stop without inventing a handoff.")


if __name__ == "__main__":
    response = run()
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if response["ok"] else 1)
