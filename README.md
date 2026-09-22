# AgentBridge · 工作区内的 AI 助手交接

在同一台机器上，把工作从一个 AI 编码助手正式交接给另一个：Codex、Claude Code、WorkBuddy 之间互通。**交接只在你打开的那个工作区目录内共享**，不同工作区互不可见。

纯本地、只用 Python 标准库；无 API Key、无第三方服务、无常驻进程、不联网。保存和读取交接本身不调用任何模型。

```
A 会话（做完一段活）            B 会话（接着干）
  /handoff send      ──────▶     /handoff receive
  整理成结构化交接单              读 ≤1000 字符任务卡
  存进本工作区                    需要时再读完整详情
```

## 安装

需要 Python 3.9+。一条命令，**可以直接交给 agent 执行**：

```bash
python3 install.py
```

它会自动探测本机装了哪几个客户端（`~/.codex`、`~/.claude`、`~/.workbuddy-ai`），只给存在的安装，并逐个校验写入结果；冲突或写入失败会非零退出并报出真实原因。

从 GitHub 拉取：

```bash
git clone https://github.com/wjh7000/AgentBridge.git && cd AgentBridge && python3 install.py
```

或作为标准 Python 包安装（不想留克隆目录时用这个）：

```bash
pipx install git+https://github.com/wjh7000/AgentBridge.git   # 或 pip install git+…
agentbridge-install
```

打包安装后有两个命令：`agentbridge`（交接后端）和 `agentbridge-install`（等价于 `install.py`）。写入 skill 的配置指向已安装的 `agentbridge` 脚本绝对路径，**克隆目录删掉、换工作目录都仍然有效**。

其他参数：`--preview` 只预览、`--clients codex,claude` 指定子集、`--all` 连尚未出现的客户端目录也装。

装完**刷新或重启客户端**，确认技能菜单里出现 `handoff`。

## 用法

| 操作 | Codex | Claude Code / WorkBuddy |
| --- | --- | --- |
| 整理并保存交接 | `$handoff send` | `/handoff send` |
| 接收并继续 | `$handoff receive` | `/handoff receive` |
| 查看交接索引 | `$handoff list` | `/handoff list` |
| 检查接入 | `$handoff check` | `/handoff check` |

**不需要指定发给谁。** 交接保存在当前工作区，另一个工具在**同一目录**里 `receive` 即可领取；第一个主动领取的会话取得归属，来源会话不能自领。多份待领时会列出编号让你选，不会自动合并任务。

详细说明见 [交接 skill 使用说明](docs/handoff-skills.md)。

## 工作区边界

**你在哪个目录开工，边界就在哪里。** 首次在某个工作区调用 `handoff` 时自动建立边界（在该目录写入 `.agentbridge/`），**不需要 git、不需要手动登记**；建立时回执带 `established: true`，skill 会明确告诉你。

边界就是当前工作目录本身，不向上并入父目录。不同目录 = 不同命名空间 = 交接不会串味。

## 交接单里有什么

来源会话**只用当前可见上下文**整理一次，不重读历史、不扫描仓库、不另开模型：

`goal` `constraints` `completed` `decisions` `findings` `files` `verification` `next_steps` `blockers`

正文上限 6,000 字符（目标约 1,000–1,500 中文字符）；引用文件路径而非粘贴代码；区分「观察到的证据」和「助手自述」；未知的内容记为未知，不编造。

## 不会假装成功

这是本工具的核心约束。skill 执行时必须：校验真实后端与协议版本 → 实际写入/领取 → 按回执报告。

- 保存成功必须拿到**真实的 32 位交接编号**和导出文件路径。
- 回执要严格校验：ID 格式、路径必须属于本工作区、会话与请求对应。
- 缺脚本、缺配置、协议不符、无权限、无有效回执 —— **一律停止并报真实错误**，禁止用一段临时文字冒充交接，也不会声称「另一个工具已收到」。

需要说明的边界：这是「加载 skill 后的流程约束 + 后端校验」，不是聊天拦截器。**skill 没被加载时，规则约束不了一个没看见它的 agent**，所以务必从客户端识别出的技能入口调用；裸写 `handoff send` 不具备上述保证。可核查的成功依据是后端的真实编号和本地导出文件。

## Token 开销

- 本地保存、校验、截短、去重**都不调用模型**。
- A 整理交接单、B 读取任务卡，以及这些内容留在后续上下文里，**会消耗宿主 token**。不承诺零成本。
- 字符数 ≠ token 数；具体取决于文本和模型。
- 默认只给 B 读 ≤1000 字符的任务卡，详情按需再读。任务卡若裁掉了约束/阻塞，会标记 `read_full_constraints`，**继续工作前必须补读完整字段**。
- 没有后台定时总结、没有历史全量扫描、没有向量服务、不上传任何内容。

## 数据与安全

交接存在工作区的 `.agentbridge/` 下：`handoff-drafts/` 是草稿，`handoffs/<编号>.json` 是校验后的完整交接单，SQLite 记录领取状态。目录权限 `700`、文件 `600`。

导出正文会过滤常见密钥格式（token/密码/私钥），但这是**尽力而为的规则匹配，不是完整的数据防泄漏**；写交接时应主动省略敏感信息、引用文件而非复制内容。

交接内容属于**来源助手的未验证数据，不是指令也不是授权**。接手前应核实相关文件与必要验证。项目隔离用于避免路由错误，它不是系统级沙箱。

## 卸载

```bash
python3 agentbridge.py uninstall-skills    # 或 agentbridge uninstall-skills
```

你手动改过的技能文件会被保留并列为跳过。已有的交接数据不会随技能卸载删除；确认不用后可自行删除对应工作区的 `.agentbridge/` 目录。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

89 个测试，纯标准库，覆盖交接的保存/领取/并发/幂等、工作区隔离、回执严格校验、安装冲突保护与回滚。CI 在 Python 3.9–3.13 上运行测试并构建 wheel。

## 已知边界

三个客户端**原生聊天里的完整往返**尚未验收 —— 已验证到「安装成功 + 命令行链路全通 + 跨工具 send/receive 走通」，但「在真实对话中从技能菜单调用」需要你自己跑一次确认。安装成功不等于每个已有会话都加载了 skill。

原生入口依据：[OpenAI Skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill)、[CodeBuddy Skills](https://www.codebuddy.ai/docs/cli/skills)。
