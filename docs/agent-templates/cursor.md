# Cursor subagent template

Cursor subagents are markdown files with YAML frontmatter. They are consumed by the Agent/Task tool and run in their own context window.

## 1. File paths

| Type | Path | Scope | Notes |
|------|------|-------|-------|
| Project subagents | `.cursor/agents/*.md` | Current project only | Preferred. Takes precedence over user-level and over `.claude/` / `.codex/` compatibility paths. |
| User subagents | `~/.cursor/agents/*.md` | All projects for the current user | Use for personal, reusable subagents. |
| Project compatibility (Claude) | `.claude/agents/*.md` | Current project only | Cursor also reads these for cross-host compatibility. |
| Project compatibility (Codex) | `.codex/agents/*.md` | Current project only | Cursor also reads these for cross-host compatibility. |
| User compatibility (Claude) | `~/.claude/agents/*.md` | All projects | Read for compatibility. |
| User compatibility (Codex) | `~/.codex/agents/*.md` | All projects | Read for compatibility. |

Precedence when names conflict: `.cursor/` > `.claude/` > `.codex/`. User files are lower precedence than project files.

## 2. Full frontmatter schema

```yaml
---
name: my-subagent          # string, optional. Defaults to the filename (without .md). Lowercase letters and hyphens.
description: "..."         # string, optional but strongly recommended. This is what the parent agent reads to decide delegation.
model: inherit             # string, optional. Default: "inherit". See model values below.
readonly: false            # boolean, optional. Default: false. If true, blocks file edits, state-changing shell commands, and ALL MCP tools.
is_background: false     # boolean, optional. Default: false. If true, the subagent runs in the background and returns immediately.
---
```

Supported keys in Cursor subagent frontmatter:

| Field | Type | Required | Default | Valid values / notes |
|-------|------|----------|---------|----------------------|
| `name` | string | No | filename | Lowercase letters, hyphens. Used as the `/name` invocation and as the identifier. |
| `description` | string | No | — | Short, specific trigger phrase. Use "Use when..." / "Use proactively to..." to encourage automatic delegation. |
| `model` | string | No | `inherit` | `inherit` OR a specific model ID string. See model values below. |
| `readonly` | boolean | No | `false` | `true` blocks writes, destructive shell commands, and all MCP tools. Use for read-only reviewers. |
| `is_background` | boolean | No | `false` | `true` makes the subagent non-blocking. Parent gets an agent ID and can resume later. |

There is **no `tools:` key**. Tool lists are not declared per subagent.

### Model values

| Value | Meaning |
|-------|---------|
| `inherit` | Use the same model as the parent agent. Default. |
| `<model-id>` | Pin a specific model, e.g. `composer-2.5`, `claude-opus-5`, `gpt-5.6-sol`. |
| `<model-id>[]` | Pin the base/standard variant instead of the fast variant. Example: `composer-2.5[]`. |
| `<model-id>[fast=false]` | Explicitly select the non-fast variant. Example: `composer-2.5[fast=false]`. |
| `<model-id>[effort=high]` | Set reasoning effort. Example: `claude-opus-5[effort=high]`. |
| `<model-id>[context=300k]` | Set context window. Example: `claude-opus-5[context=300k]`. |
| `<model-id>[effort=high,context=300k]` | Combine options, comma-separated. |

`fast` is **not** a top-level `model:` value. It is only an option inside the bracket syntax (or the inline Task tool `model` parameter). `model: fast` is not valid frontmatter.

## 3. MCP tool access

Subagents inherit the **entire** parent tool set, including every configured MCP server and every tool inside those servers.

- Global inheritance: yes. The parent agent's built-in tools and all MCP servers are passed to the subagent.
- Per-agent MCP scoping: no. You cannot whitelist a subset of MCP servers or tools from within the subagent file.
- Cloud subagents are the exception: they run on a cloud VM and use the MCP servers configured for your team at `cursor.com/agents`, not the servers from your local session.
- MCP tool names are used as they appear in Cursor's **Available Tools** list. Cursor does **not** use the `mcp__server__tool` prefix that pi/Claude Code use. If the llm-council MCP server exposes tools named `council_start`, `council_poll`, etc., invoke them by those plain names.

## 4. Tool whitelisting

Per-subagent tool whitelisting is **not supported**.

The only frontmatter switch that affects tool access is `readonly`:

- `readonly: false` (default) — subagent can read, edit, run shell commands, and use all inherited MCP tools.
- `readonly: true` — subagent can read and run non-state-changing commands, but cannot edit files, run destructive shell commands, or use **any** MCP tools. It does not allow per-tool control.

Enterprise teams can restrict MCP servers/tools at the org level via **Team Settings > MCP Configuration > MCP Allowlist**, but that is admin policy, not subagent configuration.

## 5. Schema demonstration: `my-agent` placeholder

This is a blank schema demo — replace `my-agent`, the description, and the body with your real agent content. Save as `.cursor/agents/my-agent.md` (or `~/.cursor/agents/my-agent.md`):

