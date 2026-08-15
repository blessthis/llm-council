"""Seat-runner registry — lazy importlib so a missing/corrupt runner module
never breaks unrelated imports.

Usage: `get_runner(seat.runner_kind)` returns a runner instance implementing
the SeatRunner Protocol (seats/base.py).
"""

from __future__ import annotations

import importlib

from ..base import SeatRunner

__all__ = ["get_runner"]

# runner_kind -> module name under this package
_MODULES: dict[str, str] = {
    "claude": "claude",
    "pi": "pi",
    "codex": "codex",
    "generic": "generic",
}

_CACHE: dict[str, SeatRunner] = {}


def get_runner(runner_kind: str) -> SeatRunner:
    """Return the SeatRunner for a runner kind ('claude' | 'pi' | 'codex' | anything
    else → 'generic'). Modules are imported lazily; a missing or corrupt module
    raises RuntimeError with a clear message instead of breaking package import."""
    module_name = _MODULES.get(runner_kind, "generic")
    cached = _CACHE.get(module_name)
    if cached is not None:
        return cached
    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except ImportError as e:
        raise RuntimeError(
            f"seat runner module 'llm_council.seats.runners.{module_name}' is not "
            f"available (runner_kind={runner_kind!r}): {e}"
        ) from e
    except Exception as e:  # noqa: BLE001 — syntax error / anything else = corrupt
        raise RuntimeError(
            f"seat runner module 'llm_council.seats.runners.{module_name}' failed to "
            f"import (runner_kind={runner_kind!r}): {e!r}"
        ) from e
    cls = getattr(module, "RUNNER_CLASS", None)
    if not isinstance(cls, str) or not hasattr(module, cls):
        raise RuntimeError(
            f"seat runner module 'llm_council.seats.runners.{module_name}' is corrupt: "
            "missing a RUNNER_CLASS export naming the runner class"
        )
    try:
        runner: SeatRunner = getattr(module, cls)()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"seat runner '{module_name}' could not be instantiated: {e!r}") from e
    _CACHE[module_name] = runner
    return runner
