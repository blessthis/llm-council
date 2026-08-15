"""P2: PG -> SQLite migration script. Requires a real Postgres with the legacy
schema/data (PG_MIGRATE_URL, default postgres://localhost:5433/llm_council).

Skipped unless RUN_INTEGRATION=1 (see tests/integration/conftest.py).
"""

import asyncio
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

PG_URL = os.environ.get("PG_MIGRATE_URL", "postgres://localhost:5433/llm_council")


@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="needs real PG")
def test_migrate_pg_to_sqlite_verify(tmp_path):
    sqlite_url = f"sqlite:///{tmp_path}/state.db"
    proc = subprocess.run(
        [
            sys.executable, "scripts/migrate_pg_to_sqlite.py",
            "--pg", PG_URL, "--sqlite", sqlite_url, "--verify",
        ],
        capture_output=True, text=True, timeout=300,
    )
    print(proc.stdout, proc.stderr)
    assert proc.returncode == 0, proc.stderr
    assert "MISMATCH" not in proc.stdout

    async def _counts():
        import aiosqlite

        async with aiosqlite.connect(tmp_path / "state.db") as conn:
            out = {}
            for t in ("councils", "council_hats", "council_scores", "seat_health"):
                cur = await conn.execute(f"SELECT count(*) FROM {t}")
                out[t] = (await cur.fetchone())[0]
            return out

    counts = asyncio.run(_counts())
    # Ground-truth dataset (PLAN §2): at least 198 councils / 314 scores.
    assert counts["councils"] >= 198
    assert counts["council_scores"] >= 314
