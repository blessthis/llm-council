"""Multi-model 'council' for hard-problem diagnosis.

A council fans a single brief out to N model-pinned seats that each read the
working directory independently, runs them concurrently as background tasks, and
exposes their answers as blind 'hat' labels (hat1, hat2, ...). The hat->model map
is kept server-side and revealed only via `reveal`, so an orchestrator can
synthesize across seats without knowing which model produced which answer
(defusing self-preference / authority bias at the synthesis step).

Each seat is a real `claude -p` subprocess (see claude_seat.py) with its model
swapped via `--model` and all traffic routed through the gateway — so we get the
CLI's robust agent loop and a clean final answer instead of the old bespoke loop
that hung and stored tool-call preambles as "answers".

Async model: `start` schedules one background task per seat on the running event
loop and returns immediately. Status + answers are persisted to Postgres, so
`poll` is a stateless DB read (with optional long-poll). One slow or failing seat
never blocks the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import chat, db, telemetry
from .config import Config
from .seats.base import Seat
from .seats.loader import SeatsFileError
from .seats.runners import get_runner

logger = logging.getLogger(__name__)

# Unique id for THIS server process. Councils are tagged with it, and the reaper
# only terminalizes councils whose owner instance has stopped heartbeating — so a
# passing process / restart / ad-hoc init_pool never nukes councils that are live
# in another process. (uuid4 is fine here — this is the MCP server, not a workflow
# sandbox; the value is generated once at import and persisted via the DB.)
INSTANCE_ID = uuid.uuid4().hex
_HEARTBEAT_SECONDS = 15
_STALE_SECONDS = 90  # an owner silent this long is considered dead

# Hold strong references to in-flight seat tasks: asyncio keeps only weak refs, so
# without this a background seat could be garbage-collected mid-run.
_bg_tasks: set[asyncio.Task] = set()
_hb_task: asyncio.Task | None = None


def _track(task: asyncio.Task) -> None:
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _safe(coro, what: str) -> None:
    """Await a status-persistence coroutine, swallowing+logging DB errors so a
    DB hiccup writing status can never crash a seat task or strand its council.
    PoolClosedError (pool torn down mid-run, e.g. process shutdown) is expected:
    state is lost by design then — warn and drop the write, never crash."""
    try:
        await coro
    except db.PoolClosedError:
        logger.warning("council: pool closed, dropping write (%s)", what)
    except Exception:  # noqa: BLE001
        logger.exception("council: %s failed", what)


# --------------------------------------------------------------------------- #
# Instance ownership + heartbeat (multi-process-safe reaping)
# --------------------------------------------------------------------------- #

async def register_instance() -> None:
    """Register this process, reap councils orphaned by DEAD owners, then leave the
    heartbeat running. Called ONCE from the server on startup (not from init_pool),
    so ad-hoc scripts that merely open the pool never reap anything.

    Ownership-aware: a council owned by a live, heartbeating process is spared; only
    councils whose owner is stale (crashed process) or NULL (legacy) are reaped.
    The same sweep also terminalizes chat_sessions rows left 'running' by a
    server that died mid-turn (A2)."""
    now = db.utcnow()
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=_STALE_SECONDS)
    ).isoformat()
    await db.execute(
        "INSERT INTO mcp_instances (instance_id, last_seen) VALUES (?, ?) "
        "ON CONFLICT (instance_id) DO UPDATE SET last_seen=?",
        (INSTANCE_ID, now, now),
    )
    await db.execute(
        """UPDATE council_hats SET status='error',
                error='interrupted: owner instance gone', updated_at=?
            WHERE status IN ('queued','running')
              AND council_id IN (
                SELECT c.id FROM councils c
                WHERE c.owner IS NULL
                   OR c.owner NOT IN (
                     SELECT instance_id FROM mcp_instances
                     WHERE last_seen > ?))""",
        (now, stale_before),
    )
    await db.execute(
        """UPDATE councils AS c SET status='done', updated_at=?
           WHERE c.status NOT IN ('done','closed')
             AND NOT EXISTS (SELECT 1 FROM council_hats h
                             WHERE h.council_id=c.id AND h.status IN ('queued','running'))""",
        (now,),
    )
    # A2: a chat turn in-flight when the previous server process died can never
    # complete — mark it errored and leave a visible note in its history.
    await db.execute(
        """INSERT INTO chat_messages (chat_session_id, role, content, created_at)
           SELECT id, 'assistant', 'server restarted mid-turn', ?
           FROM chat_sessions WHERE status='running'""",
        (now,),
    )
    await db.execute(
        "UPDATE chat_sessions SET status='error', last_activity=? WHERE status='running'",
        (now,),
    )


async def _heartbeat_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            await db.execute(
                "UPDATE mcp_instances SET last_seen=? WHERE instance_id=?",
                (db.utcnow(), INSTANCE_ID),
            )
        except asyncio.CancelledError:
            raise
        except db.PoolClosedError:
            # Pool torn down (shutdown): stop heartbeating cleanly instead of
            # spinning once per interval forever on a dead DB.
            logger.warning("council: heartbeat stopping — db pool closed")
            return
        except Exception:  # noqa: BLE001
            logger.exception("council: heartbeat failed")


def start_heartbeat() -> None:
    """Spawn the background heartbeat task (idempotent)."""
    global _hb_task
    if _hb_task is None or _hb_task.done():
        _hb_task = asyncio.create_task(_heartbeat_loop())


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

async def _create_council(working_dir: str, brief: str, kind: str) -> int:
    now = db.utcnow()
    return await db.insert_returning_id(
        "INSERT INTO councils "
        "(working_dir, brief, owner, council_uuid, kind, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (working_dir, brief, INSTANCE_ID, str(uuid.uuid4()), kind, now, now),
    )


async def _add_hat(council_id: int, hat: str, model: str, seat_backend: str) -> int:
    now = db.utcnow()
    return await db.insert_returning_id(
        """INSERT INTO council_hats
           (council_id, hat, model, seat_backend, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
        (council_id, hat, model, seat_backend, now, now),
    )