```markdown
---
name: my-agent
description: <what this agent does — auto-routed by Cursor when description matches>
model: inherit
readonly: false
is_background: false
---

<agent body / system instructions in markdown>
```

Invoke it explicitly:

```text
/my-agent <task description>
```

Or mention it naturally in a prompt:

```text
Use the my-agent subagent to <do the task>.
```

## 6. Transformation from pi canonical

pi canonical frontmatter uses a `tools:` list with lowercase names and the `mcp__server__tool` prefix:

```yaml
---
name: my-agent
description: ...
tools: read, write, edit, grep, find, ls, bash, mcp__llm-council__council_start
---
```

(`mcp__llm-council__council_start` is shown only to demonstrate the pi MCP-tool prefix syntax; substitute your own server and tool names.)

Cursor equivalent:

| pi concept | Cursor equivalent |
|------------|-------------------|
| `tools:` frontmatter | **Drop it entirely.** Cursor does not accept a `tools:` key in subagent frontmatter. |
| `read` / `write` / `edit` | Use `Read files` and `Edit files` (and optionally shell commands). |
| `grep`, `find`, `ls` | Use the `Search files and folders` tool or `Run shell commands` (e.g., `grep`, `find`). |
| `bash` | Use `Run shell commands`. |
| `mcp__llm-council__council_*` | Drop the `mcp__llm-council__` prefix. Use the plain tool names as exposed by the MCP server (e.g., `council_start`, `council_poll`). |
| Tool allowlisting | Not available. Use `readonly: false` (default) to allow writes/MCP, or `readonly: true` to block everything. |

The only required frontmatter for Cursor is `name`/`description`/`model` plus the `readonly`/`is_background` flags.

## 7. Gotchas

- **No `tools:` key.** Adding it does not whitelist tools and may be ignored or cause the loader to treat the file differently. Do not put a `tools:` list in `.cursor/agents/*.md` frontmatter.
- **Model may not be respected.** The parent agent can pass a `model` argument to the Task tool that overrides the subagent's frontmatter `model`. Also, team admin blocks, plan limits, or legacy Max Mode settings can force a fallback. Use bracket syntax (`composer-2.5[]` or `composer-2.5[fast=false]`) to pin non-fast variants explicitly.
- **`readonly: true` blocks MCP entirely.** It is not a per-tool safety switch; it disables all MCP tools.
- **Cloud subagents don't inherit local MCP.** If you rely on a local llm-council MCP server, a cloud subagent will not see it. Cloud subagents use the team MCP configuration at `cursor.com/agents`.
- **Automation subagents lost MCP inheritance (Jul 2026).** As of the July 2026 automation backend change, child automation runs sometimes spawn without the parent's MCP servers attached. Verify in your version before depending on MCP in automations.
- **Task tool may not list custom subagents.** In some versions (e.g., 3.12.17), committed `.cursor/agents/*.md` files can be missing from the Task tool enum, especially if the file has frontmatter but no prompt body after `---`. Always include a non-empty prompt body.
- **Subagent start with a clean context.** They do not see the parent conversation history. The parent must pass all necessary context in the prompt.
- **Nesting limit.** Since Cursor 2.5, direct subagents can launch child subagents, but a child subagent cannot launch further subagents.
- **Version notes.**
  - Cursor 2.5+ introduced child subagent spawning.
  - Cursor 3.3+ added **Settings > Agents > Subagents** for model defaults.
  - Docs and behavior above reflect the state of `cursor.com/docs` and `forum.cursor.com` as of 2026-08.

## 8. Sources

- Cursor subagent documentation: https://www.cursor.com/docs/subagents
- Customize overview (subagents, MCP, rules, skills): https://www.cursor.com/docs/customize-cursor
- MCP documentation: https://www.cursor.com/docs/mcp
- Forum — MCP toolset control / no per-subagent whitelist: https://forum.cursor.com/t/cursor-subagent-mcp-toolset-control-request/159786
- Forum — parent Task `model` parameter overrides subagent frontmatter: https://forum.cursor.com/t/parent-agent-overrides-subagent-model-settings-by-explicitly-passing-model-to-task-tool-it-used-all-of-my-api-budget/162601
- Forum — `model: inherit` not respected (Grok parent → GPT subagent): https://forum.cursor.com/t/task-subagent-model-inherit-ignores-parent-grok-runs-gpt-5-5-medium/165307
- Forum — bracket syntax only works in frontmatter, not inline Task parameter: https://forum.cursor.com/t/how-do-i-spawn-a-non-fast-composer-2-5-task-subagent-only-composer-2-5-fast-is-accepted/166082
- Forum — custom subagents missing from Task enum / empty body bug: https://forum.cursor.com/t/committed-project-custom-subagents-missing-from-task-enum-invalid-enum-on-cursor-3-12-17/166135
- Forum — automation subagents losing inherited MCP tools: https://forum.cursor.com/t/automation-subagents-lost-all-inherited-mcp-tools-since-jul-31-child-run-spawns-without-parent-agent-state/167338
