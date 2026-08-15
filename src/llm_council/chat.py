"""Direct seat-chat subsystem (Decision #18, P3d).

Standalone single-seat conversations via CLI subprocess (one-on-one, NOT a
council), persisted to chat_sessions / chat_messages (Decision #19).

Turn model (Q27=b): `chat_send` inserts the user message, spawns an in-memory
asyncio task running the seat's SeatRunner, and returns {task_id} immediately.
The task coroutine ITSELF persists the assistant reply / usage / cli session
id when it finishes — so a caller that never polls still gets its history
written (fire-and-forget safety). `chat_poll` long-polls the task registry and
renders live progress (A13) when the runner supports it.

Error contract (§8.1): failures return {error: {code, message}} — never raise.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
import uuid
from pathlib import Path

from . import availability, db
from .config import Config
from .seats.base import Seat
from .seats.loader import SeatsFileError, load_seats
from .seats.runners import get_runner

logger = logging.getLogger(__name__)

# In-memory task registry (A2): task_id -> {session_id, task, started, seat,
# model, runner_kind, cli_session_id}. Lives only in THIS process — a server
# restart orphans task_ids, and chat_poll reports `unknown_task`; the A2
# startup sweep terminalizes any chat_sessions left 'running'.
_tasks: dict[str, dict] = {}

_cfg: Config | None = None


def set_config(cfg: Config) -> None:
    """Called by the server after _ensure() so chat tools see the same Config
    (including the --seats-file override). Falls back to Config.load() when
    unset (direct library use / tests)."""
    global _cfg
    _cfg = cfg


def _config() -> Config:
    return _cfg or Config.load()


def _load_seats() -> tuple[list[Seat], list[str]]:
    """Load seats.yaml, raising SeatsFileError for both missing and invalid
    files (callers map to the seats_file_missing / seats_file_invalid codes)."""
    path = Path(_config().seats_file)
    if not path.exists():
        raise SeatsFileError([f"seats file not found: {path}."])
    return load_seats(path)


async def _pick_model(seat: Seat) -> str | None:
    """First HEALTHY model in seat.models (probe on unknown health, cache
    first). None when every model is in cooldown / fails its probe."""
    for m in seat.models:
        status = await availability.is_available(seat.name, m)
        if status is True:
            return m
        if status is False:
            continue
        if await availability.probe(seat, m):
            return m
    return None


# --------------------------------------------------------------------------- #
# Turn execution
# --------------------------------------------------------------------------- #

async def _run_turn(task_id: str, entry: dict, seat: Seat, message: str) -> None:
    """Background coroutine for ONE chat turn. Persists the assistant reply,
    usage, and the CLI session id itself — poll is a read, not the writer."""
    session_id: int = entry["session_id"]
    cli_sid: str | None = None

    async def _on_session(sid: str) -> None:
        nonlocal cli_sid
        cli_sid = sid
        entry["cli_session_id"] = sid

    row = await db.fetchrow(
        "SELECT model, working_dir, system_prompt, cli_session_id "
        "FROM chat_sessions WHERE id=?",
        (session_id,),
    )
    try:
        runner = get_runner(seat.runner_kind)
        result = await runner.invoke(
            seat,
            row["model"],
            message,
            row["working_dir"],
            resume=row["cli_session_id"] or None,
            system_prompt=row["system_prompt"] or "",
            on_session=_on_session,
        )
        reply = result.response or "(no output)"
        usage = dataclasses.asdict(result.usage)
        status = "error" if result.is_error else "done"
    except asyncio.CancelledError:
        await _persist(session_id, "assistant", "(cancelled: server shutting down)",
                       None, "error", None)
        raise
    except Exception as e:  # noqa: BLE001 — a dead seat must not wedge the chat
        logger.exception("chat: turn failed (session=%s)", session_id)
        await _persist(session_id, "assistant", f"(chat turn failed: {e!r})",
                       None, "error", cli_sid)
        return
    await _persist(session_id, "assistant", reply,
                   json.dumps(usage) if usage else None, status,
                   result.session_id or cli_sid)


async def _persist(
    session_id: int,
    role: str,
    content: str,
    usage_json: str | None,
    status: str,
    cli_session_id: str | None,
) -> None:
    now = db.utcnow()
    await db.execute(
        "INSERT INTO chat_messages (chat_session_id, role, content, usage, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, usage_json, now),
    )
    await db.execute(
        "UPDATE chat_sessions SET status=?, cli_session_id=COALESCE(?, cli_session_id), "
        "last_activity=? WHERE id=?",
        (status, cli_session_id, now, session_id),
    )


# --------------------------------------------------------------------------- #
# Public API (server.py tools)
# --------------------------------------------------------------------------- #

async def chat_start(
    seat_name: str, model: str = "", working_dir: str = "", system_prompt: str = ""
) -> dict:
    """Open a direct chat with one seat (seats.yaml key). model defaults to the
    seat's first healthy model (else its first declared model)."""
    try:
        seats, _warnings = _load_seats()
    except SeatsFileError as e:
        return {"error": {"code": "seats_file_invalid", "message": str(e)}}
    seat = next((s for s in seats if s.name == seat_name), None)
    if not seat:
        return {
            "error": {
                "code": "unknown_seat",
                "message": f"seat '{seat_name}' is not defined in seats.yaml "
                           f"(known: {', '.join(s.name for s in seats) or 'none'})",
            }
        }
    picked = model.strip() or await _pick_model(seat) or seat.models[0]
    wd = working_dir.strip() or os.getcwd()
    now = db.utcnow()
    session_id = await db.insert_returning_id(
        """INSERT INTO chat_sessions
           (seat, model, seat_backend, working_dir, system_prompt, status,
            created_at, last_activity, closed)
           VALUES (?, ?, ?, ?, ?, 'idle', ?, ?, 0)""",
        (seat.name, picked, seat.runner_kind, wd,
         system_prompt, now, now),
    )
    return {
        "chat_session_id": session_id,
        "seat": seat.name,
        "model": picked,
        "seat_backend": seat.runner_kind,
    }