async def _mark_running(hat_id: int) -> None:
    await db.execute(
        "UPDATE council_hats SET status='running', updated_at=? WHERE id=?",
        (db.utcnow(), hat_id),
    )


async def _set_hat_session(hat_id: int, session_id: str) -> None:
    await db.execute(
        "UPDATE council_hats SET session_id=?, updated_at=? WHERE id=?",
        (session_id, db.utcnow(), hat_id),
    )


async def _mark_done(
    hat_id: int, answer: str, usage: dict | None, session_id: str | None,
    used_model: str | None = None,
) -> None:
    await db.execute(
        """UPDATE council_hats SET status='done', answer=?, usage=?,
           session_id=?, model=COALESCE(?, model), updated_at=?
           WHERE id=?""",
        (answer, json.dumps(usage or {}), session_id, used_model, db.utcnow(), hat_id),
    )


async def _mark_error(hat_id: int, error: str) -> None:
    await db.execute(
        "UPDATE council_hats SET status='error', error=?, updated_at=? WHERE id=?",
        (error[:2000], db.utcnow(), hat_id),
    )


async def _refresh_status(council_id: int) -> None:
    """Mark the council 'done' once no hat is still queued/running."""
    pending = await db.fetchval(
        """SELECT count(*) FROM council_hats
           WHERE council_id=? AND status IN ('queued','running')""",
        (council_id,),
    )
    if pending == 0:
        await db.execute(
            "UPDATE councils SET status='done', updated_at=? WHERE id=?",
            (db.utcnow(), council_id),
        )


async def _get_council(council_id: int) -> dict[str, Any] | None:
    return await db.fetchrow("SELECT * FROM councils WHERE id=?", (council_id,))


async def _get_hats(council_id: int) -> list[dict[str, Any]]:
    return await db.fetch(
        "SELECT * FROM council_hats WHERE council_id=? ORDER BY hat", (council_id,)
    )


# --------------------------------------------------------------------------- #
# Seat execution
# --------------------------------------------------------------------------- #

