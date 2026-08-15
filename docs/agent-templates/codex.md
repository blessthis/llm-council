# Codex (OpenAI) Subagent Template Format

Canonical template for custom Codex subagents. Validated against the official OpenAI Codex configuration reference and subagent docs (see Sources).

> **Scope note:** This covers the **local Codex clients** (CLI, desktop app, IDE extension). ChatGPT Work runs subagents in a hosted environment and does not expose local `~/.codex/` agent files or the Codex sandbox.

---

## 1. File paths

| Scope | Declaration style | Path | Notes |
|-------|-------------------|------|-------|
| **User-level** | Standalone TOML | `~/.codex/agents/<agent-name>.toml` | One agent per file. Loaded for every trusted project. |
| **Project-level** | Standalone TOML | `.codex/agents/<agent-name>.toml` | Only loaded when the project is **trusted**. |
| **User-level** | Config table | `~/.codex/config.toml` — section `[agents.<name>]` | Global agent registry and defaults. |
| **Project-level** | Config table | `.codex/config.toml` — section `[agents.<name>]` | Only loaded when the project is trusted. Cannot override machine-local provider/auth/telemetry keys (see below). |

- Project-scoped `.codex/config.toml` **ignores** provider, auth, notification, profile, and telemetry keys: `openai_base_url`, `chatgpt_base_url`, `apps_mcp_product_sku`, `model_provider`, `model_providers`, `notify`, `profile`, `profiles`, `experimental_realtime_ws_base_url`, and `otel`.
- Profile files live next to `config.toml` as `$CODEX_HOME/profile-name.config.toml` and are selected with `--profile profile-name`.
- In a standalone agent file, the `name` field is the source of truth; matching the filename to the name is the simplest convention.

---

## 2. Full schema

### Style A — Standalone `.codex/agents/<name>.toml` file

Required keys (the file is invalid without these):

| Key | Type | Required | Purpose |
|-----|------|----------|---------|
| `name` | `string` | **Yes** | Agent identifier used when spawning or referring to this agent. Overrides a built-in agent if the names collide. |
| `description` | `string` | **Yes** | Human-facing guidance that tells Codex (and the parent model) when to delegate to this agent. |
| `developer_instructions` | `string` | **Yes** | Core behavior prompt / system instructions for the agent. |

Allowed additional keys (the agent file is a full Codex session config layer, so any top-level `config.toml` key may be used):

| Key | Type | Notes |
|-----|------|-------|
| `model` | `string` | e.g. `gpt-5.6`, `gpt-5.6-terra`, `gpt-5.6-luna`. If set, takes precedence over `[agents]` defaults and parent value. |
| `model_reasoning_effort` | `string` | `minimal` / `low` / `medium` / `high` / `xhigh` / `max` / `ultra` (model-dependent). |
| `model_provider` | `string` | Provider ID from `model_providers`. |
| `sandbox_mode` | `string` | `read-only` / `workspace-write` / `danger-full-access`. Controls filesystem/network access. |
| `default_permissions` | `string` | Built-in profile: `:read-only`, `:workspace`, `:danger-full-access`; or a custom `[permissions.<name>]` profile. |
| `approval_policy` | `string` or table | `untrusted` / `on-request` / `never` / granular table. |
| `mcp_servers` | table | Per-agent MCP server registration (see §3). |
| `skills.config` | array of tables | Per-skill enablement overrides. |
| `web_search` | `string` | `disabled` / `cached` / `indexed` / `live`. |
| `features.*` | various | Any feature flag, e.g. `features.multi_agent`. |
| ... | ... | Any other documented top-level `config.toml` key. |

### Style B — `[agents.<name>]` table in `config.toml`

The `[agents]` table is reserved; scalar setting names cannot be used as custom role names. Reserved names: `enabled`, `max_concurrent_threads_per_session`, `max_threads`, `default_subagent_model`, `default_subagent_reasoning_effort`, `interrupt_message`.

Global `[agents]` settings:

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `agents.enabled` | `boolean` | `true` | Enable or disable multi-agent tools (`spawn_agent`, `send_input`, `resume_agent`, `wait_agent`, `close_agent`). |
| `agents.max_concurrent_threads_per_session` | `number` | — | Cap on concurrently open spawned-agent threads, excluding the primary thread. |
| `agents.max_threads` | `number` | — | Legacy alias for `agents.max_concurrent_threads_per_session`. |
| `agents.default_subagent_model` | `string` | — | Default model for spawned agents. Explicit spawn value takes precedence. |
| `agents.default_subagent_reasoning_effort` | `string` | — | Default reasoning effort for spawned agents. Explicit spawn value takes precedence. |
| `agents.interrupt_message` | `boolean` | `true` | Record a model-visible message when an agent turn is interrupted. |

