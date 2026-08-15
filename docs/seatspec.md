# SeatRunner Protocol — Interface Specification

> Feeds P3 (`seats/base.py` + `seats/runners/{claude,pi,codex}.py`). Derived from
> `claude_seat.py` (current ground truth), `council.py` call sites, and PLAN.md
> P3, §8 (seat schema), Decisions #2/#7, Q3/Q4 resolutions, §5f (pi research), §5g (codex research).
> Nothing here is implemented yet; signatures only.

## 1. SeatRunner Protocol (the ABC)

Location: `src/llm_council/seats/base.py`. All methods are `async` — seats are subprocesses driven via `asyncio.create_subprocess_exec` (claude_seat.py:271-278).

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

# session callback: council.py:260-266 `_on_session` — reports a live session id
# the moment an attempt starts (enables progress polling + resumable id even on error).
OnSession = Callable[[str], Awaitable[None]]


class SeatRunner(Protocol):
    """One implementation per agent CLI binary (claude / pi / codex).

    A runner knows how to render that binary's argv, parse its output stream,
    obtain its session id, resume it, and extract usage. Model→runner binding
    is gone: the Seat's `agent.bin` IS the binding (PLAN.md P3)."""

    @property
    def runner_kind(self) -> str:
        """Derived from seat.agent.bin basename: 'claude' | 'pi' | 'codex' | ...
        Stored in council_hats.seat_backend (PLAN.md Q6 schema freeze)."""
        ...

    async def invoke(
        self,
        seat: Seat,
        model: str,
        prompt: str,
        workdir: str,
        *,
        resume: str | None = None,
        session_id: str | None = None,
        system_prompt: str = "",
        capture_to_file: bool = False,
        on_session: OnSession | None = None,
        max_turns: int = 80,      # claude_seat.py:161 _DEFAULT_MAX_TURNS
        timeout: int = 1500,      # claude_seat.py:160 _DEFAULT_TIMEOUT
        extra: dict[str, str] | None = None,
    ) -> InvokeResult:
        """Run ONE seat end-to-end and return the normalized result.

        Encompasses today's `run_seat` (claude_seat.py:341-475): the full attempt
        chain is the runner's own business (claude: native→gateway→opus), the
        in-place transient-retry budget (_MAX_TRANSIENT_RETRIES=3, backoff
        (5,15,30)), <think> stripping, and capture-to-file.

        Args:
          seat:        the seat definition (models, agent.bin/args/env) from seats.yaml §8.
          model:       the model to seat (already health-picked by the caller).
          prompt:      the council brief (capture_to_file appends the deliverable-file
                       working instruction — prompt-level, runner-agnostic, P3 risks note).
          workdir:     subprocess cwd + the directory the seat reads.
          resume:      session id of a previous run (council_ask); pinned to the
                       ORIGINAL model (claude_seat.py:392).
          session_id:  caller-pinned id for a FRESH run, so the transcript path is
                       known immediately (claude_seat.py:404-407). Mutually exclusive
                       with resume.
          system_prompt: appended to the CLI's system prompt (--append-system-prompt
                       or per-runner equivalent). Empty = none.
          capture_to_file: hand the seat a scratch ANSWER.md; prefer its content as
                       the response when >= 1200 chars (claude_seat.py:82,449-470).
          on_session:  called with the live session id as soon as known (before the
                       seat finishes). Best-effort; runner must never let it sink the seat.
          max_turns / timeout: budget passed through to the CLI where supported.
          extra:       reserved per-runner knobs (e.g. extra --add-dir paths).
                       Rendered as exec-array tokens, never a shell string (Q4).

        Error semantics: DOES NOT raise for seat-level failure — an errored seat
        returns InvokeResult(is_error=True, response=<diagnostic>) so one dead seat
        never wedges the council (council.py:278-284 relies on run_seat not raising
        for error answers; today it raises ONLY when every attempt raised AND
        nothing was recovered, claude_seat.py:455-456). Runner raises RuntimeError
        only for total failure (timeout on all attempts / no parseable output /
        binary missing). Timeout inside an attempt: kill the subprocess and treat
        as an attempt failure (claude_seat.py:283-286), NOT as a raised TimeoutError
        to the caller.
        """
        ...

    async def probe(self, seat: Seat, model: str) -> ProbeResult:
        """One cheap live check that (seat, model) works with the seat's own creds.

        Per-seat, never global (Decision #7, Q3): env comes from seat.agent.env.
        On completion, records to seat_health keyed (seat.name, model) — replacing
        availability.probe()'s model_health rows (availability.py:127-143).
        Never raises: any failure returns ProbeResult(ok=False, error=...).
        """
        ...

    def supports_progress(self) -> bool:
        """True when council_poll can read live {turns, output_tokens} for a
        running seat via read_progress(). Claude: True today. Others: see §3."""
        ...

    def read_progress(self, session_id: str) -> Progress | None:
        """Sync, best-effort. None when no transcript yet or runner doesn't
        support it. Replaces council.py:386-430 `_read_progress` (currently a
        hardcoded ~/.claude/projects/*/<sid>.jsonl glob)."""
        ...