async def _run_seat(
    council_id: int,
    hat_id: int,
    seat: Seat,
    model: str,
    working_dir: str,
    brief: str,
    seat_system_prompt: str,
) -> None:
    """Background task: run one seat through its SeatRunner and record the
    result (clean final answer + the CLI session id for later cross-examination).

    The seat work and the status persistence are separated: a DB hiccup while
    writing status must never crash the task or leave the hat stuck 'running'
    (which would wedge the council forever), so every status write goes through
    `_safe`. CancelledError (clean shutdown) is marked and re-raised."""
    await _safe(_mark_running(hat_id), f"mark_running(hat={hat_id})")
    answer: str | None = None
    usage: dict | None = None
    session_id: str | None = None
    err: str | None = None
    used_model: str | None = None

    async def _on_session(sid: str) -> None:
        # Record the CLI session id the moment an attempt starts, so council_poll can
        # read live progress from its transcript and a resumable id survives even if the
        # seat later errors.
        nonlocal session_id
        session_id = sid
        await _safe(_set_hat_session(hat_id, sid), f"set_session(hat={hat_id})")

    try:
        runner = get_runner(seat.runner_kind)
        result = await runner.invoke(
            seat, model, brief, working_dir,
            system_prompt=seat_system_prompt, capture_to_file=True,
            on_session=_on_session,
        )
        answer = result.response or "(no output)"
        usage = {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_read_tokens": result.usage.cache_read_tokens,
            "cache_write_tokens": result.usage.cache_write_tokens,
        }
        session_id = result.session_id
        # The seat may have escalated along its fallback chain — persist the model
        # that ACTUALLY answered so reveal/scoring attribute correctly.
        used_model = result.model
        if result.is_error:
            err = f"seat reported error: {answer}"
    except asyncio.CancelledError:
        await _safe(_mark_error(hat_id, "cancelled: server shutting down"), "mark_error(cancelled)")
        await _safe(_refresh_status(council_id), "refresh_status")
        raise
    except Exception as e:  # noqa: BLE001 — surface seat failures without killing the council
        err = repr(e)
    # Persist crash-safely — a failure here must not strand the hat as 'running'.
    if err is None:
        await _safe(
            _mark_done(hat_id, answer, usage, session_id, used_model),
            f"mark_done(hat={hat_id})",
        )
    else:
        await _safe(_mark_error(hat_id, err), f"mark_error(hat={hat_id})")
    await _safe(_refresh_status(council_id), f"refresh_status(council={council_id})")


# --------------------------------------------------------------------------- #
# Public API (called by the MCP tools in server.py)
# --------------------------------------------------------------------------- #

async def start(
    cfg: Config,
    models: list[str],
    working_dir: str,
    brief: str,
    seat_system_prompt: str = "",
    kind: str = "adhoc",
) -> dict[str, Any]:
    """Resolve the roster from seats.yaml, launch one seat per entry concurrently,
    and return immediately with blind hat labels. The hat->seat/model pairing is
    hidden.

    Roster (A1): an empty `models` arg seats EVERY seat in seats.yaml file order,
    one hat per seat, model = first healthy model in that seat's models[] — NO
    dedup across seats (diversity comes from creds, not names). An explicit
    `models` arg seats only seats whose models[] intersects it (non-matching
    requests are reported in `dropped_models`). A seat with zero healthy models
    is skipped with a note; an empty resulting roster returns the §8.1 error
    `no_healthy_model`.

    kind: free-text label for WHY this council was convened (e.g. 'bug', 'review',
    'architecture'). Purely descriptive — never affects routing — but persisted so
    the DB (and tools like the statusline) can show what a council is for."""
    try:
        seats, _warnings = chat._load_seats()
    except SeatsFileError as e:
        msg = str(e)
        code = "seats_file_missing" if "not found" in msg else "seats_file_invalid"
        return {"error": {"code": code, "message": msg}}

    requested = [m.strip() for m in models if m and m.strip()]
    candidates: list[Seat]
    dropped: list[str] = []
    if not requested:
        candidates = list(seats)
    else:
        req = set(requested)
        candidates = [s for s in seats if req.intersection(s.models)]
        matched = {m for s in candidates for m in s.models}
        dropped = [m for m in requested if m not in matched]

    # Health-pick each seat's model (cache first, probe on unknown health —
    # probes run concurrently). A seat with zero healthy models is skipped
    # with a note, not an error.
    async def _select(seat: Seat) -> tuple[Seat, str | None]:
        try:
            return seat, await chat._pick_model(seat)
        except Exception:  # noqa: BLE001 — health cache down must not block councils
            logger.exception("council: availability check failed for %s", seat.name)
            return seat, seat.models[0]

    results = await asyncio.gather(*(_select(s) for s in candidates))
    roster: list[tuple[Seat, str]] = []
    unavailable: dict[str, str] = {}
    for seat, picked in results:
        if picked is None:
            unavailable[seat.name] = "no healthy model (all in cooldown / failed probe)"
        else:
            roster.append((seat, picked))
    if not roster:
        return {
            "error": {
                "code": "no_healthy_model",
                "message": f"no seat has a healthy model: requested={requested}, "
                           f"unavailable={unavailable}, dropped={dropped}",
            }
        }

    council_id = await _create_council(working_dir, brief, kind.strip() or "adhoc")

    # Shuffle which seat gets which hat so the label order leaks nothing about
    # the seats.yaml file ordering.
    hats = [f"hat{i + 1}" for i in range(len(roster))]
    shuffled = roster[:]
    random.shuffle(shuffled)

    # Insert ALL hat rows FIRST, then launch the seat tasks. If we launched each
    # task inside this loop, a fast-failing early seat could run _refresh_status
    # before the later hats are inserted, see pending==0, and mark the council
    # 'done' prematurely (poll would return truncated results).
    seated: list[tuple[int, Seat, str]] = []  # (hat_id, seat, model)
    for hat, (seat, model) in zip(hats, shuffled, strict=False):
        hat_id = await _add_hat(council_id, hat, model, seat.runner_kind)
        seated.append((hat_id, seat, model))
    for hat_id, seat, model in seated:
        _track(asyncio.create_task(
            _run_seat(council_id, hat_id, seat, model, working_dir, brief,
                      seat_system_prompt)
        ))

    return {
        "council_id": council_id,
        "kind": kind.strip() or "adhoc",
        "working_dir": working_dir,
        "hats": hats,               # blind labels
        "models": sorted(model for _s, model in roster),  # roster known; pairing not
        "count": len(roster),
        "dropped_models": dropped,
        "unavailable": unavailable,   # seats skipped for zero healthy models
        "status": "running",
        "note": "Poll with council_poll(council_id) until done=true.",
    }