async def chat_send(chat_session_id: int, message: str) -> dict:
    """Queue a message to the chat's seat — ASYNC (Q27=b); returns {task_id}."""
    row = await db.fetchrow("SELECT * FROM chat_sessions WHERE id=?", (chat_session_id,))
    if not row:
        return {
            "error": {
                "code": "unknown_chat_session",
                "message": f"chat session {chat_session_id} not found",
            }
        }
    if row["closed"]:
        return {
            "error": {
                "code": "session_closed",
                "message": f"chat session {chat_session_id} is closed "
                           "(history is preserved; open a new chat with chat_start)",
            }
        }
    if row["status"] == "running":
        return {
            "error": {
                "code": "busy",
                "message": f"chat session {chat_session_id} already has a turn "
                           "running — poll it with chat_poll first",
            }
        }
    try:
        seats, _warnings = _load_seats()
    except SeatsFileError as e:
        return {"error": {"code": "seats_file_invalid", "message": str(e)}}
    seat = next((s for s in seats if s.name == row["seat"]), None)
    if not seat:
        return {
            "error": {
                "code": "unknown_seat",
                "message": f"seat '{row['seat']}' disappeared from seats.yaml "
                           "since this chat was opened",
            }
        }

    await db.execute(
        "INSERT INTO chat_messages (chat_session_id, role, content, created_at) "
        "VALUES (?, 'user', ?, ?)",
        (chat_session_id, message, db.utcnow()),
    )
    await db.execute(
        "UPDATE chat_sessions SET status='running', last_activity=? WHERE id=?",
        (db.utcnow(), chat_session_id),
    )

    task_id = uuid.uuid4().hex
    entry: dict = {
        "session_id": chat_session_id,
        "task": None,  # filled below
        "started": time.monotonic(),
        "seat": seat,
        "model": row["model"],
        "runner_kind": seat.runner_kind,
        "cli_session_id": None,
    }
    entry["task"] = asyncio.create_task(_run_turn(task_id, entry, seat, message))
    _tasks[task_id] = entry
    return {"task_id": task_id}


def _progress(entry: dict) -> dict | None:
    """A13: live {turns, output_tokens} for a running turn, when the runner
    supports progress reading and the CLI session id is known. Else None."""
    cli_sid = entry.get("cli_session_id")
    if not cli_sid:
        return None
    try:
        runner = get_runner(entry["runner_kind"])
        if not runner.supports_progress():
            return None
        prog = runner.read_progress(cli_sid)
    except Exception:  # noqa: BLE001 — progress is best-effort
        return None
    if prog is None:
        return None
    return {
        "turns": prog.turns,
        "input_tokens": prog.input_tokens,
        "output_tokens": prog.output_tokens,
        "last_event_type": prog.last_event_type,
    }