```

**Not on the Protocol (deliberately):**
- `resolve_seat_model` / `chain_for` / `DEFAULT_COUNCIL_MODELS` (claude_seat.py:148-174) — these are model-roster policy. In P3 the seat's `models[]` list in seats.yaml §8 replaces FALLBACK_CHAINS; the roster lives in seats.yaml, not a module constant. Move to `seats/loader.py` or drop.
- `availability.classify` / `record_failure` / `record_success` — stay in `availability.py`, but gain a `seat: str` param and write `seat_health` (Q3). The runner calls them; they are not runner methods.
- `capture_to_file` scratch-dir lifecycle (claude_seat.py:415-420, 474-475) — shared helper in `seats/base.py`, runner-agnostic since it's prompt-level.

## 2. Data classes

`InvokeResult` fields pulled from `run_seat`'s return dict (claude_seat.py:466-473) + council.py readers (council.py:270-277). `claude_session_id` is renamed `session_id` per Q6 (`council_hats.claude_session_id` → `session_id`).

```python
@dataclass(frozen=True)
class AgentSpec:
    bin: str                      # argv[0]; runner prepends it (Q4: no {bin} placeholder)
    args: list[str]               # pure exec-array, ONE token per element (Q4)
    env: dict[str, str]           # injected verbatim into the subprocess (§8)

@dataclass(frozen=True)
class Seat:
    name: str                     # seats.yaml key, e.g. 'fable' — an LLM family (Decision #8)
    models: list[str]             # preferred first, then same-seat fallbacks (§8)
    agent: AgentSpec
    runner_kind: str              # derived from agent.bin basename

@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # matches claude_seat.py:234-243 `_usage`; stored as jsonb in council_hats.usage
    # (council.py:191-200). Unknown per-runner → zeros, NOT None.

@dataclass(frozen=True)
class InvokeResult:
    response: str                 # deliverable-file content if >=1200 chars, else final
                                  # chat result, else "(seat produced no answer — <subtype>)"
                                  # (claude_seat.py:458-465)
    usage: Usage
    session_id: str | None        # resumable id for council_ask (was claude_session_id)
    model: str                    # model that ACTUALLY answered after fallback
                                  # (council.py:275-276 persists it for attribution)
    is_error: bool                # False when a full file was recovered even if the
                                  # CLI flagged an end-of-run error (claude_seat.py:471)
    num_turns: int | None = None
    subtype: str | None = None    # CLI error taxonomy, e.g. error_max_turns —
                                  # surfaced so council errors are actionable (claude_seat.py:463)

@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    model: str
    error: str | None = None      # repr of failure; classified by availability.classify
    reason: Literal["balance", "quota"] | None = None  # availability.py:61-71

@dataclass(frozen=True)
class Progress:
    turns: int
    input_tokens: int
    output_tokens: int
    last_event_type: str | None = None  # most recent event type seen, for debugging