def _read_progress(seat_backend: str | None, session_id: str | None) -> dict[str, int] | None:
    """Best-effort LIVE progress for a running seat, read through its runner's
    read_progress() (gated on supports_progress()). Returns a monotonically growing
    signal so the orchestrator can tell a slow seat is progressing vs wedged.
    None when unsupported, no session id yet, or no transcript yet."""
    if not seat_backend or not session_id:
        return None
    try:
        runner = get_runner(seat_backend)
        if not runner.supports_progress():
            return None
        prog = runner.read_progress(session_id)
    except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
        return None
    if prog is None:
        return None
    return {"turns": prog.turns, "output_tokens": prog.output_tokens}


def _render(council_id: int, council: dict[str, Any], hats: list[dict[str, Any]]) -> dict[str, Any]:
    """Blind STATUS view — never model names, never the full answer body (that bloats the
    orchestrator's context on every poll). Fetch a seat's text with council_answer(). A
    running seat carries live `progress` (turns/output_tokens) read from its transcript."""
    out_hats = []
    for h in hats:
        entry: dict[str, Any] = {"hat": h["hat"], "status": h["status"]}
        if h["status"] == "done":
            entry["answer_chars"] = len(h["answer"] or "")
        elif h["status"] == "error":
            entry["error"] = (h["error"] or "")[:300]
        elif h["status"] == "running":
            prog = _read_progress(h.get("seat_backend"), h.get("session_id"))
            if prog:
                entry["progress"] = prog
        out_hats.append(entry)
    return {
        "council_id": council_id,
        "kind": council["kind"],
        "status": council["status"],
        "done": council["status"] == "done",
        "ready": sum(1 for h in hats if h["status"] == "done"),
        "errored": sum(1 for h in hats if h["status"] == "error"),
        "total": len(hats),
        "hats": out_hats,
        "note": "Statuses only. Fetch a seat's full text with council_answer(council_id, hat).",
    }


async def get_answer(council_id: int, hat: str) -> dict[str, Any]:
    """Return ONE seat's full answer text (or error) by blind hat label — the content
    council_poll deliberately omits. Never reveals the model."""
    council = await _get_council(council_id)
    if not council:
        return {"error": {"code": "unknown_council",
                          "message": f"council {council_id} not found"}}
    hats = await _get_hats(council_id)
    match = next((h for h in hats if h["hat"] == hat), None)
    if not match:
        return {"error": {"code": "unknown_hat",
                          "message": f"hat '{hat}' not found in council {council_id}"}}
    return {
        "council_id": council_id,
        "hat": hat,
        "status": match["status"],
        "answer": match["answer"],
        "error": match["error"],
    }


