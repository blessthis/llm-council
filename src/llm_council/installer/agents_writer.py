"""Copy per-host agent files into the host's agent dir (PLAN P4a).

Source: repo tree `agents/<host>/blessthis-council-*.{md,toml,agent.md}`.
We own ONLY the `blessthis-council-*` prefix (Decision #13): existing files
with that prefix are overwritten silently (they're ours); everything else is
untouched. A missing host dir in the source tree = skip with a note.
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path

from llm_council.installer.hosts import HostBinding

log = logging.getLogger(__name__)

AGENTS_PREFIX = "blessthis-council-"


def default_agents_root() -> Path:
    """Resolve the agents source dir.

    Order: (1) packaged `llm_council/data/agents/` (installed wheel,
    via importlib.resources), (2) repo-root `agents/` (dev checkout).
    """
    try:
        packaged = importlib.resources.files("llm_council").joinpath("data", "agents")
        if packaged.is_dir():
            with importlib.resources.as_file(packaged) as p:
                if (p / "pi").is_dir():
                    return p
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    return Path(__file__).resolve().parents[3] / "agents"


def write_agents(
    host: HostBinding,
    agents_root: Path | None = None,
    conductor_roles: list[str] | None = None,
) -> dict:
    """Deploy agent files for *host*. Returns {"written": [...], "skipped": reason?}."""
    root = Path(agents_root) if agents_root is not None else default_agents_root()
    src_dir = root / host.platform.name
    if not src_dir.is_dir():
        note = f"no source agent dir {src_dir}; skipped"
        log.info("%s: %s", host.platform.name, note)
        return {"written": [], "skipped": note}

    dest = host.agents_dir()
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for src in sorted(src_dir.iterdir()):
        if not src.is_file() or not src.name.startswith(AGENTS_PREFIX):
            continue
        (dest / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(src.name)

    if host.platform.name == "copilot":
        name = write_copilot_conductor(dest, conductor_roles)
        if name:
            written.append(name)

    return {"written": written}


def write_copilot_conductor(
    dest: Path, roles: list[str] | None = None
) -> str | None:
    """Generate the Copilot parent orchestrator `.agent.md`.

    `tools: [agent]` + `agents:` whitelist of the deployed sub-agents
    (docs/agent-templates/copilot.md, Decision #13).
    """
    if roles is None:
        roles = sorted(
            p.name
            for p in dest.iterdir()
            if p.is_file()
            and p.name.startswith(AGENTS_PREFIX)
            and p.name != "blessthis-council-conductor.agent.md"
        )
        roles = [r.rsplit(".", 1)[0].removesuffix(".agent") for r in roles]
    if not roles:
        return None
    whitelist = ", ".join(f'"{r}"' for r in roles)
    content = f"""---
name: blessthis-council-conductor
description: Parent orchestrator for the bless-this council sub-agents.
tools: ["agent"]
agents: [{whitelist}]
---

Invoke the blessthis council sub-agents to run a blind multi-model council.
This orchestrator dispatches to the whitelisted sub-agents and aggregates
their verdicts. Do not answer substantive questions yourself — delegate.
"""
    path = dest / "blessthis-council-conductor.agent.md"
    path.write_text(content, encoding="utf-8")
    return path.name


def remove_agents(host: HostBinding) -> list[str]:
    """Delete only our `blessthis-council-*` files from the host agent dir."""
    dest = host.agents_dir()
    if not dest.is_dir():
        return []
    removed: list[str] = []
    for p in sorted(dest.iterdir()):
        if p.is_file() and p.name.startswith(AGENTS_PREFIX):
            p.unlink()
            removed.append(p.name)
    return removed
