import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .integrations import CLIENTS, install, registered, uninstall, validate_client_cwd


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentBridge — 同一台 Mac，按项目隔离共享 AI 助手进展")
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
    enable = sub.add_parser("enable-workbuddy", help="为已登记项目配置 WorkBuddy Desktop 用户级路由钩子")
    enable.add_argument("--project", required=True, action="append")
    enable.add_argument("--preview", action="store_true")
    sub.add_parser("disable-workbuddy", help="安全恢复 WorkBuddy 用户级钩子配置")
    dispatch = sub.add_parser("dispatch", help=argparse.SUPPRESS)
    dispatch.add_argument("--registry", required=True)
    dispatch.add_argument("--agent", choices=("workbuddy",), default="workbuddy")
    for name in ("init", "uninstall", "status", "context", "report", "serve", "hook"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", required=True, help="项目目录的路径")
        if name in ("context", "report", "serve", "hook"):
            cmd.add_argument("--agent", choices=CLIENTS, required=True)
        if name == "init":
            cmd.add_argument("--clients", default=",".join(CLIENTS))
            cmd.add_argument("--mode", choices=("manual", "session", "interval"), help="同步方式；首次默认manual，重装保留原值")
            cmd.add_argument("--with-mcp", action="store_true", help="额外安装按需查询工具和项目规则；默认只安装hooks以节约上下文")
            cmd.add_argument("--preview", action="store_true", help="只列出将修改的文件")
        if name == "report":
            cmd.add_argument("--summary", required=True)
            cmd.add_argument("--kind", default="summary", choices=("summary", "activity", "decision", "blocker", "interrupted"))
            cmd.add_argument("--session", default="manual")
            cmd.add_argument("--file", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "handoff":
            from .handoff_service import dispatch
            value = dispatch(args.action, args.cwd, args.agent, session=args.session,
                             file=args.file, packet_id=args.packet_id)
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            return 0 if value["ok"] else 1
        elif args.command in ("install-skills", "uninstall-skills"):
            from .skill_install import install_skills, uninstall_skills
            clients = tuple(x.strip() for x in args.clients.split(","))
            value = install_skills(clients, preview=args.preview) if args.command == "install-skills" else uninstall_skills(clients)
        elif args.command == "enable-workbuddy":
            from .workbuddy import install_workbuddy
            value = install_workbuddy(args.project, preview=args.preview)
        elif args.command == "disable-workbuddy":
            from .workbuddy import uninstall_workbuddy
            value = uninstall_workbuddy()
        elif args.command == "dispatch":
            from .workbuddy import dispatch_hook
            dispatch_hook(args.registry, args.agent)
            return 0
        elif args.command == "init":
            value = install(args.project, tuple(x.strip() for x in args.clients.split(",")), args.preview, args.mode, args.with_mcp)
        elif args.command == "uninstall":
            value = uninstall(args.project)
        elif args.command == "hook":
            from .hooks import run_hook
            run_hook(Path(args.project).expanduser().resolve(), args.agent)
            return 0
        else:
            root = registered(args.project)
            if args.command == "serve":
                from .mcp import serve
                validate_client_cwd(root)
                serve(root, args.agent)
                return 0
            from .store import Store
            store = Store(root)
            if args.command == "status":
                value = {"project": str(root), "database": str(store.db_path), "recent_reports": store.list_events(limit=20),
                         "note": "Stored reports do not prove that all clients are connected or hooks are trusted."}
            elif args.command == "context":
                print(store.context(args.agent))
                return 0
            else:
                value = store.publish(args.agent, args.session, args.summary, kind=args.kind, files=args.file)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print("AgentBridge: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
