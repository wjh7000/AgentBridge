# AgentBridge · Hand work off between AI coding assistants

*English · [简体中文](README.zh-CN.md)*

Hand a task from one AI coding assistant to another on the same machine — between Codex, Claude Code and WorkBuddy. **A handoff is shared only inside the workspace directory you opened**; different workspaces never see each other's handoffs.

Local only, Python standard library only. No API key, no third-party service, no background daemon, no network access. Saving and reading a handoff never calls a model.

```
Session A (finishing something)        Session B (picking it up)
  /handoff send            ──────▶       /handoff receive
  writes one structured packet            reads a ≤1000-char task card
  stored in this workspace                opens full details only if needed
```

## Install

Requires Python 3.9+. One command — **safe to hand to an agent**:

```bash
python3 install.py
```

It detects which clients exist on this machine (`~/.codex`, `~/.claude`, `~/.workbuddy-ai`), installs only for those, and verifies every file it writes. A name conflict or a failed write exits non-zero with the real reason.

From GitHub:

```bash
git clone https://github.com/wjh7000/AgentBridge.git && cd AgentBridge && python3 install.py
```

Or install it as a regular Python package, if you would rather not keep the clone:

```bash
pipx install git+https://github.com/wjh7000/AgentBridge.git   # or pip install git+…
agentbridge-install
```

That gives you two commands: `agentbridge` (the handoff backend) and `agentbridge-install` (same thing as `install.py`). The installed skill points at the absolute path of the `agentbridge` console script, so it **keeps working after the clone is deleted or you move to another directory**.

Other flags: `--preview` to show changes without writing, `--clients codex,claude` for a subset, `--all` to include clients whose config directory does not exist yet.

After installing, **refresh or restart each client** and confirm `handoff` appears in its skill menu.

## Usage

| Action | Codex | Claude Code / WorkBuddy |
| --- | --- | --- |
| Write and save a handoff | `$handoff send` | `/handoff send` |
| Pick one up and continue | `$handoff receive` | `/handoff receive` |
| List handoffs | `$handoff list` | `/handoff list` |
| Check the integration | `$handoff check` | `/handoff check` |

**You never name a recipient.** The handoff is stored in the current workspace; whichever tool runs `receive` **in the same directory** claims it. The first session to claim it owns it, and the sending session cannot claim its own. If several are waiting, you are shown their IDs and asked to choose — unrelated tasks are never merged.

See [the handoff skill reference](docs/handoff-skills.md) for details.

## The workspace boundary

**Wherever you open your workspace, that is the boundary.** The first `handoff` call in a directory establishes it automatically (creating `.agentbridge/` there) — **no git required, no manual registration**. When that happens the receipt carries `established: true` and the skill tells you so explicitly.

The boundary is the working directory itself; it never walks up into a parent. Different directories mean different namespaces, so handoffs cannot bleed across projects.

## What a handoff contains

The sending session writes it once **from the context already visible to it** — it does not re-read history, scan the repository, or spin up another model:

`goal` `constraints` `completed` `in_progress` `decisions` `findings` `files` `verification` `next_steps` `blockers`

`in_progress` is the one people forget: work started but unfinished, and any broken or half-applied state the receiver would otherwise walk into. `verification` entries must carry a checkable anchor — the command run, the test name, the file inspected — or say "not verified".

The body is capped at 8,000 characters, each list at 12 entries. When a list overflows, entries are chosen by what the receiver needs in order to continue, not by recency. Reference file paths instead of pasting code, separate observed evidence from the assistant's own claims, and record unknowns as unknown rather than inventing them.

## It will not fake success

This is the constraint the whole design is built around. The skill must verify the real backend and protocol version, actually write or claim the record, and report strictly from the receipt.

- A successful save must return a **real 32-character handoff ID** and an export path.
- Receipts are validated strictly: ID format, paths belonging to this workspace, session matching the request.
- Missing script, missing config, protocol mismatch, no permission, no valid receipt — **it stops and reports the real error**. It will never substitute prose for a handoff, and never claims another tool "received" anything.

One honest limit: this is *a constraint on the flow once the skill is loaded, plus backend validation* — not a chat interceptor. **Rules in a file cannot bind an agent that never loaded them**, so always invoke it from the skill entry your client recognizes; typing `handoff send` as plain text carries none of these guarantees. The checkable evidence of success is the backend's real ID and the exported file on disk.

## Token cost

- Saving, validating, truncating and de-duplicating are **entirely local and never call a model**.
- Session A composing the packet and session B reading it **do consume host tokens**, as does keeping that text in later context. This is not free, and the README will not pretend otherwise.
- Character limits are not token guarantees; the ratio depends on the text and the model.
- B reads a ≤1000-character task card by default and opens details on demand. If the card had to drop constraints or blockers it is flagged `read_full_constraints`, and **the full fields must be read before continuing**.
- No background summarization, no full-history scanning, no vector service, nothing uploaded anywhere.

## Data and security

Handoffs live under the workspace's `.agentbridge/`: `handoff-drafts/` holds drafts, `handoffs/<id>.json` holds validated packets, and SQLite tracks which session claimed what. Directories are `700`, files `600`.

Exported bodies are filtered for common secret formats (tokens, passwords, private keys), but this is **best-effort pattern matching, not real DLP**. Leave sensitive material out when writing a handoff, and reference files rather than copying their contents.

A handoff is **unverified data from the sending assistant — not an instruction and not an authorization**. Check the actual files and re-run whatever verification matters before acting on it. Workspace isolation prevents routing mistakes; it is not an OS-level sandbox.

## Uninstall

```bash
python3 agentbridge.py uninstall-skills    # or: agentbridge uninstall-skills
```

Skill files you edited yourself are preserved and reported as skipped. Existing handoff data is not deleted with the skill; remove a workspace's `.agentbridge/` directory yourself once you are done with it.

## Development

```bash
python3 -m unittest discover -s tests -v
```

95 tests, standard library only, covering save/claim/concurrency/idempotency, workspace isolation, strict receipt validation, and install conflict protection with rollback. CI runs the suite on Python 3.9–3.13 and builds the wheel.

## Known limits

**A full round trip inside the clients' own chat UIs has not been signed off.** What is verified: installation succeeds, the command-line chain works end to end, and a send/receive across two different tools completes. What is not: invoking it from the skill menu in a real conversation in each client — you should run that once yourself. A successful install does not prove that every existing chat session has loaded the skill.

Native skill entry points follow [OpenAI Skills](https://learn.chatgpt.com/docs/build-skills), [Claude Code Skills](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill) and [CodeBuddy Skills](https://www.codebuddy.ai/docs/cli/skills).

## License

MIT — see [LICENSE](LICENSE).