async def poll(council_id: int, wait: bool = True, timeout: int = 20) -> dict[str, Any]:
    """Return current seat statuses/answers (blind). With wait=True, long-poll up to
    `timeout` seconds (an ABSOLUTE deadline — never longer), returning early as soon
    as a NEW seat finishes or all are done. A dead/closed DB returns an {error} dict
    instead of raising or hanging."""
    try:
        council = await _get_council(council_id)
        if not council:
            return {"error": {"code": "unknown_council",
                              "message": f"council {council_id} not found"}}
        hats = await _get_hats(council_id)

        def terminal_count(hs: list[dict]) -> int:
            return sum(1 for h in hs if h["status"] in ("done", "error"))

        if wait and council["status"] != "done":
            baseline = terminal_count(hats)
            deadline = asyncio.get_running_loop().time() + max(0, timeout)
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(1.0)
                council = await _get_council(council_id)
                hats = await _get_hats(council_id)
                if council["status"] == "done" or terminal_count(hats) > baseline:
                    break
    except Exception as e:  # noqa: BLE001 — a dead DB must not hang or raise out of poll
        logger.exception("council: poll(%s) db read failed", council_id)
        return {"error": {"code": "db_unavailable",
                          "message": f"db read failed while polling: {e!r}"}}

    return _render(council_id, council, hats)


async def _seat_for_hat(match: dict[str, Any]) -> Seat | None:
    """Resolve the Seat definition a finished hat ran on, so council_ask can
    resume it. council_hats stores only model + seat_backend (Q6 freeze), so we
    match against seats.yaml: a seat qualifies when its runner_kind matches and
    the hat's model is in its models[]. First match wins (two seats sharing a
    model AND runner kind are ambiguous — a documented v1 limitation)."""
    try:
        seats, _warnings = chat._load_seats()
    except SeatsFileError:
        return None
    backend = match.get("seat_backend")
    model = match.get("model")
    for s in seats:
        if s.runner_kind == backend and model in s.models:
            return s
    return None


async def ask(
    cfg: Config, council_id: int, hat: str, message: str
) -> dict[str, Any]:
    """Cross-examine one seat by its blind hat label. Resumes that seat's CLI
    session via its runner (it may re-read files) and returns the reply
    synchronously."""
    del cfg  # creds come from the seat definition, not Config (seatspec §4.10)
    council = await _get_council(council_id)
    if not council:
        return {"error": {"code": "unknown_council",
                          "message": f"council {council_id} not found"}}
    hats = await _get_hats(council_id)
    match = next((h for h in hats if h["hat"] == hat), None)
    if not match:
        return {"error": {"code": "unknown_hat",
                          "message": f"hat '{hat}' not found in council {council_id}"}}
    if not match.get("session_id"):
        return {"error": {"code": "resume_failed",
                          "message": f"seat {hat} has no resumable session "
                                     "(it may have errored before answering)"}}
    seat = await _seat_for_hat(match)
    if seat is None:
        return {"error": {"code": "resume_failed",
                          "message": f"cannot resolve the seat definition for hat "
                                     f"'{hat}' (model={match['model']}, backend="
                                     f"{match.get('seat_backend')}) — seats.yaml "
                                     "may have changed since the council ran"}}
    try:
        runner = get_runner(seat.runner_kind)
        result = await runner.invoke(
            seat, match["model"], message, council["working_dir"],
            resume=match["session_id"],
        )
    except Exception as e:  # noqa: BLE001
        return {"error": {"code": "resume_failed", "message": repr(e),
                          "council_id": council_id, "hat": hat}}
    if result.is_error:
        return {"error": {"code": "resume_failed", "message": result.response,
                          "council_id": council_id, "hat": hat}}
    return {
        "council_id": council_id,
        "hat": hat,
        "response": result.response,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_read_tokens": result.usage.cache_read_tokens,
            "cache_write_tokens": result.usage.cache_write_tokens,
        },
    }


