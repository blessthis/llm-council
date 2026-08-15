"""Configuration for blessthis-llm-council.

Reads ONLY two env vars (plus dev conveniences):

  SEATS_FILE    — path to seats.yaml (default ~/.blessthis-llm-council/seats.yaml)
  DATABASE_URL  — default sqlite:///$HOME/.blessthis-llm-council/state.db

No provider/proxy fields: all LLM access is via seat CLI subprocesses,
whose credentials live in seats.yaml (Decision #7/#8/#14). A `--seats-file` CLI
override is passed to Config.load() by server.main (read-only, Q9).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path


def state_dir() -> Path:
    """~/.blessthis-llm-council — seats.yaml, state.db, server.log live here."""
    return Path.home() / ".blessthis-llm-council"


def default_seats_file() -> str:
    return str(state_dir() / "seats.yaml")


def default_database_url() -> str:
    return f"sqlite:///{state_dir() / 'state.db'}"


def _load_dev_env() -> None:
    """Dev convenience ONLY: if a .env exists in the current working directory,
    load it without overriding any already-set variable. Never raises, and no
    import-time side effects (called from Config.load, not at module import)."""
    try:
        from dotenv import load_dotenv

        env_file = Path.cwd() / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except Exception:  # noqa: BLE001 — dotenv issues must never break startup
        pass


@dataclass(frozen=True)
class Config:
    seats_file: str
    database_url: str
    server_instance_id: str

    @staticmethod
    def load(seats_file_override: str | None = None, instance_id: str = "") -> Config:
        _load_dev_env()
        seats_file = (
            (seats_file_override or "").strip()
            or os.environ.get("SEATS_FILE", "").strip()
            or default_seats_file()
        )
        database_url = (
            os.environ.get("DATABASE_URL", "").strip() or default_database_url()
        )
        return Config(
            seats_file=seats_file,
            database_url=database_url,
            server_instance_id=instance_id or uuid.uuid4().hex,
        )
