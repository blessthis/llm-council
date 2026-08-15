#!/usr/bin/env python3
"""One-shot Postgres -> SQLite migration (Q14).

STOP-THE-WORLD (A15): the old server MUST be stopped before running this —
councils running mid-migration are not carried over safely.

    1. Stop the old server (every MCP host using it).
    2. Run:  python scripts/migrate_pg_to_sqlite.py \\
                 --pg postgres://localhost:5433/llm_council \\
                 --sqlite sqlite:///$HOME/.blessthis-llm-council/state.db --verify
    3. Unset DATABASE_URL (or point it at the sqlite URL) and start the new server.

Carries forward (with the P2 renames applied):
  councils, council_hats (claude_session_id -> session_id, seat_backend NULL for
  legacy rows), mcp_instances (TRUNCATED — fresh instance ids on next startup),
  council_scores, model_health -> seat_health (seat='legacy').

Does NOT migrate the 4 legacy tables (sessions, messages, model_switches,
pending_attachments) — they are dropped by the new server's init.

jsonb -> TEXT(json); TIMESTAMPTZ -> TEXT ISO-8601 UTC; ids preserved.
--verify compares per-table row counts PG vs SQLite and exits non-zero on mismatch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")

from llm_council import db  # noqa: E402


def _ts(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    return str(v)


async def migrate(pg_url: str, sqlite_url: str) -> dict[str, tuple[int, int]]:
    try:
        import asyncpg
    except ImportError:
        sys.exit("asyncpg is not installed — install blessthis-llm-council[postgres]")

    pg = await asyncpg.connect(pg_url)
    await db.init_pool(sqlite_url)  # creates the final v1 schema on SQLite
    counts: dict[str, tuple[int, int]] = {}

    # councils
    rows = [dict(r) for r in await pg.fetch("SELECT * FROM councils ORDER BY id")]
    for r in rows:
        await db.execute(
            """INSERT INTO councils (id, working_dir, brief, status, kind, owner,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"], r["working_dir"], r["brief"], r["status"],
                r.get("kind") or "adhoc", r.get("owner"),
                _ts(r["created_at"]), _ts(r["updated_at"]),
            ),
        )
    counts["councils"] = (
        await pg.fetchval("SELECT count(*) FROM councils"),
        await db.fetchval("SELECT count(*) FROM councils"),
    )

    # council_hats: claude_session_id -> session_id; seat_backend NULL (legacy);
    # jsonb usage -> TEXT. Old session_id BIGINT (dead sessions ref) is dropped.
    hat_rows = [dict(r) for r in await pg.fetch("SELECT * FROM council_hats ORDER BY id")]
    for r in hat_rows:
        usage = r.get("usage")
        await db.execute(
            """INSERT INTO council_hats (id, council_id, hat, model, session_id,
                   seat_backend, status, answer, error, usage, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"], r["council_id"], r["hat"], r["model"],
                r.get("claude_session_id"), r["status"], r.get("answer"),
                r.get("error"),
                json.dumps(usage) if usage is not None else None,
                _ts(r["created_at"]), _ts(r["updated_at"]),
            ),
        )
    counts["council_hats"] = (
        await pg.fetchval("SELECT count(*) FROM council_hats"),
        await db.fetchval("SELECT count(*) FROM council_hats"),
    )

    # mcp_instances: TRUNCATE — fresh instance ids on next startup (do not copy).
    counts["mcp_instances"] = (0, await db.fetchval("SELECT count(*) FROM mcp_instances"))

    # council_scores
    score_rows = [dict(r) for r in await pg.fetch("SELECT * FROM council_scores ORDER BY id")]
    for r in score_rows:
        await db.execute(
            """INSERT INTO council_scores (id, council_id, hat, model, score, notes,
                                           created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"], r["council_id"], r["hat"], r["model"], r["score"],
                r.get("notes"), _ts(r["created_at"]),
            ),
        )
    counts["council_scores"] = (
        await pg.fetchval("SELECT count(*) FROM council_scores"),
        await db.fetchval("SELECT count(*) FROM council_scores"),
    )

    # model_health -> seat_health with seat='legacy'
    health_rows = [dict(r) for r in await pg.fetch("SELECT * FROM model_health")]
    for r in health_rows:
        await db.execute(
            """INSERT INTO seat_health (seat, model, status, reason, last_error,
                                        cooldown_until, checked_at)
               VALUES ('legacy', ?, ?, ?, ?, ?, ?)""",
            (
                r["model"], r["status"], r.get("reason"), r.get("last_error"),
                _ts(r.get("cooldown_until")), _ts(r["checked_at"]),
            ),
        )
    counts["seat_health"] = (
        await pg.fetchval("SELECT count(*) FROM model_health"),
        await db.fetchval("SELECT count(*) FROM seat_health"),
    )

    # Keep sqlite_sequence consistent with the preserved ids.
    for t in ("councils", "council_hats", "council_scores"):
        mx = await db.fetchval(f"SELECT max(id) FROM {t}")
        if mx is not None:
            await db.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name=?", (mx, t)
            )

    await pg.close()
    await db.close_pool()
    return counts


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pg", required=True, help="postgres:// URL of the old database")
    ap.add_argument("--sqlite", required=True, help="sqlite:/// path of the new database")
    ap.add_argument("--verify", action="store_true",
                    help="compare per-table row counts; exit non-zero on mismatch")
    args = ap.parse_args()

    print("STOP-THE-WORLD: ensure the old server is stopped before migrating (A15).")
    counts = await migrate(args.pg, args.sqlite)
    ok = True
    for table, (n_pg, n_lite) in counts.items():
        match = "OK " if n_pg == n_lite else "MISMATCH"
        if n_pg != n_lite:
            ok = False
        print(f"  {match} {table}: pg={n_pg} sqlite={n_lite}")
    if args.verify and not ok:
        print("VERIFY FAILED: row count mismatch")
        return 1
    print("migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
