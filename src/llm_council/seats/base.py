"""Seat foundation: SeatRunner Protocol, data classes, seats.yaml loader (P3a).

Specs: docs/seatspec.md §1-2, docs/seats-schema.md §2-5.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

# --- Callback type -------------------------------------------------------------

# session callback: council.py `_on_session` — reports a live session id the
# moment an attempt starts (enables progress polling + resumable id on error).
OnSession = Callable[[str], Awaitable[None]]


# --- Protocol -------------------------------------------------------------------

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
        max_turns: int = 80,
        timeout: int = 1500,
        extra: dict[str, str] | None = None,
    ) -> InvokeResult:
        """Run ONE seat end-to-end and return the normalized result.

        Error semantics: DOES NOT raise for seat-level failure — an errored seat
        returns InvokeResult(is_error=True, response=<diagnostic>) so one dead
        seat never wedges the council. Runner raises RuntimeError only for
        total failure (timeout on all attempts / no parseable output / binary
        missing). Timeout inside an attempt: kill the subprocess and treat as
        an attempt failure, NOT a raised TimeoutError to the caller."""
        ...

    async def probe(self, seat: Seat, model: str) -> ProbeResult:
        """One cheap live check that (seat, model) works with the seat's own
        creds. Never raises: any failure returns ProbeResult(ok=False, ...)."""
        ...

    def supports_progress(self) -> bool:
        """True when council_poll can read live {turns, output_tokens} for a
        running seat via read_progress()."""
        ...

    def read_progress(self, session_id: str) -> Progress | None:
        """Sync, best-effort. None when no transcript yet or unsupported."""
        ...


# --- Data classes (seatspec.md §2, verbatim) ------------------------------------

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
    # matches claude_seat.py `_usage`; stored as jsonb in council_hats.usage.
    # Unknown per-runner → zeros, NOT None.


@dataclass(frozen=True)
class InvokeResult:
    response: str                 # deliverable-file content if >= _MIN_FILE_ANSWER chars,
                                  # else final chat result, else
                                  # "(seat produced no answer — <subtype>)"
    usage: Usage
    session_id: str | None        # resumable id for council_ask (was claude_session_id)
    model: str                    # model that ACTUALLY answered after fallback
    is_error: bool                # False when a full file was recovered even if the
                                  # CLI flagged an end-of-run error
    num_turns: int | None = None
    subtype: str | None = None    # CLI error taxonomy, e.g. error_max_turns


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    model: str
    error: str | None = None      # repr of failure; classified by availability.classify
    reason: Literal["balance", "quota"] | None = None


@dataclass(frozen=True)
class Progress:
    turns: int
    input_tokens: int
    output_tokens: int
    last_event_type: str | None = None  # most recent event type seen, for debugging


# --- Shared runner-agnostic helpers (seatspec.md §1 "Not on the Protocol") ------

# claude_seat.py:82 — answers shorter than this from the scratch file are not
# preferred over the final chat result.
_MIN_FILE_ANSWER = 1200

# Availability-probe subprocess ceiling (all runners). A trivial "reply ok" probe
# loads the CLI's own ~40k-token system prompt + tools BEFORE the model answers,
# and a reasoning model (opus/fable/*-thinking) spends its first budget on
# reasoning tokens — so 60s produced false-negative timeouts that benched the
# whole seat. 180s covers reasoning + a degraded-but-working network path while
# still bounding a genuine hang (the real invoke ceiling is far higher, 1500s).
PROBE_TIMEOUT = 180


def seat_working_instruction(path: str, ast_grep_path: str = "") -> str:
    """Appended to a seat's prompt: HOW to work efficiently on a big codebase and WHERE
    to put its answer. Prevents the two observed failure modes on huge repos:
      1. burning the whole turn/time budget READING and hitting error_max_turns / timeout
         before writing anything (gemini/fable/minimax all did this on Hypesky), and
      2. compressing the final chat message to a useless one-liner (the .result is all we
         would otherwise capture — esp. MiniMax).

    Runner-agnostic by design (P3 risks note): prompt-level, lives in base.py."""
    ast = (
        f"Use ast-grep ({ast_grep_path}) for structural code search — it is faster and "
        "more precise than reading whole files or grepping by regex. "
    ) if ast_grep_path else ""
    return (
        "\n\n---\n"
        "HOW TO WORK (your turn/time budget is limited — do NOT spend it all reading):\n"
        f"- {ast}For broad or deep codebase scanning, DELEGATE to Explore subagents — you "
        "may launch SEVERAL IN PARALLEL in a single message (each costs ~1 of your turns "
        "but reads many files internally), so you map the code fast without exhausting "
        "your own turn budget on sequential file reads.\n"
        "- Start WRITING your answer to the deliverable file EARLY and refine it as you "
        "learn more — a written partial design beats an unwritten perfect one.\n\n"
        "DELIVERABLE FILE (IMPORTANT): write your COMPLETE, fully-structured answer to "
        f"this exact file using your write/edit tools:\n  {path}\n"
        "APPEND to it as you go so nothing is lost if you run long. THIS FILE is your "
        "real deliverable and is read IN FULL — your final chat message can be as short "
        "as you like and will be ignored. Before finishing, make sure the file holds your "
        "entire structured response (every requested section), not a summary of it."
    )
