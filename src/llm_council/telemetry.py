"""P6 telemetry client — anonymized council scores to a Cloudflare Worker.

Design (PLAN.md P6, Decision #22):
  * Allowlist payload ONLY: {model, kind, score, usage{...},
    tool_version, ts, council_uuid, agent}. Never code, paths, briefs, notes or
    seat names — per-user health state is NEVER telemetered. `agent` is which
    agent CLI hosts the council (claude/pi/codex/cursor/copilot/gemini),
    "unknown" if undetectable.
  * Consent: ONLY `telemetry.enabled: true` in seats.yaml flushes. A missing
    seats file (no consent possible) or a missing/invalid `telemetry` key
    means NO flush — the schema requires an explicit key.
  * Non-blocking: failures land in the local `telemetry_queue` SQLite table
    with exponential backoff; `retry_pending()` runs opportunistically at
    server startup. Telemetry can NEVER raise into the council flow.
  * All network I/O lives in ONE function (`_post_event`) so tests can patch
    a single seam (socket guard). stdlib urllib.request only — no new dep.

Env overrides:
  LLM_COUNCIL_TELEMETRY_ENDPOINT  — worker URL (default placeholder below)
  LLM_COUNCIL_TELEMETRY_DISABLED=1 — hard off, regardless of seats.yaml
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://llm-council-telemetry.blessthis.software/v1/score"
_TIMEOUT_SECONDS = 5
_BASE_BACKOFF_SECONDS = 60.0  # retry when now - last_attempt > 2^attempts * 60s

# The allowlist. Constructing an event with any other key is impossible via
# build_payload/serialize_event — and serialize_event REJECTS extras loudly.
_EVENT_KEYS = frozenset(
    {"model", "kind", "score", "usage", "tool_version", "ts", "council_uuid", "agent"}
)
_USAGE_KEYS = frozenset({"input", "output", "cache_read", "cache_write"})


class TelemetryPayloadError(ValueError):
    """An event tried to carry keys outside the P6 allowlist."""


def get_endpoint() -> str:
    """Worker endpoint (env-overridable)."""
    return (
        os.environ.get("LLM_COUNCIL_TELEMETRY_ENDPOINT", "").strip()
        or DEFAULT_ENDPOINT
    )


# Short alias
endpoint = get_endpoint


def hard_disabled() -> bool:
    """LLM_COUNCIL_TELEMETRY_DISABLED=1 wins over everything."""
    return os.environ.get("LLM_COUNCIL_TELEMETRY_DISABLED", "").strip() in (
        "1",
        "true",
        "yes",
    )


def consent_enabled(seats_file: str | Path | None = None) -> bool:
    """Read telemetry consent from seats.yaml at flush time (not import time).

    Rules (Decision #22 + seats schema §telemetry):
      * file missing            -> False (no seats file = no consent possible)
      * file unreadable/invalid -> False (missing/invalid key = no flush)
      * telemetry.enabled is true (exactly) -> True; anything else -> False
    """
    if seats_file is None:
        seats_file = os.environ.get("SEATS_FILE", "").strip() or str(
            Path.home() / ".blessthis-llm-council" / "seats.yaml"
        )
    path = Path(seats_file)
    if not path.is_file():
        return False
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — malformed file = no consent
        return False
    if not isinstance(doc, dict):
        return False
    telemetry = doc.get("telemetry")
    if not isinstance(telemetry, dict):
        return False
    return telemetry.get("enabled") is True


# --------------------------------------------------------------------------- #
# Allowlist payload construction
# --------------------------------------------------------------------------- #

def _usage(raw: Any) -> dict[str, int]:
    """Coerce a hat's usage JSON (or None) into the exact usage sub-object."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, int] = {}
    for key in sorted(_USAGE_KEYS):
        try:
            out[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def _detect_agent() -> str:
    """Best-effort: which agent CLI hosts the server? Default "unknown".

    pi has no reliable env marker, so it falls through to "unknown".
    """
    env = os.environ
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if env.get("CURSOR_TRACE_ID"):
        return "cursor"
    if env.get("GEMINI_CLI"):
        return "gemini"
    if env.get("VSCODE_PID"):
        return "copilot"
    for key in env:
        if key.startswith("CODEX_"):
            return "codex"
    if env.get("OPENAI_CODEX"):
        return "codex"
    return "unknown"


def build_payload(
    council_row: dict[str, Any],
    hats: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    tool_version: str,
    agent: str = "unknown",
) -> list[dict[str, Any]]:
    """Build one allowlisted event per (hat, score).

    `council_row` / `hats` come from the DB; `scores` are the recorded
    council_scores rows (or dicts with hat/model/score). Notes, briefs,
    paths and seat names are structurally DISCARDED — they never enter an
    event, so they can never be serialized or sent.
    """
    usage_by_hat = {h.get("hat"): h.get("usage") for h in hats}
    council_uuid = council_row.get("council_uuid") or ""
    kind = str(council_row.get("kind") or "adhoc")
    ts = str(council_row.get("updated_at") or "")
    events = []
    for s in scores:
        events.append(
            {
                "model": str(s["model"]),
                "kind": kind,
                "score": int(s["score"]),
                "usage": _usage(usage_by_hat.get(s.get("hat"))),
                "tool_version": str(tool_version),
                "ts": ts,
                "council_uuid": str(council_uuid),
                "agent": str(agent),
            }
        )
    return events


def serialize_event(event: dict[str, Any]) -> str:
    """Validate against the allowlist and return JSON text.

    Raises TelemetryPayloadError on ANY extra key (top-level or in usage) or
    on a missing key — the worker would 4xx these anyway, so we refuse to
    even serialize them locally.
    """
    extra = set(event) - _EVENT_KEYS
    if extra:
        raise TelemetryPayloadError(f"payload keys outside allowlist: {sorted(extra)}")
    missing = _EVENT_KEYS - set(event)
    if missing:
        raise TelemetryPayloadError(f"payload missing allowlist keys: {sorted(missing)}")
    usage = event["usage"]
    if not isinstance(usage, dict) or set(usage) - _USAGE_KEYS:
        raise TelemetryPayloadError("usage keys outside allowlist")
    return json.dumps(event, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# Network (the ONLY place a socket is opened — tests patch this seam)
# --------------------------------------------------------------------------- #

def _post_event(endpoint: str, event: dict[str, Any]) -> None:
    """POST one allowlisted event. Raises on any failure (caller queues)."""
    body = serialize_event(event).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
        if resp.status >= 300:
            raise RuntimeError(f"telemetry endpoint returned {resp.status}")


# --------------------------------------------------------------------------- #
# Queue + flush
# --------------------------------------------------------------------------- #

async def _enqueue(event: dict[str, Any]) -> None:
    from . import db

    await db.execute(
        "INSERT INTO telemetry_queue (payload, attempts, last_attempt, created_at) "
        "VALUES (?, 0, NULL, ?)",
        (serialize_event(event), db.utcnow()),
    )


async def flush(endpoint: str, events: list[dict[str, Any]]) -> int:
    """Best-effort POST of each event; failures go to the retry queue.

    Returns the number of events successfully sent. Never raises.
    """
    sent = 0
    for event in events:
        try:
            _post_event(endpoint, event)
            sent += 1
        except Exception:  # noqa: BLE001 — telemetry must never raise outward
            try:
                await _enqueue(event)
            except Exception:  # noqa: BLE001
                logger.exception("telemetry: failed to queue event")
    return sent


async def retry_pending(endpoint: str | None = None) -> int:
    """Retry queued events whose backoff window has elapsed (2^attempts × 60s).

    Deletes rows on success, bumps attempts otherwise. Never raises.
    """
    from . import db

    url = endpoint if endpoint is not None else get_endpoint()
    now = time.time()
    try:
        rows = await db.fetch("SELECT * FROM telemetry_queue")
    except Exception:  # noqa: BLE001
        return 0
    sent = 0
    for row in rows:
        last = row.get("last_attempt")
        if last:
            parsed = db.parse_ts(last)
            if parsed is not None:
                elapsed_since = now - parsed.timestamp()
                if elapsed_since < _BASE_BACKOFF_SECONDS * (2 ** int(row["attempts"])):
                    continue
        try:
            _post_event(url, json.loads(row["payload"]))
            await db.execute("DELETE FROM telemetry_queue WHERE id=?", (row["id"],))
            sent += 1
        except Exception:  # noqa: BLE001
            try:
                await db.execute(
                    "UPDATE telemetry_queue SET attempts=?, last_attempt=? WHERE id=?",
                    (int(row["attempts"]) + 1, db.utcnow(), row["id"]),
                )
            except Exception:  # noqa: BLE001
                logger.exception("telemetry: retry bookkeeping failed")
    return sent


# --------------------------------------------------------------------------- #
# Council hook (called from council.score() as a background task)
# --------------------------------------------------------------------------- #

async def maybe_flush_council(
    council_id: int,
    seats_file: str | None = None,
    tool_version: str = "",
    agent: str | None = None,
) -> int:
    """Enqueue + flush events for a scored council if consent allows.

    Safe to call unconditionally — the hard-off env override and the consent
    check short-circuit before ANY network or queue write.
    """
    if hard_disabled() or not consent_enabled(seats_file):
        return 0
    from . import db

    council_row = await db.fetchrow("SELECT * FROM councils WHERE id=?", (council_id,))
    if not council_row:
        return 0
    hats = await db.fetch(
        "SELECT hat, usage FROM council_hats WHERE council_id=?", (council_id,)
    )
    scores = await db.fetch(
        "SELECT hat, model, score FROM council_scores WHERE council_id=?", (council_id,)
    )
    events = build_payload(
        council_row, hats, scores, tool_version,
        agent=_detect_agent() if agent is None else agent,
    )
    return await flush(get_endpoint(), events)
