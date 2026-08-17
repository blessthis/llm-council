"""First-run interactive wizard (P4b, docs/installer-ux.md §2).

Two phases:
  A — build seats.yaml (templates + per-seat builder + probe + telemetry)
  B — wire ONE consumer host (MCP registration + agent deploy + verify)

Everything interactive goes through questionary; rendering through rich.
`--yes` accepts every default; `--host` skips Phase B selection; `--offline`
skips all probe/spawn steps. Non-TTY without `--yes` refuses to run (CI safety).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import questionary

# Ctrl+C must abort the whole wizard, not be swallowed as a None answer.
# questionary's .ask() returns None on Ctrl+C (call sites do `or ""` → loops
# re-prompt, feels like the installer "jumps to another step"). unsafe_ask()
# raises KeyboardInterrupt instead, which typer/click turn into a clean exit 130.
import questionary.question as _question_mod
import yaml

if getattr(_question_mod.Question.ask, "__name__", "") != "unsafe_ask":
    _question_mod.Question.ask = _question_mod.Question.unsafe_ask
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import seats_io
from .config import Config
from .installer.agents_writer import write_agents
from .installer.fingerprint import CANONICAL_KEY, is_ours
from .installer.hosts import RegistrationConflict, get_host_binding
from .installer.platforms import load_platforms
from .installer.server_source import spawn_check_command
from .seats import SeatsFileError, load_seats
from .seats.base import AgentSpec, Seat

__all__ = ["run_wizard", "build_seat_interactive", "probe_seat"]

SEAT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ENV_KV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=.*$")
KNOWN_HOSTS = ("claude", "pi", "codex", "cursor", "copilot", "gemini")

#: Default exec-array args per runner (docs/seats-schema.md §1 examples).
ARGS_TEMPLATES: dict[str, list[str]] = {
    "claude": [
        "-p", "{prompt}", "--output-format", "json",
        "--dangerously-skip-permissions", "--model", "{model}",
        "--add-dir", "{workdir}",
    ],
    # NOTE: no --tools allowlist by default — it would cut off the user's MCP
    # servers/extensions. Add --tools <allowlist> per seat to sandbox it.
    "pi": [
        "--mode", "json", "--no-extensions", "--no-skills",
        "--no-prompt-templates", "--no-context-files",
        "-p", "{prompt}", "--model", "{model}",
    ],
    "codex": [
        "exec", "--json", "--skip-git-repo-check", "-s", "read-only",
        "--color", "never", "-m", "{model}", "-C", "{workdir}", "{prompt}",
    ],
    "gemini": [
        "--model", "{model}", "-p", "{prompt}",
    ],
}

#: Prebuilt seat templates (installer-ux §2 screen A2). env_kind drives A3.4.
TEMPLATES: dict[str, dict[str, Any]] = {
    "fable": {
        "name": "fable", "bin": "claude", "env_kind": "anthropic",
        "models": ["claude-fable-5", "claude-opus-4.8"],
        "creds": "needs ANTHROPIC_API_KEY",
    },
    "moonshot": {
        "name": "moonshot", "bin": "claude", "env_kind": "anthropic_gateway",
        "models": ["kimi-k2.5"],
        "creds": "needs ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN",
    },
    "minimax": {
        "name": "minimax", "bin": "claude", "env_kind": "anthropic_gateway",
        "models": ["minimax-m2.5"],
        "creds": "needs ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN",
    },
    "glm": {
        "name": "glm", "bin": "pi", "env_kind": "none",
        "models": ["glm-5.2"],
        "creds": "pi owns ~/.pi/agent/models.json — no creds",
    },
    "gpt-5": {
        "name": "gpt-5", "bin": "codex", "env_kind": "openai",
        "models": ["gpt-5"],
        "creds": "needs OPENAI_API_KEY or prior codex login",
    },
}

# NOTE: TEMPLATES is no longer offered as a menu — seats are always built as
# (name, runner, models, env). Kept only for backwards-compat imports/tests.

NON_TTY_MESSAGE = (
    "blessthis-llm-council installer is interactive but stdin/stdout is not a TTY.\n"
    "Options:\n"
    "  - run `blessthis-llm-council install` in a terminal\n"
    "  - CI/non-interactive: create seats.yaml manually (see docs/seats-schema.md)\n"
    "    and register the MCP entry per docs/mcp-registration.md\n"
    "  - `--yes` accepts wizard defaults (still interactive where safe)\n"
)


def _version() -> str:
    from importlib import metadata

    try:
        return metadata.version("blessthis-llm-council")
    except metadata.PackageNotFoundError:
        return "0.0.0+dev"


def _confirm(console: Console, question: str, *, default: bool = True,
             yes: bool = False) -> bool:
    """y/N confirm that also accepts non-latin keyboard layouts.

    questionary's built-in confirm binds only y/Y/n/N; every other key is
    swallowed — on a ЙЦУКЕН layout the physical Y/N keys emit «н»/«т» and the
    prompt appears dead. We re-create the confirm prompt with extra bindings
    (Y→нН, N→тТ) so the answer works regardless of the active layout.
    """
    if yes:
        return default
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from questionary import constants as q_const

    status: dict[str, Any] = {"answer": None, "complete": False}
    yes_no = q_const.YES_OR_NO if default else q_const.NO_OR_YES

    def get_prompt_tokens():
        tokens: list[tuple[str, str]] = [
            ("class:qmark", q_const.DEFAULT_QUESTION_PREFIX),
            ("class:question", f" {question} "),
        ]
        if not status["complete"]:
            tokens.append(("class:instruction", f"{yes_no} "))
        if status["answer"] is not None:
            ans = q_const.YES if status["answer"] else q_const.NO
            tokens.append(("class:answer", ans))
        return to_formatted_text(tokens)

    def exit_with_result(event) -> None:
        status["complete"] = True
        event.app.exit(result=status["answer"])

    bindings = KeyBindings()

    @bindings.add(Keys.ControlC, eager=True)
    def _(event):  # noqa: ANN001
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    for key in ("y", "Y", "н", "Н"):  # ЙЦУКЕН: physical Y row emits н
        @bindings.add(key, eager=True)
        def _yes(event, _key=key):  # noqa: ANN001
            status["answer"] = True
            exit_with_result(event)

    for key in ("n", "N", "т", "Т"):  # ЙЦУКЕН: physical N row emits т
        @bindings.add(key, eager=True)
        def _no(event, _key=key):  # noqa: ANN001
            status["answer"] = False
            exit_with_result(event)

    @bindings.add(Keys.ControlM, eager=True)
    def _enter(event):  # noqa: ANN001
        if status["answer"] is None:
            status["answer"] = default
        exit_with_result(event)

    result = PromptSession(get_prompt_tokens, key_bindings=bindings).app.run()
    return bool(result)


def _abort_on_none(answer: Any) -> None:
    if answer is None:  # Ctrl+C
        raise SystemExit(130)


# --- seat helpers ---------------------------------------------------------------

def seat_to_seat_obj(name: str, seat_def: dict) -> Seat:
    """Plain dict seat definition → loader-shaped Seat (for probing)."""
    agent = seat_def["agent"]
    return Seat(
        name=name,
        models=list(seat_def["models"]),
        agent=AgentSpec(bin=agent["bin"], args=list(agent["args"]), env=dict(agent["env"])),
        runner_kind=os.path.basename(agent["bin"]).lower(),
    )


def probe_seat(seat_def: dict) -> tuple[bool, str]:
    """Best-effort live probe of one seat (dict form). Returns (ok, error/tail)."""
    seat = seat_to_seat_obj(seat_def.get("name", "seat"), seat_def)
    try:
        from .seats.runners import get_runner

        try:
            runner = get_runner(seat.runner_kind)
        except Exception:  # noqa: BLE001 — unknown/corrupt module → generic
            runner = get_runner("generic")
        result = asyncio.run(runner.probe(seat, seat.models[0]))
        return result.ok, result.error or ""
    except Exception as exc:  # noqa: BLE001 — probes never raise out
        return False, repr(exc)


def _env_prompts(env_kind: str, console: Console, *, yes: bool) -> dict[str, str]:
    """Runner-aware credential prompts (installer-ux §2 A3.4)."""
    env: dict[str, str] = {}
    if env_kind == "anthropic":
        key = "" if yes else (questionary.password("ANTHROPIC_API_KEY:").ask() or "")
        if key:
            env["ANTHROPIC_API_KEY"] = key
        if not yes:
            base = (questionary.text("ANTHROPIC_BASE_URL (empty for native):").ask() or "").strip()
            if base:
                env["ANTHROPIC_BASE_URL"] = base
                tok = questionary.password("ANTHROPIC_AUTH_TOKEN:").ask() or ""
                if tok:
                    env["ANTHROPIC_AUTH_TOKEN"] = tok
    elif env_kind == "anthropic_gateway":
        base = "" if yes else (questionary.text("ANTHROPIC_BASE_URL:").ask() or "").strip()
        tok = "" if yes else (questionary.password("ANTHROPIC_AUTH_TOKEN:").ask() or "")
        if base:
            env["ANTHROPIC_BASE_URL"] = base
        if tok:
            env["ANTHROPIC_AUTH_TOKEN"] = tok
        key = questionary.password("ANTHROPIC_API_KEY (optional):").ask() or "" if not yes else ""
        if key:
            env["ANTHROPIC_API_KEY"] = key
    elif env_kind == "openai":
        key = "" if yes else (questionary.password(
            "OPENAI_API_KEY (empty — codex login auth also works):").ask() or "")
        if key:
            env["OPENAI_API_KEY"] = key
        base = "" if yes else (questionary.text("OPENAI_BASE_URL (optional):").ask() or "").strip()
        if base:
            env["OPENAI_BASE_URL"] = base
    else:  # "none" — pi/gemini manage their own auth
        console.print("  runner manages its own auth — no env needed.")
    return env


def _extra_env_loop(*, yes: bool, console: Console | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    if yes or not _confirm(console=console, question="Add extra env vars?",
                           default=False):
        return env
    while True:
        raw = questionary.text("KEY=VALUE (empty to finish):").ask() or ""
        raw = raw.strip()
        if not raw:
            return env
        if not ENV_KV_RE.match(raw):
            print(f"  ! {raw!r} must match KEY=VALUE (KEY uppercase/digits/_)")
            continue
        key, _, value = raw.partition("=")
        env[key] = value


def _runner_select(*, yes: bool, current: str | None = None) -> str:
    choices = []
    for binname in ("claude", "pi", "codex", "gemini"):
        found = shutil.which(binname)
        tag = "[detected]" if found else "[NOT FOUND on PATH]"
        if current and (binname == current or str(current).endswith(f"/{binname}")):
            tag += " — current"
        choices.append(questionary.Choice(f"{binname} {tag}", value=binname))
    choices.append(questionary.Choice(
        "custom binary (enter path, generic runner)", value="custom"))
    if yes:
        return current or "claude"
    # Pre-select the seat's current runner when editing
    default_idx = next((i for i, c in enumerate(choices)
                        if current and (c.value == current
                                        or str(current).endswith(f"/{c.value}"))), None)
    if default_idx is not None:
        answer = questionary.select("Runner binary:", choices=choices,
                                    default=choices[default_idx]).ask()
    else:
        answer = questionary.select("Runner binary:", choices=choices).ask()
    _abort_on_none(answer)
    binname = str(answer)
    if binname == "custom":
        return _prompt_binary_path(console=None)
    return binname


def _prompt_binary_path(*, console, default: str = "") -> str:
    """Ask for a binary path when the runner is not on PATH. Loops until the
    path exists and is executable (or user aborts with Ctrl+C/empty answer)."""
    while True:
        raw = questionary.path(
            "Binary is not on PATH — enter the full path to the CLI binary:",
            default=default,
            validate=lambda p: bool(p) and Path(p).expanduser().is_file(),
        ).ask()
        if not raw:
            raise KeyboardInterrupt  # aborted → matches Ctrl+C handling
        p = Path(str(raw)).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        print(f"  ! {p} is not an executable file")


def build_seat_interactive(
    console: Console, *, yes: bool = False, offline: bool = False,
    existing: set[str] | None = None, preset: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One seat builder (installer-ux §2 A3). Returns a seat definition dict
    (name → {models, agent:{bin,args,env}} mapping form) or None if dropped."""
    taken = set(existing or set())
    preset = preset or {}
    # A3.1 name
    default_name = preset.get("name", "")
    while True:
        name = default_name if (yes and default_name) else (
            questionary.text("Seat name (lowercase, [a-z0-9-]):",
                             default=default_name).ask() or "")
        if not SEAT_NAME_RE.match(name):
            print(f"  ! {name!r} must match ^[a-z0-9][a-z0-9-]*$")
            continue
        if name in taken:
            overwrite = _confirm(
                console=console,
                question=f"Seat {name!r} already exists — overwrite its definition?",
                default=False)
            if overwrite is None:
                return None
            if overwrite:
                break
            continue
        break
    # A3.2 models
    models_raw = ",".join(preset["models"]) if (yes and preset.get("models")) else (
        questionary.text("Models, comma-separated, preferred first:",
                         default=",".join(preset.get("models", []))).ask() or "")
    models = [m.strip() for m in models_raw.split(",") if m.strip()]
    if not models:
        return None
    # A3.3 runner
    binname = preset.get("bin") if (yes and preset.get("bin")) else _runner_select(
        yes=yes, current=(preset or {}).get("bin"))
    if not shutil.which(binname) and not Path(binname).expanduser().is_file():
        # editing: preset holds the current full path — offered as default
        cur_path = (preset or {}).get("bin", "")
        if not (yes and cur_path):
            console.print(f"[yellow]{binname!r} not on PATH — enter its full path:[/yellow]")
            binname = _prompt_binary_path(console=console, default=cur_path)
        else:
            binname = cur_path
    template_key = next((k for k in ARGS_TEMPLATES if str(binname).endswith(k)
                         or str(binname).endswith(f"/{k}")), None)
    args = list(ARGS_TEMPLATES[template_key]) if template_key else [
        "{prompt}",  # generic: prompt as first arg, caller reads stdout
    ]
    # A3.4 env — runner-aware creds prompts; pi needs none (models.json).
    env_kind = preset.get("env_kind", "") if preset else {
        "claude": "anthropic", "pi": "none", "codex": "openai",
        "gemini": "none",
    }.get(str(binname).rsplit("/", 1)[-1].lower(), "")
    env = _env_prompts(env_kind, console, yes=yes)
    for k, v in (preset or {}).get("env", {}).items():
        env.setdefault(k, v)  # keep existing values as-is; prompts may override
    env.update(_extra_env_loop(yes=yes, console=console))
    seat_def: dict[str, Any] = {
        "models": models,
        "agent": {"bin": binname, "args": args, "env": env},
    }
    # A3.5 probe (default Yes; --offline skips entirely)
    if not offline:
        if _confirm(console, f"Probe this seat now? (spawns `{binname}` with a 1-token "
                             "prompt)", default=True, yes=yes):
            ok, err = probe_seat({**seat_def, "name": name})
            if ok:
                console.print(f"[green]✓ probe ok[/green] ({binname} answered)")
            while not ok:
                console.print(f"[red]Probe failed:[/red] {err[-400:]}")
                if yes:
                    break
                choice = questionary.select(
                    "Probe failed — what now?",
                    choices=["Keep seat anyway", "Re-enter env", "Drop seat"]).ask()
                if choice == "Keep seat anyway":
                    break
                if choice == "Drop seat":
                    return None
                seat_def["agent"]["env"].update(
                    _env_prompts("anthropic", console, yes=False))
                ok, err = probe_seat({**seat_def, "name": name})
    return {name: seat_def}


