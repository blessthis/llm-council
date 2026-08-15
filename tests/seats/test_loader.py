"""Loader tests — one parametrized case per seats-schema.md §2 rule, plus cache tests."""

from __future__ import annotations

# ruff: noqa: E501
import dataclasses

import pytest

from llm_council.seats import (
    AgentSpec,
    Seat,
    invalidate_cache,
    load_seats,
)
from llm_council.seats import loader as loader_mod
from llm_council.seats.loader import SeatsFileError

VALID_SEAT = """
telemetry:
  enabled: false
seats:
  fable:
    models: [m1]
    agent:
      bin: /bin/echo
      args: ["-p", "{prompt}", "--model", "{model}"]
      env: {}
"""


def write(tmp_path, text, mode=0o600):
    p = tmp_path / "seats.yaml"
    p.write_text(text)
    p.chmod(mode)
    return p


def seat_block(agent_body, models="[m1]", seat="  fable:"):
    return f"telemetry:\n  enabled: false\nseats:\n{seat}\n    models: {models}\n    agent:\n{agent_body}"


VALID_AGENT = "      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"

# (rule, yaml text, expected message fragment, kind)
# kind: 'doc' = SeatsFileError, 'seat' = seat skipped (message reported), 'warn' = warning only
CASES = [
    # --- document structure ---
    (1, "telemetry: [unclosed\n", "invalid YAML in seats file", "doc"),
    (2, "- just\n- a list\n", "must be a YAML mapping at the top level, got list", "doc"),
    (3, VALID_SEAT + "extra: 1\n", "unknown top-level key 'extra'; allowed: telemetry, seats", "doc"),
    (4, "telemetry:\n  enabled: false\n", "'seats' must be a mapping of seat name to seat definition", "doc"),
    (4, "telemetry:\n  enabled: false\nseats: [a]\n", "'seats' must be a mapping of seat name to seat definition", "doc"),
    (5, "telemetry:\n  enabled: false\nseats: {}\n", "'seats' must define at least one seat", "doc"),
    (6, "seats:\n  fable:\n    models: [m1]\n", "missing required top-level key 'telemetry'", "doc"),
    (7, "telemetry: {}\nseats: {}\n", "telemetry.enabled must be explicit true or false", "doc"),
    (7, "telemetry:\n  enabled: yes-please\nseats: {}\n", "telemetry.enabled must be explicit true or false", "doc"),
    (8, "telemetry:\n  enabled: false\n  extra: 1\nseats: {}\n", "unknown key 'telemetry.extra'", "doc"),
    # --- per seat ---
    (9, seat_block(VALID_AGENT, seat="  Bad Name:"), "invalid name; use lowercase letters", "seat"),
    (10, seat_block(VALID_AGENT) + "  fable:\n    models: [m2]\n    agent:\n" + VALID_AGENT,
     "duplicate seat name 'fable'", "doc"),
    (11, "telemetry:\n  enabled: false\nseats:\n  fable: [a, b]\n", "definition must be a mapping", "seat"),
    (12, "telemetry:\n  enabled: false\nseats:\n  fable:\n    extra: 1\n    models: [m1]\n    agent:\n" + VALID_AGENT,
     "unknown key 'extra'; allowed: models, agent", "seat"),
    (13, seat_block(VALID_AGENT, models="[]"), "'models' must be a non-empty list of model names", "seat"),
    (13, seat_block(VALID_AGENT, models="notalist"), "'models' must be a non-empty list of model names", "seat"),
    (14, seat_block(VALID_AGENT, models='[""]'), "models[0] must be a non-empty string", "seat"),
    (14, seat_block(VALID_AGENT, models="[5]"), "models[0] must be a non-empty string", "seat"),
    (15, seat_block(VALID_AGENT, models="[m1, m1]"), "duplicate model 'm1' ignored", "warn"),
    (16, "telemetry:\n  enabled: false\nseats:\n  fable:\n    models: [m1]\n    agent: []\n",
     "'agent' must be a mapping with bin, args, env", "seat"),
    (16, "telemetry:\n  enabled: false\nseats:\n  fable:\n    models: [m1]\n",
     "'agent' must be a mapping with bin, args, env", "seat"),
    (17, seat_block(VALID_AGENT + "      extra: 1\n"),
     "unknown key 'agent.extra'; allowed: bin, args, env", "seat"),
    (18, seat_block("      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"),
     "agent.bin must be a non-empty string", "seat"),
    (18, seat_block("      bin: 5\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"),
     "agent.bin must be a non-empty string", "seat"),
    (19, seat_block("      bin: definitely-not-a-real-binary-xyz\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"),
     "not found on PATH (or not executable); seat will fail at spawn", "warn"),
    (20, seat_block("      bin: /bin/echo\n      args: [\"-p\"]\n      env: {}\n"),
     "agent.args must be a list of at least 2 argv tokens", "seat"),
    (20, seat_block("      bin: /bin/echo\n      args: \"-p {prompt}\"\n      env: {}\n"),
     "agent.args must be a list of at least 2 argv tokens", "seat"),
    (21, seat_block("      bin: /bin/echo\n      args: [\"-p\", 5, \"{prompt}\", \"{model}\"]\n      env: {}\n"),
     "agent.args[1] must be a string (got int)", "seat"),
    (22, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"{model}\", \"two words\"]\n      env: {}\n"),
     "looks like a compound shell fragment", "seat"),
    (23, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"{model}\", \"foo|bar\"]\n      env: {}\n"),
     "contains shell metacharacters", "seat"),
    (24, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"prompt\", \"--model\", \"{model}\"]\n      env: {}\n"),
     "must contain a '{prompt}' placeholder", "seat"),
    (25, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"model\"]\n      env: {}\n"),
     "must contain a '{model}' placeholder", "seat"),
    (26, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"{model}\", \"{foo}\"]\n      env: {}\n"),
     "unknown placeholder '{foo}' in agent.args[3]", "seat"),
    (27, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"pre {prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"),
     "'{prompt}' must be a whole argv token on its own", "seat"),
    (28, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n"),
     "agent.env is required (use {} if the runner owns its config", "seat"),
    (29, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: []\n"),
     "agent.env must be a mapping of NAME: value", "seat"),
    (30, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env:\n        1BAD: x\n"),
     "invalid env var name '1BAD'", "seat"),
    (31, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env:\n        PORT: 443\n"),
     "env.PORT must be a string (got int); quote the value", "seat"),
    (32, seat_block("      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env:\n        KEY: __REPLACE_ME__\n"),
     "env.KEY looks like an unfilled template value", "warn"),
    (33, seat_block("      bin: /bin/some-agent\n      args: [\"-p\", \"{prompt}\", \"{model}\"]\n      env: {}\n"),
     "council_ask (cross-examination) disabled for this seat", "warn"),
    (34, "telemetry:\n  enabled: false\nseats:\n  a:\n    models: [m1]\n    agent:\n" + VALID_AGENT +
     "  b:\n    models: [m1, m2]\n    agent:\n" + VALID_AGENT,
     "both prefer model 'm1'", "warn"),
    (35, "version: 2\ntelemetry:\n  enabled: false\nseats: {}\n",
     "upgrade blessthis-llm-council", "doc"),
]


