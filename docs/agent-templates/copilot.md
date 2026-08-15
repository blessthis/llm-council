# GitHub Copilot Subagent Template Format

Canonical template for repo-level GitHub Copilot custom agents that can spawn (and be spawned as) context-isolated subagents. Validated against current GitHub Docs and VS Code Copilot docs (see Sources at the end).

> **Scope note:** The `agents:` whitelist and subagent mechanics documented here are implemented in VS Code / IDE Copilot custom agents. GitHub.com Copilot cloud agent currently documents the `agent` tool alias but does **not** expose the `agents` frontmatter key in its reference schema.

---

## 1. File paths

| Scope | Path | Notes |
|-------|------|-------|
| **Repo-level (workspace)** | `.github/agents/<agent-name>.agent.md` | The canonical location. Cloud agent and VS Code both read this path. VS Code also detects any `.md` file in `.github/agents`. |
| **User profile** | `~/.copilot/agents/<agent-name>.agent.md` | Available across all your workspaces. |
| **Claude-format fallback** | `.claude/agents/<agent-name>.agent.md` | VS Code recognizes this path for interoperability; prefer `.github/agents` for Copilot. |

The file name (without `.md` or `.agent.md`) is the default agent identifier if the `name` frontmatter key is omitted.

---

## 2. Full frontmatter schema

```yaml
---
# Display name. Optional; defaults to the file name with .agent.md/.md stripped.
name: string

# Required. One-line description of what this agent does.
description: string

# Target environment. Optional; omit for both.
# Values: "vscode" | "github-copilot"
target: vscode

# Tool whitelist. Optional. Omit = all tools. [] = no tools.
# Can be a YAML array or a comma-separated string.
tools: [string]

# AI model. Optional.
# In VS Code this may be a single string or a prioritized array.
model: string | [string]

# Deprecated. Use disable-model-invocation + user-invocable instead.
infer: boolean

# When true, other agents cannot invoke this agent as a subagent. Default false.
disable-model-invocation: boolean

# When false, the agent does not appear in the user chat dropdown.
# Set false for subagents that should only be invoked programmatically. Default true.
user-invocable: boolean

# Subagent whitelist. IDE / VS Code. Optional.
# [] = no subagents; "*" = all agents; ["name", ...] = named agents.
agents: [string] | "*" | []

# MCP server configuration. Cloud agent only; ignored in VS Code IDE.
mcp-servers:
  server-name:
    type: local        # stdio is mapped to local for cloud agent compatibility
    command: string
    args: [string]
    tools: ["*"]       # optional server-level tool filter
    env:
      KEY: ${{ secrets.COPILOT_SECRET }}

# Metadata annotations. Cloud agent only; ignored in IDE.
metadata:
  key: value

# Input hint shown in the chat box. IDE only; ignored by cloud agent.
argument-hint: string

# Suggested next-agent buttons after a response. IDE only; ignored by cloud agent.
handoffs:
  - label: Start implementation
    agent: implementer
    prompt: Now implement the plan above.
    send: false
    model: GPT-5.2 (copilot)

# Preview. Agent-scoped hook commands. IDE only.
hooks:
  ...
---
```

The Markdown body below the frontmatter is the agent prompt. It is prepended to the user prompt in chat. Maximum prompt length: **30,000 characters**.

---

## 3. MCP tool access

MCP tools are referenced by **server-name / tool-name** in the `tools:` array.

```yaml
tools:
  # Single tool from an MCP server
  - my-server/my-tool
  # All tools from an MCP server
  - my-server/*
  # Built-in aliases
  - read
  - edit
  - search
  - execute
  - agent
```

In VS Code, MCP servers must be configured in the workspace or user settings first (for example, via `.vscode/mcp.json` or the **MCP** settings). In GitHub Copilot cloud agent, you can define them inline under `mcp-servers:` or rely on repository-level MCP server configuration.

---

## 4. Tool whitelisting

The `tools:` frontmatter key is a **filter**: only listed tools are available to the agent.

| Value | Meaning |
|-------|---------|
| omitted | All available tools (built-in + MCP + extension). |
| `[]` | No tools at all. |
| `["*"]` | All available tools (explicit). |
| `["read", "edit", ...]` | Only those tools/aliases are exposed. |

To allow an agent to spawn subagents, you **must** include the `agent` tool in its `tools:` list.

```yaml
# Parent orchestrator that can hand off work to other agents
tools: ["agent"]
```

The `agents:` frontmatter key is the parent-side whitelist. It accepts:

```yaml
agents: []                    # forbid any subagent invocation
agents: ["*"]                # allow any available agent (default behavior)
agents: ["my-agent"] # allow only the named agent(s)
```

Names in `agents:` are **case-sensitive** and must match the target agent’s `name:` frontmatter value (or its file name if `name` is omitted).

A special behavior documented by VS Code: if an agent is explicitly listed in a parent’s `agents:` array, it can be invoked even when the target agent has `disable-model-invocation: true`. This lets you create "protected" subagents that are only callable from designated coordinators.

### Filtering workaround: `tools: null`

Some IDE builds apply the `tools:` filter too strictly and only recognize built-in aliases, silently dropping MCP/extension tools even when they are correctly prefixed. A community workaround is to set:

```yaml
tools: null
```

This is **not** documented in the official schema, but it can act as a "disable filtering" fallback, exposing every available tool. Prefer `tools: ["*"]` when it works, and use `tools: null` only for debugging or when a confirmed filtering bug blocks an MCP tool.