```

No `Any` anywhere above — today's `dict[str, Any]` return (claude_seat.py:355, 471) is exactly what this replaces.

## 3. Per-runner specifics

### claude (`seats/runners/claude.py`) — refactor of claude_seat.py, preserve ALL behavior (P3)
- **invoke → CLI:** `claude -p {prompt} --model {model} --output-format json --dangerously-skip-permissions --max-turns N --add-dir {workdir}` (claude_seat.py:254-268). env from `seat.agent.env` (replaces `_env(cfg, gateway=...)` claude_seat.py:203-231; the native/gateway split becomes two seats or two env blocks — flag for human, §5 Q1). stdin DEVNULL, stdout/stderr PIPE (271-278).
- **session_id:** fresh run — caller pins a uuid via `--session-id` (263-264); after the run read from the result JSON `.session_id`. Resume: `--resume <id>` (261-262).
- **resume:** `--resume` flag; pinned to original model's session (392-393).
- **usage:** `.usage` on the single result JSON → `_usage` mapping (234-243).
- **probe:** cheapest = one `--max-turns 1` trivial prompt via the seat env, OR keep today's direct gateway `messages(max_tokens=16)` (availability.py:127-143) when the env exposes an Anthropic-compatible base URL. Runner decides; §5 Q2.
- **supports_progress:** `True`. `read_progress`: glob `~/.claude/projects/*/<sid>.jsonl`, count `type=="assistant"` lines, sum `message.usage.output_tokens` (council.py:400-428).

### pi (`seats/runners/pi.py`) — PLAN.md §5f
- **invoke → CLI:** `pi --mode json --no-extensions --no-skills --no-prompt-templates --no-context-files -p {prompt} --model {model} --tools read,write,edit,grep,find,ls,bash`. pi has NO `--add-dir` flag — the working dir is the subprocess **cwd** (the runner passes `workdir` as `cwd` to `create_subprocess_exec`). Safety flags keep the headless seat free of host-side extensions/skills/context (`--offline` also available). **No `--no-session`** — sessions are required for council_ask.
- **session_id:** first JSONL line `{type:"session", id:<uuid>}` — must be captured from the stream, so pi invoke CANNOT just read final stdout; it must stream-parse (or tee stdout to a temp file). Report via `on_session` immediately — same contract as claude.
- **resume:** prefer `--session-id <id>` (deterministic resume — creates the session if missing); the runner currently appends `--session <uuid>` (accepts path or partial uuid) as the fallback form. Either way the explicit id is always passed.
- **usage:** OPEN in PLAN.md §5f ("usage/tokens extraction — not a direct field; parse from `agent_end.messages` or heuristic; confirm during spike"). Resolved to RPC side-channel getUsage (Q11) — either way, runner normalizes to `Usage`; zeros permitted if unobtainable. §5 Q3.
- **probe:** trivial 1-turn prompt with the seat's models.json creds.
- **supports_progress:** likely `True` via the same JSONL session file pi writes (path TBD in spike — pi writes sessions under `~/.pi/agent/`); count `message_end` events. Fallback `False` if the path isn't stable.

### codex (`seats/runners/codex.py`) — PLAN.md §5g (P5)
- **invoke → CLI:** `codex exec --json --skip-git-repo-check -s read-only --color never -m {model} -C {workdir} "{prompt}"`; `-o {file}` = built-in capture-to-file analog. Extra dirs via `--add-dir`. Routing note: codex is OpenAI-wire — non-OpenAI models need an OpenAI-compatible gateway URL in `agent.env` (`OPENAI_BASE_URL`); auth failures surface as stderr, user fixes their own codex setup (Q12).
- **session_id:** `thread_id` from the first `{type:"thread.started"}` event.
- **resume:** `codex exec resume <thread_id> "{prompt}"` — a SUBCOMMAND, not a flag (runner renders different argv on resume; allowed — runners append/prepend discrete tokens, Q4).
- **usage:** parse from JSON events (`turn.completed` carries usage in codex v0.147+); normalize to `Usage`.
- **probe:** trivial `codex exec` 1-turn run.
- **supports_progress:** `True` in principle — count `item.completed`/`turn.*` events from the session rollout file; path/format confirmed in P5 spike. Default `False` until verified.

## 4. council.py integration points

Every claude-specific assumption that must go through the Protocol:

1. **council.py:33** `from . import ... claude_seat` → replace with `seats.loader` registry; seats come from seats.yaml, not a module.
2. **council.py:268-273** `_run_seat` calls `claude_seat.run_seat(...)` → `runner.invoke(seat, model, brief, working_dir, system_prompt=..., capture_to_file=True, on_session=_on_session)`.
3. **council.py:272, 285-293, 300-301, 386-401** — `claude_session_id` everywhere: `_set_hat_session`, `_mark_done` param/column, `_read_progress` arg. Rename column/param to `session_id` (Q6 freezes this rename at P2).
4. **council.py:386-430** `_read_progress` — hardcoded `_CLAUDE_PROJECTS` glob (line 388) + claude JSONL event shape (`type=="assistant"`, `message.usage`). Replace with `registry[hat.seat_backend].read_progress(h["session_id"])`, gated on `supports_progress()`; `_render` (line 452) calls through the Protocol.
5. **council.py:336-341** `ask` calls `claude_seat.run_seat(..., resume=match["claude_session_id"])` → look up the hat's `seat_backend` (new column, P3/Q6) to pick the runner, then `runner.invoke(..., resume=match["session_id"])`. Note: resume is pinned to the original model — preserve claude_seat.py:392 semantics per runner.
6. **council.py:314-330** `start` roster logic uses `claude_seat.DEFAULT_COUNCIL_MODELS`, `resolve_seat_model`, `chain_for` → replaced by seats.yaml `models[]` preferred-first semantics (§8); `availability.pick_available` becomes per-seat `(seat.name, model)` health (Q3), probe via `runner.probe(seat, m)` using the seat's env.
7. **council.py:191-200** `_mark_done` writes `usage` jsonb + `claude_session_id` — gains `seat_backend` column write (Q6/P3).
8. **council.py:503-513** `reveal` returns `claude_session_id` → rename to `session_id`, add `seat_backend`.
9. **`_MIN_FILE_ANSWER`/capture-to-file instruction** (claude_seat.py:82, 96-126) — move verbatim to `seats/base.py` as shared helpers; runner-agnostic by design (P3 risks note).
10. **`Config` coupling** — `run_seat` takes `cfg` for gateway URL/key + ast_grep_path (claude_seat.py:203-231, 354). Post-P3, creds come from `seat.agent.env`; `cfg` shrinks to server-level concerns.

## 5. Open design questions (from planner)

1. **Claude native→gateway split (claude_seat.py:203-231, 381-389) — RESOLVED (a) 2026-08-14.** There is NO internal two-env attempt chain in the claude runner. The native-vs-gateway distinction is expressed purely through seats.yaml — each seat declares its own `agent.env` block, and the council walks seats in preferred-first order, taking the first healthy one (Decision #16). If a user wants native-then-gateway fallback for the same model family, they declare two seats (e.g. `fable-native` with `ANTHROPIC_API_KEY` env, `fable-gw` with `ANTHROPIC_BASE_URL`+token env). The runner stays dumb: it receives ONE Seat with ONE env dict and spawns `agent.bin` with it.
2. **Should `probe()` spawn the CLI or hit the API directly?** Today's probe (availability.py:127-143) is a raw gateway `messages()` call — 45s timeout, ~16 tokens. A CLI probe exercises auth/argv too but costs ~40k input tokens for claude (claude_seat.py:157-159). **Lean: API-direct probe where the seat env has a known base url; CLI probe only for pi/codex** (no HTTP endpoint to hit). Probe results cached exactly as today (`_OK_TTL=10min`, availability.py:47) — never always-live.
3. **pi usage extraction (Q11 RPC side-channel).** If the RPC getUsage side-channel isn't reachable for a headless `--mode json` subprocess, `Usage` will be zeros for pi seats, degrading the leaderboard data that motivates the whole project (PLAN.md:39). **Lean: spike confirms before P3 closes; if unavailable, record `usage=None`-equivalent zeros and document.**
4. **Does `invoke` raise or return on timeout? — RESOLVED (a) 2026-08-14.** Runner raises `RuntimeError` ONLY on total failure (every attempt raised AND nothing recovered); a single timeout inside an attempt is an attempt failure, not a raised exception (timeout → kill subprocess, treat as attempt failure). Matches claude_seat.py:283-286, 455-456 today. Spec §1 already encodes this.
5. **Progress granularity for pi/codex — RESOLVED (b) 2026-08-14.** Extended `Progress` — turns, input_tokens, output_tokens, last_event_type. Each runner maps whatever events it can cheaply count onto these fields. `last_event_type` is a debugging hint (e.g. `'assistant'`, `'message_end'`, `'item.completed'`).

## Risks
- The spec renames `claude_session_id` → `session_id`; the actual DB rename happens in P2 (Q6 schema freeze), so P3 code must target the P2 schema, not today's Postgres schema.
- pi usage (§5 Q3) is the biggest data-quality risk; the 314-score dataset's value depends on usage being populated.
- `supports_progress()=False` runners silently lose the `progress` field in poll output — `_render` must tolerate that (it already tolerates `_read_progress` returning None, council.py:453-455).
