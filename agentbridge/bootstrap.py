"""One-step installer for the handoff skill, shared by `install.py` and the
`agentbridge-install` console script.

Detects which of Codex, Claude Code, and WorkBuddy are present on this machine
and installs the explicit, user-invoked handoff skill into each. It never
fabricates success: a name conflict or a write error stops with a nonzero exit
and the actual reason, and every install is verified against the files it claims
to have written.
"""
import argparse
import json
from pathlib import Path
import sys

from .skill_install import CLIENTS, detect_clients, install_skills


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentbridge-install",
        description="Install the AgentBridge handoff skill for detected clients.")
    parser.add_argument("--clients", help="Comma-separated subset of codex,claude,workbuddy. Default: auto-detect.")
    parser.add_argument("--all", action="store_true", help="Install for all three clients even if their config directory is absent.")
    parser.add_argument("--preview", action="store_true", help="List what would change without writing anything.")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 9):
        print("AgentBridge needs Python 3.9 or newer; found %d.%d." % sys.version_info[:2], file=sys.stderr)
        return 1

    if args.clients:
        clients = tuple(x.strip() for x in args.clients.split(",") if x.strip())
    elif args.all:
        clients = CLIENTS
    else:
        clients = detect_clients()
        if not clients:
            print("No Codex, Claude Code, or WorkBuddy configuration directory was found under\n"
                  "%s. Open the client once so it creates ~/.codex, ~/.claude, or ~/.workbuddy-ai,\n"
                  "or re-run with --all or --clients <name>." % Path.home(), file=sys.stderr)
            return 1

    try:
        result = install_skills(clients, preview=args.preview)
    except ValueError as exc:
        print("Install stopped without changes: %s" % exc, file=sys.stderr)
        return 1

    verb = "Would install" if args.preview else "Installed"
    for item in result["skills"]:
        path = Path(item["path"])
        if not args.preview:
            try:
                config = json.loads((path / "bridge.json").read_text(encoding="utf-8"))
                verified = config.get("agent") == item["agent"] and (path / "scripts/handoff.py").is_file()
            except (OSError, ValueError):
                verified = False
            if not verified:
                print("Verification failed for %s at %s; not reporting success." % (item["agent"], path), file=sys.stderr)
                return 1
        print("%s %-9s -> %s" % (verb, item["agent"], path))

    if args.preview:
        print("\nPreview only; nothing was written. Re-run without --preview to install.")
        return 0
    print("\nNext: refresh or restart each client, then choose `handoff` from its native skill menu.")
    print("Codex: `$handoff send` / `$handoff receive`.  Claude Code / WorkBuddy: `/handoff send` / `/handoff receive`.")
    print("The boundary is the workspace you open; the first call establishes it, with no manual registration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
