# Gemini CLI Subagent Template Format

Canonical template for Gemini CLI custom subagents. Validated against the Gemini CLI source (`packages/core/src/agents`) and the official docs.

> **Status:** The public docs use `mcpServers` in the frontmatter example, but the parser expects **snake_case** (`mcp_servers`). This template follows the parser.

---

## 1. File paths

| Scope | Path |
|-------|------|
| **User-level (personal)** | `~/.gemini/agents/<agent-name>.md` |
| **Project-level (workspace)** | `.gemini/agents/<agent-name>.md` |

Discovery order: built-in agents → user agents → project agents. Higher-precedence definitions override lower-precedence ones with the same `name`.

The file name is **not** the agent identifier; the `name:` frontmatter field is.

---

## 2. Full frontmatter schema

```yaml
---
# Required. Slug: lowercase letters, numbers, hyphens, underscores.
name: string

# Required. One-line description used by the orchestrator for automatic routing.
description: string

# Optional. "local" (default) or "remote" (A2A / Agent2Agent).
kind: local

# Optional. Display name for UI / slash-command listings.
display_name: string

# Optional. Strict tool whitelist. Omit = inherit all tools from the parent session.
# See Section 4 for wildcard rules.
tools:
  - read_file
  - write_file

# Optional. Inline MCP servers isolated to this agent (snake_case key).
mcp_servers:
  my-server:
    command: string
    args: [string]
    env:
      KEY: value
    cwd: string
    url: string          # SSE / HTTP endpoint
    http_url: string     # deprecated alias for url + type
    headers:
      X-Key: value
    tcp: string
    type: sse | http
    timeout: number      # seconds
    trust: boolean
    description: string
    include_tools: [string]
    exclude_tools: [string]
    auth:
      type: google-credentials | oauth
      ...

# Optional. Model to use. Omit = inherit the parent session model.
model: string

# Optional. Sampling temperature. 0.0 - 2.0, default 1.
temperature: number

# Optional. Max conversation turns before forced return. Positive int, default 30.
max_turns: number

# Optional. Max execution time in minutes. Positive int, default 10.
timeout_mins: number
---
```

### Built-in tool names (valid values for `tools:`)

These names are accepted verbatim in the `tools:` array.

| Tool name | Purpose |
|-----------|---------|
| `read_file` | Read a file (text, images, audio, PDF). Supports line ranges. |
| `write_file` | Write or overwrite a file. |
| `replace` | Targeted text replacement / edit within a file. |
| `grep_search` | Search file contents by regex (max 100 matches by default). |
| `glob` | Find files by glob pattern. |
| `list_directory` | List a directory. |
| `read_many_files` | Read multiple files in one call. |
| `run_shell_command` | Execute a shell command. |
| `google_web_search` | Google web search with citations. |
| `web_fetch` | Fetch and summarize a URL. |
| `write_todos` | Manage a todo list. |
| `get_internal_docs` | Read Gemini CLI internal documentation. |
| `ask_user` | Ask the user a question. |
| `activate_skill` | Load an agent skill by name. |
| `enter_plan_mode` | Enter plan mode. |
| `exit_plan_mode` | Exit plan mode. |
| `update_topic` | Update the current topic context. |
| `complete_task` | Mark the current task complete. |
| `read_mcp_resource` | Read an MCP resource. |
| `list_mcp_resources` | List available MCP resources. |
| `invoke_agent` | Built-in subagent delegator. **Subagents cannot invoke other agents** (recursion protection), so listing this is effectively a no-op for a subagent. |
| `tracker_create_task` | Create a task-tracker task. |
| `tracker_update_task` | Update a task-tracker task. |
| `tracker_get_task` | Get a task-tracker task. |
| `tracker_list_tasks` | List task-tracker tasks. |
| `tracker_add_dependency` | Add a dependency between tracker tasks. |
| `tracker_visualize` | Visualize tracker tasks. |

**Legacy alias:** `search_file_content` is accepted and maps to `grep_search`.

**Discovered tools:** any name starting with `discovered_tool_` is valid.

---

## 3. MCP tool access

Gemini CLI exposes every MCP tool as a **single-underscore** fully-qualified name:

```text
mcp_<server-name>_<tool-name>
```

### Referencing MCP tools in the `tools:` list

- **Single tool:** `mcp_github_list_pull_requests`
- **All tools from one server:** `mcp_github_*`
- **All MCP tools everywhere:** `mcp_*`

### Inline MCP server configuration

Add the server under the **snake_case** `mcp_servers:` frontmatter key:

```yaml
mcp_servers:
  github:
    command: docker
    args:
      - run
      - -i
      - --rm
      - ghcr.io/github/github-mcp-server:latest
```

> **Do not use `mcpServers:` in the agent markdown frontmatter.** `mcpServers:` is the key used in `~/.gemini/settings.json`; the agent frontmatter parser requires `mcp_servers:`.

If the MCP server is already configured in `settings.json`, you only need to whitelist its tools in `tools:`.

---

## 4. Tool whitelisting

The `tools:` array is **strictly enforced**. If a tool name is not listed (and no wildcard covers it), the subagent cannot see or call it.

