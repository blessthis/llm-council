# MCP Server + Agent Registration — per host

Canonical MCP server name: **`llm-council`** (do NOT rename — all council agent files reference `mcp__llm-council__council_*`). The package ships two console scripts (Decision #15): `blessthis-llm-council` (installer) and `blessthis-llm-council-server` (the MCP server). Host configs register the dedicated server binary — `args: ["blessthis-llm-council-server"]`. `llm-council` is the server registration name.

## Registration strategy — all hosts via read→modify→write

**Universal path:** the installer does a **non-destructive merge** — it reads the ENTIRE host config file into an in-memory object, adds ONLY our `llm-council` entry into the existing `mcpServers`/`servers`/`[mcp_servers]` collection (leaving every other server entry untouched), then writes the full object back atomically (temp file + rename, with a `.bak` backup). This is a true merge, never a blind overwrite — other servers, comments, and formatting all survive. This works for **ALL six hosts**. Where the host exposes a CLI, we PREFER it (faster, host-managed); file-merge is the universal fallback and the primary path for the four non-CLI hosts.

| Host | Config file | Format | Round-trip lib | Preferred path |
|------|-------------|--------|------------------|----------------|
| Claude Code | `~/.claude.json` (user) / `.mcp.json` (project) | JSON | stdlib `json` | **CLI** `claude mcp add --scope user` |
| Gemini CLI | `~/.gemini/settings.json` (user) / `.gemini/settings.json` (project) | JSON | stdlib `json` | **CLI** `gemini mcp add -s user` |
| Cursor | `.cursor/mcp.json` (project) / `~/.cursor/mcp.json` (user) | JSON | stdlib `json` | **file-merge** (`--add-mcp` flag less clean) |
| pi | `~/.pi/agent/mcp.json` (user) / `.pi/mcp.json` (project) | JSON | stdlib `json` | **file-merge** (no `pi mcp` CLI) |
| Codex | `~/.codex/config.toml` (user) / `.codex/config.toml` (project) | TOML | **`tomlkit`** (preserves comments/formatting) | **file-merge** (no CLI; TOML-only) |
| GitHub Copilot/VS Code | `.vscode/mcp.json` (project) / user profile mcp.json | JSON | stdlib `json` | **file-merge** (no CLI; top-level key is `servers`) |

### Merge safety rules (all hosts)

**This is a merge, NOT an overwrite.** The installer never writes a fresh file and never replaces an existing collection wholesale. It also never injects non-standard keys (no `_managed_by`) — the MCP spec defines only `type`/`command`/`args`/`env` (+ `url`/`headers` for HTTP), and unknown keys risk future strict-validator rejection. Ownership is detected by content fingerprint instead (see "Ownership detection" below).

1. **Read the whole file** (or start from `{}` only if the file genuinely doesn't exist). Parse into an in-memory object — JSON → dict, TOML → `tomlkit` document.
2. **Locate the host's server collection** (the object that holds all registered servers): `mcpServers` (Claude/Cursor/pi/Gemini), `servers` (VS Code/Copilot), or the `[mcp_servers.*]` tables (Codex). Keep the object as-is.
3. **Add ONLY our key** `llm-council` into that collection (upsert): if absent → append; if present AND it matches our content fingerprint (see below) → overwrite our own previous entry; if present but does NOT match our fingerprint → STOP, warn the user, refuse to clobber a user-owned entry.
4. **Everything else is untouched** — other servers, comments (`tomlkit` preserves them), key order, formatting, unknown top-level keys.
5. **Write back atomically:** serialize the full (modified) object → write to `<file>.tmp` → `os.replace(<file>.tmp, <file>)` (atomic on same fs) → keep `<file>.bak`.
6. **Preserve file mode** (`0600` for seats.yaml; host file modes untouched).

### Ownership detection (Detect-by-content, NOT a custom key)

We detect our entries by their content fingerprint, never by injecting a non-standard key:

> **Fingerprint:** server entry where `command` is `uvx` or `uv` AND `args` contains the string `blessthis-llm-council-server`.

Rules:
- **Install (upsert):** if a matching fingerprint already exists under a DIFFERENT key name, warn and refuse (user has a manually-registered copy; don't create a duplicate).
- **Uninstall:** scan the server collection, delete every entry matching the fingerprint (regardless of key name), delete agent files we wrote (we own the `blessthis-council-*` filename prefix — see "Agent filename convention" below).
- **`doctor`:** cross-checks fingerprint presence against the optional sidecar `~/.blessthis-llm-council/installed-hosts.json` (fast status display) and reports drift. The sidecar is NOT the source of truth — content-detect is — so a user deleting our entry by hand is handled gracefully.

Agent files (`.md` / `.toml` / `.agent.md`) are written by the same installer step to the host's agent dir — pure file copy of the derived template into the directory, never touching other agents' files (we own only the `blessthis-council-*` filename prefix).

### Agent filename convention (uninstall-safety)

All our agent files use the prefix **`blessthis-council-`** — NOT the bare `council-` (too broad; an uninstall glob of `council-*` would clobber unrelated council agents from other packages/users). Concrete names:

- `blessthis-council-conductor` — the main conductor (parent orchestrator in Copilot)
- `blessthis-council-architect`, `blessthis-council-bug`, `blessthis-council-review` — per-role agents

The installer writes only files matching `blessthis-council-*`; uninstall deletes only files matching `blessthis-council-*`. The `name:` field inside frontmatter matches the filename stem. MCP server name (`llm-council`) and tool names (`council_start`, `council_poll`, …) are UNCHANGED — those are the server's exposed identifiers, not agent filenames.

---

## CLI-preferred hosts (fall back to file-merge if the CLI binary is absent)

### Claude Code

```bash
claude mcp add --scope user --transport stdio \
  --env SEATS_FILE="${HOME}/.blessthis-llm-council/seats.yaml" \
  llm-council -- uvx --from blessthis-llm-council blessthis-llm-council-server
claude mcp list && claude mcp get llm-council   # verify
```

**File-merge fallback:** edit `~/.claude.json` → merge `mcpServers.llm-council` entry (or `projects.<cwd>.mcpServers.llm-council` for local scope). Same JSON shape the CLI writes:

```json
{ "mcpServers": { "llm-council": { "type": "stdio", "command": "uvx",
  "args": ["blessthis-llm-council-server"], "env": { "SEATS_FILE": "..." } } } }
```

Agent files → `~/.claude/agents/*.md` (TitleCase `tools:`, `mcp__llm-council__*`).
**Remove:** `claude mcp remove llm-council` (or delete the JSON entry) + delete `~/.claude/agents/blessthis-council-*.md`.

### Gemini CLI

```bash
gemini mcp add -s user -e SEATS_FILE="${HOME}/.blessthis-llm-council/seats.yaml" \
  llm-council uvx --from blessthis-llm-council blessthis-llm-council-server
gemini mcp list   # verify
```

**File-merge fallback:** edit `~/.gemini/settings.json` → merge `mcpServers.llm-council`.

Agent files → `~/.gemini/agents/*.gemini.md` (YAML `tools:` list, `mcp_llm-council_council_*` single-underscore).
**Remove:** `gemini mcp remove -s user llm-council` (or delete the JSON entry) + delete agent files.

---

## File-merge-only hosts (no CLI — installer edits config directly)

### Cursor

Installer writes (or merges into) `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (user):

```json
{
  "mcpServers": {
    "llm-council": {
      "type": "stdio",
      "command": "uvx",
      "args": ["blessthis-llm-council-server"],
      "env": { "SEATS_FILE": "${HOME}/.blessthis-llm-council/seats.yaml" }
    }
  }
}
```

Agent files → `.cursor/agents/*.cursor.md` (NO `tools:` key — Cursor inherits MCP globally; tools auto-available).
**Remove:** delete the `llm-council` entry from the JSON + delete `.cursor/agents/blessthis-council-*.cursor.md`.

### pi

No `pi mcp` CLI exists. Installer edits `~/.pi/agent/mcp.json` (user) or `.pi/mcp.json` (project) — merge `mcpServers.llm-council`, preserve any existing servers. pi-specific keys `lifecycle: lazy-keep-alive` and `toolPrefix: mcp` are written by the installer:

```json
{
  "mcpServers": {
    "llm-council": {
      "command": "uvx",
      "args": ["blessthis-llm-council-server"],
      "env": { "SEATS_FILE": "${HOME}/.blessthis-llm-council/seats.yaml" },
      "lifecycle": "lazy-keep-alive",
      "toolPrefix": "mcp"
    }
  }
}
```

User runs `/reload` in a live pi session to pick up changes. Agent files → `~/.pi/agent/agents/*.md` (canonical lowercase `tools:`, `mcp__llm-council__*`).
**Remove:** delete the `mcpServers.llm-council` entry + `~/.pi/agent/agents/blessthis-council-*.md`, then `/reload`.

### Codex (TOML — uses `tomlkit` for round-trip)

Installer edits `~/.codex/config.toml` (user) or `.codex/config.toml` (project). **`tomlkit`** (not `tomllib`, which is read-only and drops comments) preserves comments, key order, and formatting on write. Merge the `[mcp_servers."llm-council"]` table:

```toml
[mcp_servers."llm-council"]
command = "uvx"
args    = ["blessthis-llm-council-server"]
env     = { SEATS_FILE = "/home/YOU/.blessthis-llm-council/seats.yaml" }
enabled = true
```

> ⚠️ The server name MUST be exactly `llm-council`. Codex agents reference it by this id; renaming breaks tool calls. `mcp_servers` CAN live in project-local `.codex/config.toml` (not in the ignored-keys list).

Agent files → `~/.codex/agents/*.codex.toml` (Codex has no per-agent tool whitelist — role files carry `description` + `developer_instructions` only; tool access is global via sandbox/app approval modes in the same `config.toml`).
**Remove:** installer deletes the `[mcp_servers."llm-council"]` table (via `tomlkit`, preserving the rest) + `~/.codex/agents/blessthis-council-*.toml`.

### GitHub Copilot / VS Code (JSON — top-level key is `servers`, not `mcpServers`)

Installer writes (or merges into) `.vscode/mcp.json` (project):

```json
{
  "servers": {
    "llm-council": {
      "type": "stdio",
      "command": "uvx",
      "args": ["blessthis-llm-council-server"],
      "env": { "SEATS_FILE": "/home/YOU/.blessthis-llm-council/seats.yaml" }
    }
  }
}
```

> ⚠️ Name MUST be exactly `llm-council`. VS Code prompts to trust the server on first load — user accepts once. (Cloud/GitHub.com Copilot agent cannot run a local stdio server — local VS Code only; that remote case needs a deployed URL configured via repo Settings web UI, out of scope for the installer.)

Agent files → `.github/agents/*.agent.md` (Copilot uses `server/tool` slash format in `tools:` array — encoded as `llm-council/council_*`). A parent orchestrator `.agent.md` with `tools: [agent]` + `agents: [blessthis-council-conductor]` is also written so the sub-agents are invocable.
**Remove:** delete the `.vscode/mcp.json` `servers.llm-council` entry + `.github/agents/blessthis-council-*.agent.md`.

---

## Canonical server name enforcement

Every agent template (see `docs/agent-templates/INDEX.md`) hardcodes the MCP server name `llm-council`. The installer writes/validates this name on every merge. If a user has manually registered a different name, the installer warns and refuses to silently rename user-owned entries.

| Host | MCP tool reference in agent `tools:` |
|------|---------------------------------------|
| pi | `mcp__llm-council__council_start` |
| Claude Code | `mcp__llm-council__council_start` |
| Codex | (global — no per-agent ref; server id `llm-council`) |
| Cursor | (global inherit — no per-agent ref; server id `llm-council`) |
| Copilot | `llm-council/council_start` |
| Gemini | `mcp_llm-council_council_start` (single underscore) |

## Dependencies

- `tomlkit` — TOML round-trip for Codex `config.toml` (preserves comments/formatting). Added to `[project.dependencies]`.
- stdlib `json` — all JSON hosts (Claude/Gemini/Cursor/pi/Copilot).

## Sources

- Claude Code: `claude mcp add --help`; https://docs.anthropic.com/en/docs/claude-code/mcp
- pi: `/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/docs/`; `~/.pi/agent/mcp.json`
- Codex: `docs/research/codex-config-ref.md` (§`mcp_servers`); https://learn.chatgpt.com/docs/config-file/config-reference
- Cursor: https://cursor.com/docs/mcp; `cursor --help` (`--add-mcp` flag)
- Copilot: https://code.visualstudio.com/docs/copilot/customization/mcp-servers; https://code.visualstudio.com/docs/agents/reference/mcp-configuration
- Gemini: `gemini mcp --help`; github.com/google-gemini/gemini-cli docs