async def reveal(council_id: int) -> dict[str, Any]:
    """De-anonymize: return the hat->model map and per-seat status. For human/debug
    insight AFTER synthesis — never to weight the diagnosis."""
    council = await _get_council(council_id)
    if not council:
        return {"error": {"code": "unknown_council",
                          "message": f"council {council_id} not found"}}
    hats = await _get_hats(council_id)
    return {
        "council_id": council_id,
        "kind": council["kind"],
        "status": council["status"],
        "map": {h["hat"]: h["model"] for h in hats},
        "hats": [
            {"hat": h["hat"], "model": h["model"], "status": h["status"],
             "session_id": h.get("session_id"),
             "seat_backend": h.get("seat_backend")}
            for h in hats
        ],
    }


async def is_model_replied(council_id: int, model: str) -> bool:
    """Blind existence check: has `model`'s seat finished with status='done' in this
    council? Never reveals which hat it occupies — just yes/no, so callers can confirm
    a model participated without breaking hat blindness for scoring/synthesis."""
    hats = await _get_hats(council_id)
    return any(h["model"] == model and h["status"] == "done" for h in hats)


async def score(council_id: int, scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Record the orchestrator's blind 1-10 score per hat. The hat->model mapping is
    resolved SERVER-SIDE, so the caller scores before (and without) knowing which
    model wore which hat — the per-model leaderboard stays bias-free."""
    council = await _get_council(council_id)
    if not council:
        return {"error": {"code": "unknown_council",
                          "message": f"council {council_id} not found"}}
    hats = {h["hat"]: h for h in await _get_hats(council_id)}
    recorded: list[dict[str, Any]] = []
    errors: list[str] = []
    for s in scores:
        hat = str(s.get("hat", "")).strip()
        match = hats.get(hat)
        if not match:
            errors.append(f"hat '{hat}' not found")
            continue
        try:
            val = int(s.get("score"))
        except (TypeError, ValueError):
            errors.append(f"hat '{hat}': score must be an integer 1-10")
            continue
        if not 1 <= val <= 10:
            errors.append(f"hat '{hat}': score {val} out of range 1-10")
            continue
        now = db.utcnow()
        notes = (s.get("notes") or "")[:1000]
        await db.execute(
            """INSERT INTO council_scores (council_id, hat, model, score, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (council_id, hat) DO UPDATE
                 SET score=?, notes=?, model=?, created_at=?""",
            (council_id, hat, match["model"], val, notes, now,
             val, notes, match["model"], now),
        )
        recorded.append({"hat": hat, "score": val})
    if recorded:
        # P6 telemetry: never blocking, never raising into the council flow.
        _track(asyncio.create_task(telemetry.maybe_flush_council(council_id)))
    return {
        "council_id": council_id,
        "recorded": recorded,
        "errors": errors,
        "note": "Scores attributed to models server-side (blind). "
                "See model_scores for the leaderboard.",
    }


async def model_scores() -> list[dict[str, Any]]:
    """Per-model quality leaderboard aggregated from all council scores."""
    rows = await db.fetch(
        """SELECT model, avg(score) AS avg_score, count(*) AS evals,
                  max(created_at) AS last_scored
           FROM council_scores GROUP BY model ORDER BY avg(score) DESC"""
    )
    out = []
    for r in rows:
        recent = await db.fetch(
            "SELECT score FROM council_scores WHERE model=? "
            "ORDER BY created_at DESC LIMIT 5",
            (r["model"],),
        )
        out.append({
            "model": r["model"],
            "avg_score": round(float(r["avg_score"]), 2),
            "evals": r["evals"],
            "recent_scores": [s["score"] for s in recent],
            "last_scored": r["last_scored"],
        })
    return out


async def close(council_id: int) -> dict[str, Any]:
    """Mark a council closed WITHOUT deleting anything. The council row, the
    council_hats (with each seat's answer/error/model/status) and the seat
    sessions are all KEPT for history and post-hoc debugging — otherwise a failed
    or restarted council leaves no trace of why it failed. Reclaim space later with
    an explicit purge if ever needed."""
    council = await _get_council(council_id)
    if not council:
        return {"error": {"code": "unknown_council",
                          "message": f"council {council_id} not found"}}
    await db.execute(
        "UPDATE councils SET status='closed', updated_at=? WHERE id=?",
        (db.utcnow(), council_id),
    )
    return {"council_id": council_id, "closed": True, "records_kept": True}
