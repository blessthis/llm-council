from __future__ import annotations

import json

import pytest

from llm_council.installer.fingerprint import CANONICAL_KEY, find_ours, is_ours
from llm_council.installer.hosts import (
    CodexBinding,
    CopilotBinding,
    RegistrationConflict,
    get_host_binding,
)
from llm_council.installer.platforms import load_platforms

SEATS = "/tmp/seats.yaml"

FOREIGN_SERVER = {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "some-other-server"],
    "env": {"FOO": "bar"},
}


# ---------------------------------------------------------------- fingerprint

def test_is_ours_positive_variants():
    assert is_ours({"command": "uvx", "args": ["blessthis-llm-council-server"]})
    assert is_ours({"command": "uv", "args": ["run", "blessthis-llm-council-server"]})


def test_is_ours_negative():
    assert not is_ours({"command": "npx", "args": ["blessthis-llm-council-server"]})
    assert not is_ours({"command": "uvx", "args": ["other-server"]})
    assert not is_ours({"command": "uvx"})
    assert not is_ours({"args": ["blessthis-llm-council-server"]})
    assert not is_ours("not a dict")
    assert not is_ours(None)


def test_find_ours():
    servers = {
        "theirs": FOREIGN_SERVER,
        "ours": {"command": "uvx", "args": ["blessthis-llm-council-server"]},
    }
    assert set(find_ours(servers)) == {"ours"}
    assert find_ours({}) == {}
    assert find_ours(None) == {}


def test_no_managed_by_keys(tmp_path):
    host = get_host_binding("cursor", home=tmp_path)
    host.register(CANONICAL_KEY, SEATS)
    data = json.loads(host.config_path().read_text())
    raw = host.config_path().read_text()
    assert "_managed_by" not in raw
    assert set(data["mcpServers"][CANONICAL_KEY]) <= {"type", "command", "args", "env"}


# --------------------------------------------------------- JSON host helpers

JSON_HOSTS = ["claude", "gemini", "cursor", "pi"]


def seed_json_config(host, servers: dict, extra_top: dict | None = None):
    path = host.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {host.servers_key: servers}
    if extra_top:
        data.update(extra_top)
    path.write_text(json.dumps(data, indent=2) + "\n")


def entry_after_register(host):
    return json.loads(host.config_path().read_text())[host.servers_key][CANONICAL_KEY]


# ---------------------------------------------------------- round-trip tests

@pytest.mark.parametrize("name", JSON_HOSTS)
def test_json_merge_preserves_foreign_servers(name, tmp_path):
    host = get_host_binding(name, home=tmp_path)
    seed_json_config(host, {"their-server": FOREIGN_SERVER},
                     extra_top={"topLevel": {"untouched": True}})
    result = host.register(CANONICAL_KEY, SEATS)
    assert result["method"] == "file-merge"
    data = json.loads(host.config_path().read_text())
    assert data["mcpServers"]["their-server"] == FOREIGN_SERVER  # byte-identical entry
    assert data["topLevel"] == {"untouched": True}
    entry = data["mcpServers"][CANONICAL_KEY]
    assert entry["command"] == "uvx"
    assert entry["args"] == ["blessthis-llm-council-server"]
    assert entry["env"] == {"SEATS_FILE": SEATS}
    # backup was made
    assert host.config_path().with_name(host.config_path().name + ".bak").exists()


def test_pi_lifecycle_keys(tmp_path):
    host = get_host_binding("pi", home=tmp_path)
    host.register(CANONICAL_KEY, SEATS)
    entry = entry_after_register(host)
    assert entry["lifecycle"] == "lazy-keep-alive"
    assert entry["toolPrefix"] == "mcp"


def test_register_idempotent_upsert(tmp_path):
    host = get_host_binding("cursor", home=tmp_path)
    host.register(CANONICAL_KEY, SEATS)
    host.register(CANONICAL_KEY, "/other/seats.yaml")
    assert entry_after_register(host)["env"]["SEATS_FILE"] == "/other/seats.yaml"


def test_rejects_non_canonical_name(tmp_path):
    host = get_host_binding("cursor", home=tmp_path)
    with pytest.raises(ValueError):
        host.register("council", SEATS)


# ---------------------------------------------------------- conflict rules

def test_conflict_foreign_llm_council_key(tmp_path):
    host = get_host_binding("cursor", home=tmp_path)
    seed_json_config(host, {CANONICAL_KEY: FOREIGN_SERVER})
    with pytest.raises(RegistrationConflict, match="won't clobber"):
        host.register(CANONICAL_KEY, SEATS)
    # config untouched
    data = json.loads(host.config_path().read_text())
    assert data["mcpServers"][CANONICAL_KEY] == FOREIGN_SERVER


def test_conflict_fingerprint_under_other_key(tmp_path):
    host = get_host_binding("cursor", home=tmp_path)
    seed_json_config(host, {
        "my-council": {"command": "uvx", "args": ["blessthis-llm-council-server"]},
    })
    with pytest.raises(RegistrationConflict, match="different key"):
        host.register(CANONICAL_KEY, SEATS)