# --- Phase A --------------------------------------------------------------------

def _seat_choices(existing: dict[str, Any]) -> list[questionary.Choice]:
    """Edit mode: offer to modify each EXISTING seat, plus add a new one."""
    choices: list[questionary.Choice] = []
    for name, seat in existing.items():
        binname = seat.get("agent", {}).get("bin", "?")
        models = seat.get("models", [])
        choices.append(questionary.Choice(
            f"edit {name} — {binname} runner, models: {models}", value=name))
    choices.append(questionary.Choice("+ Add a new seat…", value="__new__"))
    return choices


def run_phase_a(console: Console, *, yes: bool, offline: bool,
                baseline: dict | None = None, changes: dict | None = None) -> dict:
    """Build + write the seats document. Returns the written doc.

    `baseline` seeds the doc (edit mode): existing seats are kept verbatim
    unless removed/overwritten here, and the baseline telemetry block wins
    unless re-confirmed below.

    `changes` (optional, mutated in place) receives `"touched"` and
    `"removed"` sets of seat names the user explicitly added/overwrote or
    removed — everything else is untouched.
    """
    console.print(Panel(
        "[bold]Phase A — seats[/bold]\n"
        "A seat = (name, runner binary, models, env). Same runner can serve many "
        "seats with different creds.\n"
        "A council needs at least 2 seats to be useful; 1 works."
    ))
    seats: dict[str, Any] = dict((baseline or {}).get("seats") or {})
    touched: set[str] = set()
    removed: set[str] = set()
    if baseline and seats and not yes:
        removable = sorted(seats)
        if _confirm(console, "Remove any existing seats first?", default=False,
                    yes=False):
            drop = questionary.checkbox(
                "Seats to REMOVE (enter to keep all):",
                choices=removable).ask() or []
            for name in drop:
                seats.pop(name, None)
                removed.add(name)
                console.print(f"  removed seat: {name}")
    if baseline and seats and not yes:
        # Edit mode: offer to modify the EXISTING seats, then optionally add
        # new ones. No template menu — a seat is (name, runner, models, env).
        while True:
            selected = questionary.checkbox(
                "Modify which seats? (space to toggle, enter to accept)",
                choices=_seat_choices(seats)).ask() or []
            _abort_on_none(selected)
            for name in selected:
                if name == "__new__":
                    continue
                # Pre-seed the builder with the CURRENT seat definition so
                # Enter keeps each answer; only what you retype changes.
                cur = seats.get(name) or {}
                seat = build_seat_interactive(
                    console, yes=False, offline=offline, existing=set(seats),
                    preset={"name": name,
                            "bin": cur.get("agent", {}).get("bin", "claude"),
                            "models": list(cur.get("models", [])),
                            "env_kind": "",
                            "env": dict(cur.get("agent", {}).get("env", {}))})
                if seat:
                    seats.update(seat)
                    touched.update(seat)
            if "__new__" in selected:
                seat = build_seat_interactive(console, yes=False, offline=offline,
                                              existing=set(seats))
                if seat:
                    seats.update(seat)
                    touched.update(seat)
                    continue
            break
    else:
        # Fresh install: no menus — just build seats one by one.
        if yes or _confirm(console, "Add a seat now?", default=True, yes=yes):
            seat = build_seat_interactive(console, yes=yes, offline=offline,
                                          existing=set(seats))
            if seat:
                seats.update(seat)
                touched.update(seat)
        else:
            if not _confirm(
                console,
                "No seats at all. The MCP server will install but `council_start` "
                "will fail until you add seats (`blessthis-llm-council seats add`). "
                "Continue?", default=False, yes=yes,
            ):
                # not confirmed → go add a seat after all
                seat = build_seat_interactive(console, yes=False, offline=offline,
                                              existing=set(seats))
                if seat:
                    seats.update(seat)
                    touched.update(seat)
    while not offline and _confirm(console, "Add another seat?",
                                   default=False, yes=False):
        seat = build_seat_interactive(console, yes=False, offline=offline,
                                      existing=set(seats))
        if not seat:
            break
        seats.update(seat)
        touched.update(seat)
    console.print("  Two seats may reuse the same key names with different values "
                  "— that's the point.")
    # A4 telemetry (Decision #22: default Yes)
    baseline_telemetry = dict((baseline or {}).get("telemetry") or {})
    if baseline and not yes:
        prev = baseline_telemetry.get("enabled")
        console.print(f"  existing telemetry.enabled = {prev}")
    telemetry = _confirm(
        console,
        "Share anonymized model performance scores? We NEVER send code, file "
        "paths, or briefs — only anonymized model/kind/score/usage and run "
        "metadata.",
        default=bool(baseline_telemetry.get("enabled", True)), yes=yes,
    )
    telemetry_block = {**baseline_telemetry, "enabled": telemetry}
    doc = {**{k: v for k, v in (baseline or {}).items()
              if k not in ("seats", "telemetry")},
           "telemetry": telemetry_block, "seats": seats}
    if changes is not None:
        changes["touched"] = touched
        changes["removed"] = removed
    return doc


