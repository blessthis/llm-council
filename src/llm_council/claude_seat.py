"""Run a council seat as a `claude -p` subprocess, routed through the gateway.

The old seats ran a bespoke Anthropic tool loop (agent.py / minimax_agent.py).
It hung, looped, and sometimes stored a model's last *thinking* line (a tool-call
preamble like "Let me read X:") as its final "answer". This module replaces that
engine with the real Claude Code CLI in headless print mode (`claude -p`), which:

  * drives a robust, battle-tested agent loop (Read/Write/Edit/Bash/Grep/Glob),
  * emits a single clean final `result` (no preamble-as-answer bug),
  * lets us swap the seat's model via `--model`, and
  * routes EVERY model (Claude, Gemini, GLM, MiniMax) through our CLIProxyAPI
    gateway by pointing ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN at it — the
    gateway translates each provider's protocol, so one CLI speaks to all of them.

`--output-format json` gives us `.result`, `.usage`, `.session_id` and `.is_error`
in one object. The returned session_id lets council_ask resume the same seat
(`claude -p --resume <id>`) for cross-examination.

`capture_to_file` hands the seat a scratch file to write its FULL structured answer
to (appending as it works) which we read back as the response — because some models
(esp. MiniMax) do all the work but compress their final chat message to a useless
one-liner. The file is the deliverable; the terse chat `.result` is only a fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from . import availability
from .config import Config

logger = logging.getLogger(__name__)


async def _health(coro) -> None:
    """Best-effort health-cache write — a DB hiccup must never sink a seat."""
    try:
        await coro
    except Exception:  # noqa: BLE001
        logger.exception("claude seat: health-cache write failed")

# Some models (MiniMax) emit their chain-of-thought as literal <think>...</think>
# text in the final message — the CLI does not treat it as a thinking block, so it
# lands in `.result`. Strip it so the council sees only the conclusion.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

# Transient network/infra errors worth retrying (ECONNRESET / dropped socket / CF 524 /
# 5xx / 429 rate-limit-cooldown / overloaded). Deliberately does NOT match
# `error_max_turns` (a budget problem — retrying won't help) or our own subprocess
# timeout (not a transient blip), so those escalate instead of burning retries.
_RETRYABLE_RE = re.compile(
    r"(econnreset|socket (?:connection|hang up)|connection (?:closed|reset|error)|"
    r"\b5(?:0[0-4]|24)\b|\b429\b|rate.?limit|cooling down|overloaded|"
    r"temporarily unavailable|service unavailable|timeout occurred|"
    r"the connection was closed)",
    re.IGNORECASE,
)
# Total transient retries across one run_seat call, and the backoff before each.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_S = (5, 15, 30)

# A file whose captured content is at least this long is treated as a real
# deliverable and preferred over the (often terse) final chat message. Set well above
# a scaffolding stub: a seat that wrote "## Findings (placeholder — being filled)" then
# died on a quota (481 chars) must NOT be mistaken for a real answer, or it short-circuits
# the fable→gateway→opus escalation (observed council #42). Genuine council answers are
# many KB; a short-but-COMPLETE answer still comes through the chat `.result` path.
_MIN_FILE_ANSWER = 1200


def _clean(text: str | None) -> str:
    return _THINK_RE.sub("", (text or "").strip()).strip()


def _seat_working_instruction(path: str, ast_grep_path: str) -> str:
    """Appended to a seat's prompt: HOW to work efficiently on a big codebase and WHERE
    to put its answer. Prevents the two observed failure modes on huge repos:
      1. burning the whole turn/time budget READING and hitting error_max_turns / timeout
         before writing anything (gemini/fable/minimax all did this on Hypesky), and
      2. compressing the final chat message to a useless one-liner (the .result is all we
         would otherwise capture — esp. MiniMax)."""
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

# The CLI makes occasional "small/fast" housekeeping calls; pin them to a model
# that exists on the gateway so they never leak to api.anthropic.com.
_SMALL_FAST_MODEL = "claude-haiku-4-5-20251001"

# Claude-seat policy (user rule): try Fable first, fall back to the latest Opus.
CLAUDE_PRIMARY = "claude-fable-5"
CLAUDE_FALLBACK = "claude-opus-4-8"

# Per-model fallback chains (same-family only — councils need seat DIVERSITY, so a
# dead gemini must degrade to another gemini, never silently double another family).
# Gemini: unprefixed = Antigravity OAuth (free, first); aistudio/ = the paid AI
# Studio key, an explicit fallback (see cliproxyapi config.template.yaml).
FALLBACK_CHAINS: dict[str, list[str]] = {
    "gemini-3.1-pro-preview": [
        "gemini-3.1-pro-preview", "gemini-3-pro-preview",
        "aistudio/gemini-3-pro-preview", "aistudio/gemini-2.5-pro",
    ],
    "gemini-3-pro-preview": [
        "gemini-3-pro-preview",
        "aistudio/gemini-3-pro-preview", "aistudio/gemini-2.5-pro",
    ],
    "minimax-m3": ["minimax-m3", "minimax-m2.7"],
    "glm-5.2": ["glm-5.2", "glm-5.1"],
    "kimi-k3": ["kimi-k3", "kimi-k2.7-code"],
}


def chain_for(model: str) -> list[str]:
    return FALLBACK_CHAINS.get(model, [model])


def seat_family(model: str) -> str:
    """The seat/family name that keys seat_health rows (pre-P3 seats.yaml):
    the head of the model's fallback chain."""
    return chain_for(model)[0]


