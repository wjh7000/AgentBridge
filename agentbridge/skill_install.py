"""Install explicit-only personal skills without replacing unrelated skills."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


CLIENTS = ("codex", "claude", "workbuddy")
RUNNER = Path(__file__).resolve().parents[1] / "agentbridge.py"
SOURCE = Path(__file__).resolve().parent / "skills/handoff"
MANIFEST = ".agentbridge-skill.json"
DIRECTORIES = {"codex": ".codex", "claude": ".claude", "workbuddy": ".workbuddy-ai"}


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agentbridge-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _installed_console():
    """The ``agentbridge`` console script in this package's own environment.

    Located from the package directory rather than ``sys.executable`` or PATH,
    so it stays correct even where the interpreter reports a different prefix
    (e.g. some conda-created virtualenvs). Its shebang embeds the right Python.
    """
    package = Path(__file__).resolve()
    for parent in package.parents:
        for bindir in ("bin", "Scripts"):
            for name in ("agentbridge", "agentbridge.exe"):
                candidate = parent / bindir / name
                if candidate.is_file():
                    return candidate
    return None


def backend_argv():
    """The portable command prefix that runs this backend from anywhere.

    Each form is independent of the caller's later working directory and keeps
    working after the launching process exits:

    * ``[console]`` — the installed ``agentbridge`` script (preferred).
    * ``[python, agentbridge.py]`` — running from a plain clone.
    * ``[python, "-m", "agentbridge"]`` — installed with no locatable script.
    """
    console = _installed_console()
    if console:
        return [str(console.resolve())]
    if RUNNER.is_file():
        return [str(Path(sys.executable).resolve()), str(RUNNER)]
    found = shutil.which("agentbridge")
    if found:
        return [str(Path(found).resolve())]
    return [str(Path(sys.executable).resolve()), "-m", "agentbridge"]


def detect_clients(home=None):
    """Which of the supported clients have a configuration directory present.

    Presence of ``~/.codex``/``~/.claude``/``~/.workbuddy-ai`` means the client
    has run on this machine, so installing its skill is meaningful. Absent ones
    are skipped rather than creating empty client trees.
    """
    home = Path.home() if home is None else Path(home).resolve()
    return tuple(agent for agent in CLIENTS if (home / DIRECTORIES[agent]).is_dir())


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _safe(path):
    if path.absolute() != path.resolve():
        raise ValueError("Symlinked skill path is not supported: " + str(path))


def _manifest(destination):
    _safe(destination)
    path = destination / MANIFEST
    _safe(path)
    if not path.exists():
        if destination.exists():
            raise ValueError("An unmanaged handoff skill already exists: " + str(destination))
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("owner") != "agentbridge" or value.get("version") != 1 or not isinstance(value.get("files"), dict):
        raise ValueError("Invalid skill installation manifest")
    for relative, digest in value["files"].items():
        if relative not in ("SKILL.md", "agents/openai.yaml", "scripts/handoff.py", "bridge.json"):
            raise ValueError("Invalid managed skill filename")
        target = destination / relative
        _safe(target)
        if not target.is_file() or _digest(target.read_bytes()) != digest:
            raise ValueError("Skill has local edits; preserving it: " + str(target))
    return value


def _files(agent):
    files = {name: (SOURCE / name).read_bytes() for name in ("SKILL.md", "agents/openai.yaml", "scripts/handoff.py")}
    if agent != "codex":
        text = files["SKILL.md"].decode("utf-8")
        if not text.startswith("---\n"):
            raise ValueError("Invalid skill frontmatter")
        text = text.replace("---\n", "---\ndisable-model-invocation: true\nuser-invocable: true\nargument-hint: \"send | receive [ID] | list | check\"\n", 1)
        variable = "CLAUDE_SESSION_ID" if agent == "claude" else "CODEBUDDY_SESSION_ID"
        identity = "\nNative session for this invocation: `${" + variable + "}`. If expanded to a concrete ID, pass that exact ID as `--session` on check and all actions; it takes precedence over a remembered identity. If still an unexpanded placeholder, use the verified conversation identity procedure below, never the literal placeholder.\n"
        text = text.replace("# Handoff\n", "# Handoff\n" + identity, 1)
        files["SKILL.md"] = text.encode("utf-8")
    files["bridge.json"] = (json.dumps({"backend": "agentbridge", "protocol_version": 2,
        "agent": agent, "command": backend_argv()}, indent=2) + "\n").encode("utf-8")
    return files


def install_skills(clients=CLIENTS, home=None, preview=False):
    home = Path.home() if home is None else Path(home).resolve()
    clients = tuple(dict.fromkeys(clients))
    if not clients or any(c not in CLIENTS for c in clients):
        raise ValueError("clients must be codex,claude,workbuddy")
    plans = []
    for agent in clients:
        destination = home / DIRECTORIES[agent] / "skills/handoff"
        previous = _manifest(destination)
        files = _files(agent)
        for name in files:
            _safe(destination / name)
            if previous is not None and name not in previous["files"] and (destination / name).exists():
                raise ValueError("Unmanaged skill file already exists: " + name)
        plans.append((agent, destination, files))
    if not preview:
        rollback, created_directories = [], set()
        try:
            for agent, destination, files in plans:
                manifest = {"owner": "agentbridge", "version": 1, "agent": agent,
                            "files": {name: _digest(content) for name, content in files.items()}}
                contents = dict(files, **{MANIFEST: (json.dumps(manifest, indent=2) + "\n").encode("utf-8")})
                for name, content in contents.items():
                    target = destination / name
                    rollback.append((target, target.read_bytes() if target.exists() else None))
                    parent = target.parent
                    while not parent.exists():
                        created_directories.add(parent)
                        parent = parent.parent
                    _write(target, content)
        except Exception:
            for target, original in reversed(rollback):
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    _write(target, original)
            for directory in sorted(created_directories, key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
    return {"preview": preview, "skills": [{"agent": agent, "path": str(destination)} for agent, destination, _ in plans],
            "next": "Refresh/restart clients and select the recognized handoff skill. Bare text is not a verified invocation."}


def uninstall_skills(clients=CLIENTS, home=None):
    home = Path.home() if home is None else Path(home).resolve()
    clients = tuple(dict.fromkeys(clients))
    if not clients or any(c not in CLIENTS for c in clients):
        raise ValueError("clients must be codex,claude,workbuddy")
    removed, skipped = [], []
    for agent in clients:
        destination = home / DIRECTORIES[agent] / "skills/handoff"
        if not destination.exists():
            continue
        try:
            previous = _manifest(destination)
        except ValueError:
            skipped.append(str(destination))
            continue
        for relative in previous["files"]:
            (destination / relative).unlink()
        (destination / MANIFEST).unlink()
        for directory in (destination / "scripts", destination / "agents", destination):
            try:
                directory.rmdir()
            except OSError:
                pass
        removed.append(str(destination))
    return {"removed": removed, "skipped_modified_or_unmanaged": skipped}
