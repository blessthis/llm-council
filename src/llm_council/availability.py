"""Seat/model availability: error classification + DB-backed health cache.

Seats record CAPACITY failures (depleted credits, exhausted quota, account-level
cooldown) here so later councils skip dead models instead of re-discovering them
one 3-retry budget at a time. A capacity error is an account state, not a network
blip — retrying the same model in place never helps; escalating to the next model
in its fallback chain does.

Health is keyed (seat, model) (Q3, Decision #7): the same model id served by two
different seats (different creds) has independent health. `is_available` answers
from the cache; a stale/missing row means "unknown" and the caller decides
whether to probe.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import db
from .seats.base import ProbeResult

if TYPE_CHECKING:
    from .seats.base import Seat

logger = logging.getLogger(__name__)

# Account/balance problems: dead until someone pays — long cooldown.
_BALANCE_RE = re.compile(
    r"(credits (?:are )?depleted|insufficient.?balance|\b402\b|prepayment|"
    r"token plan usage limit reached)",
    re.IGNORECASE,
)
# Quota/cooldown exhaustion: heals on its own — shorter cooldown.
_QUOTA_RE = re.compile(
    r"(exceeded your current quota|quota exceeded|individual quota reached|"
    r"all credentials(?: for model \S+)? are cooling down|"
    r"auth_unavailable|no auth available)",
    re.IGNORECASE,
)

_BALANCE_COOLDOWN = timedelta(minutes=60)
_QUOTA_COOLDOWN = timedelta(minutes=10)
# Non-capacity probe failures (timeout, connection blip, a slow reasoning seat
# that didn't answer in PROBE_TIMEOUT) are NOT an account state — they heal on
# their own. A short cooldown stops a flapping seat from being respawn-probed on
# every call WITHOUT benching a healthy-but-slow seat for the rest of a session
# (the old code reused _QUOTA_COOLDOWN's 10 min here, so one slow probe knocked
# a whole model family out of the panel across successive councils).
_TRANSIENT_COOLDOWN = timedelta(seconds=90)
_OK_TTL = timedelta(minutes=10)   # a fresh 'ok' row skips the probe

# Quota messages often carry their own reset time ("Resets in 154h49m16s") —
# honor it instead of re-probing a week-dead model every 10 min.
_RESET_IN_RE = re.compile(r"resets?\s+in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", re.IGNORECASE)
_MAX_QUOTA_COOLDOWN = timedelta(days=7)


def _cooldown_for(reason: str, error_text: str) -> timedelta:
    if reason == "quota":
        m = _RESET_IN_RE.search(error_text)
        if m and any(m.groups()):
            h, mi, s = (int(g) if g else 0 for g in m.groups())
            delta = timedelta(hours=h, minutes=mi, seconds=s)
            if delta > timedelta(0):
                return min(delta, _MAX_QUOTA_COOLDOWN)
        return _QUOTA_COOLDOWN
    return _BALANCE_COOLDOWN


def classify(error_text: str | None) -> str | None:
    """'balance' | 'quota' (both capacity-class: escalate, don't retry) or None
    (transient/unknown: existing in-place retry logic applies)."""
    if not error_text:
        return None
    if _BALANCE_RE.search(error_text):
        return "balance"
    if _QUOTA_RE.search(error_text):
        return "quota"
    return None


async def record_failure(
    seat: str, model: str, error_text: str, reason: str | None = None
) -> None:
    """Upsert a cooldown row for a capacity-class failure. reason defaults to
    classify(error_text); no-op if the error isn't capacity-class."""
    reason = reason or classify(error_text)
    if reason is None:
        return
    now = datetime.now(timezone.utc)
    cooldown_until = (now + _cooldown_for(reason, error_text)).isoformat()
    await db.execute(
        """INSERT INTO seat_health
           (seat, model, status, reason, last_error, cooldown_until, checked_at)
           VALUES (?, ?, 'cooldown', ?, ?, ?, ?)
           ON CONFLICT (seat, model) DO UPDATE SET status='cooldown', reason=?,
             last_error=?, cooldown_until=?, checked_at=?""",
        (
            seat, model, reason, error_text[:2000], cooldown_until, now.isoformat(),
            reason, error_text[:2000], cooldown_until, now.isoformat(),
        ),
    )
    logger.info("availability: %s/%s -> cooldown (%s)", seat, model, reason)


async def record_success(seat: str, model: str) -> None:
    now = db.utcnow()
    await db.execute(
        """INSERT INTO seat_health
           (seat, model, status, reason, last_error, cooldown_until, checked_at)
           VALUES (?, ?, 'ok', NULL, NULL, NULL, ?)
           ON CONFLICT (seat, model) DO UPDATE SET status='ok', reason=NULL,
             last_error=NULL, cooldown_until=NULL, checked_at=?""",
        (seat, model, now, now),
    )


async def get_health(seat: str, model: str) -> dict[str, Any] | None:
    return await db.fetchrow(
        "SELECT * FROM seat_health WHERE seat=? AND model=?", (seat, model)
    )


async def list_health() -> list[dict[str, Any]]:
    return await db.fetch("SELECT * FROM seat_health ORDER BY seat, model")


async def is_available(seat: str, model: str) -> bool | None:
    """True/False from a fresh cache row; None = unknown (no row, expired
    cooldown, or stale 'ok') — caller should probe."""
    row = await get_health(seat, model)
    if not row:
        return None
    now = datetime.now(timezone.utc)
    if row["status"] == "cooldown":
        until = db.parse_ts(row["cooldown_until"])
        if until and until > now:
            return False
        return None  # cooldown expired — worth re-probing
    checked = db.parse_ts(row["checked_at"])
    if row["status"] == "ok" and checked and checked > now - _OK_TTL:
        return True
    return None


# NOTE: the old HTTP probe() / pick_available() were deleted with gateway.py
# (Decision #17). P3c rewrites probe() as a per-seat CLI trivial-prompt spawn
# delegated to the seat's runner (Q16: no HTTP anywhere).


async def probe(seat: Seat, model: str) -> bool:
    """CLI availability probe via the seat's runner (Q16 — no HTTP anywhere).

    Honors the health cache first: a fresh 'ok' row (within _OK_TTL) skips the
    spawn entirely. Otherwise runs `runner.probe(seat, model)` and records the
    outcome into seat_health keyed (seat.name, model): ok on success; a
    cooldown row (reason from classify()) on failure — capacity-class reasons
    get their normal cooldowns, anything else a short cooldown so a flapping
    seat isn't respawn-probed every call."""
    if await is_available(seat.name, model) is True:
        return True  # TTL cache hit — no respawn

    from .seats.runners import get_runner  # lazy: avoids import cycles

    try:
        runner = get_runner(seat.runner_kind)
        res = await runner.probe(seat, model)
    except Exception as e:  # noqa: BLE001 — unknown runner kind, spawn failure — never raise
        res = ProbeResult(ok=False, model=model, error=repr(e))

    if res.ok:
        await record_success(seat.name, model)
        return True

    error_text = res.error or "probe failed"
    reason = classify(error_text)
    if reason is not None:
        await record_failure(seat.name, model, error_text, reason)
    else:
        now = datetime.now(timezone.utc)
        until = (now + _TRANSIENT_COOLDOWN).isoformat()
        await db.execute(
            """INSERT INTO seat_health
               (seat, model, status, reason, last_error, cooldown_until, checked_at)
               VALUES (?, ?, 'cooldown', NULL, ?, ?, ?)
               ON CONFLICT (seat, model) DO UPDATE SET status='cooldown',
                 reason=NULL, last_error=?, cooldown_until=?, checked_at=?""",
            (seat.name, model, error_text[:2000], until, now.isoformat(),
             error_text[:2000], until, now.isoformat()),
        )
    logger.info("availability.probe: %s/%s FAILED (%s)", seat.name, model, reason)
    return False


async def unavailable_reason(seat: str, model: str) -> str:
    row = await get_health(seat, model)
    if not row or row["status"] != "cooldown":
        return "unavailable"
    until = db.parse_ts(row["cooldown_until"])
    stamp = f"{until:%H:%M UTC}" if until else "unknown"
    return (
        f"cooldown ({row['reason']}) until {stamp}: "
        f"{(row['last_error'] or '')[:120]}"
    )
