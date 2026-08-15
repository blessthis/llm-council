# pi (canonical) — Subagent Template

**pi is the canonical source format.** All other host templates are derived from this via the transformation rules in `INDEX.md`. A git hook (post-save on `agents/*.pi.md`) regenerates the 5 derived files.

## File paths

- **User-level:** `~/.pi/agent/agents/<name>.md`
- **Project-level:** `<repo>/.pi/agents/<name>.md`

## Frontmatter schema (YAML)

| Key | Type | Required | Notes |
|------|------|----------|-------|
| `name` | string | ✅ | kebab-case, must match filename stem |
| `description` | string (folded `>-` ok) | ✅ | Used by main agent for auto-routing |
| `tools` | comma-list (lowercase) | ✅ | Lowercase tool names + `mcp__<server>__<tool>` for MCP |
| `model` | string | ❌ | **OMIT = inherit** session model. pi does NOT accept `model: inherit` keyword (unlike Claude Code) — leave blank. |

### Tool name conventions

- **Built-ins:** lowercase verbs — `read, write, edit, grep, find, ls, bash` (note: pi uses `find` for glob, `ls` for list)
- **MCP:** `mcp__<server>__<tool>` — double-underscore, server and tool names verbatim
- **No** `WebSearch`/`WebFetch` — pi has no built-in web tools (use MCP if needed)

## Example (generic placeholder: `my-agent.pi.md`)

```markdown
---
name: my-agent
description: >-
  <one-paragraph description of what this agent does; used for auto-routing>
tools: read, write, edit, grep, find, ls, bash, mcp__<server>__<tool>, mcp__<server>__<other_tool>
# model omitted → inherits session-resolved model
---

<agent body / system instructions here>
```

> This is the **canonical source format reference** — `my-agent` is a placeholder documenting the file's shape. Real canonical agent files live under `agents/*.pi.md` using the mandatory project prefix; see AGENTS.md. The transformation hook derives all other host files from these.

## Tool whitelisting

**Strictly enforced.** Only listed tools are available to the subagent process. Isolated context window + isolated process.

## Transformation FROM pi (this is the source)

See `INDEX.md` — pi is row 0, all others transform from here.

## Gotchas

- `model: inherit` keyword **NOT supported** — causes parse issues. Omit the key entirely to inherit.
- `tools:` MUST be lowercase. TitleCase (`Read`) silently fails.
- MCP tools must use `mcp__` double-underscore prefix exactly.
- pi session MUST NOT use `--no-session` (needed for `council_ask` resume; session id saved to DB).

## Sources

- Live ground-truth file: `~/.pi/agent/agents/architect-council.md`
- pi docs: `/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/docs/`
