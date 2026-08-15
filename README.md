# blessthis-llm-council

**Blind multi-model councils, and direct seat chat, as a local MCP server.**

Ask one question — get N independent answers from N different LLMs, each running as a real agent CLI on your machine, each answering **anonymously** so no model can bandwagon another's take. A synthesizer merges the answers, the council scores them blind, and only then are the hats lifted. When you don't need a whole council, open a direct 1:1 chat with any seat over the same MCP connection. You bring your own models, gateways, and credentials — the package is pure orchestration.

## How it works

```
 you (via your agent host)
        │  brief
        ▼
  council_start ──► N anonymous seats (hat_1 … hat_N)
        │            each seat = one LLM family, spawned as a
        │            headless CLI subprocess (claude / pi / codex)
        ▼
  council_poll  ◄── watch seats work (live progress)
        │
        ▼
  council_answer / council_ask ──► collect answers, cross-examine
        │
        ▼
  synthesis ──► council_score (blind: hats still on)
        │
        ▼
  council_reveal ──► hats off: which model wrote what
        │
        ▼
  council_close
```

The conductor (your agent host: Claude Code, pi, Cursor, …) talks to the MCP server. The server spawns the seat CLIs, tracks per-(seat, model) health and cooldowns, and persists councils, scores, and chat sessions in a local SQLite database. No seat ever sees another seat's answer before scoring.

## How the council works: blind synthesis

The diagram above compresses the flow; here is what actually happens and *why* it is
built that way. The full step-by-step procedures live in the ready-made council
agents under [`agents/`](agents/) — start with `agents/pi/blessthis-council-architect.md`.

**Hat blindness.** Every seat gets an opaque label — `hat1`, `hat2`, … — and the
hat→model map stays hidden on the server until you call `council_reveal`. The
orchestrating agent (and the human reading its output) therefore collects,
cross-examines, and synthesizes answers *blind*: model identity cannot bias
scoring or synthesis. Scores are attached to models server-side only at reveal
time, feeding the historical per-model leaderboard (`model_scores`).

**The loop is agent-driven, not server-driven.** The MCP server is deliberately
dumb — `start` / `poll` / `answer` / `ask` / `reveal` / `score` / `close` and
nothing else. It makes no hidden LLM calls and applies no judgment. All thinking
lives in the council agent (shipped for claude/codex/cursor/copilot/gemini/pi),
which runs the loop:

```
 council_start ─► council_poll ─► council_answer ×N ─► council_ask? ─►
                  (long-poll        (fetch each hat's   (cross-examine
                   until done)       full proposal)      conflicts)

        ─► verify + synthesize ─► council_score ─► council_reveal ─► council_close
           (mechanical checks,     (1–10 per hat,     (hats off,           (records
            ONE coherent answer)    hats still on)     de-anonymized)       kept)
```

1. **Collect** all hat answers, treating every hat as equally credible until assessed.
2. **Cross-examine** conflicts with `council_ask` — probe a specific claim against a
   specific file or constraint; never nudge seats toward consensus.
3. **Verify** each proposal on four axes: requirements coverage, codebase fit,
   trade-off soundness, and a method-driven failure-path sweep — reading the real
   code, not trusting the seat's self-assessment.
4. **Synthesize ONE coherent answer**: the strongest proposal as the spine, verified
   compatible ideas grafted from the others, then an **omission audit** — every point
   raised by a non-chosen proposal is either included or explicitly excluded, never
   silently dropped.
5. **Score blind** (1–10 per hat) *before* `council_reveal`, then close out.

**Choosing the best is not a vote.** Agreement is not evidence — models share
biases, so a popular answer can be systematically wrong. The agent mechanically
verifies each option's claims against the real codebase and the stated
requirements, and labels every option **SOUND**, **FLAWED** (with the specific
violation), or **UNPROVEN** (carried forward as an explicit risk). One seat with a
verified trade-off outranks three that merely converge. Where a load-bearing
trade-off is genuinely balanced, both options are surfaced with the deciding
question — that call stays with the human.

## Quick start

