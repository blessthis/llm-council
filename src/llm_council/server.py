"""FastMCP stdio server for blessthis-llm-council.

Exactly 17 MCP tools (Decisions #15/#17/#18/#20):
  10 council tools  — council_start/poll/answer/ask/reveal/is_model_replied/
                      score/close, model_scores, seat_health
   6 chat tools     — chat_start/send/poll/history/list/close (P1 stubs)
   1 discovery tool — list_seats (P1 stub)

All LLM interaction happens via seat CLI subprocesses — there is NO direct LLM
HTTP anywhere in this package (Q16). Runs over stdio; never logs to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import logging
import logging.handlers
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import availability, chat, council, db, telemetry
from .config import Config, state_dir

if sys.version_info < (3, 11):
    # Module-level so it never shadows the py3.11+ builtin inside main().
    from exceptiongroup import BaseExceptionGroup  # type: ignore[attr-defined]  # noqa: ICN003

mcp = FastMCP("llm-council")

_log = logging.getLogger("llm_council")


def _setup_logging() -> None:
    """A8: RotatingFileHandler 5MB×3 at ~/.blessthis-llm-council/server.log
    (LLM_COUNCIL_LOG_FILE overrides), level via LLM_COUNCIL_LOG_LEVEL (default
    INFO). A stdio MCP server must NOT log to stdout — that's the protocol
    channel. The `asyncio` logger propagates to root, so background seat-task
    crashes land here too. Secret env values are never logged."""
    path = os.environ.get(
        "LLM_COUNCIL_LOG_FILE", str(state_dir() / "server.log")
    )
    level_name = os.environ.get("LLM_COUNCIL_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(handler)
        _log.info(
            "blessthis-llm-council logging started -> %s (instance %s)",
            path, council.INSTANCE_ID,
        )
    except Exception:  # noqa: BLE001 — never let logging setup break startup
        pass


_cfg: Config | None = None
_seats_file_override: str | None = None
_init_lock = asyncio.Lock()


async def _ensure() -> Config:
    global _cfg
    async with _init_lock:
        if _cfg is None:
            # Build into locals and only publish to the module global once EVERY
            # await has succeeded — a mid-init failure must not leave a half-init.
            cfg = Config.load(
                seats_file_override=_seats_file_override,
                instance_id=council.INSTANCE_ID,
            )
            await db.init_pool(cfg.database_url)
            # Share the resolved Config (incl. --seats-file override) with chat.
            chat.set_config(cfg)
            # Register THIS process and reap only councils whose owner is dead, then
            # start heartbeating. Done here (server startup) — NOT in init_pool — so
            # ad-hoc scripts / other projects' servers can't nuke live councils.
            await council.register_instance()
            council.start_heartbeat()
            # Surface unhandled exceptions from background seat tasks to the log file
            # instead of letting them vanish.
            asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
            # P6: opportunistic retry of queued telemetry events (never blocks
            # startup, never raises into the server).
            asyncio.create_task(telemetry.retry_pending())
            _cfg = cfg
    assert _cfg is not None
    return _cfg


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    msg = context.get("message", "")
    if exc is not None:
        _log.error("unhandled asyncio exception: %s", msg, exc_info=exc)
    else:
        _log.error("asyncio loop error: %s | %s", msg, context)


def _json(obj: Any) -> Any:
    """Make DB rows / datetimes JSON-friendly."""
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json(v) for v in obj]
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    return obj


# --------------------------------------------------------------------------- #
# Council: blind multi-model fan-out for hard-problem diagnosis
# --------------------------------------------------------------------------- #

@mcp.tool()
async def council_start(
    brief: str,
    models: list[str] = [],  # noqa: B006 — read-only default, never mutated
    working_dir: str = "",
    seat_system_prompt: str = "",
    kind: str = "adhoc",
) -> dict:
    """Convene a blind multi-model council on a hard problem. Fans `brief` out to one
    seat session per model — each an independent file-reading agent bound to
    `working_dir` — runs them CONCURRENTLY in the background, and returns IMMEDIATELY
    with a council_id and blind hat labels (hat1, hat2, ...). The hat->model mapping is
    hidden (revealed only via council_reveal), so you can synthesize across seats without
    knowing which model produced which answer. Poll for answers with council_poll.

    models: OPTIONAL — omit it (or pass []) to get the default one-seat-per-family
    roster. working_dir defaults to the server's current working directory.

    kind: what this council is FOR, e.g. "bug", "review", "architecture" (default "adhoc").
    Purely descriptive record-keeping — never affects routing — but persisted so the council
    history shows why a council was convened."""
    cfg = await _ensure()
    wd = working_dir.strip() or os.getcwd()
    return _json(await council.start(
        cfg, models, wd, brief, seat_system_prompt.strip(), kind.strip() or "adhoc",
    ))


@mcp.tool()
async def council_poll(council_id: int, wait: bool = True, timeout: int = 360) -> dict:
    """Poll a council for seat STATUS ONLY (blind — no model names, no answer bodies, so
    it never bloats your context). Each hat returns its status; a DONE hat returns
    `answer_chars` (size hint); a RUNNING hat returns live `progress` {turns,
    output_tokens} read natively from its transcript so you can tell it's advancing vs
    wedged. With wait=True this long-polls up to `timeout` seconds, returning early as
    soon as a new seat finishes or all are done.

    FIRST poll: use timeout >= 360 — seats take minutes, and a short first poll just
    burns turns on no-op status checks (the long-poll returns EARLY the moment any
    seat finishes, so a big timeout costs nothing). Subsequent polls can be shorter.
    Call repeatedly until done=true, THEN fetch each answer with
    council_answer(council_id, hat)."""
    await _ensure()
    return _json(await council.poll(council_id, wait=wait, timeout=max(1, min(timeout, 600))))


@mcp.tool()
async def council_answer(council_id: int, hat: str) -> dict:
    """Fetch ONE seat's FULL answer text by blind hat label (e.g. 'hat2') — the content
    council_poll deliberately omits to keep your context lean. Call once a hat shows
    status=done in council_poll. Never reveals the model."""
    await _ensure()
    return _json(await council.get_answer(council_id, hat.strip()))


@mcp.tool()
async def council_ask(council_id: int, hat: str, message: str) -> dict:
    """Cross-examine ONE seat by its blind hat label (e.g. 'hat2') within a council.
    Runs a follow-up turn on that seat's session (it may re-read files) and returns its
    reply synchronously. Never reveals the model. Use to probe disagreements before you
    synthesize."""
    cfg = await _ensure()
    return _json(await council.ask(cfg, council_id, hat.strip(), message))


@mcp.tool()
async def council_reveal(council_id: int) -> dict:
    """De-anonymize a council: return the hat->model mapping and per-seat status. For
    human/debug insight AFTER synthesis — do NOT use this to weight the diagnosis."""
    await _ensure()
    return _json(await council.reveal(council_id))


@mcp.tool()
async def council_is_model_replied(council_id: int, model: str) -> bool:
    """Blind check: has `model` finished answering (status=done) in this council? Returns
    True/False only — never the hat label or answer — so you can confirm a model
    participated without breaking hat blindness for scoring/synthesis."""
    await _ensure()
    return await council.is_model_replied(council_id, model.strip())


@mcp.tool()
async def council_score(council_id: int, scores: list[dict]) -> dict:
    """Score each seat's reply — the MANDATORY end step of every council, AFTER your
    synthesis and BEFORE council_reveal (so scores can't be biased by model identity).
    scores: [{"hat": "hat1", "score": 7, "notes": "verified root cause, thin on fix"}, ...]
    score is 1-10; judge correctness against the verified code, depth, and actionability.
    The server resolves hat->model itself, feeding the per-model quality leaderboard
    (see model_scores)."""
    await _ensure()
    return _json(await council.score(council_id, scores))


@mcp.tool()
async def model_scores() -> list[dict]:
    """Per-model quality leaderboard from all council scores: avg_score, eval count,
    and the 5 most recent scores per model."""
    await _ensure()
    return [_json(r) for r in await council.model_scores()]


@mcp.tool()
async def seat_health() -> list[dict]:
    """Dump the seat/model availability cache: per-model status (ok/cooldown), the
    classified reason (balance/quota), the last error, and cooldown expiry. Seats
    record capacity failures here; council_start skips models in cooldown.
    (Renamed from model_health, Decision #20; backing table renamed in P2.)"""
    await _ensure()
    return [_json(r) for r in await availability.list_health()]


@mcp.tool()
async def council_close(council_id: int) -> dict:
    """Mark a council closed. Records are KEPT (council, hats with answers/errors,
    and seat sessions) for history/debugging — nothing is deleted. Score the seats
    with council_score FIRST if you haven't."""
    await _ensure()
    return _json(await council.close(council_id))


# --------------------------------------------------------------------------- #
# Direct seat chat (Decision #18) — P1 NotImplementedError stubs (P3: SeatRunner)
# --------------------------------------------------------------------------- #

@mcp.tool()
async def chat_start(
    seat: str, model: str = "", working_dir: str = "", system_prompt: str = ""
) -> dict:
    """Open a direct 1:1 chat with one seat (seats.yaml key). Returns
    {chat_session_id, seat, model}. model defaults to the seat's first healthy model."""
    await _ensure()
    return _json(await chat.chat_start(seat, model, working_dir, system_prompt))


@mcp.tool()
async def chat_send(chat_session_id: int, message: str) -> dict:
    """Send a message to the chat's seat — ASYNC; returns immediately with {task_id}.
    Poll the turn with chat_poll."""
    await _ensure()
    return _json(await chat.chat_send(chat_session_id, message))


@mcp.tool()
async def chat_poll(task_id: str, wait: bool = True, timeout: int = 360) -> dict:
    """Long-poll a queued chat turn; returns early when the turn completes:
    {status, reply?, usage?, progress?}."""
    await _ensure()
    return _json(await chat.chat_poll(task_id, wait, timeout))


@mcp.tool()
async def chat_history(chat_session_id: int) -> list[dict]:
    """Message-level history of a chat: [{role, content, ts, usage}, ...]."""
    await _ensure()
    return [_json(r) for r in await chat.chat_history(chat_session_id)]


@mcp.tool()
async def chat_list(working_dir: str = "") -> list[dict]:
    """List chat sessions (closed included, marked), optionally filtered to a
    single working directory."""
    await _ensure()
    return [_json(r) for r in await chat.chat_list(working_dir)]


@mcp.tool()
async def chat_close(chat_session_id: int) -> dict:
    """Mark a chat closed. History is KEPT; a running turn is cancelled."""
    await _ensure()
    return _json(await chat.chat_close(chat_session_id))


# --------------------------------------------------------------------------- #
# Discovery (Decision #20) — P1 stub (P3: seats.yaml loader)
# --------------------------------------------------------------------------- #

@mcp.tool()
async def list_seats() -> dict:
    """List the seats defined in seats.yaml: {seats: [{name, models, runner_kind},
    ...], warnings: [...]} (loader warnings surfaced, e.g. a seat skipped for
    validation errors). A seat is an LLM family served by a local agent CLI
    (claude/pi/codex)."""
    await _ensure()
    return _json(await chat.list_seats())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blessthis-llm-council-server",
        description="blessthis-llm-council MCP server (stdio).",
    )
    parser.add_argument(
        "--seats-file",
        default=None,
        help="Read-only override for the seats.yaml path (wins over SEATS_FILE).",
    )
    args = parser.parse_args()

    global _seats_file_override
    _seats_file_override = args.seats_file

    _setup_logging()
    from .crash import install

    install(headless_ok=True)
    try:
        mcp.run()
    except (BrokenPipeError, ConnectionResetError) as exc:
        # Host closed the stdio pipe (session restart/exit) — benign shutdown.
        _log.info("stdio pipe closed by host (%s) — exiting cleanly", exc)
    except BaseException as exc:
        # anyio wraps the BrokenPipeError in a TaskGroup ExceptionGroup; only
        # treat it as a crash if a sub-exception is something other than a
        # broken/reset pipe.
        causes = (exc.exceptions if isinstance(exc, BaseExceptionGroup)
                  else (exc,))
        if all(isinstance(e, (BrokenPipeError, ConnectionResetError))
               for e in causes):
            _log.info("stdio pipe closed by host (%s) — exiting cleanly", exc)
        else:
            raise


if __name__ == "__main__":
    main()
