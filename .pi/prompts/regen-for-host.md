---
description: Regenerate derived agent files for a host from canonical agents/pi/ source
argument-hint: "<host>"
---

# Regenerate derived agents for host: $1

You are a transformation worker. The main agent spawned you to regenerate the derived agent files for the host **`$1`** from the canonical source in `agents/pi/`.

## Scope

- **Canonical source:** every `agents/pi/blessthis-council-*.md` file.
- **Your output dir:** `agents/$1/` (create if missing).
- **Do NOT edit `agents/pi/`** — that is the canonical source, owned by humans.
- **Do NOT edit other hosts' dirs** — other subagents handle those in parallel.

## Transformation rule for host `$1`

Read the full per-host format reference first: `docs/agent-templates/$1.md`. It documents the host's frontmatter schema, tool-name conventions, MCP prefix syntax, `model:` handling, and gotchas. Follow it EXACTLY.

For EACH `agents/pi/blessthis-council-<role>.md`:

1. **Parse** the YAML frontmatter (between `---` fences).
2. **Transform the frontmatter** per the host's format reference (`docs/agent-templates/$1.md`):
   - **`name:`** keep verbatim (it already uses the `blessthis-council-` prefix — constant across all hosts).
   - **`description:`** keep verbatim (prose is host-agnostic).
   - **`tools:`** rewrite per the host's tool-name convention. Use the transformation matrix in `docs/agent-templates/INDEX.md`. Key rules by host:
     - **claude** — TitleCase built-ins (`read`→`Read`, `find`→`Glob`, `ls`→`Glob`, `bash`→`Bash`); MCP stays `mcp__llm-council__council_*` (double underscore, unchanged).
     - **cursor** — DROP the `tools:` key entirely (Cursor subagents inherit tools globally; only `name`/`description`/`model` are valid frontmatter keys).
     - **codex** — DROP `tools:` (Codex has no per-agent tool whitelist; tools are global via sandbox/app approval modes). Output is TOML, not YAML.
     - **copilot** — convert to YAML array form `tools: [...]`; built-ins are `read`/`edit`/`search`/`execute` (see matrix); MCP becomes `llm-council/council_*` (slash form).
     - **gemini** — convert to YAML list `tools:` (one `- item` per line); built-ins are `read_file`/`write_file`/`replace`/`grep_search`/`glob`/`list_directory`/`run_shell_command`; MCP becomes `mcp_llm-council_council_*` (single underscore).
   - **`model:`** — pi omits it (inherit). Convert per host: claude → `model: inherit`; codex/cursor → drop or set per host convention (see `$1.md`); copilot → drop (IDE-only); gemini → drop (optional).
   - **Container:** YAML for claude/cursor/copilot/gemini; **TOML** for codex (keys `name = "..."`, `description = "..."`, `developer_instructions = "..."`).
3. **Body** (everything below the frontmatter) — copy VERBATIM. Do not rephrase. All hosts accept markdown bodies.
4. **Output filename** per host (same stem as canonical, host-specific extension):
   - claude → `agents/claude/blessthis-council-<role>.md`
   - cursor → `agents/cursor/blessthis-council-<role>.md`
   - gemini → `agents/gemini/blessthis-council-<role>.md`
   - copilot → `agents/copilot/blessthis-council-<role>.agent.md`
   - codex → `agents/codex/blessthis-council-<role>.toml`
5. **Header:** prepend a generated-marker comment on the FIRST line of each derived file (after the top-of-file, before frontmatter for markdown; as a TOML comment for codex):
   - markdown hosts: `<!-- GENERATED from agents/pi/blessthis-council-<role>.md — edit the pi/ source, then re-run regen-agents. Do not hand-edit. -->`
   - codex TOML: `# GENERATED from agents/pi/blessthis-council-<role>.md — edit the pi/ source, then re-run regen-agents. Do not hand-edit.`

## Copilot special case

For copilot ONLY, in addition to per-role agent files, ensure a parent orchestrator file exists at `agents/copilot/blessthis-council-conductor.agent.md` with:
- `name: blessthis-council-conductor`
- `description: <orchestrator description>`
- `tools: [agent]` (the literal `agent` tool enables sub-agent spawning)
- `agents: [blessthis-council-architect, blessthis-council-bug, blessthis-council-review]` (whitelist of invocable sub-agents — list every `blessthis-council-*` role present in `agents/copilot/`)
- body describing the orchestration role.

If `agents/pi/` does not contain a `blessthis-council-conductor.md`, derive the orchestrator from the existing role files. Keep the `agents:` whitelist in sync with whatever roles actually exist after this run.

## Verification before you finish

After writing all derived files for `$1`:
- Every `agents/pi/blessthis-council-*.md` has a corresponding file in `agents/$1/`.
- MCP tool references resolve to the host's convention (no leftover `mcp__llm-council__` in cursor/codex/copilot/gemini files; no `council_*` bare in claude).
- `name:` field in each derived file matches its filename stem.
- Each derived file carries the GENERATED marker.
- Copilot (only): the `blessthis-council-conductor.agent.md` orchestrator exists and its `agents:` array matches the roles present.

Report a one-line summary: `<host>: regenerated <N> files (<role1>, <role2>, ...)`. If a host's format reference (`docs/agent-templates/$1.md`) conflicts with anything here, STOP and report the conflict — do not guess.