def test_conflict_codex(tmp_path):
    host = get_host_binding("codex", home=tmp_path)
    path = host.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '# my config\n[mcp_servers."llm-council"]\ncommand = "npx"\n'
        'args = ["other"]\n'
    )
    with pytest.raises(RegistrationConflict):
        host.register(CANONICAL_KEY, SEATS)


# ------------------------------------------------------------------ uninstall

@pytest.mark.parametrize("name", JSON_HOSTS)
def test_json_uninstall_restores_foreign_only(name, tmp_path):
    host = get_host_binding(name, home=tmp_path)
    seed_json_config(host, {"their-server": FOREIGN_SERVER})
    host.register(CANONICAL_KEY, SEATS)
    removed = host.uninstall()
    assert removed == [CANONICAL_KEY]
    data = json.loads(host.config_path().read_text())
    assert set(data["mcpServers"]) == {"their-server"}
    assert data["mcpServers"]["their-server"] == FOREIGN_SERVER


def test_uninstall_removes_fingerprint_under_any_key(tmp_path):
    host = get_host_binding("cursor", home=tmp_path)
    seed_json_config(host, {
        "my-council": {"command": "uvx", "args": ["blessthis-llm-council-server"]},
        "theirs": FOREIGN_SERVER,
    })
    assert host.uninstall() == ["my-council"]
    assert set(json.loads(host.config_path().read_text())["mcpServers"]) == {"theirs"}


# ---------------------------------------------------------------------- codex

def test_codex_roundtrip_preserves_comments_and_foreign(tmp_path):
    host = get_host_binding("codex", home=tmp_path)
    assert isinstance(host, CodexBinding)
    path = host.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "# codex config with precious comments\n"
        'model = "gpt-5"\n'
        "\n"
        "[mcp_servers.other]\n"
        "command = \"npx\"\n"
        "args = [\"other\"]\n"
        "\n# trailing comment\n"
    )
    path.write_text(original)
    host.register(CANONICAL_KEY, SEATS)
    text = path.read_text()
    assert "# codex config with precious comments" in text
    assert "# trailing comment" in text
    assert 'model = "gpt-5"' in text
    import tomlkit
    doc = tomlkit.parse(text)
    assert doc["model"] == "gpt-5"
    assert doc["mcp_servers"]["other"]["command"] == "npx"
    ours = doc["mcp_servers"][CANONICAL_KEY]
    assert ours["command"] == "uvx"
    assert ours["enabled"] is True
    assert ours["env"]["SEATS_FILE"] == SEATS
    # second register = upsert, still comment-safe
    host.register(CANONICAL_KEY, "/x/seats.yaml")
    assert "# trailing comment" in path.read_text()
    # uninstall keeps comments
    assert host.uninstall() == [CANONICAL_KEY]
    after = path.read_text()
    assert "# trailing comment" in after
    assert CANONICAL_KEY not in tomlkit.parse(after)["mcp_servers"]
    assert "other" in tomlkit.parse(after)["mcp_servers"]


# -------------------------------------------------------------------- copilot

def test_copilot_uses_servers_key(tmp_path):
    host = get_host_binding("copilot", home=tmp_path, project_path=tmp_path / "proj")
    assert isinstance(host, CopilotBinding)
    host.register(CANONICAL_KEY, SEATS)
    data = json.loads(host.config_path().read_text())
    assert set(data) == {"servers"}
    assert data["servers"][CANONICAL_KEY]["command"] == "uvx"


def test_copilot_merge_preserves_foreign(tmp_path):
    host = get_host_binding("copilot", home=tmp_path, project_path=tmp_path / "proj")
    host.register(CANONICAL_KEY, SEATS)
    host.uninstall()  # clean our entry, keep structure
    # seed with foreign and re-register
    path = host.config_path()
    data = json.loads(path.read_text()) if path.exists() else {"servers": {}}
    data["servers"]["foreign"] = FOREIGN_SERVER
    path.write_text(json.dumps(data, indent=2))
    host.register(CANONICAL_KEY, SEATS)
    data = json.loads(path.read_text())
    assert data["servers"]["foreign"] == FOREIGN_SERVER
    assert host.uninstall() == [CANONICAL_KEY]
    assert json.loads(path.read_text())["servers"]["foreign"] == FOREIGN_SERVER


# ------------------------------------------------------------------ platforms

def test_platforms_registry_six_hosts():
    platforms = load_platforms()
    assert set(platforms) == {"claude", "pi", "codex", "cursor", "copilot", "gemini"}


def test_detect_via_config_file(tmp_path):
    (tmp_path / ".cursor" / "mcp.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cursor" / "mcp.json").write_text("{}")
    assert get_host_binding("cursor", home=tmp_path).detect()
    assert not get_host_binding("codex", home=tmp_path).detect()