# Default council roster when a caller omits `models` — one seat per family. This is
# the SINGLE place that names the default panel; callers should prefer omitting
# `models` entirely so a roster change here doesn't need touching every caller.
DEFAULT_COUNCIL_MODELS: list[str] = ["claude-fable-5", "kimi-k3", "glm-5.2", "minimax-m3"]

# A trivial ping is ~40k input tokens (the CLI's own system prompt + tools) and
# glm can take >60s for it, so real multi-file diagnosis needs a generous ceiling.
# On a HUGE codebase (e.g. Hypesky) seats were exhausting 40 turns just reading and
# hitting error_max_turns / the 900s wall before writing their answer — so the budget
# is roomier now, and the deliverable instruction pushes them to delegate reading to
# parallel Explore subagents and write early.
_DEFAULT_TIMEOUT = 1500
_DEFAULT_MAX_TURNS = 80


def _claude_bin() -> str:
    """Locate the claude CLI: explicit override, then the user-local install
    (~/.claude/local/claude — where the alias points), then PATH."""
    override = os.environ.get("CLAUDE_CLI_PATH", "").strip()
    if override:
        return override
    local = os.path.expanduser("~/.claude/local/claude")
    if os.path.exists(local):
        return local
    return shutil.which("claude") or "claude"


def resolve_seat_model(model: str, available: set[str]) -> str:
    """Map a requested seat model to the one actually run. Any Claude request
    prefers Fable, else the latest Opus (per user policy). Non-Claude models pass
    through unchanged (availability is checked separately by the caller)."""
    if model.lower().startswith("claude"):
        if CLAUDE_PRIMARY in available:
            return CLAUDE_PRIMARY
        if CLAUDE_FALLBACK in available:
            return CLAUDE_FALLBACK
    return model


def _env(cfg: Config, *, gateway: bool) -> dict[str, str]:
    """Build the seat subprocess env.

    gateway=True  → inherit whatever translating-proxy pointer the operator
                    exported (ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN — BYO,
                    Decision #7); the ONLY way to reach non-Claude models, and
                    the fallback for Claude.
    gateway=False → 'native': run the CLI on the user's own Claude Code auth
                    (subscription) straight to api.anthropic.com — faster, native
                    prompt caching. Used FIRST for Claude seats; if it fails we
                    retry the same seat with gateway=True. Only Claude models
                    can run native."""
    env = dict(os.environ)
    env["CI"] = "1"  # keep the CLI non-interactive / quiet
    if gateway:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = _SMALL_FAST_MODEL
        # A stray real Anthropic key would override the proxy token — drop it.
        env.pop("ANTHROPIC_API_KEY", None)
    else:
        # Native: strip any gateway pointer so the CLI uses api.anthropic.com with the
        # user's logged-in subscription; leave ANTHROPIC_API_KEY / login creds as-is,
        # and don't pin a gateway-only small-fast model.
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("ANTHROPIC_SMALL_FAST_MODEL", None)
    return env