Requirements: [`uv`](https://docs.astral.sh/uv/), and at least one seat CLI installed (`claude`, `pi`, or `codex`) with credentials. Linux/macOS first-class; Windows untested (best-effort). Tip: see `seats.example.yaml` in the repo for a commented full example.

```bash
uvx blessthis-llm-council
```

That's it. The interactive wizard will:

1. Build your `seats.yaml` (seat templates for common setups, per-seat env prompts, optional 1-token probe per seat).
2. Ask about telemetry (on by default; opt out anytime).
3. Wire up one host of your choice (MCP registration + council agent files). Re-run `blessthis-llm-council install` to add more hosts.

Then restart/reload your host and either ask the installed council agent ("run a council on …") or call the tools directly (e.g. `mcp__llm-council__council_start`).

Useful CLI commands (same binary):

```bash
blessthis-llm-council seats list|add|edit|remove|probe   # manage seats.yaml
blessthis-llm-council doctor                             # full diagnostics
blessthis-llm-council status                             # fast wiring overview
blessthis-llm-council uninstall                          # clean removal
```

> Two console scripts ship in the package: **`blessthis-llm-council`** is the installer/management CLI; **`blessthis-llm-council-server`** is the MCP server your host launches (you never run it by hand — the wizard registers it).

## seats.yaml

All seat topology **and** secrets live in one file: `~/.blessthis-llm-council/seats.yaml`, mode `0600`, never committed anywhere.

```yaml
telemetry:
  enabled: true          # explicit; ON by default (Decision #22)

seats:
  fable:
    models: [claude-fable-5, claude-opus-4-8]   # preferred first
    agent:
      bin: claude
      args: ["-p", "{prompt}", "--output-format", "json",
             "--dangerously-skip-permissions",
             "--model", "{model}", "--add-dir", "{workdir}"]
      env:
        ANTHROPIC_API_KEY: __REPLACE_ME__

  glm:                       # pi reads its own models.json — no env needed
    models: [glm-5.2]
    agent:
      bin: pi
      args: ["--mode", "json", "--no-extensions", "--no-skills",
             "--no-prompt-templates", "--no-context-files",
             "-p", "{prompt}", "--model", "{model}",
             "--tools", "read,write,edit,grep,find,ls,bash"]
      env: {}

  gpt:                       # codex runner via an OpenAI-wire gateway
    models: [gpt-5]
    agent:
      bin: codex
      args: ["exec", "--json", "--skip-git-repo-check", "-s", "read-only",
             "--color", "never", "-m", "{model}", "-C", "{workdir}", "{prompt}"]
      env:
        OPENAI_BASE_URL: https://your-gateway.example/v1
        OPENAI_API_KEY: __REPLACE_ME__
```

A **seat is an LLM family, not a runner** — the same binary can back several seats with different credentials (e.g. native API + gateway fallback as two seats, preferred-first). `args` is a pure exec-array (one argv token per element, `{prompt}` / `{model}` placeholders required). The wizard writes this file for you; edit later with `blessthis-llm-council seats edit <name>`.

**Codex seats are OpenAI-wire.** Non-OpenAI models behind a codex seat need an OpenAI-compatible gateway — set `OPENAI_BASE_URL` (plus the key) in that seat's `agent.env` — because the `codex` CLI only speaks the OpenAI protocol. Codex authentication itself (`codex login` or `OPENAI_API_KEY`) is your own concern and is never managed by the council: a missing/failed login simply surfaces as a seat error (nonzero exit with stderr, e.g. `codex auth failed`), where you can inspect it via `seat_health` or the error response.

## Tools reference (17)

**Council (10)**

| Tool | What it does |
|---|---|
| `council_start` | Start a blind council: brief + seat roster → hat assignments, spawns seats. |
| `council_poll` | Check progress of running seats (turns, tokens, done/error). |
| `council_answer` | Fetch a finished seat's answer (still anonymized by hat). |
| `council_ask` | Cross-examine a hat (resumes that seat's CLI session with a follow-up). |
| `council_is_model_replied` | Blind boolean: has a given model answered yet? |
| `council_reveal` | Lift the hats: map each hat → seat/model. Use after scoring. |
| `council_score` | Record blind scores (1-10 per hat) + notes — the mandatory end step. |
| `council_close` | Close the council; records are kept for history. |
| `model_scores` | Leaderboard: aggregated historical scores per model. |
| `seat_health` | Per-(seat, model) health/cooldown status. |

**Direct seat chat (6)**

| Tool | What it does |
|---|---|
| `chat_start` | Open a 1:1 chat session with a named seat. |
| `chat_send` | Send a message (async; returns a task_id immediately). |
| `chat_poll` | Long-poll for the turn's reply (+ usage; first turn yields the resume id). |
| `chat_history` | Read back a chat session's messages. |
| `chat_list` | List chat sessions (optional working_dir filter). |
| `chat_close` | Close a chat session (history preserved). |

**Discovery (1)**

| Tool | What it does |
|---|---|
| `list_seats` | List configured seats (name, models, runner kind) from seats.yaml. |

## Host support

| Host | Agent files | MCP registration |
|---|---|---|
| Claude Code | `~/.claude/agents/blessthis-council-*.md` | `claude mcp add --scope user` (CLI) |
| Gemini CLI | `~/.gemini/agents/` | `gemini mcp add -s user` (CLI) |
| pi | `~/.pi/agent/agents/blessthis-council-*.md` | file-merge into `~/.pi/agent/mcp.json` |
| Codex | `~/.codex/agents/` (TOML) | file-merge into `~/.codex/config.toml` |
| Cursor | `.cursor/agents/blessthis-council-*.md` | file-merge into `~/.cursor/mcp.json` |
| GitHub Copilot / VS Code | `.github/agents/blessthis-council-*.agent.md` (+ conductor orchestrator) — per-project | file-merge into `.vscode/mcp.json` (top-level key `servers`) |

Everywhere the server is registered under the canonical name **`llm-council`** as `{ "command": "uvx", "args": ["blessthis-llm-council-server"] }` — a non-destructive merge that never touches your other servers or agents (we only ever write/remove entries matching our fingerprint and `blessthis-council-*` files).

## Philosophy

- **Why blind?** Models bandwagon. Show an LLM another model's answer and you get agreement theater, not independent judgment. Hats stay on until scoring is done, so scores reflect the answer, not the author.
- **Why real CLIs instead of API calls?** Your seat CLIs already have working auth, tooling, session resume, and model routing. The council spawns the same `claude` / `pi` / `codex` binaries you use interactively — headless, with your config — so there's no second credential store to break and no reimplemented client to drift. This package never makes a direct LLM HTTP call.
- **BYO everything.** Models, gateways, API keys, base URLs — all yours, all in `seats.yaml`. No hosted component, no account, no lock-in.

## Telemetry

On by default; opt out anytime (`telemetry.enabled: false`, asked during install with default Yes — Decision #22).

When enabled, we send only anonymized scoring events: `{model, kind, score, usage, host, tool_version, ts, council_uuid}`.

We **never** send: your code, file paths, briefs, answer content, notes, seat names, credentials, or seat-health data.

## Upgrading from the Postgres pre-release

The store moved from Postgres to SQLite (`~/.blessthis-llm-council/state.db`). Migration is **stop-the-world** (A15):

1. Stop the old server (quit every MCP host using it).
2. Run `python scripts/migrate_pg_to_sqlite.py --pg postgres://localhost:5433/llm_council --sqlite sqlite:///$HOME/.blessthis-llm-council/state.db --verify` — it carries councils, hats (renamed `session_id`), scores, and model health (`seat='legacy'`), and verifies per-table row counts (non-zero exit on mismatch).
3. Unset `DATABASE_URL` and start the new server. Postgres remains supported via `DATABASE_URL=postgres://...` (install the `[postgres]` extra).

## Uninstall & privacy

```bash
blessthis-llm-council uninstall            # removes MCP entries + agent files
blessthis-llm-council uninstall --purge    # also deletes seats.yaml + state.db
```

Everything lives under `~/.blessthis-llm-council/` (seats, SQLite state) plus the `llm-council` entry and `blessthis-council-*` files in your host configs. Nothing else on your system is touched. seats.yaml contains your API keys — keep it `0600` and never commit it.

### Upgrading from pre-v1 (Postgres)
The internal pre-release stored state in Postgres. Migration is stop-the-world: stop the old server, run `python scripts/migrate_pg_to_sqlite.py --pg <url> --sqlite sqlite:///$HOME/.blessthis-llm-council/state.db --verify` (requires `pip install blessthis-llm-council[postgres]`), then start the new server. Row counts are verified per table.

## License

AGPL-3.0-or-later. Fork it, improve it — but improvements to this package must be shared back under the same license.