@pytest.mark.parametrize("rule,text,fragment,kind", CASES, ids=[f"rule{c[0]}_{i}" for i, c in enumerate(CASES)])
def test_rule(tmp_path, rule, text, fragment, kind):
    p = write(tmp_path, text)
    invalidate_cache(p)
    if kind == "doc":
        with pytest.raises(SeatsFileError) as ei:
            load_seats(p)
        assert any(fragment in e for e in ei.value.errors), ei.value.errors
    else:
        seats, warnings = load_seats(p)
        assert any(fragment in w for w in warnings), warnings


# --- explicit targeted tests (clearer than the parametrized skip assertions) -----

def test_valid_document_loads(tmp_path):
    p = write(tmp_path, VALID_SEAT)
    seats, _ = load_seats(p)
    assert len(seats) == 1
    assert seats[0] == Seat(
        name="fable",
        models=["m1"],
        agent=AgentSpec(bin="/bin/echo", args=["-p", "{prompt}", "--model", "{model}"], env={}),
        runner_kind="generic",
    )


def test_invalid_seat_skipped_valid_seat_loads(tmp_path):
    text = (
        "telemetry:\n  enabled: false\nseats:\n"
        "  good:\n    models: [m1]\n    agent:\n"
        "      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"
        "  bad:\n    models: []\n    agent:\n"
        "      bin: /bin/echo\n      args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\"]\n      env: {}\n"
    )
    p = write(tmp_path, text)
    seats, warnings = load_seats(p)
    assert [s.name for s in seats] == ["good"]
    assert any("'models' must be a non-empty list" in w for w in warnings)