def _seat_changes(baseline: dict | None, doc: dict,
                  changes: dict | None) -> tuple[list[str], list[str], list[str], list[str]]:
    """Classify seats vs the baseline: (added, modified, removed, untouched)."""
    baseline_seats = (baseline or {}).get("seats") or {}
    touched = set((changes or {}).get("touched") or set())
    removed = set((changes or {}).get("removed") or set()) - touched
    final = doc.get("seats") or {}
    added = sorted(k for k in touched if k not in baseline_seats)
    modified = sorted(k for k in touched if k in baseline_seats
                      and final.get(k) != baseline_seats[k])
    untouched = sorted(k for k in baseline_seats
                       if k not in touched and k not in removed and k in final)
    return added, modified, sorted(removed), untouched


def _preview_yaml(doc: dict, untouched: Iterable[str]) -> str:
    """Masked YAML preview; untouched seats collapse to a one-line comment."""
    masked = seats_io.mask_secrets(doc)
    keep = set(untouched)
    lines: list[str] = []
    for key, val in masked.items():
        if key == "seats":
            continue
        lines.extend(yaml.safe_dump({key: val}, sort_keys=False).rstrip("\n").splitlines())
    if "seats" in masked:
        lines.append("seats:")
        for name, body in masked["seats"].items():
            if name in keep:
                lines.append(f"  {name}:  # unchanged — kept verbatim from existing file")
            else:
                block = yaml.safe_dump({name: body}, sort_keys=False).rstrip("\n")
                lines.extend(("  " + b) for b in block.splitlines())
    return "\n".join(lines)