def _last_json(text: str) -> dict[str, Any] | None:
    """Best-effort: if stdout has leading noise before the JSON object, scan lines
    from the end for the last parseable JSON object."""
    for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _usage(data: dict[str, Any]) -> dict[str, int]:
    """Map the CLI's Anthropic-shaped usage into our totals dict."""
    u = data.get("usage") or {}
    return {
        "input_tokens": int(u.get("input_tokens", 0) or 0),
        "output_tokens": int(u.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
    }


async def _invoke(
    cfg: Config,
    model: str,
    working_dir: str,
    prompt: str,
    *,
    resume: str | None,
    system_prompt: str,
    max_turns: int,
    timeout: int,
    gateway: bool,
    session_id: str | None = None,
    extra_add_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """Run one `claude -p` invocation and return the parsed JSON result object.

    session_id: pin the CLI session id (we generate it) so its transcript is at a known
    path immediately (~/.claude/projects/*/<id>.jsonl) — this lets council_poll read live
    progress and stores a resumable id even before the seat finishes. Ignored when resuming
    (--resume already selects the session)."""
    args = [
        _claude_bin(), "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--add-dir", working_dir,
    ]
    for d in extra_add_dirs or []:
        args += ["--add-dir", d]
    if resume:
        args += ["--resume", resume]
    elif session_id:
        args += ["--session-id", session_id]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=working_dir,
        env=_env(cfg, gateway=gateway),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"claude seat timed out after {timeout}s (model={model})"
        ) from None

    stdout = raw_out.decode("utf-8", "replace")
    stderr = raw_err.decode("utf-8", "replace")

    data: dict[str, Any] | None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = _last_json(stdout)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"claude produced no JSON (model={model}, rc={proc.returncode}): "
            f"{stdout[:600]!r} / stderr={stderr[:400]!r}"
        )
    return data


