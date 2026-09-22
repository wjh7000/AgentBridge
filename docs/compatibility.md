# 客户端接入核实

核实日期：2026-09-21。同一台 Mac；数据库和共享内容按项目根目录隔离。

## Codex

本机 CLI 版本 `0.144.5`，桌面版本 `26.506.31421`。官方支持项目 `.codex/hooks.json`、上述生命周期事件、Stop 的 `last_assistant_message`，以及 `.codex/config.toml` 的 stdio MCP。项目必须受信任，钩子定义首次或变更后需要原生信任流程。来源：[Hooks](https://learn.chatgpt.com/docs/hooks)、[MCP](https://developers.openai.com/codex/mcp)。

本轮在临时目录（包括临时 Git 项目）执行 `codex mcp list --json`，命令正常退出，但未检出项目 AgentBridge 服务；仅用命令行配置覆盖 trust，未修改用户的持久信任设置。因此不能把原生项目加载视为已验证，仍需在用户真实项目通过原生信任后验收。默认 hooks 模式本身也尚未进行真实 Codex 对话验收。独立 stdio 子进程的协议往返已经测试通过。

## Claude Code

官方文档确认：项目 hooks 可放在 `.claude/settings.local.json`；`SessionStart` 和 `UserPromptSubmit` 可通过 `hookSpecificOutput.additionalContext` 注入上下文；`Stop` 提供 `last_assistant_message`。本地 stdio MCP 可配置在项目 `.mcp.json`。规则文件是 `CLAUDE.md`、`CLAUDE.local.md`、`.claude/rules/*.md`。

来源：[Hooks](https://code.claude.com/docs/en/hooks)、[MCP](https://code.claude.com/docs/en/mcp)、[Memory](https://code.claude.com/docs/en/memory)。客户端首次信任 hooks/MCP 的原生流程仍需完成。

## WorkBuddy AI 桌面 5.5.2

本机产品：`/Applications/WorkBuddy AI.app`，版本来自 `Contents/Info.plist`。随应用发布的 `Contents/Resources/app.asar.unpacked/cli/product.json` 将 `customUserDataDir` 设为 `.workbuddy-ai`，并把 hooks 帮助链接指向腾讯 CodeBuddy 官方文档。

只读检查 `Contents/Resources/app.asar` 中的 `main/cli-prewarm-pool.js`：桌面启动随附 CLI 时传入 `--setting-sources user` 和 `--strict-mcp-config`，MCP 配置通过 `--mcp-config` 单独传递。

只读检查 `Contents/Resources/app.asar.unpacked/cli/dist/codebuddy.js`：

- `HookManager.getHooks()` 从 `settingsManager.get("hooks")` 读取 hooks；settings 合并遵守 `cliSettingSourcesProvider.getSettingScopes()`。因此桌面这条启动路径不加载项目 `.codebuddy/settings.local.json` 中的 hooks。
- CLI 自身的 `PathUtils.getProjectHomeDir()` 返回 `<项目>/.codebuddy`；项目 MCP 默认查找 `<项目>/.mcp.json`、其次 `<项目>/mcp.json`。这说明 CodeBuddy CLI 的项目配置能力不能直接当作 WorkBuddy 桌面的加载承诺。
- `Stop` 事件实现传递 `last_assistant_message`；`SessionStart` 和 `UserPromptSubmit` 消费 `hookSpecificOutput.additionalContext`。
- CLI 的规则文件为 `CODEBUDDY.md`、`CODEBUDDY.local.md`、`.codebuddy/rules/*.md`，存在 `AGENTS.md` fallback；桌面规则加载没有经过真实会话验收。

来源：[腾讯 Hooks](https://www.codebuddy.ai/docs/cli/hooks)、[腾讯 MCP](https://www.codebuddy.ai/docs/cli/mcp)、[腾讯 Memory](https://www.codebuddy.ai/docs/cli/memory)。以上桌面差异依据本机此版本发布代码；升级后应重新确认。

## AgentBridge 的桌面适配

在 `~/.workbuddy-ai/settings.json` 安装四个用户 hooks：`SessionStart`、`UserPromptSubmit`、`PostToolUse`、`Stop`。它们运行同一个本地 dispatcher，默认 allowlist 在 `~/.agentbridge/workbuddy-projects.json`；全局文件只保存入口和已启用项目路径，不保存项目进展。

默认项目模式是 `manual`：普通消息和会话开始不注入报告，只有用户单独发送“同步进展”时获取短增量。检查/记录在本地运行，不调用模型。MCP 与额外项目规则默认不安装。

dispatcher 从事件 `cwd` 向上查找最近 `.agentbridge/project.json`。只有 marker 正确且该根目录已明确登记才调用该项目的 hooks；遇到未启用或损坏的嵌套项目 marker 时不向父项目回退。缺少 cwd、无法读取配置或输入过大时静默返回 `{}`。多个项目逐一启用会合并 allowlist。

这是基于公开接口和本机发布代码的适配；**仍需一次真实 WorkBuddy 桌面会话，验证 hook 确实触发、cwd 正确且可读写摘要**。安装成功本身不代表客户端已连接。全局接入不改变项目数据库边界，也不创建 MCP 全局跨项目搜索入口。

安装保留已有 settings/hooks，备份安装前字节，并记录安装后的哈希。卸载仅还原未被修改的文件；用户修改过的文件保留原样和备份。项目内的共享数据库在卸载全局桥接时保留。