def _print_change_summary(console: Console, added: list[str], modified: list[str],
                          removed: list[str], untouched: list[str],
                          telemetry_changed: bool = False) -> bool:
    """Edit-mode diff panel. Returns True when there is anything to write."""
    if not (added or modified or removed):
        if telemetry_changed:
            console.print("No seat changes — telemetry setting updated only.")
            return True
        console.print("No changes to seats (telemetry unchanged)")
        return False
    parts: list[str] = []
    parts.extend(f"[green]+{n}[/green]" for n in added)
    parts.extend(f"[yellow]~{n}[/yellow]" for n in modified)
    parts.extend(f"[red]-{n}[/red]" for n in removed)
    suffix = f" ({len(untouched)} untouched seat" + ("s" if len(untouched) != 1 else "") \
        + " kept)" if untouched else ""
    console.print(f"Changes: {', '.join(parts)}{suffix}")
    return True


# --- Phase B --------------------------------------------------------------------

def _spawn_check(seats_file: str, console: Console) -> bool:
    """Server spawn check via the registered launch command (warms uv cache)."""
    if not shutil.which("uvx"):
        console.print("[yellow]WARN: uvx not found on PATH[/yellow]")
        return False
    env = {**os.environ, "SEATS_FILE": seats_file}
    try:
        proc = subprocess.run(
            spawn_check_command(),
            capture_output=True, text=True, env=env, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(f"[red]server spawn check failed:[/red] {exc}")
        return False
    if proc.returncode != 0:
        console.print(f"[red]server spawn check rc={proc.returncode}:[/red] "
                      f"{proc.stderr[-300:]}")
        return False
    console.print("[green]server spawn check ok[/green] (uvx cache warm)")
    return True


def _verify_host(console: Console, binding, seats_file: str) -> bool:
    """Config + agents read-back (installer-ux §4)."""
    ok = True
    try:
        from .installer.hosts import HostBinding  # noqa: F401  (type hint only)

        if binding.platform.name == "codex":
            import tomlkit
            servers = dict(tomlkit.parse(
                binding.config_path().read_text(encoding="utf-8")).get("mcp_servers") or {})
        else:
            import json
            data = json.loads(binding.config_path().read_text(encoding="utf-8"))
            servers = data.get(binding.servers_key) or {}
        entry = servers.get(CANONICAL_KEY)
        if not entry or not is_ours(dict(entry)):
            console.print(f"[red]verification failed:[/red] no `{CANONICAL_KEY}` "
                          "fingerprint entry in " + str(binding.config_path()))
            ok = False
        elif dict(entry).get("env", {}).get("SEATS_FILE") not in (None, seats_file):
            console.print("[yellow]WARN: env.SEATS_FILE mismatch in host entry[/yellow]")
        agents_dir = binding.agents_dir()
        found = [p.name for p in agents_dir.glob("blessthis-council-*")] \
            if agents_dir.is_dir() else []
        if not found:
            console.print(f"[yellow]WARN: no blessthis-council-* agents in "
                          f"{agents_dir}[/yellow]")
    except Exception as exc:  # noqa: BLE001 — verification must not crash install
        console.print(f"[red]verification failed:[/red] {exc}")
        ok = False
    return ok


def run_phase_b(console: Console, seats_file: str, *, host: str | None,
                yes: bool, offline: bool, project_path: str | None = None) -> list[dict]:
    """Wire one-or-more hosts. Returns per-host result dicts."""
    platforms = load_platforms()
    results: list[dict] = []
    spawn_done = False
    while True:
        if host:
            chosen = host
            host = None  # "another host?" loop re-prompts
        else:
            choices = []
            for name in KNOWN_HOSTS:
                plat = platforms[name]
                binding = get_host_binding(name, project_path=project_path)
                tag = "detected" if binding.detect() else "not detected"
                choices.append(f"{plat.display_name} [{tag}]")
            answer = questionary.select(
                "Which host should consume the council? "
                "(re-run `install` later to add another)", choices=choices).ask()
            _abort_on_none(answer)
            chosen = str(answer).split(" [")[0].lower()
            plat = platforms.get(chosen)
            chosen = chosen if plat else next(
                n for n in KNOWN_HOSTS if platforms[n].display_name.lower().startswith(chosen))
            if not get_host_binding(chosen, project_path=project_path).detect():
                if not _confirm(console, "The host doesn't appear to be installed. "
                                         "Register anyway?", default=False, yes=yes):
                    continue
        binding = get_host_binding(chosen, project_path=project_path)
        scope_note = f"Registering at user scope: {binding.config_path()}"
        if binding.project_scoped:
            pp = project_path or (Path.cwd() if yes else None)
            if pp is None:
                while True:
                    raw = questionary.path(
                        "Project path to wire up (must contain or accept .vscode/ "
                        "and .github/):", only_directories=True).ask() or ""
                    if Path(raw).is_dir():
                        pp = raw
                        break
            binding = get_host_binding(chosen, project_path=pp)
            scope_note = f"Registering at project scope: {binding.config_path()}"
        console.print(scope_note)
        result: dict[str, Any] = {"host": chosen, "registered": False, "agents": 0,
                                  "verified": False}
        try:
            info = binding.register(CANONICAL_KEY, seats_file)
            result["registered"] = True
            result["register_info"] = info
            console.print(f"[green]MCP registered[/green] ({info.get('method')}) → "
                          f"{binding.config_path()}")
        except RegistrationConflict as exc:
            console.print(f"[red]STOP: {exc}[/red]")
            results.append(result)
            if not _confirm(console, "Try another host?", default=False, yes=yes):
                break
            continue
        agents = write_agents(binding)
        result["agents"] = len(agents.get("written", []))
        if agents.get("skipped"):
            console.print(f"[yellow]agents skipped:[/yellow] {agents['skipped']}")
        else:
            console.print(f"[green]{result['agents']} agent files deployed[/green] → "
                          f"{binding.agents_dir()}")
        if chosen == "pi":
            console.print("  Run `/reload` in live pi sessions.")
        elif chosen == "copilot":
            console.print("  Accept the trust prompt on next VS Code window load.")
        result["verified"] = _verify_host(console, binding, seats_file)
        if not spawn_done and not offline:
            console.print("warming uvx cache (first run may take a minute)…")
            result["spawn_ok"] = _spawn_check(seats_file, console)
            spawn_done = True
        results.append(result)
        if not _confirm(console, "Wire up another host now?", default=False, yes=yes):
            break
    return results


# --- Wizard entry ----------------------------------------------------------------

def run_wizard(offline: bool = False, host: str | None = None, yes: bool = False,
               project_path: str | None = None) -> dict:
    """The full two-phase installer (installer-ux §2). Returns a summary dict."""
    console = Console()
    if not sys.stdout.isatty() and not yes:
        console.print(NON_TTY_MESSAGE)
        raise SystemExit(1)
    console.print(Panel(f"blessthis-llm-council installer — v{_version()}\n"
                        "Ctrl+C aborts; nothing is written until the final confirm "
                        "of each phase."))
    cfg = Config.load()
    seats_path = Path(cfg.seats_file)
    summary: dict[str, Any] = {"seats_path": str(seats_path), "hosts": []}

    doc: dict | None = None
    edit_baseline: dict | None = None
    if seats_path.exists():
        try:
            load_seats(seats_path, force=True)
            valid = True
        except SeatsFileError:
            valid = False
        if valid:
            choice = "Keep seats (skip to host setup)" if yes else (
                questionary.select("Existing configuration found", choices=[
                    "Keep seats (skip to host setup)",
                    "Edit seats now (opens seats flow)",
                    "Replace from scratch",
                ]).ask())
            _abort_on_none(choice)
            if str(choice).startswith("Keep"):
                doc = seats_io.load_yaml(seats_path)
            elif str(choice).startswith("Edit"):
                # Edit mode: seed the seats flow with the existing doc so the
                # preview + write show the MERGED doc (untouched seats and the
                # telemetry block survive verbatim).
                edit_baseline = seats_io.load_yaml(seats_path)
                doc = None
            elif str(choice).startswith("Replace"):
                if _confirm(console, "This will overwrite seats.yaml. A .bak is kept.",
                            default=False, yes=yes):
                    doc = None
                else:
                    doc = seats_io.load_yaml(seats_path)
        else:
            console.print("[yellow]Existing seats.yaml is invalid.[/yellow]")
            choice = "Back up and start fresh" if yes else questionary.select(
                "Existing seats.yaml does not parse", choices=[
                    "Back up and start fresh",
                    "Abort so I can fix it manually",
                ]).ask()
            _abort_on_none(choice)
            if not str(choice).startswith("Back up"):
                raise SystemExit(1)
            import time

            backup = seats_path.with_name(
                f"seats.yaml.invalid-{int(time.time())}.bak")
            seats_path.rename(backup)
            console.print(f"backed up → {backup}")

    if doc is None:
        changes: dict[str, set] = {}
        doc = run_phase_a(console, yes=yes, offline=offline,
                          baseline=edit_baseline, changes=changes)
        added, modified, removed, untouched = _seat_changes(
            edit_baseline, doc, changes)
        write_needed = True
        if edit_baseline is not None:
            telemetry_changed = doc.get("telemetry") != edit_baseline.get("telemetry")
            seat_changes = _print_change_summary(
                console, added, modified, removed, untouched,
                telemetry_changed=telemetry_changed)
            if not seat_changes:
                write_needed = False
                console.print(f"`{seats_path}` left as-is; continuing to host setup.")
        if write_needed:
            console.print(Panel(
                _preview_yaml(doc, untouched),
                title="seats.yaml preview (secrets masked, last-4 shown)",
            ))
            if edit_baseline is not None and untouched:
                console.print("Full file is written; `# unchanged` marks seats kept "
                              "unchanged.")
            if _confirm(console, f"Write `{seats_path}` (mode 0600)?", default=True, yes=yes):
                seats_path.parent.mkdir(parents=True, exist_ok=True)
                seats_io.atomic_write_seats(seats_path, doc, allow_empty=True)
                console.print(f"[green]wrote[/green] {seats_path}")
            else:
                console.print("seats.yaml NOT written; aborting before host setup.")
                raise SystemExit(1)

    summary["seats"] = len(doc.get("seats") or {})
    summary["hosts"] = run_phase_b(console, str(seats_path.resolve()), host=host,
                                   yes=yes, offline=offline, project_path=project_path)

    table = Table(title="Install summary")
    table.add_column("what")
    table.add_column("result")
    table.add_row("seats", f"{summary['seats']} → {seats_path}")
    for r in summary["hosts"]:
        state = "ok" if r.get("verified") else "needs attention"
        table.add_row(r["host"], f"mcp={r.get('registered')} agents={r['agents']} "
                                 f"[{state}]")
    console.print(table)
    console.print("Next: restart/reload your host, ask the council agent, "
                  "run `doctor` anytime.")
    return summary