def _read_answer_file(path: str | None) -> str:
    """Read + clean a seat's deliverable file, tolerating a missing/unwritten file."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return _clean(fh.read())
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001 — a bad read must not sink the seat
        logger.exception("claude seat: failed reading answer file %s", path)
        return ""


async def _invoke_step(
    cfg: Config,
    model: str,
    working_dir: str,
    prompt: str,
    *,
    resume: str | None,
    system_prompt: str,
    max_turns: int,
    timeout: int,
    gateway: bool,
    session_id: str | None,
    on_session: Callable[[str], Awaitable[None]] | None,
    extra_add_dirs: list[str] | None,
    answer_path: str | None,
    budget: list[int],
) -> dict[str, Any]:
    """Run ONE chain step (a given model+transport), retrying TRANSIENT failures in
    place — an ECONNRESET / dropped socket / 524 / 429-cooldown that dies mid-request is
    a network blip, not a verdict. Retries draw from the shared `budget` (a 1-element
    mutable counter, so the whole run_seat call is capped at N transient retries total),
    with backoff, and RESUME the seat's session so the reading already done isn't thrown
    away. A non-transient failure (max_turns, auth, bad request) or an exhausted budget
    returns immediately so the outer chain can escalate (native→gateway→opus).

    `active` (resume id, or our pinned session_id) is reported via on_session so the
    council can record it live — enabling progress polling and post-hoc recovery."""
    active = resume or session_id
    if on_session and active:
        try:
            await on_session(active)
        except Exception:  # noqa: BLE001 — bookkeeping must never sink the seat
            logger.exception("claude seat: on_session callback failed")
    step_resume = resume
    step_sid = None if resume else session_id
    data: dict[str, Any] = {}
    while True:
        used = _MAX_TRANSIENT_RETRIES - budget[0]
        try:
            data = await _invoke(
                cfg, model, working_dir, prompt,
                resume=step_resume, session_id=step_sid, system_prompt=system_prompt,
                max_turns=max_turns, timeout=timeout,
                gateway=gateway, extra_add_dirs=extra_add_dirs,
            )
        except Exception as e:  # noqa: BLE001 — subprocess crash may be a transient reset
            if availability.classify(repr(e)):
                # Capacity-class (depleted credits / quota / account cooldown): an
                # account state, not a blip — record it and escalate, don't retry.
                await _health(availability.record_failure(seat_family(model), model, repr(e)))
                raise
            if budget[0] <= 0:
                raise
            budget[0] -= 1
            await asyncio.sleep(_RETRY_BACKOFF_S[min(used, len(_RETRY_BACKOFF_S) - 1)])
            logger.warning(
                "claude seat invoke crashed (%r) — transient retry, budget left %d",
                e, budget[0],
            )
            step_resume, step_sid = active, None  # resume our pinned session
            continue

        result = _clean(data.get("result"))
        file_answer = _read_answer_file(answer_path)
        usable = len(file_answer) >= _MIN_FILE_ANSWER or (result and not data.get("is_error"))
        if data.get("is_error") and availability.classify(result):
            # Capacity-class error: retrying the SAME model in place is the observed
            # waste (3× backoff on "credits depleted"). Record and return so the
            # outer chain escalates to the next fallback model.
            await _health(availability.record_failure(seat_family(model), model, result))
            return data
        if usable and not data.get("is_error"):
            await _health(availability.record_success(seat_family(model), model))
        transient = bool(data.get("is_error")) and bool(_RETRYABLE_RE.search(result))
        if usable or not transient or budget[0] <= 0:
            return data
        budget[0] -= 1
        step_resume, step_sid = (data.get("session_id") or active), None  # resume warm
        await asyncio.sleep(_RETRY_BACKOFF_S[min(used, len(_RETRY_BACKOFF_S) - 1)])
        logger.warning(
            "claude seat transient error (model=%s gateway=%s): %s — retry, "
            "budget left %d (resume=%s)",
            model, gateway, result[:120], budget[0], bool(step_resume),
        )


async def run_seat(
    cfg: Config,
    model: str,
    working_dir: str,
    prompt: str,
    *,
    resume: str | None = None,
    system_prompt: str = "",
    capture_to_file: bool = False,
    on_session: Callable[[str], Awaitable[None]] | None = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run a seat and return a normalized result:

        {response, usage, claude_session_id, model, is_error, num_turns}

    capture_to_file: give the seat a scratch file to write its FULL structured answer
    to (appending as it goes) and read that back as the response — because some models
    (esp. MiniMax) do all the work but compress their final chat message to a useless
    one-liner. The file is the deliverable; the chat message is only a fallback. Even a
    timed-out / errored seat can leave partial work in the file (it's read regardless).

    Attempt chain (escalate only on failure):
      * Claude seat: NATIVE first (the user's own subscription, straight to
        api.anthropic.com — faster, native caching, no CF tunnel); if that fails,
        retry the SAME model via the gateway; a Fable seat then degrades to Opus on
        the gateway. So the gateway is only used when native doesn't work.
      * Non-Claude seat: gateway only (the CLI can't reach Gemini/GLM/MiniMax direct).
    An attempt "succeeds" when it recovers a substantial file OR returns a non-error
    chat result; anything else (error, empty, timeout, no-JSON) escalates to the next.
    """
    scratch_dir: str | None = None
    answer_path: str | None = None
    seat_prompt = prompt
    extra_dirs: list[str] | None = None
    if capture_to_file:
        scratch_dir = tempfile.mkdtemp(prefix="council-seat-")
        answer_path = os.path.join(scratch_dir, "ANSWER.md")
        extra_dirs = [scratch_dir]
        seat_prompt = prompt + _seat_working_instruction(answer_path, "")

    # Build the ordered (model, use_gateway) attempt chain.
    if model.lower().startswith("claude"):
        attempts: list[tuple[str, bool]] = [(model, False), (model, True)]
        if model == CLAUDE_PRIMARY and not resume:
            attempts.append((CLAUDE_FALLBACK, True))
    elif resume:
        attempts = [(model, True)]  # a resume is pinned to the original model's session
    else:
        # Non-Claude: escalate along the model's same-family fallback chain (a
        # capacity-dead gemini degrades to another gemini, etc.).
        attempts = [(m, True) for m in chain_for(model)]

    try:
        data: dict[str, Any] = {}
        result = ""
        file_answer = ""
        is_error = True
        recovered = False
        used_model = model
        last_exc: Exception | None = None
        # Shared transient-retry budget across the whole chain (ECONNRESET/524/429 blips
        # are retried in place by _invoke_step, resuming to keep warm context).
        budget = [_MAX_TRANSIENT_RETRIES]

        for attempt_model, use_gateway in attempts:
            # Resume ties to a session id created for the ORIGINAL model, so never carry
            # it onto the Opus fallback attempt. For a fresh attempt we PIN our own session
            # id (uuid) so its transcript path is known immediately (live progress + a
            # resumable id recorded before the seat finishes).
            step_resume = resume if attempt_model == model else None
            step_sid = None if step_resume else str(uuid.uuid4())
            try:
                d = await _invoke_step(
                    cfg, attempt_model, working_dir, seat_prompt,
                    resume=step_resume, session_id=step_sid, on_session=on_session,
                    system_prompt=system_prompt, max_turns=max_turns,
                    timeout=timeout, gateway=use_gateway, extra_add_dirs=extra_dirs,
                    answer_path=answer_path, budget=budget,
                )
            except Exception as e:  # noqa: BLE001 — timeout/no-JSON → escalate, don't die
                last_exc = e
                logger.warning(
                    "claude seat attempt raised: model=%s gateway=%s: %r (escalating)",
                    attempt_model, use_gateway, e,
                )
                # A crash can still leave a substantial partial file — take it if so.
                file_answer = _read_answer_file(answer_path)
                if len(file_answer) >= _MIN_FILE_ANSWER:
                    recovered, is_error, used_model = True, False, attempt_model
                    break
                continue

            data, used_model = d, attempt_model
            result = _clean(data.get("result"))
            file_answer = _read_answer_file(answer_path)
            is_error = bool(data.get("is_error"))
            recovered = len(file_answer) >= _MIN_FILE_ANSWER
            # Stop escalating ONLY on a clean success (a non-errored attempt with a real
            # file or a chat result). An ERRORED attempt escalates to the next model even
            # if it left a partial file — so a fable quota-limit really does fall through
            # to gateway-fable and then opus, instead of being accepted mid-chain. Any
            # substantial partial from the LAST attempt is still returned after the loop.
            if not is_error and (recovered or result):
                break
            logger.warning(
                "claude seat attempt failed: model=%s gateway=%s is_error=%s (escalating)",
                attempt_model, use_gateway, is_error,
            )

        # Every attempt raised and nothing was recovered → surface the last error.
        if not data and not recovered and last_exc is not None:
            raise last_exc

        # Diagnostic on failure: don't mask the reason as a bare "(no output)". The CLI's
        # result `subtype` tells us WHY (e.g. error_max_turns = ran out of turns reading,
        # error_during_execution = an API/tool error), so the council error is actionable.
        if recovered:
            response = file_answer
        elif result:
            response = result
        else:
            subtype = data.get("subtype") or ("timeout_or_crash" if last_exc else "no_output")
            response = f"(seat produced no answer — {subtype})"
        return {
            "response": response,
            "usage": _usage(data),
            "claude_session_id": data.get("session_id"),
            "model": used_model,
            # If we recovered a full file, the seat succeeded even if the CLI flagged an
            # end-of-run error (e.g. a final housekeeping call failed).
            "is_error": is_error and not recovered,
            "num_turns": data.get("num_turns"),
            "subtype": data.get("subtype"),
        }
    finally:
        if scratch_dir:
            shutil.rmtree(scratch_dir, ignore_errors=True)
