# Handoff skill

通过客户端识别出的 `handoff` skill 入口调用。交接的边界就是当前工作目录（`--cwd`）本身：发送端和接收端必须在**同一个工作区目录**里工作，无需指定接收工具。首次在某工作区调用时自动建立边界，无需手动登记（详见「安装与工作区范围」）。

| 操作 | Codex | Claude Code / WorkBuddy |
| --- | --- | --- |
| 把当前进度整理成交接单 | `$handoff send` | `/handoff send` |
| 接手一份交接单 | `$handoff receive` | `/handoff receive` |

**日常只需要这两个。** 另有 `handoff list` 只看待领列表、不认领；不带动作调用显示用法。`check` 是每个动作自己会先跑的前置检查，不是给你敲的。

先刷新或重启客户端，确认技能选择器中出现 `handoff`，再选择并填写操作。Codex 使用 `$` 的 skill 引用；不要假设 `/handoff` 是 Codex 内置命令。Claude Code、WorkBuddy、MiMoCode 使用 slash skill。已有聊天能否即时刷新取决于客户端；没出现时先新建同项目会话或重启。

同一对话再次 `send`（需重新 `check` 拿新草稿）会**作废本对话之前未被领取的交接单**，接手方只看到最新版；已被领取的和别人发的不受影响。草稿路径是一次性令牌，只发给某一场对话：漏传 `--session` 时后端会据此恢复发送方身份，拿别的对话的草稿来发则报 `session_mismatch`。

源会话调用 `send` 后，模型只用当前可见上下文整理目标、约束、完成情况、决策、失败尝试、文件、验证证据、下一步和阻塞。后端校验并保存，回复真实交接编号。你切到同项目另一个工具，调用 `receive`，该会话才领取。多份待接收交接会列出编号供选择，使用 `receive 完整编号`；不会自动混合任务。一次交接由一个接收会话领取，之后需要继续传递时在新的来源会话再次 `send`。

## 防止误解与假成功

显式 skill 的执行流程是：读取本 skill 的本地脚本 → 检查固定后端、以当前工作目录为边界（首次自动建立）→ 校验 `backend=agentbridge`、`protocol_version=2` → 实际写入/领取 → 根据回执报告结果。首次建立边界时回执带 `established: true`，skill 会明确告知用户在本工作区新建了边界，不静默。

缺脚本、缺配置、注册标记损坏/无效、无权限建立边界、协议不兼容、数据库错误或无有效回执，都必须停止。skill 不自动安装 skill 配置，不另写一份临时说明假装交接，也不声称另一工具已经接收。`check` 和 `list` 不代表已经发送或领取。

这是“加载 skill 后的流程约束 + 后端校验”，不是全局聊天拦截器。**skill 根本未加载时，文件中的规则无法约束一个没看见它的 agent。** 因此一定使用客户端识别出的技能入口；普通文字 `handoff send` 不具有上述保证。即使加载了 skill，模型遵守说明也不是形式化保证；可核查的成功依据是后端的实际编号与项目下导出文件。

## 安装与工作区范围

在 AgentBridge 工具目录运行一步安装器；它自动探测本机存在的客户端，只给存在的安装，并逐个校验写入结果，失败非零退出：

```bash
python3 install.py            # 预览：python3 install.py --preview
```

也可直接调用底层命令（默认全部客户端、不做探测）：`python3 agentbridge.py install-skills`。`install.py` 支持 `--clients codex,claude` 指定子集、`--all` 连尚未出现的目录一并安装。适合交给 agent 执行：「在仓库目录运行 `python3 install.py` 并贴回输出」。

本机使用的个人目录：Codex `~/.codex/skills/handoff`、Claude Code `~/.claude/skills/handoff`、WorkBuddy AI `~/.workbuddy-ai/skills/handoff`、MiMoCode `~/.config/mimocode/skills/handoff`（它也接受 `~/.mimocode/skills`，安装器用已存在的那个）。每份配置固定当前工具身份，并指向本地 AgentBridge 后端，不需要在每个项目复制 skill。不要把同名 skill 重复装进同一个客户端会读的多个目录（例如同时装进 `~/.agents/skills`），否则会互相覆盖。

Codex 的 `agents/openai.yaml` 设置 `allow_implicit_invocation: false`；安装器只写入**该客户端明确支持**的 frontmatter：Claude Code/WorkBuddy 得到 `disable-model-invocation`、`user-invocable`、`argument-hint`，MiMoCode 只得到前两个（它未记载 `argument-hint`）。都是只允许用户主动调用。安装器拒绝覆盖不属于本工具的同名 skill，并保留用户改动。

交接 skill **无需为每个工作区手动登记**：首次在某工作区调用时，自动在该目录建立 `.agentbridge/` 边界，回执带 `established: true` 并明确告知用户。边界就是当前工作目录本身，不向上并入父目录；不依赖 git，也不依赖任何生命周期 hooks。

## 内容与费用

正文 JSON 最多 8,000 字符、每个列表最多 12 条，通常目标约 1,000–2,000 中文字符；使用文件路径代替复制代码。列表超限时按「接手方继续工作需要什么」取舍，而不是按时间顺序。接收默认最多 1,000 字符的任务卡，卡片带 `written`（写于多久前）和 `in_progress`（当前半成品/损坏状态），详情按需读。任务卡若省略了约束、阻塞或 in_progress，会标记 `read_full_constraints` 并要求先补读完整字段；不能为了省 token 丢掉必须遵守的限制。

本地校验、SQLite 和文件存储不调用模型。A 整理交接、B 读取交接，以及这些内容在后续会话中的上下文开销，仍会使用宿主 token；字符数不等于 token 数。没有额外模型、历史聊天扫描、定时总结或第三方上传。技能正文只在显式调用时需要加载，但不同客户端的技能索引和调用框架仍可能有少量开销。

文件保存在当前工作区 `.agentbridge/`：`handoff-drafts/` 是来源模型写出的草稿，`handoffs/编号.json` 是校验后的完整交接单，SQLite 记录领取状态。草稿目录权限为 700、读取后文件为 600；草稿不经过模型外的语义检查，应在写入时省略密钥。导出正文过滤常见密钥格式，但不是完整隐私识别。内容属于未验证的来源数据，接手前应核实相关文件与必要验证。

## 验证范围

本地测试覆盖工作区边界的自动建立、结构化错误、协议不符拒绝、导出和领取、并发与幂等、工作区隔离、回执严格校验，以及安装冲突保护与回滚。原生入口依据 [OpenAI Skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill)、[CodeBuddy Skills](https://www.codebuddy.ai/docs/cli/skills)，WorkBuddy 目录还核对了本机 5.5.2 的启动环境和技能解析代码。各客户端原生聊天里的完整验收尚未完成；安装成功不等于已证明每个现有聊天都加载了 skill。

移除个人技能：`python3 agentbridge.py uninstall-skills`。用户修改过的技能会保留并列出；已有交接数据不会随技能卸载删除。