| Value | Meaning |
|-------|---------|
| omitted | Inherit all tools from the parent session (built-in + MCP + discovered). |
| `[]` | No tools at all. |
| `["*"]` | All available built-in and discovered tools. |
| `["read_file", "mcp_github_*"]` | Only those specific tools / wildcards. |

### Wildcard rules

- `*` — every built-in and discovered tool.
- `mcp_*` — every MCP tool from every connected server.
- `mcp_<server>_*` — every tool from the named MCP server (e.g., `mcp_github_*`).

### Recursion protection

Subagents run in an isolated loop and **cannot invoke other agents**, even if `*` or `invoke_agent` is in their `tools:` list.

---

## 5. Concrete example

### `.gemini/agents/my-agent.md`

```markdown
---
name: my-agent
description: <what this agent does>
kind: local
tools:
  # Built-in filesystem / shell / research tools
  - read_file
  - write_file
  - replace
  - grep_search
  - glob
  - list_directory
  - run_shell_command
  - google_web_search
  - web_fetch
  # MCP tools from a configured server (explicit names)
  - mcp_<your-server>_<your-tool>
  - mcp_<your-server>_<other-tool>
  # Or, equivalently, whitelist every tool from that server:
  # - mcp_<your-server>_*
temperature: 0.3
max_turns: 25
timeout_mins: 10
---

<agent body>
```

> **MCP wiring note:** This example assumes the MCP server is already configured in `~/.gemini/settings.json` or in the project `.gemini/settings.json`. If it is not, add an `mcp_servers:` block to this file.

### Invoking the subagent

- **Automatic:** the main agent routes tasks matching the description to `my-agent`.
- **Explicit:** start your prompt with `@my-agent ...`.
- **List agents:** use the `/agents` slash command in an interactive session.

---

## 6. Transformation from pi canonical

When moving from the `pi` coding-agent harness to Gemini CLI subagents, use this mapping.

| pi tool / concept | Gemini CLI equivalent | Notes |
|-------------------|----------------------|-------|
| `read` | `read_file` | Exact match. |
| `write` | `write_file` | Exact match. |
| `edit` | `replace` | Gemini's edit tool is named `replace`, not `edit_file`. |
| `grep` / `search` | `grep_search` | `search_file_content` is also accepted as a legacy alias. |
| `find` (glob) | `glob` | Exact match. |
| `ls` | `list_directory` | Exact match. |
| `bash` | `run_shell_command` | Exact match. |
| `web_search` | `google_web_search` | Must use the canonical name. |
| `ask_user` | `ask_user` | Exact match. |
| Subagent spawn | `invoke_agent` / `@agent-name` | Subagents cannot recursively invoke other agents. |
| MCP tool name | `mcp_<server>_<tool>` | **Single** underscore separator. Pi often uses `mcp__<server>__<tool>` with double underscores; Gemini does not. |

So the pi harness tool set `{read, write, edit, grep, find, ls, bash}` becomes:

```yaml
tools:
  - read_file
  - write_file
  - replace
  - grep_search
  - glob
  - list_directory
  - run_shell_command
```

Add `google_web_search` and `web_fetch` if the agent needs to research.

---

## 7. Gotchas

- **Edit tool is `replace`.** Do not use `edit_file` in `tools:`.
- **Search tool is `grep_search`.** `search_file_content` is a legacy alias only.
- **Shell tool is `run_shell_command`, not `bash`.**
- **Web search is `google_web_search`, not `web_search`.**
- **MCP names use single underscores:** `mcp_<server>_<tool>` (e.g., `mcp_github_list_pull_requests`). Double-underscore `mcp__` naming is not supported.
- **`mcp_servers:` is snake_case in agent markdown.** `mcpServers:` is for `settings.json` only.
- **`tools:` is strictly enforced.** If you omit a needed tool or misspell it, the agent will not have access.
- **Omitting `tools:` grants inheritance, not zero tools.** Use `[]` if you truly want no tools.
- **Subagents cannot call other subagents**, even with `*` in `tools:`.
- **`max_turns` (not `max_turn`)** is the frontmatter key.
- **Name regex:** only lowercase letters, numbers, `-`, and `_`.
- **Model value:** any model string accepted by the CLI; omit to inherit from the parent session.

---

## 8. Sources

- [Gemini CLI — Subagents](https://github.com/google-gemini/gemini-cli/blob/main/docs/core/subagents.md) — official docs (paths, invocation, schema).
- [Gemini CLI — MCP setup](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/tutorials/mcp-setup.md) — MCP server config and fully-qualified tool names.
- [`packages/core/src/agents/agentLoader.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/agents/agentLoader.ts) — frontmatter schema and parser (confirms `mcp_servers`, `tools`, `max_turns`, etc.).
- [`packages/core/src/tools/tool-names.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/tool-names.ts) — canonical built-in tool names and validation rules.
- [`packages/core/src/tools/mcp-tool.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/mcp-tool.ts) — MCP tool naming convention (`mcp_server_tool`).
- [`tools/gemini-cli-bot/.gemini/agents/WORKER.md`](https://github.com/google-gemini/gemini-cli/blob/main/tools/gemini-cli-bot/.gemini/agents/WORKER.md) — real-world project-level agent example.
