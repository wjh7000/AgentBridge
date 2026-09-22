#!/usr/bin/env python3
"""Exercise synthetic project 1/2 traffic without changing desktop settings."""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbridge.hooks import handle_hook
from agentbridge.integrations import install
from agentbridge.mcp import Server


def main():
    with tempfile.TemporaryDirectory(prefix="agentbridge-demo-") as temp:
        first, second = Path(temp) / "project1", Path(temp) / "project2"
        first.mkdir()
        second.mkdir()
        install(first)
        install(second)
        handle_hook(first, "claude", {"cwd": str(first), "session_id": "claude-demo", "turn_id": "t1", "hook_event_name": "Stop", "last_assistant_message": "已实现登录校验；新增两项测试均通过。下一步补充错误提示。"})
        def read(root, agent, prompt="同步进展"):
            return handle_hook(root, agent, {"cwd": str(root), "session_id": "reader-demo", "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        assert read(first, "codex", "继续工作") == {}
        first_delivery = read(first, "codex")
        same = json.dumps(first_delivery, ensure_ascii=False)
        other = json.dumps(read(second, "codex"), ensure_ascii=False)
        assert "已实现登录校验" in same
        assert "已实现登录校验" not in other
        buddy = json.dumps(read(first, "workbuddy"), ensure_ascii=False)
        assert "已实现登录校验" in buddy
        print("通过：普通消息不注入；发送‘同步进展’才获取同项目报告")
        print("通过：项目 1 Claude 结束 → 手动同步到项目 1 Codex / WorkBuddy")
        print("通过：项目 2 Codex 收不到项目 1 的报告")
        print("\n项目 1 Codex 注入内容：")
        print(first_delivery["hookSpecificOutput"]["additionalContext"])
        assert "已实现登录校验" not in json.dumps(read(first, "codex"), ensure_ascii=False)
        print("通过：重复同步不重复发送同一进展")
        print("\n演示使用模拟事件和临时目录；不代表真实客户端已经接入。")


if __name__ == "__main__":
    main()
