# AGENTS.md

Instructions for an AI coding agent asked to install AgentBridge. A human can
follow them too. If you are an agent, follow this file literally and report the
real output — do not improvise an installation.

## What this repository is

AgentBridge lets one AI coding assistant hand work to another on the same
machine, through an explicit `handoff` skill. It installs a small skill into
each supported client's personal skill directory. It is local only, standard
library only, and it never calls a model or reaches the network.

Supported clients: **Codex**, **Claude Code**, **WorkBuddy**, **MiMoCode**.

## Install

From the repository root, run exactly this:

```bash
python3 install.py
```

There are no modes to choose. The installer detects which clients exist on this
machine, picks the correct personal skill directory for each, writes the skill,
and verifies every file it claims to have written.

Requires Python 3.9 or newer. Nothing else is needed — no dependencies, no
virtualenv, no API key.

To see what it would do without writing anything, add `--preview`.

## What success looks like

One line per detected client, then the next steps:

```
Installed codex     -> /Users/you/.codex/skills/handoff
Installed claude    -> /Users/you/.claude/skills/handoff
Installed workbuddy -> /Users/you/.workbuddy-ai/skills/handoff
Installed mimocode  -> /Users/you/.config/mimocode/skills/handoff
```

Exit code 0. Fewer lines simply means fewer clients are installed here; that is
normal, not a failure.

**Report success only if you actually saw those lines and the exit code was 0.**

## If it fails

The installer exits non-zero and prints the real reason. Common ones:

| Message | What it means |
| --- | --- |
| `No supported client was found` | None of the four clients has run on this machine yet. Open one, or pass `--clients <name>`. |
| `An unmanaged handoff skill already exists` | A different `handoff` skill is already installed there. It is left untouched. Ask the user before doing anything about it. |
| `Skill has local edits; preserving it` | The user edited a previously installed file. It is left untouched. Ask the user. |
| `Verification failed for <client>` | A file was written but did not check out. Report it verbatim. |

**Report the output as-is and stop.** Do not work around a failure.

## Rules for an agent doing this

- Do not copy files into skill directories by hand. Use `install.py`.
- Do not edit the generated `bridge.json`. It is written by the installer and
  pins the backend path and caller identity.
- Do not claim the installation worked without the `Installed ...` lines.
- Do not claim the skill is usable in a chat. You cannot verify that; see below.

## After installing

Tell the user to **refresh or restart each client**, then confirm `handoff`
appears in that client's own skill menu.

This last step cannot be verified from a shell. A successful install proves the
files are in place; it does not prove any client has loaded them. Say so plainly
rather than implying the round trip has been tested.

## Using it afterwards

| Action | Codex | Claude Code / WorkBuddy / MiMoCode |
| --- | --- | --- |
| Save a handoff | `$handoff send` | `/handoff send` |
| Pick one up | `$handoff receive` | `/handoff receive` |
| List handoffs | `$handoff list` | `/handoff list` |
| Check the integration | `$handoff check` | `/handoff check` |

Handoffs are shared only inside the workspace directory you have open, and the
first call in a workspace establishes that boundary automatically.

Full documentation: [README.md](README.md) · [中文](README.zh-CN.md)

## Uninstall

```bash
python3 agentbridge.py uninstall-skills
```

Files the user edited are preserved and reported as skipped. Existing handoff
data is not deleted.