async def chat_poll(task_id: str, wait: bool = True, timeout: int = 360) -> dict:
    """Long-poll a queued turn; returns early when the turn completes:
    {status, elapsed_s, progress?, reply?, usage?, session_id?}."""
    entry = _tasks.get(task_id)
    if not entry:
        return {
            "error": {
                "code": "unknown_task",
                "message": "unknown task_id (server restarted?)",
            }
        }
    task: asyncio.Task = entry["task"]
    deadline = time.monotonic() + max(1, timeout)
    while not task.done() and wait and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    elapsed = time.monotonic() - entry["started"]
    if not task.done():
        return {
            "status": "running",
            "elapsed_s": round(elapsed, 1),
            "progress": _progress(entry),
        }
    # Terminal — read the persisted result (the task coroutine did the write).
    row = await db.fetchrow(
        "SELECT status, cli_session_id FROM chat_sessions WHERE id=?",
        (entry["session_id"],),
    )
    msg = await db.fetchrow(
        "SELECT content, usage FROM chat_messages WHERE chat_session_id=? "
        "AND role='assistant' ORDER BY id DESC LIMIT 1",
        (entry["session_id"],),
    )
    out: dict = {"status": (row or {}).get("status") or "done",
                 "elapsed_s": round(elapsed, 1), "progress": None}
    if msg:
        out["reply"] = msg["content"]
        if msg["usage"]:
            try:
                out["usage"] = json.loads(msg["usage"])
            except ValueError:
                pass
    if row and row.get("cli_session_id"):
        out["session_id"] = row["cli_session_id"]
    if out["status"] == "error":
        out["error"] = (msg["content"] if msg else "")[:600]
    return out


async def chat_history(chat_session_id: int) -> list[dict]:
    """Message-level history: [{role, content, ts, usage}, ...]."""
    rows = await db.fetch(
        "SELECT role, content, usage, created_at FROM chat_messages "
        "WHERE chat_session_id=? ORDER BY id",
        (chat_session_id,),
    )
    out = []
    for r in rows:
        usage = None
        if r["usage"]:
            try:
                usage = json.loads(r["usage"])
            except ValueError:
                usage = None
        out.append({"role": r["role"], "content": r["content"],
                    "ts": r["created_at"], "usage": usage})
    return out


async def chat_list(working_dir: str = "") -> list[dict]:
    """List chat sessions (closed included, marked), optionally filtered by
    working_dir."""
    if working_dir.strip():
        rows = await db.fetch(
            "SELECT * FROM chat_sessions WHERE working_dir=? ORDER BY id DESC",
            (working_dir.strip(),),
        )
    else:
        rows = await db.fetch("SELECT * FROM chat_sessions ORDER BY id DESC")
    return [
        {
            "chat_session_id": r["id"],
            "seat": r["seat"],
            "model": r["model"],
            "seat_backend": r["seat_backend"],
            "working_dir": r["working_dir"],
            "status": r["status"],
            "closed": bool(r["closed"]),
            "created_at": r["created_at"],
            "last_activity": r["last_activity"],
        }
        for r in rows
    ]


async def chat_close(chat_session_id: int) -> dict:
    """Mark a chat closed. Running turns for it are cancelled; history is KEPT."""
    row = await db.fetchrow("SELECT * FROM chat_sessions WHERE id=?", (chat_session_id,))
    if not row:
        return {
            "error": {
                "code": "unknown_chat_session",
                "message": f"chat session {chat_session_id} not found",
            }
        }
    for entry in _tasks.values():
        if entry["session_id"] == chat_session_id and not entry["task"].done():
            entry["task"].cancel()
    await db.execute(
        "UPDATE chat_sessions SET closed=1, last_activity=? WHERE id=?",
        (db.utcnow(), chat_session_id),
    )
    return {
        "chat_session_id": chat_session_id,
        "closed": True,
        "records_kept": True,
    }


async def list_seats() -> dict:
    """Discovery (Decision #20): seats from seats.yaml.
    Returns {seats: [{name, models, runner_kind}, ...], warnings: [...]}."""
    try:
        seats, warnings = _load_seats()
    except SeatsFileError as e:
        msg = str(e)
        code = "seats_file_missing" if "not found" in msg else "seats_file_invalid"
        return {"error": {"code": code, "message": msg}}
    return {
        "seats": [
            {"name": s.name, "models": list(s.models),
             "runner_kind": s.runner_kind}
            for s in seats
        ],
        "warnings": warnings,
    }