Per-role `[agents.<name>]` settings:

| Key | Type | Required | Purpose |
|-----|------|----------|---------|
| `agents.<name>.description` | `string` | Yes | Role guidance shown to Codex when choosing and spawning that agent type. |
| `agents.<name>.config_file` | `string` (path) | No | Path to a TOML config layer for that role. Relative paths resolve from the config file that declares the role. This is how you point an inline table at a standalone Style A file. |

---

## 3. MCP tool access

Codex registers MCP servers in `config.toml` or in an agent file under `[mcp_servers.<id>]`. A child subagent inherits MCP servers from its parent configuration layer; if the server is defined in the custom agent file, it is applied to the child session.

### `[mcp_servers.<id>]` schema (relevant subset)

| Key | Type | Purpose |
|-----|------|---------|
| `mcp_servers.<id>.command` | `string` | Launcher command for a stdio server. |
| `mcp_servers.<id>.args` | `array<string>` | Arguments passed to the command. |
| `mcp_servers.<id>.env` | `map<string,string>` | Environment variables forwarded to the server. |
| `mcp_servers.<id>.env_vars` | array of strings or `{ name, source }` | Extra env vars to whitelist (`source = "local"` or `"remote"`). |
| `mcp_servers.<id>.cwd` | `string` | Working directory for the server process. |
| `mcp_servers.<id>.url` | `string` | Endpoint for a streamable HTTP server. |
| `mcp_servers.<id>.auth` | `"oauth"` / `"chatgpt"` | Authentication fallback for HTTP servers. |
| `mcp_servers.<id>.bearer_token_env_var` | `string` | Env var sourcing the bearer token. |
| `mcp_servers.<id>.http_headers` | `map<string,string>` | Static HTTP headers. |
| `mcp_servers.<id>.enabled` | `boolean` | Disable without removing config. |
| `mcp_servers.<id>.required` | `boolean` | Fail startup if this enabled server cannot initialize. |
| `mcp_servers.<id>.startup_timeout_sec` | `number` | Default 10s. |
| `mcp_servers.<id>.tool_timeout_sec` | `number` | Default 60s per tool call. |
| `mcp_servers.<id>.enabled_tools` | `array<string>` | Allow list of tool names from this server. |
| `mcp_servers.<id>.disabled_tools` | `array<string>` | Deny list applied after `enabled_tools`. |
| `mcp_servers.<id>.default_tools_approval_mode` | `"auto"` / `"prompt"` / `"writes"` / `"approve"` | Default approval behavior for this server's tools. |
| `mcp_servers.<id>.tools.<tool>.approval_mode` | `"auto"` / `"prompt"` / `"writes"` / `"approve"` | Per-tool approval override. |

In an agent file, invoke MCP tools by their plain tool names (no `mcp__server__tool` prefix). If a server is defined in the parent `config.toml`, the child sees it automatically. If it is only defined in the child agent file, that child gets the server when spawned.

---

## 4. Tool whitelisting

### Built-in Codex tools (read, write, edit, grep/glob, shell, etc.)

**There is no per-agent `tools:` whitelist.** Tool access is controlled globally by the active sandbox / permission mode:

| Control | What it does |
|---------|--------------|
| `sandbox_mode = "read-only"` | Allows reads; blocks writes, destructive shell commands, and network access. |
| `sandbox_mode = "workspace-write"` | Allows reads/writes within the workspace roots; destructive commands still require approval depending on `approval_policy`. |
| `sandbox_mode = "danger-full-access"` | Broad access; use only in tightly controlled environments. |
| `default_permissions` / `[permissions.<name>]` | Named permission profiles (beta) that grant explicit filesystem/network rules. |
| `approval_policy` | `untrusted` / `on-request` / `never` / granular table. Controls when Codex pauses for approval. |

Subagents **inherit** the parent session's live sandbox/approval choices, including runtime overrides such as `/permissions` changes or `--yolo`, even if the custom agent file sets different defaults.

### MCP tools

MCP tools can be restricted per **server** via `enabled_tools` / `disabled_tools` and per-tool `approval_mode`. There is no agent-level whitelist that spans both built-in and MCP tools.

