from __future__ import annotations

from pathlib import Path

from llm_council.installer.agents_writer import (
    AGENTS_PREFIX,
    default_agents_root,
    remove_agents,
    write_agents,
)
from llm_council.installer.hosts import get_host_binding


def make_source(root: Path, host: str, names: list[str]) -> Path:
    host_dir = root / host
    host_dir.mkdir(parents=True)
    for n in names:
        (host_dir / n).write_text(f"---\nname: {n.rsplit('.', 1)[0]}\n---\nbody {n}\n")
    return host_dir


def test_agents_written_with_prefix(tmp_path):
    src = make_source(tmp_path / "agents", "pi", [
        "blessthis-council-architect.md",
        "blessthis-council-bug.md",
    ])
    host = get_host_binding("pi", home=tmp_path)
    res = write_agents(host, agents_root=src.parent)
    dest = tmp_path / ".pi" / "agent" / "agents"
    assert sorted(p.name for p in dest.iterdir()) == res["written"]
    assert (dest / "blessthis-council-architect.md").exists()


def test_overwrite_only_ours(tmp_path):
    src = make_source(tmp_path / "agents", "cursor", ["blessthis-council-review.md"])
    host = get_host_binding("cursor", home=tmp_path)
    dest = host.agents_dir()
    dest.mkdir(parents=True)
    foreign = dest / "user-agent.md"
    foreign.write_text("DO NOT TOUCH")
    ours_old = dest / "blessthis-council-review.md"
    ours_old.write_text("stale")
    write_agents(host, agents_root=src.parent)
    assert foreign.read_text() == "DO NOT TOUCH"
    assert ours_old.read_text().endswith("body blessthis-council-review.md\n")


def test_missing_host_dir_skips_with_note(tmp_path):
    (tmp_path / "agents" / "pi").mkdir(parents=True)
    host = get_host_binding("cursor", home=tmp_path)
    res = write_agents(host, agents_root=tmp_path / "agents")
    assert res["written"] == []
    assert "skipped" in res


def test_copilot_conductor_generated(tmp_path):
    src = make_source(tmp_path / "agents", "copilot", [
        "blessthis-council-architect.agent.md",
        "blessthis-council-bug.agent.md",
    ])
    host = get_host_binding("copilot", home=tmp_path, project_path=tmp_path / "proj")
    res = write_agents(host, agents_root=src.parent)
    dest = tmp_path / "proj" / ".github" / "agents"
    conductor = dest / "blessthis-council-conductor.agent.md"
    assert conductor.exists()
    text = conductor.read_text()
    assert 'tools: ["agent"]' in text
    assert '"blessthis-council-architect"' in text
    assert '"blessthis-council-bug"' in text
    assert "blessthis-council-conductor.agent.md" in res["written"]


def test_remove_agents_only_ours(tmp_path):
    host = get_host_binding("pi", home=tmp_path)
    dest = host.agents_dir()
    dest.mkdir(parents=True)
    foreign = dest / "other-agent.md"
    foreign.write_text("keep")
    for n in ("blessthis-council-architect.md", "blessthis-council-bug.md"):
        (dest / n).write_text("x")
    removed = remove_agents(host)
    assert sorted(removed) == [
        "blessthis-council-architect.md",
        "blessthis-council-bug.md",
    ]
    assert foreign.exists()


def test_default_agents_root_points_at_repo_tree():
    root = default_agents_root()
    assert root.name == "agents"
    assert (root / "pi").is_dir()
    assert any(p.name.startswith(AGENTS_PREFIX) for p in (root / "pi").iterdir())
