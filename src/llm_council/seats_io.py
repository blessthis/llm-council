"""seats.yaml IO for the CLI family (P4b, docs/seats-schema.md §5).

All writes are atomic (tmp in same dir → fsync → chmod 0600 → os.replace),
keep a rolling `.bak`, and are validated through `seats.loader` BEFORE the
real file is touched — an invalid document never replaces a valid one.

Comment-preserving edits go through ruamel.yaml round-trip; when ruamel is
unavailable we fall back to pyyaml safe_dump with a warning (comments lost,
content intact).
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .seats.loader import SeatsFileError, load_seats

__all__ = [
    "SEATS_MODE",
    "atomic_write_seats",
    "atomic_write_seats_text",
    "dump_yaml",
    "edit_seats_file",
    "load_yaml",
    "example_path",
    "mask_secrets",
]

log = logging.getLogger(__name__)

SEATS_MODE = 0o600

try:  # ruamel is a real dependency; guard anyway so imports never explode
    from ruamel.yaml import YAML as _RuamelYAML  # noqa: N811

    _HAS_RUAMEL = True
except ImportError:  # pragma: no cover - defensive
    _RuamelYAML = None  # type: ignore[assignment]
    _HAS_RUAMEL = False


def example_path() -> Path:
    """Path to seats.example.yaml.

    Order: (1) packaged `llm_council/data/seats.example.yaml` (installed
    wheel, via importlib.resources), (2) repo-root file (dev checkout).
    """
    try:
        res = importlib.resources.files("llm_council").joinpath("data", "seats.example.yaml")
        if res.is_file():
            with importlib.resources.as_file(res) as p:
                return p
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    return Path(__file__).resolve().parents[2] / "seats.example.yaml"


def load_yaml(path: Path) -> Any:
    """Read seats.yaml. Raises yaml.YAMLError / OSError as-is (callers render)."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def dump_yaml(doc: Any) -> str:
    """Serialize to YAML text — ruamel round-trip when doc supports it, else pyyaml."""
    if _HAS_RUAMEL and type(doc).__module__.startswith("ruamel"):
        import io

        ry = _RuamelYAML()  # rt mode by default, comments preserved
        buf = io.StringIO()
        ry.dump(doc, buf)
        return buf.getvalue()
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _validate_before_write(text: str, tmp: Path, *, allow_empty: bool) -> None:
    """Validate candidate content through the real loader (schema rules 1-35)."""
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, SEATS_MODE)
    try:
        seats, _warnings = load_seats(tmp, force=True)
        if not seats and not allow_empty:
            # Every seat failed validation — the editor would turn a working
            # file into an unusable one; refuse (per-seat errors are warnings
            # in the loader, so re-check here).
            raise SeatsFileError(["document would contain no valid seats; refusing"])
    except SeatsFileError as exc:
        # Zero-seat documents are rejected by the loader (rule 5) but are a
        # deliberate installer edge case (installer-ux §2 A2 / §3.3): allow them
        # through this one call-site-controlled escape hatch.
        if allow_empty and any("at least one seat" in e for e in exc.errors):
            return
        raise
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_seats_text(
    path: Path, text: str, *, allow_empty: bool = False
) -> None:
    """Atomically write pre-rendered YAML text to *path* (mode 0600, `.bak` first).

    Content is validated via seats.loader BEFORE the existing file is touched;
    a SeatsFileError propagates and nothing is written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        _validate_before_write(text, tmp, allow_empty=allow_empty)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        tmp.write_text(text, encoding="utf-8")
        with open(tmp, "a", encoding="utf-8") as fh:
            os.fsync(fh.fileno())
        os.chmod(tmp, SEATS_MODE)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_seats(path: Path, data: dict, *, allow_empty: bool = False) -> None:
    """Atomically write a plain dict as seats.yaml (validated, 0600, `.bak`)."""
    atomic_write_seats_text(path, yaml.safe_dump(data, sort_keys=False), allow_empty=allow_empty)


def edit_seats_file(path: Path, mutator: Callable[[Any], None]) -> None:
    """Round-trip edit: load (ruamel when available) → mutate → validate → atomic write.

    Falls back to pyyaml (comments lost) with a logged warning when ruamel is
    missing. Validation failures raise SeatsFileError and leave the file intact."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if _HAS_RUAMEL:
        ry = _RuamelYAML()  # rt mode: comments/anchors/order survive
        doc = ry.load(text)
    else:
        log.warning(
            "ruamel.yaml unavailable — editing %s with pyyaml; comments will be lost",
            path,
        )
        doc = yaml.safe_load(text)
    mutator(doc)
    atomic_write_seats_text(path, dump_yaml(doc))


def _mask(value: str) -> str:
    """Mask a secret: keep only the last 4 chars (schema §5)."""
    if not isinstance(value, str) or not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def mask_secrets(data: Any) -> Any:
    """Deep-copy a seats document with every seat env VALUE masked to last-4."""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "seats" and isinstance(value, dict):
            seats_out: dict[str, Any] = {}
            for name, seat in value.items():
                if isinstance(seat, dict) and isinstance(seat.get("agent"), dict):
                    agent = dict(seat["agent"])
                    env = agent.get("env")
                    if isinstance(env, dict):
                        agent["env"] = {k: _mask(v) for k, v in env.items()}
                    seats_out[name] = {**seat, "agent": agent}
                else:
                    seats_out[name] = seat
            out[key] = seats_out
        elif isinstance(value, dict):
            out[key] = mask_secrets(value)
        else:
            out[key] = value
    return out
