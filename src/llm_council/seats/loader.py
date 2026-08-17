"""seats.yaml loader — docs/seats-schema.md §2 (all 35 rules), §3 (cache), §4, §5.

`load_seats(path)` returns `(seats, warnings)`. Valid seats load; invalid seats
are skipped (their error texts accumulate into warnings); document-level errors
(rules 1-8, 10, 35) raise `SeatsFileError` (the triggering tool renders the
numbered error list per §3 "Invalid file").
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .base import AgentSpec, Seat

__all__ = ["SeatsFileError", "load_seats", "invalidate_cache"]

_SEAT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
_ALLOWED_PLACEHOLDERS = ("{prompt}", "{model}", "{workdir}", "{session_id}")
_SHELL_METACHARS = set('|&;<>`$(')
_TOP_KEYS = ("telemetry", "seats")
_KNOWN_RUNNERS = ("claude", "pi", "codex", "gemini")
_TEMPLATE_VALUES = ("__REPLACE_ME__", "<your-")


class SeatsFileError(Exception):
    """Document-level validation failure — the whole file is rejected."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class _UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML safe loader that rejects duplicate mapping keys (rule 10)."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:  # noqa: D102
        seen: set[Any] = set()
        for key_node, _value in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise SeatsFileError([f"duplicate seat name '{key}'"])
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _runner_kind(bin_path: str) -> str:
    base = os.path.basename(bin_path).lower()
    if base in _KNOWN_RUNNERS:
        return base
    # test fakes (tests/fixtures/fake_bin): fake-claude -> claude, etc.
    m = re.match(r"^fake-(claude|pi|codex|gemini)(?:[-_.].*)?$", base)
    if m:
        return m.group(1)
    # strip a version suffix: claude-2.0 / pi_v1.9 / codex-1
    m = re.match(r"^(claude|pi|codex|gemini)[-_]?v?\d", base)
    if m:
        return m.group(1)
    return "generic"


def _type_name(v: Any) -> str:
    return type(v).__name__