---

## 5. Generic example — `my-agent`

A placeholder agent that reads the codebase, can run shell/search tools, edits files, and uses a `my-mcp-server` MCP server. Substitute your own `name`, `description`, `developer_instructions`, package, and tool list.

### 5a. Global/project agent registry (Style B — `[agents.<name>]` table)

Place this in `~/.codex/config.toml` or `.codex/config.toml`:

```toml
[agents]
max_concurrent_threads_per_session = 8

[agents.my-agent]
description = "<one-line description of when to delegate to this agent>"
config_file = ".codex/agents/my-agent.toml"
```

### 5b. Standalone agent file (Style A — `.codex/agents/my-agent.toml`)

```toml
name = "my-agent"
description = "<one-line description of when to delegate to this agent>"
model = "gpt-5.6"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"

developer_instructions = """
<agent body / system instructions go here — markdown prose, same as the
pi canonical body, copied verbatim. Describe the agent's role, the steps it
should follow, and any rules or constraints.>
"""

[mcp_servers."my-mcp-server"]
command = "uvx"
args = ["<your-mcp-package>"]
enabled = true
required = true
startup_timeout_sec = 20
enabled_tools = [
  "<tool_one>",
  "<tool_two>",
  "<tool_three>",
]
default_tools_approval_mode = "auto"
```

### 5c. MCP server registration in `config.toml` (if not inside the agent file)

```toml
[mcp_servers."my-mcp-server"]
command = "uvx"
args = ["<your-mcp-package>"]
enabled = true
required = true
startup_timeout_sec = 20
enabled_tools = [
  "<tool_one>",
  "<tool_two>",
  "<tool_three>",
]
```

### 5d. Invocation prompt

Ask the parent agent naturally:

```text
Use the my-agent subagent to <do the task>. Then apply the result with the
smallest possible code change.
```

Codex resolves the `my-agent` name to the TOML file and spawns it with the configured model, effort, sandbox, and MCP server.

> **Real example (for this repo only).** When authoring this repo's own Codex agents, substitute the server id `llm-council`, the package `blessthis-llm-council`, and the real `council_*` tool names. This generic template exists so the format reference is not coupled to a single agent's content.

---

## 6. Transformation from pi canonical

pi harness uses a lowercase `tools:` list. Codex has no equivalent per-agent list. Map like this:

| pi tool / concept | Codex equivalent | Notes |
|-------------------|------------------|-------|
| `tools:` frontmatter | **Not supported.** | Use `sandbox_mode`, `default_permissions`, and `mcp_servers.<id>.enabled_tools` instead. |
| `read` | `read` (built-in) | Available when the sandbox/permissions allow reads. |
| `write` / `edit` | `write` / `edit` (built-in) | Available when `sandbox_mode` or permissions allow writes. |
| `grep` / `glob` / `find` / `ls` | `shell` + `grep`/`glob`/`find`/`ls` | Codex exposes a shell tool and built-in search/file discovery. Use the sandbox to restrict what the shell can touch. |
| `bash` | `shell` | Same function; `bash` is the shell invocation. |
| `mcp__<server>__<tool>` | `<server>` MCP server tools | Register the server once, then invoke plain tool names (no `mcp__server__tool` prefix). |
| Subagent name | `name` field (Style A) or `[agents.<name>]` key (Style B) | Custom agent is selected by that name. |

So a pi template like:

```yaml
---
name: my-agent
description: ...
tools: [read, write, edit, grep, find, ls, bash, mcp__my-mcp-server__tool_one, ...]
---
```

becomes the TOML in §5b (or §5a + §5b). The `tools:` list is replaced by `sandbox_mode = "workspace-write"` plus the `[mcp_servers."my-mcp-server"]` block with `enabled_tools`.

---

## 7. Gotchas

