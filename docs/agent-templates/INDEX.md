# Agent Templates — Transformation Index

**Canonical source: `pi` format**, located in `agents/pi/blessthis-council-<role>.md`. Six host formats are supported as subagent consumers. A pi extension hook (`.pi/extensions/regen-agents/index.ts`) watches canonical edits and nudges the main agent, which spawns parallel subagents — each running the `/regen-for-host <host>` prompt-template (`.pi/prompts/regen-for-host.md`) — to regenerate the 5 non-pi derived dirs.

## Supported hosts (6)

| Host | Format file | Per-agent tools whitelist | `model:` key | MCP prefix |
|------|-------------|---------------------------|--------------|------------|
| **pi** (canonical) | `pi.md` | ✅ enforced, lowercase | omit (blank=inherit) | `mcp__server__tool` |
| **Claude Code** | `claude.md` | ✅ enforced, TitleCase | `inherit` keyword ok | `mcp__server__tool` |
| **Codex** | `codex.md` | ❌ global only (sandbox/app modes) | global default | `[mcp_servers.<id>]` in config.toml |
| **Cursor** | `cursor.md` | ❌ global inherit (only `readonly:` flag) | `inherit` or model ID | global (no prefix) |
| **GitHub Copilot** | `copilot.md` | ⚠️ partial (`tools:` array + parent `agents:` whitelist) | IDE only | `server/tool` slash |
| **Gemini CLI** | `gemini.md` | ✅ enforced, YAML list (strictest) | optional | `mcp_server_tool` single `_` |

## NOT supported

- **Windsurf** — rules/skills only, no user-defined subagent files (excluded from installer options)

## Tool name transformation matrix (pi → host)

Built-in tool name mapping. MCP tools handled per-host (see MCP prefix column above).

| pi (source) | Claude | Codex | Cursor | Copilot | Gemini |
|-------------|--------|-------|--------|---------|--------|
| `read` | `Read` | (sandbox) | (global) | `read` | `read_file` |
| `write` | `Write` | (sandbox) | (global) | `edit` | `write_file` |
| `edit` | `Edit` | (sandbox) | (global) | `edit` | `replace` |
| `grep` | `Grep` | (sandbox) | (global) | `search` | `grep_search` |
| `find` | `Glob` | (sandbox) | (global) | `search` | `glob` |
| `ls` | `Glob`/`Bash` | (sandbox) | (global) | `search` | `list_directory` |
| `bash` | `Bash` | (sandbox) | (global) | `execute` | `run_shell_command` |
| `mcp__s__t` | `mcp__s__t` | `[mcp_servers.s]` | global | `s/t` | `mcp_s_t` |

Hosts marked "(sandbox)" or "(global)" don't accept per-agent tool whitelists — the `tools:` frontmatter key is dropped entirely and tool access is controlled via host-global config.

## Directory layout (canonical + derived)

```
agents/
├── pi/                          ← CANONICAL (edited by humans)
│   └── blessthis-council-<role>.md
├── claude/                      ← GENERATED
│   └── blessthis-council-<role>.md
├── codex/                       ← GENERATED (TOML)
│   └── blessthis-council-<role>.toml
├── cursor/                      ← GENERATED
│   └── blessthis-council-<role>.md
├── copilot/                     ← GENERATED
│   ├── blessthis-council-<role>.agent.md
│   └── blessthis-council-conductor.agent.md   ← parent orchestrator (copilot only)
└── gemini/                      ← GENERATED
    └── blessthis-council-<role>.md
```

Filename = same stem as canonical (`blessthis-council-<role>`); extension per host. Directory indicates the host. The canonical `agents/pi/` files have NO host suffix — the dir name is the host.

## Regen pipeline (pi hook → main agent → subagents)

1. **Hook** (`.pi/extensions/regen-agents/index.ts`) listens to pi `tool_result` events for `edit`/`write` on `agents/pi/blessthis-council-*.md`. On match, it debounces (300ms) and sends the main agent a `pi.sendUserMessage` nudge listing the edited files.
2. **Main agent** receives the nudge and spawns 5 subagents in parallel — one per non-pi host — each with the task `Run /regen-for-host <host>`.
3. **Each subagent** is a headless pi instance; the slash-command expands `.pi/prompts/regen-for-host.md` with `$1 = <host>`, which loads the per-host transformation rule. The subagent reads `docs/agent-templates/<host>.md` (format reference) + the matrix below, rewrites every canonical file into the host dir, and verifies.
4. (Optional pre-commit validator — NOT planned for v1. The LLM regen is the only mechanism; no deterministic guard.)

The LLM (subagent) does the transformation, not a sed script — because Cursor drops `tools:` entirely, Copilot needs a parent orchestrator, and TOML/Claude-casing need real parsing. Deterministic guards only validate.

## Installer behavior

- **Subagent-capable hosts (6):** installer offers to drop the conductor `.md` (transformed) into the host's agents dir + register llm-council MCP server.
- **MCP-only hosts (Windsurf, others):** installer registers llm-council MCP server only; user invokes `council_*` tools directly from main agent.

## Sources per host

See the "Sources" section of each `<host>.md` file. Primary research artifacts in `docs/research/`:
- `codex-config-ref.md` — official Codex config reference (90KB)
- `codex-subagents.md` — official Codex subagents page
- `bmad-study/` — bmad installer reference (platform-codes.yaml, IdeManager)
