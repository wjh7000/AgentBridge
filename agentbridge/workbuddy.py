"""WorkBuddy Desktop hook bridge; its desktop launcher loads user settings only.

The global hook contains no project data. Its allowlist routes events to an
independent project store. A real desktop conversation must still verify delivery.
"""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import sys

from .hooks import MAX_INPUT_BYTES, handle_hook
from .integrations import backend_argv, _json, _merge_hooks, _write, registered


def _destination(value):
    path = Path(value).expanduser().absolute()
    if path.resolve() != path:
        raise ValueError("Symlinked WorkBuddy configuration path is not supported")
    return path


def _paths(settings_path, registry_path):
    settings = _destination(settings_path or Path.home() / ".workbuddy-ai/settings.json")
    registry = _destination(registry_path or Path.home() / ".agentbridge/workbuddy-projects.json")
    manifest = registry.with_name(registry.stem + "-install.json")
    _destination(manifest)
    if len({settings, registry, manifest}) != 3:
        raise ValueError("Settings, registry, and backup paths must be distinct")
    return settings, registry, manifest


def _read(path):
    if not path.exists():
        return None, {}
    content = path.read_bytes()
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("Configuration exceeds size limit")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Configuration must contain a JSON object")
    return content, data


def _project(path):
    root = registered(path)
    marker = root / ".agentbridge/project.json"
    if marker.resolve() != marker or type(json.loads(marker.read_text()).get("version")) is not int:
        raise ValueError("Invalid project registration")
    return str(root)


def _allowlist(data):
    if not data:
        return []
    if type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("projects"), list):
        raise ValueError("Invalid WorkBuddy registry")
    projects = []
    for value in data["projects"]:
        if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value).resolve()) != value:
            raise ValueError("Registry projects must be canonical absolute paths")
        projects.append(value)
    return sorted(set(projects))


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def install_workbuddy(projects, settings_path=None, registry_path=None, preview=False):
    """Merge project registrations and install four user hooks, with backups."""
    settings, registry, manifest_path = _paths(settings_path, registry_path)
    if isinstance(projects, (str, Path)):
        projects = [projects]
    requested = [_project(project) for project in projects]
    if not requested:
        raise ValueError("At least one registered project is required")
    originals = {}
    originals[settings], settings_data = _read(settings)
    originals[registry], registry_data = _read(registry)
    manifest_before, previous = _read(manifest_path)
    if previous and (previous.get("version") != 1 or previous.get("settings_path") != str(settings) or previous.get("registry_path") != str(registry)):
        raise ValueError("Backup belongs to another WorkBuddy installation")
    roots = sorted(set(_allowlist(registry_data) + requested))
    # Validate old registrations too, so stale/moved projects cannot silently return.
    for root in roots:
        if _project(root) != root:
            raise ValueError("Project registration no longer matches its directory")
    command = shlex.join(backend_argv() + ["dispatch", "--registry", str(registry), "--agent", "workbuddy"])
    old_commands = set(previous.get("hook_commands", [])) | {command}
    planned = {
        settings: _json(_merge_hooks(settings_data, command, old_commands)).encode("utf-8"),
        registry: _json({"version": 1, "projects": roots}).encode("utf-8"),
    }
    changed = [str(path) for path, content in planned.items() if content != originals[path]]
    result = {"preview": preview, "settings": str(settings), "registry": str(registry), "projects": roots, "files": changed,
              "next": "Restart WorkBuddy and verify one real conversation; desktop delivery is not yet verified."}
    if preview:
        result["hook_command"] = command
        return result
    manifest = {"version": 1, "settings_path": str(settings), "registry_path": str(registry),
                "hook_commands": [command], "files": dict(previous.get("files", {}))}
    for path, content in planned.items():
        key = "settings" if path == settings else "registry"
        old = manifest["files"].get(key)
        original = originals[path]
        if old and (original is None or _digest(original) != old["installed_sha256"]):
            # Do not adopt changed user files as a new restoration baseline.
            # Retaining this flag prevents a later uninstall from overwriting edits.
            # Deletion is also a user change: never resurrect the initial backup.
            old = dict(old, modified_before_reinstall=True)
        entry = old or {"before": None if original is None else base64.b64encode(original).decode("ascii")}
        manifest["files"][key] = dict(entry, installed_sha256=_digest(content))
    try:
        for path, content in planned.items():
            if originals[path] != content:
                _write(path, content)
        _write(manifest_path, _json(manifest).encode("utf-8"))
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _write(path, original)
        if manifest_before is None:
            manifest_path.unlink(missing_ok=True)
        else:
            _write(manifest_path, manifest_before)
        raise
    return result


def uninstall_workbuddy(settings_path=None, registry_path=None):
    """Restore untouched files exactly; retain modified files and their backups."""
    settings, registry, manifest_path = _paths(settings_path, registry_path)
    _, manifest = _read(manifest_path)
    if not manifest:
        return {"restored": [], "skipped_modified_files": [], "note": "No WorkBuddy bridge installation found."}
    if manifest.get("version") != 1 or manifest.get("settings_path") != str(settings) or manifest.get("registry_path") != str(registry):
        raise ValueError("Backup belongs to another WorkBuddy installation")
    entries = manifest.get("files")
    if not isinstance(entries, dict) or any(key not in ("settings", "registry") for key in entries):
        raise ValueError("Invalid WorkBuddy backup")
    restored, skipped = [], []
    # Validate all backup data before making any changes.
    before = {key: None if entry["before"] is None else base64.b64decode(entry["before"], validate=True) for key, entry in entries.items()}
    for key, path in (("settings", settings), ("registry", registry)):
        entry = entries.get(key)
        if not entry:
            continue
        if not path.exists():
            del entries[key]
            continue
        if entry.get("modified_before_reinstall") or _digest(path.read_bytes()) != entry["installed_sha256"]:
            skipped.append(str(path))
            continue
        if before[key] is None:
            path.unlink()
        else:
            _write(path, before[key])
        restored.append(str(path))
        del entries[key]
    if entries:
        _write(manifest_path, _json(manifest).encode("utf-8"))
    else:
        manifest_path.unlink()
    return {"restored": restored, "skipped_modified_files": skipped,
            "note": "Project memory is retained. Modified files and their backups are untouched."}


def dispatch_hook(registry_path, agent="workbuddy", stdin=None, stdout=None):
    """Route by nearest registration, never crossing an unlisted project boundary."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    output = {}
    try:
        if agent != "workbuddy":
            raise ValueError("Invalid desktop agent")
        raw = stdin.read(MAX_INPUT_BYTES + 1)
        raw = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("Input exceeds size limit")
        event = json.loads(raw)
        if not isinstance(event, dict) or not isinstance(event.get("cwd"), str) or not Path(event["cwd"]).is_absolute():
            raise ValueError("Missing absolute cwd")
        cwd = Path(event["cwd"]).resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("Invalid cwd")
        _, registry = _read(_destination(registry_path))
        roots = set(_allowlist(registry))
        for cursor in (cwd, *cwd.parents):
            marker = cursor / ".agentbridge/project.json"
            if marker.exists() or marker.is_symlink():
                if str(cursor) in roots and _project(cursor) == str(cursor):
                    output = handle_hook(cursor, agent, event)
                break
    except Exception:
        # Hooks must not block work or leak input in diagnostics.
        output = {}
    try:
        stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
        stdout.flush()
    except Exception:
        pass
    return output