---

## 5. Concrete example

### 5a. Sub-agent: `.github/agents/my-agent.agent.md`

This worker agent can read, edit, search, run shell commands, and call tools from a configured MCP server. It is marked `user-invocable: false` so it is only usable as a subagent.

```markdown
---
name: my-agent
description: |
  <one-line description of what this worker agent does>
tools:
  - read
  - edit
  - search
  - execute
  - my-server/my-tool       # a single MCP tool
  - my-server/*             # all tools from an MCP server
model: Claude Sonnet 4.5 (copilot)
user-invocable: false
# Only meaningful in VS Code; cloud agent ignores this key.
target: vscode
---

<Agent body / system instructions go here. This markdown is prepended to the
user prompt in chat. Describe the agent's role, inputs, outputs, and any
constraints. Maximum 30,000 characters total (frontmatter + body).>
```

> **MCP wiring note:** Replace `my-server/my-tool` with your own MCP server name and tool. In VS Code, the MCP server must already be configured in workspace/user settings (e.g. `.vscode/mcp.json`). In GitHub Copilot cloud agent, you can add an `mcp-servers:` block to the frontmatter instead (see Section 2).

### 5b. Parent orchestrator: `.github/agents/my-orchestrator.agent.md`

This coordinator only needs the `agent` tool and permission to invoke the `my-agent` subagent.

```markdown
---
name: my-orchestrator
description: |
  <one-line description of what this orchestrator does>
tools:
  - agent
agents:
  - my-agent
model: GPT-5.2 (copilot)
user-invocable: true
target: vscode
---

<Orchestrator body / system instructions go here. Describe how the
orchestrator should decompose work, which subagents to invoke, what context
to pass, and how to assemble the subagents' results into a final answer.>
```

To use it, place both files in `.github/agents/`, select the orchestrator agent in Copilot chat, and ask for a task. The orchestrator will spawn the `my-agent` subagent in its own isolated context window.

---

## 6. Transformation from pi canonical

When moving from the `pi` coding-agent harness to Copilot custom agents, use this mapping.

| pi tool / concept | Copilot equivalent | Notes |
|-------------------|--------------------|-------|
| `read` | `read` | Maps to the `view` tool under the hood. |
| `grep`, `find`, `ls` | `search` | The `search` alias covers `Grep` and `Glob`. Use it for all file discovery. |
| `bash` | `execute` | Also accepts `shell`, `Bash`, or `powershell` aliases. |
| `edit` (or `write`) | `edit` | `Write`, `MultiEdit`, and `NotebookEdit` are compatible aliases. |
| `agent` / subtask | `agent` + `agents:` | Include `agent` in `tools:` and list allowed subagents in `agents:`. |
| Isolated context window | Built-in subagent | Each subagent gets its own context; parent only sees the returned summary. |

So the pi harness tool set `{read, grep, find, ls, bash}` becomes:

```yaml
tools:
  - read
  - search
  - execute
```

If the agent also needs to edit, add `edit` to that list.

---

## 7. Gotchas

- **Cloud vs. IDE variance:**
  - `mcp-servers:` is processed by **GitHub Copilot cloud agent** but is **ignored** in VS Code (MCP servers are configured in IDE settings there).
  - `agents:`, `handoffs:`, `argument-hint:`, and `model:` as an array are **IDE features**; cloud agent ignores them.
  - `target: vscode` keeps the file from being misinterpreted as a cloud agent profile.

- **The `agent` tool is required for subagent spawning:** A parent orchestrator must list `agent` in `tools:`; merely having `agents:` is not enough.

- **Agent names are case-sensitive:** Match the exact `name:` value from the target `.agent.md` file.

- **Subagents are stateless:** Each invocation is a fresh context window. Pass all relevant context, expected output format, and constraints in the subagent prompt.

- **Nested subagents are off by default in VS Code:** Enable `chat.subagents.allowInvocationsFromSubagents` if a subagent needs to spawn further subagents. Maximum nesting depth is 5.

- **`tools:` filtering bug:** If a whitelisted MCP or extension tool is silently unavailable, try `tools: ["*"]` first. If the filter still rejects non-built-in names, the community workaround `tools: null` can disable the filter entirely. It is not officially documented, so test it and remove it once the underlying tool is correctly registered.

- **Prompt length limit:** The body plus frontmatter must stay under 30,000 characters. Offload long reference material into linked files and use the `read` tool to load them.

- **All unrecognized tool names are ignored:** This lets you use product-specific aliases without breaking the profile in a different client, but it also means typos silently disable tools.

---

## 8. Sources

- [Creating custom agents for Copilot cloud agent in your IDE](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/create-custom-agents-in-your-ide) — GitHub Docs
- [Custom agents configuration reference](https://docs.github.com/en/copilot/reference/custom-agents-configuration) — GitHub Docs (frontmatter, tools, aliases, MCP, processing rules)
- [Custom agents in VS Code](https://code.visualstudio.com/docs/copilot/customization/custom-agents) — VS Code docs (file structure, `agents:` property, `handoffs`, orchestration examples)
- [Subagents in Visual Studio Code](https://code.visualstudio.com/docs/agents/run/subagents) — VS Code docs (invocation, `agents:` whitelist, nested subagents, model selection)
- [Awesome Copilot agents collection](https://github.com/github/awesome-copilot/tree/main/agents) — Community examples
