import argparse
import json
import sys

from . import __version__
from .skill_install import CLIENTS


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentBridge — 同一台 Mac，按工作区隔离的 AI 助手交接")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install-skills", "uninstall-skills"):
        skill = sub.add_parser(name, help="安装/移除三个客户端的显式 handoff skill")
        skill.add_argument("--clients", default=",".join(CLIENTS))
        if name == "install-skills":
            skill.add_argument("--preview", action="store_true")
    handoff = sub.add_parser("handoff", help="供正式 skill 调用的交接后端；返回 JSON 回执")
    handoff.add_argument("action", choices=("check", "send", "receive", "list", "help"))
    handoff.add_argument("--cwd", required=True)
    handoff.add_argument("--agent", choices=CLIENTS, required=True)
    handoff.add_argument("--session")
    handoff.add_argument("--file")
    handoff.add_argument("--id", dest="packet_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "handoff":
            from .handoff_service import dispatch
            value = dispatch(args.action, args.cwd, args.agent, session=args.session,
                             file=args.file, packet_id=args.packet_id)
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            return 0 if value["ok"] else 1
        from .skill_install import install_skills, uninstall_skills
        clients = tuple(x.strip() for x in args.clients.split(","))
        value = install_skills(clients, preview=args.preview) if args.command == "install-skills" else uninstall_skills(clients)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print("AgentBridge: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