def _check_args(seat: str, args: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(args, list) or len(args) < 2:
        return [f"seat '{seat}': agent.args must be a list of at least 2 argv tokens "
                "(pure exec-array, one token per element)"]
    has_model = False
    has_prompt = False
    for i, el in enumerate(args):
        if not isinstance(el, str):
            errors.append(f"seat '{seat}': agent.args[{i}] must be a string "
                          f"(got {_type_name(el)}); numbers/bools must be quoted")
            continue
        if el == "{prompt}" or "{prompt}" in el:
            has_prompt = True
        if el == "{model}":
            has_model = True
        # rule 26: unknown placeholders
        for ph in _PLACEHOLDER_RE.findall(el):
            if ph not in _ALLOWED_PLACEHOLDERS:
                errors.append(f"seat '{seat}': unknown placeholder '{ph}' in "
                              f"agent.args[{i}]; allowed: {{prompt}}, {{model}}, "
                              "{{workdir}}, {{session_id}}")
        stripped = _PLACEHOLDER_RE.sub("", el)
        # rule 27: {prompt} must be the whole token (checked before compound rule
        # so the more specific message wins)
        if "{prompt}" in el and el != "{prompt}":
            errors.append(f"seat '{seat}': '{{prompt}}' must be a whole argv token "
                          "on its own")
        # rule 23: shell metacharacters outside placeholders
        if any(c in _SHELL_METACHARS for c in stripped):
            errors.append(f"seat '{seat}': agent.args[{i}] '{el}' contains shell "
                          "metacharacters; args is exec'd directly, never through a "
                          "shell — remove the operator and use discrete tokens")
        # rule 22: compound fragment after placeholder removal
        elif any(c.isspace() for c in stripped):
            errors.append(f"seat '{seat}': agent.args[{i}] '{el}' looks like a "
                          "compound shell fragment; pure exec-array requires ONE argv "
                          "token per element (split it into separate list items)")
    # rule 24
    if not has_prompt:
        errors.append(f"seat '{seat}': agent.args must contain a '{{prompt}}' "
                      "placeholder (the council brief has nowhere to go)")
    # rule 25
    if not has_model:
        errors.append(f"seat '{seat}': agent.args must contain a '{{model}}' "
                      "placeholder (the health-picked model has nowhere to go)")
    return errors


def _validate_seat(name: str, seat_def: Any) -> tuple[Seat | None, list[str], list[str]]:
    """Returns (seat_or_None, errors, warnings) for one seat definition."""
    errors: list[str] = []
    warnings: list[str] = []
    s = f"seat '{name}'"

    if not isinstance(seat_def, dict):
        return None, [f"{s}: definition must be a mapping"], warnings

    unknown = set(seat_def) - {"models", "agent"}
    for key in unknown:
        errors.append(f"{s}: unknown key '{key}'; allowed: models, agent")

    models = seat_def.get("models")
    if not isinstance(models, list) or not models:
        errors.append(f"{s}: 'models' must be a non-empty list of model names")
        models = []
    else:
        seen: set[str] = set()
        clean: list[str] = []
        for i, m in enumerate(models):
            if not isinstance(m, str) or not m.strip():
                errors.append(f"{s}: models[{i}] must be a non-empty string")
                continue
            if m in seen:
                warnings.append(f"{s}: duplicate model '{m}' ignored")
                continue
            seen.add(m)
            clean.append(m)
        models = clean

    agent = seat_def.get("agent")
    if not isinstance(agent, dict):
        return None, errors + [f"{s}: 'agent' must be a mapping with bin, args, env"], warnings

    unknown = set(agent) - {"bin", "args", "env"}
    for key in unknown:
        errors.append(f"{s}: unknown key 'agent.{key}'; allowed: bin, args, env")

    bin_path = agent.get("bin")
    if not isinstance(bin_path, str) or not bin_path.strip():
        errors.append(f"{s}: agent.bin must be a non-empty string (binary name on "
                      "PATH or absolute path)")
        bin_path = ""
    else:
        resolved = shutil.which(bin_path) or (
            bin_path if os.path.isabs(bin_path) and os.access(bin_path, os.X_OK) else None
        )
        if resolved is None:
            warnings.append(
                f"{s}: agent.bin '{bin_path}' not found on PATH (or not executable); "
                "seat will fail at spawn — run 'blessthis-llm-council doctor'")

    args = agent.get("args")
    errors.extend(_check_args(name, args))
    if not isinstance(args, list):
        args = []
    args = [a for a in args if isinstance(a, str)]

    env = agent.get("env")
    if "env" not in agent:
        errors.append(f"{s}: agent.env is required (use {{}} if the runner owns its "
                      "config, e.g. pi)")
        env = {}
    elif not isinstance(env, dict):
        errors.append(f"{s}: agent.env must be a mapping of NAME: value")
        env = {}
    else:
        clean_env: dict[str, str] = {}
        for k, v in env.items():
            if not isinstance(k, str) or not _ENV_NAME_RE.match(k):
                errors.append(f"{s}: invalid env var name '{k}'")
                continue
            if not isinstance(v, str):
                errors.append(f"{s}: env.{k} must be a string (got {_type_name(v)}); "
                              f"quote the value, e.g. '{k}: \"443\"'")
                continue
            if any(t in v for t in _TEMPLATE_VALUES):
                warnings.append(f"{s}: env.{k} looks like an unfilled template value")
            clean_env[k] = v
        env = clean_env

    if errors:
        return None, errors, warnings

    runner_kind = _runner_kind(bin_path)
    # rule 33: no {session_id} placeholder AND unknown runner
    if runner_kind == "generic" and "{session_id}" not in args:
        warnings.append(
            f"{s}: no {{session_id}} placeholder and runner '{runner_kind}' has no "
            "known resume flag; council_ask (cross-examination) disabled for this seat")

    seat = Seat(
        name=name,
        models=list(models),
        agent=AgentSpec(bin=bin_path, args=list(args), env=dict(env)),
        runner_kind=runner_kind,
    )
    return seat, [], warnings


def _parse(path: Path) -> tuple[list[Seat], list[str]]:
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 (custom safe loader)
    except SeatsFileError:  # duplicate keys (rule 10)
        raise
    except yaml.YAMLError as e:
        raise SeatsFileError([f"invalid YAML in seats file: {e}"]) from e

    warnings: list[str] = []

    # 0600 permissions warning (§5)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        warnings.append(f"seats file {path} has mode {mode:o}; expected 0600 "
                        "(contains secrets) — run: chmod 600 {path}")

    if not isinstance(doc, dict):
        raise SeatsFileError(
            [f"seats file must be a YAML mapping at the top level, got {_type_name(doc)}"])

    for key in doc:
        if key not in _TOP_KEYS and key != "version":
            warnings_doc = f"unknown top-level key '{key}'; allowed: telemetry, seats"
            raise SeatsFileError([warnings_doc])

    # rule 35: version
    version = doc.get("version", 1)
    if not isinstance(version, int) or version > 1:
        v = version if isinstance(version, int) else _type_name(version)
        raise SeatsFileError(
            [f"seats.yaml version {v} is not supported by this version of "
             "blessthis-llm-council; upgrade blessthis-llm-council"])

    # rule 6/7/8: telemetry
    telemetry = doc.get("telemetry")
    if not isinstance(telemetry, dict):
        raise SeatsFileError(
            ["missing required top-level key 'telemetry' (set telemetry.enabled "
             "explicitly; the installer prompts for it)"])
    enabled = telemetry.get("enabled")
    if not isinstance(enabled, bool):
        raise SeatsFileError(["telemetry.enabled must be explicit true or false"])
    for key in set(telemetry) - {"enabled"}:
        raise SeatsFileError([f"unknown key 'telemetry.{key}'"])

    # rules 4/5: seats mapping
    seats_doc = doc.get("seats")
    if not isinstance(seats_doc, dict):
        raise SeatsFileError(["'seats' must be a mapping of seat name to seat definition"])
    if not seats_doc:
        raise SeatsFileError(["'seats' must define at least one seat"])

    seats: list[Seat] = []
    for name, seat_def in seats_doc.items():
        # rule 9
        if not isinstance(name, str) or not _SEAT_NAME_RE.match(name):
            warnings.append(f"seat '{name}': invalid name; use lowercase letters, "
                            "digits, '.', '_', '-', starting with a letter or digit")
            continue
        seat, errs, warns = _validate_seat(name, seat_def)
        warnings.extend(warns)
        if errs:
            warnings.extend(errs)
            continue
        assert seat is not None
        seats.append(seat)

    # rule 34: shared first model
    first: dict[str, str] = {}
    for seat in seats:
        if not seat.models:
            continue
        m = seat.models[0]
        if m in first:
            warnings.append(f"seats '{first[m]}' and '{seat.name}' both prefer model "
                            f"'{m}'; review whether this is intended")
        else:
            first[m] = seat.name

    return seats, warnings


# --- mtime+size cache (§3) ------------------------------------------------------

_CACHE: dict[Path, tuple[tuple[float, int], tuple[list[Seat], list[str]]]] = {}


def load_seats(path: Path, *, force: bool = False) -> tuple[list[Seat], list[str]]:
    """Load and validate seats.yaml. Returns (seats, warnings).

    Document-level errors raise SeatsFileError. Cached on (mtime, size);
    `force=True` bypasses the cache (used by `seats probe`)."""
    path = Path(path).resolve()
    if not path.exists():
        raise SeatsFileError([f"seats file not found: {path}."])
    if not force:
        st = path.stat()
        key = (st.st_mtime, st.st_size)
        cached = _CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]
    result = _parse(path)
    st = path.stat()
    _CACHE[path] = ((st.st_mtime, st.st_size), result)
    return result


def invalidate_cache(path: Path | None = None) -> None:
    """Drop the cache for one path (all paths when None)."""
    if path is None:
        _CACHE.clear()
    else:
        _CACHE.pop(Path(path).resolve(), None)
