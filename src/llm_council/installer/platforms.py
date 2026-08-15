"""Load the declarative host registry from platforms.yaml (data only, Q7)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import yaml


@dataclass(frozen=True)
class Platform:
    name: str
    display_name: str
    detect_paths: tuple[str, ...]
    mcp_config_path: str
    agent_dir: str
    preferred: str


def _load_yaml() -> dict:
    res = resources.files("llm_council.installer").joinpath("platforms.yaml")
    with res.open("rb") as fh:
        return yaml.safe_load(fh)


def load_platforms() -> dict[str, Platform]:
    data = _load_yaml()
    out: dict[str, Platform] = {}
    for row in data["hosts"]:
        plat = Platform(
            name=row["name"],
            display_name=row["display_name"],
            detect_paths=tuple(row.get("detect_paths", ())),
            mcp_config_path=row["mcp_config_path"],
            agent_dir=row["agent_dir"],
            preferred=row.get("preferred", "file-merge"),
        )
        out[plat.name] = plat
    return out