def test_rule15_dup_model_still_loads(tmp_path):
    text = seat_block(VALID_AGENT, models="[m1, m1]")
    p = write(tmp_path, text)
    seats, warnings = load_seats(p)
    assert seats[0].models == ["m1"]
    assert any("duplicate model 'm1' ignored" in w for w in warnings)


def test_rule33_unknown_bin_generic_runner(tmp_path):
    text = seat_block(
        "      bin: /bin/definitely-generic-agent\n"
        "      args: [\"-p\", \"{prompt}\", \"{model}\"]\n      env: {}\n")
    p = write(tmp_path, text)
    seats, warnings = load_seats(p)
    assert seats[0].runner_kind == "generic"
    assert any("council_ask (cross-examination) disabled" in w for w in warnings)


def test_runner_kind_known_runners(tmp_path):
    text = (
        "telemetry:\n  enabled: false\nseats:\n"
        "  c1:\n    models: [m]\n    agent:\n      bin: /usr/local/bin/claude\n"
        "      args: [\"-p\", \"{prompt}\", \"{model}\"]\n      env: {}\n"
    )
    p = write(tmp_path, text)
    seats, _ = load_seats(p)
    assert seats[0].runner_kind == "claude"


def test_runner_kind_version_suffix_stripped(tmp_path):
    text = (
        "telemetry:\n  enabled: false\nseats:\n"
        "  p1:\n    models: [m]\n    agent:\n      bin: pi-1.9.3\n"
        "      args: [\"-p\", \"{prompt}\", \"{model}\"]\n      env: {}\n"
    )
    p = write(tmp_path, text)
    seats, _ = load_seats(p)
    assert seats[0].runner_kind == "pi"


def test_version_absent_means_1(tmp_path):
    p = write(tmp_path, VALID_SEAT)
    seats, _ = load_seats(p)
    assert seats


def test_version_1_ok(tmp_path):
    p = write(tmp_path, "version: 1\n" + VALID_SEAT)
    seats, _ = load_seats(p)
    assert seats


def test_perms_warning(tmp_path):
    p = write(tmp_path, VALID_SEAT, mode=0o644)
    seats, warnings = load_seats(p)
    assert seats
    assert any("expected 0600" in w for w in warnings)


# --- cache ----------------------------------------------------------------------

def test_cache_hit_and_miss(tmp_path, monkeypatch):
    p = write(tmp_path, VALID_SEAT)
    calls = []
    real_parse = loader_mod._parse

    def counting_parse(path):
        calls.append(path)
        return real_parse(path)

    monkeypatch.setattr(loader_mod, "_parse", counting_parse)
    invalidate_cache()
    seats1, _ = load_seats(p)
    assert len(calls) == 1
    # second call: stat matches cache → no re-parse
    seats2, _ = load_seats(p)
    assert len(calls) == 1
    assert seats1 == seats2
    # edit the file (size change) → re-parse, new content visible
    p.write_text(VALID_SEAT.replace("fable", "moonshot"))
    p.chmod(0o600)
    seats3, _ = load_seats(p)
    assert len(calls) == 2
    assert seats3[0].name == "moonshot"


def test_force_bypasses_cache(tmp_path, monkeypatch):
    p = write(tmp_path, VALID_SEAT)
    calls = []
    real_parse = loader_mod._parse

    def counting_parse(path):
        calls.append(path)
        return real_parse(path)

    monkeypatch.setattr(loader_mod, "_parse", counting_parse)
    invalidate_cache()
    load_seats(p)
    load_seats(p, force=True)
    assert len(calls) == 2


def test_missing_file_raises(tmp_path):
    with pytest.raises(SeatsFileError):
        load_seats(tmp_path / "nope.yaml")


# --- frozen dataclasses ----------------------------------------------------------

def test_frozen_dataclasses():
    seat = Seat(name="x", models=["m"], agent=AgentSpec(bin="b", args=["a", "c"], env={}),
                runner_kind="generic")
    with pytest.raises(dataclasses.FrozenInstanceError):
        seat.name = "y"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        seat.agent.bin = "z"  # type: ignore[misc]


def test_base_helpers():
    from llm_council.seats.base import _MIN_FILE_ANSWER, seat_working_instruction
    assert _MIN_FILE_ANSWER == 1200
    txt = seat_working_instruction("/tmp/ANSWER.md")
    assert "DELIVERABLE FILE" in txt and "/tmp/ANSWER.md" in txt
    assert "ast-grep" not in txt
    assert "ast-grep" in seat_working_instruction("/tmp/A.md", "/opt/sg")