- **Project-level config requires trust.** If the project is marked untrusted, `.codex/config.toml`, `.codex/agents/`, hooks, and rules are skipped.
- **No per-agent built-in tool whitelist.** If you need an agent to be read-only, set `sandbox_mode = "read-only"` (or `default_permissions = ":read-only"`).
- **Subagents inherit parent runtime overrides.** A CLI session's live `/permissions`, `--yolo`, or interactive approval choices are reapplied to the child, even if the agent file disagrees.
- **Multi-Agent V2 changes the `spawn_agent` schema.** CLI 0.137+ introduced Multi-Agent V2; the 5.6 model family (`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`) uses V2 by default. V2 defaults `hide_spawn_agent_metadata` to `true`, which can strip `model`, `reasoning_effort`, `agent_type`, and `service_tier` from the model-visible `spawn_agent` interface. Set `features.multi_agent_v2.hide_spawn_agent_metadata = false` to restore them (or use V1 via `features.multi_agent`). See openai/codex#32031 and openai/codex#31814.
- **Model eligibility varies by release.** `gpt-5.6-luna` may be tagged as Multi-Agent V1, so V2 `spawn_agent` rejects it with `Unknown model gpt-5.6-luna` while Sol/Terra work. Check the model catalog or override `multi_agent_version` if supported. See openai/codex#35097 and openai/codex#34024.
- **Custom agent selection is not always exposed in the tool schema.** In some V2 builds, `spawn_agent` only advertises `task_name` and the parent cannot reliably select a named agent via `agent_type` (openai/codex#33244, openai/codex#33314). Natural-language delegation (e.g. "spawn my-agent") is usually more reliable than tool-level `agent_type` until the schema is fully restored.
- **Spawn argument quirks.** Empty optional `message`/`items` fields can be rejected as mutually exclusive (openai/codex#37037). Non-OpenAI/custom model providers may silently drop the task payload, creating empty subagents (openai/codex#37237, openai/codex#36586).
- **Hangs at concurrency/thread limits.** `spawn_agent` can hang indefinitely while waiting for a thread slot or evicting a resident thread (openai/codex#34653, openai/codex#33777). Keep `max_concurrent_threads_per_session` bounded and be ready to abort a stuck parent turn.
- **Windows custom-agent profile bugs.** On Windows, named subagents may spawn with the default config instead of the TOML profile, ignoring `model`, `reasoning_effort`, and instructions (openai/codex#19399). Also, `SubagentStart`/`SubagentStop` hooks may not fire for project-defined custom agents (openai/codex#33097).
- **MCP tool restrictions are per-server, not per-agent.** Use `mcp_servers.<id>.enabled_tools` / `disabled_tools` to limit what an MCP server exposes.
- **Agent file `name` is the source of truth.** The filename is only convention; if `name = "my-agent"` does not match the filename, the `name` field wins.
- **Standalone agent files are config layers.** They can contain any `config.toml` key, but they do not accept an `agents` table or `[agents.<name>]` structure.

---

## 8. Sources

- Local research dump: `docs/research/codex-config-ref.md` — official Codex `config.toml` / `requirements.toml` schema.
- Local research dump: `docs/research/codex-subagents.md` — official OpenAI Codex subagents / custom agents page.
- Official docs: `https://developers.openai.com/codex/subagents.md`
- Official docs: `https://learn.chatgpt.com/docs/config-file/config-reference.md`
- Simon Willison announcement: `https://simonwillison.net/2026/Mar/16/codex-subagents/`
- OpenAI Codex releases: `https://github.com/openai/codex/releases`
- GitHub issue references used for gotchas:
  - openai/codex#32031 — Multi-Agent V2 `spawn_agent` hides model overrides and rejects the default call shape.
  - openai/codex#31814 — GPT-5.6 Sol cannot specify subagent models by default.
  - openai/codex#33244 — Custom agents cannot be selected because `spawn_agent` exposes only `task_name`.
  - openai/codex#33314 — Multi-Agent V2 full-profile application and lifecycle continuity for custom agents.
  - openai/codex#35097 — `gpt-5.6-luna` is marked as Multi-Agent V1, so V2 `spawn_agent` rejects it.
  - openai/codex#34024 — Regression: luna cannot be specified in `spawn_agent` tool model anymore.
  - openai/codex#37037 — `spawn_agent` rejects empty optional `message`/`items` fields as mutually exclusive.
  - openai/codex#37237 — `spawn_agent` silently creates empty sub-agents for non-OpenAI Responses providers.
  - openai/codex#36586 — Subagent task payload invisible to custom non-OpenAI providers.
  - openai/codex#34653 — `spawn_agent` hangs indefinitely without returning control.
  - openai/codex#33777 — Multi-Agent V2 `spawn_agent` can hang indefinitely while evicting a resident thread.
  - openai/codex#19399 — Subagent-specific TOML config no longer works on Codex Windows.
  - openai/codex#33097 — `SubagentStart`/`SubagentStop` hooks not dispatched for project-defined custom subagents on Windows.
