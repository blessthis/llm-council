"""blessthis-llm-council installer/management CLI (typer app).

Bare invocation (`uvx blessthis-llm-council`) launches the installer. The MCP
server is a SEPARATE console script: blessthis-llm-council-server (Decision #15).

P4b: full command family — install (wizard), seats list/add/edit/remove/probe/
path, status, doctor (with --deep MCP stdio handshake), uninstall, update.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import questionary
import tomlkit
import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import seats_io
from .config import Config
from .installer.agents_writer import remove_agents
from .installer.fingerprint import CANONICAL_KEY, find_ours, is_ours
from .installer.hosts import get_host_binding
from .seats import SeatsFileError, load_seats
from .wizard import KNOWN_HOSTS, build_seat_interactive, probe_seat, run_wizard

app = typer.Typer(
    name="blessthis-llm-council",
    help="Universal installer + manager for blind multi-model LLM councils.",
    no_args_is_help=False,
    add_completion=False,
)
seats_app = typer.Typer(help="Manage seats.yaml (per-seat topology + credentials).")
app.add_typer(seats_app, name="seats")

console = Console()


def _version() -> str:
    try:
        return metadata.version("blessthis-llm-council")
    except metadata.PackageNotFoundError:
        return "0.0.0+dev"


# --- shared helpers ---------------------------------------------------------------

def _cfg() -> Config:
    return Config.load()


def _host_servers(binding) -> dict:
    """Read the host's server table (JSON or TOML per host). Never raises."""
    path = binding.config_path()
    if not path.exists():
        return {}
    try:
        if binding.platform.name == "codex":
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
            return dict(doc.get("mcp_servers") or {})
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get(binding.servers_key)
        return servers if isinstance(servers, dict) else {}
    except Exception:  # noqa: BLE001 — a broken host config must not crash status
        return {}


def _scan_host(name: str, project_path: str | None = None) -> dict:
    """Fingerprint scan of one host: registered keys, name ok, agents present."""
    binding = get_host_binding(name, project_path=project_path)
    servers = _host_servers(binding)
    ours = find_ours(servers)
    return {
        "host": name,
        "detected": binding.detect(),
        "config_path": str(binding.config_path()),
        "agents_dir": str(binding.agents_dir()),
        "keys": sorted(ours),
        "name_ok": CANONICAL_KEY in ours,
        "conflict": CANONICAL_KEY in servers and not is_ours(servers[CANONICAL_KEY]),
        "agents": sorted(p.name for p in binding.agents_dir().glob("blessthis-council-*"))
        if binding.agents_dir().is_dir() else [],
    }


def _load_or_fail(path: Path) -> tuple[list, list] | None:
    """Load seats via the real loader; print a friendly error on failure."""
    try:
        return load_seats(path, force=True)
    except SeatsFileError as exc:
        console.print(f"[red]seats file problem:[/red] {path}")
        for i, err in enumerate(exc.errors, 1):
            console.print(f"  {i}. {err}")
        console.print("Fix with `blessthis-llm-council seats path --edit`.")
        return None


def _seat_referenced(name: str, database_url: str) -> bool:
    """Best-effort: does recent council state reference this seat? (SQLite only)"""
    if not database_url.startswith("sqlite:///"):
        return False
    db_file = database_url.removeprefix("sqlite:///")
    if not Path(db_file).exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT seat FROM council_hats WHERE seat = ?", (name,)).fetchall()
            return bool(rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _spawn_check(seats_file: str) -> tuple[bool, str]:
    """`uvx blessthis-llm-council-server --help` — validates the entry point."""
    if not shutil.which("uvx"):
        return False, "uvx not found on PATH"
    env = {**os.environ, "SEATS_FILE": seats_file}
    try:
        proc = subprocess.run(
            ["uvx", "blessthis-llm-council-server", "--help"],
            capture_output=True, text=True, env=env, timeout=180, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, repr(exc)
    if proc.returncode != 0:
        return False, f"rc={proc.returncode}: {proc.stderr[-300:]}"
    return True, ""


def _deep_mcp_handshake() -> tuple[bool, str]:
    """Real MCP stdio handshake asserting EXACTLY 17 tools (scripts/smoke_tools.py
    logic, exposed as a module function for `doctor --deep`)."""
    import asyncio

    expected = sorted([
        "council_start", "council_poll", "council_answer", "council_ask",
        "council_reveal", "council_is_model_replied", "council_score",
        "council_close", "model_scores", "seat_health",
        "chat_start", "chat_send", "chat_poll", "chat_history", "chat_list",
        "chat_close", "list_seats",
    ])

    async def _run() -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "llm_council.server",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout

        async def rpc(method: str, params: dict | None = None, rid: int = 1) -> dict:
            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": rid, "method": method,
                "params": params or {},
            }).encode() + b"\n")
            await proc.stdin.drain()
            line = await proc.stdout.readline()
            return json.loads(line)

        try:
            await rpc("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "doctor", "version": "0"},
            })
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await proc.stdin.drain()
            tools = await rpc("tools/list", rid=2)
            names = sorted(t["name"] for t in tools["result"]["tools"])
            if len(names) != 17:
                return False, f"expected 17 tools, got {len(names)}: {names}"
            if names != expected:
                return False, f"tool mismatch: {names}"
            return True, ""
        finally:
            proc.terminate()
            await proc.wait()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — diagnostics never crash
        return False, repr(exc)


# --- root / install ----------------------------------------------------------------

@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    """Bare invocation == `install` (launches the wizard)."""
    if ctx.invoked_subcommand is None:
        run_wizard()


@app.command()
def install(
    host: str = typer.Option(None, "--host", help="Skip host select; wire this host."),
    project_path: str = typer.Option(None, "--project-path",
                                     help="Copilot only; default cwd."),
    yes: bool = typer.Option(False, "--yes", help="Accept defaults at every confirm."),
    offline: bool = typer.Option(False, "--offline", help="Skip probe/spawn checks."),
) -> None:
    """Interactive installer: build seats.yaml, then wire ONE consumer host."""
    try:
        run_wizard(offline=offline, host=host, yes=yes, project_path=project_path)
    except SystemExit:
        raise


# --- seats --------------------------------------------------------------------------

@seats_app.command("list")
def seats_list() -> None:
    """List seats from seats.yaml (secrets masked)."""
    path = Path(_cfg().seats_file)
    loaded = _load_or_fail(path)
    if loaded is None:
        raise typer.Exit(1)
    seats, warnings = loaded
    table = Table(title=f"seats — {path}")
    table.add_column("name")
    table.add_column("runner")
    table.add_column("models (preferred first)")
    table.add_column("env keys")
    for seat in seats:
        table.add_row(
            seat.name, seat.agent.bin, ", ".join(seat.models),
            ", ".join(seat.agent.env) or "—",
        )
    console.print(table)
    for w in warnings:
        console.print(f"[yellow]warn:[/yellow] {w}")


@seats_app.command("add")
def seats_add(name: str = typer.Argument("")) -> None:
    """Add a seat (interactive builder, installer-ux §2 A3). Appends."""
    path = Path(_cfg().seats_file)
    if not path.exists():
        console.print(f"[red]no seats file at {path} — run `install` first[/red]")
        raise typer.Exit(1)
    try:
        doc = seats_io.load_yaml(path) or {}
        existing = set((doc.get("seats") or {}).keys())
    except yaml.YAMLError as exc:
        console.print(f"[red]seats file invalid ({exc}); refusing to operate. "
                      "See `seats path --edit`.[/red]")
        raise typer.Exit(1) from exc
    preset = None
    if name:
        if name in existing:
            console.print(f"[red]seat {name!r} already exists[/red]")
            raise typer.Exit(1)
        preset = {"name": name}
    built = build_seat_interactive(console, yes=False, offline=False, existing=existing,
                                   preset=preset)
    if not built:
        console.print("no seat added")
        return
    doc.setdefault("telemetry", {"enabled": False})
    doc.setdefault("seats", {}).update(built)
    seats_io.atomic_write_seats(path, doc)
    console.print(f"[green]added[/green] {', '.join(built)} → {path}")


@seats_app.command("edit")
def seats_edit(name: str = typer.Argument(...)) -> None:
    """Interactively edit one seat (models / bin / env / args)."""
    path = Path(_cfg().seats_file)
    if not path.exists():
        console.print(f"[red]no seats file at {path}[/red]")
        raise typer.Exit(1)
    doc = seats_io.load_yaml(path) or {}
    seat = (doc.get("seats") or {}).get(name)
    if seat is None:
        console.print(f"[red]seat {name!r} not found[/red]")
        raise typer.Exit(1)

    field = questionary.select(
        f"Edit which field of {name}?", choices=["models", "bin", "env", "args"]).ask()
    if field is None:
        raise typer.Exit(130)

    def mutate(d: Any) -> None:
        target = d["seats"][name]
        if field == "models":
            raw = questionary.text(
                "Models, comma-separated, preferred first:",
                default=",".join(target["models"])).ask() or ""
            target["models"] = [m.strip() for m in raw.split(",") if m.strip()]
        elif field == "bin":
            target["agent"]["bin"] = (
                questionary.text("Runner binary:", default=target["agent"]["bin"])
                .ask() or target["agent"]["bin"])
        elif field == "env":
            env = target["agent"].get("env") or {}
            keys = list(env) + ["+ add key…"]
            key = questionary.select("Which env key?", choices=keys).ask()
            if key is None:
                raise typer.Exit(130)
            if key == "+ add key…":
                key = questionary.text("New KEY:").ask() or ""
                if not key:
                    return
            shown = env.get(key, "")
            masked = ("****" + shown[-4:]) if len(shown) > 4 else "****" if shown else ""
            value = questionary.password(
                f"{key} (current: {masked or '<unset>'}):").ask() or ""
            target["agent"].setdefault("env", {})[key] = value
        elif field == "args":
            raw = questionary.text(
                "args, comma-separated (one argv token per element; {prompt} and "
                "{model} required):",
                default=",".join(target["agent"]["args"])).ask() or ""
            target["agent"]["args"] = [a.strip() for a in raw.split(",") if a.strip()]

    try:
        seats_io.edit_seats_file(path, mutate)
        console.print(f"[green]updated[/green] {name} → {path}")
    except SeatsFileError as exc:
        console.print("[red]validation failed — file NOT changed:[/red]")
        for i, err in enumerate(exc.errors, 1):
            console.print(f"  {i}. {err}")
        raise typer.Exit(1) from exc


@seats_app.command("remove")
def seats_remove(name: str = typer.Argument(...)) -> None:
    """Remove a seat (confirm; warns on recent council references)."""
    path = Path(_cfg().seats_file)
    doc = seats_io.load_yaml(path) or {}
    if name not in (doc.get("seats") or {}):
        console.print(f"[red]seat {name!r} not found[/red]")
        raise typer.Exit(1)
    if _seat_referenced(name, _cfg().database_url):
        console.print(f"[yellow]WARN: seat {name!r} is referenced in recent council "
                      "state (council_hats); those records keep the name.[/yellow]")
    if not questionary.confirm(f"Remove seat {name!r}?", default=False).ask():
        console.print("aborted")
        return

    def mutate(d: Any) -> None:
        d["seats"].pop(name, None)

    try:
        seats_io.edit_seats_file(path, mutate)
        console.print(f"[green]removed[/green] {name}")
    except SeatsFileError as exc:
        console.print(f"[red]refusing to write now-invalid file: {exc}[/red]")
        raise typer.Exit(1) from exc


@seats_app.command("probe")
def seats_probe(
    name: str = typer.Argument(""),
    all_: bool = typer.Option(False, "--all", help="Probe every seat."),
) -> None:
    """Fresh-load seats.yaml and live-probe one seat or all (cache bypassed)."""
    path = Path(_cfg().seats_file)
    loaded = _load_or_fail(path)
    if loaded is None:
        raise typer.Exit(1)
    seats, _ = loaded
    targets = seats if all_ else [s for s in seats if s.name == name]
    if not targets:
        console.print(f"[red]no matching seat {name!r}[/red]")
        raise typer.Exit(1)
    failures = 0
    for seat in targets:
        defn = {"name": seat.name, "models": list(seat.models),
                "agent": {"bin": seat.agent.bin, "args": list(seat.agent.args),
                          "env": dict(seat.agent.env)}}
        ok, err = probe_seat(defn)
        if ok:
            console.print(f"[green]ok[/green]   {seat.name} ({seat.models[0]})")
        else:
            failures += 1
            console.print(f"[red]fail[/red] {seat.name} ({seat.models[0]}): "
                          f"{err[-400:]}")
    if failures:
        raise typer.Exit(1)


@seats_app.command("path")
def seats_path(edit: bool = typer.Option(False, "--edit", help="Open $EDITOR.")) -> None:
    """Print the resolved seats.yaml path (SEATS_FILE env or default)."""
    path = Path(_cfg().seats_file)
    if not edit:
        typer.echo(str(path))
        return
    editor = os.environ.get("EDITOR") or "vi"
    raise typer.Exit(subprocess.call([editor, str(path)]))


# --- status -------------------------------------------------------------------------

@app.command()
def status() -> None:
    """Fast, no probes: seats, DB backend, per-host wiring, version."""
    cfg = _cfg()
    seats_path_ = Path(cfg.seats_file)
    console.print(f"blessthis-llm-council {_version()}")
    count = "—"
    if seats_path_.exists():
        try:
            count = str(len(load_seats(seats_path_, force=True)[0]))
        except SeatsFileError:
            count = "INVALID"
    console.print(f"seats file: {seats_path_} "
                  f"({'exists' if seats_path_.is_file() else 'MISSING'}, "
                  f"{count} seat(s))")
    backend = "postgres" if cfg.database_url.startswith("postgres") else "sqlite"
    console.print(f"db backend: {backend} ({cfg.database_url})")
    table = Table(title="host wiring")
    table.add_column("host")
    table.add_column("registered")
    table.add_column("name ok")
    table.add_column("agents")
    for host in KNOWN_HOSTS:
        info = _scan_host(host)
        table.add_row(
            host,
            "yes" if info["keys"] else "no",
            "yes" if info["name_ok"] else ("CONFLICT" if info["conflict"] else "no"),
            str(len(info["agents"])),
        )
    console.print(table)


# --- doctor -------------------------------------------------------------------------

@app.command()
def doctor(
    deep: bool = typer.Option(False, "--deep", help="Full MCP stdio handshake."),
    offline: bool = typer.Option(False, "--offline", help="Skip spawn checks."),
) -> None:
    """Read-only diagnostics. Exit 0 when nothing FAILs, else 1."""
    failures = 0

    def line(level: str, text: str) -> None:
        nonlocal failures
        color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[level]
        if level == "FAIL":
            failures += 1
        console.print(f"[{color}]{level}[/{color}] {text}")

    cfg = _cfg()
    path = Path(cfg.seats_file)
    # 1. seats file
    if not path.exists():
        line("FAIL", f"seats file not found: {path} — run `install`")
    else:
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            line("WARN", f"seats file mode {mode:o}, expected 0600 — chmod 600 {path}")
        else:
            line("PASS", f"seats file {path} (mode 0600)")
        try:
            seats, warnings = load_seats(path, force=True)
        except SeatsFileError as exc:
            for err in exc.errors:
                line("FAIL", err)
            seats = []
        else:
            if not seats:
                line("WARN", "no seats → council cannot start (`seats add`)")
            for w in warnings:
                line("WARN", w)
        # 2. bins on PATH
        for seat in seats:
            if shutil.which(seat.agent.bin) or (
                Path(seat.agent.bin).is_absolute() and Path(seat.agent.bin).is_file()
            ):
                line("PASS", f"seat {seat.name}: bin {seat.agent.bin} resolvable")
            else:
                line("WARN", f"seat {seat.name}: bin {seat.agent.bin} NOT on PATH")
        # 3. runner-required env
        for seat in seats:
            required: list[str] = []
            if seat.runner_kind == "claude" and not (
                seat.agent.env.get("ANTHROPIC_API_KEY")
                or (seat.agent.env.get("ANTHROPIC_AUTH_TOKEN")
                    and seat.agent.env.get("ANTHROPIC_BASE_URL"))
            ):
                required = ["ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN+BASE_URL"]
            elif seat.runner_kind == "codex" and not seat.agent.env.get("OPENAI_API_KEY"):
                line("WARN", f"seat {seat.name}: OPENAI_API_KEY empty "
                             "(ok if `codex login` was used)")
                continue
            if required:
                line("WARN", f"seat {seat.name}: missing {required[0]}")
            else:
                line("PASS", f"seat {seat.name}: env present")
    # 4. spawn check
    if not offline:
        ok, err = (_deep_mcp_handshake() if deep else _spawn_check(str(path)))
        if ok:
            line("PASS", "server spawn check (17 tools)" if deep else
                 "uvx blessthis-llm-council-server --help")
        else:
            line("FAIL", f"server spawn check: {err}")
    # 5. per-host wiring
    for host in KNOWN_HOSTS:
        info = _scan_host(host)
        if info["conflict"]:
            line("FAIL", f"{host}: key `{CANONICAL_KEY}` exists but is NOT ours "
                         "(rename/remove it)")
            continue
        notes = []
        if info["keys"]:
            notes.append(f"registered under {info['keys']}")
            if not info["name_ok"]:
                line("WARN", f"{host}: our fingerprint found under "
                             f"{info['keys']}, not `{CANONICAL_KEY}`")
                continue
        if info["agents"]:
            notes.append(f"{len(info['agents'])} agents")
        if notes:
            line("PASS", f"{host}: " + ", ".join(notes))
        elif info["detected"]:
            line("WARN", f"{host}: host detected but council not wired "
                         "(run `install --host {host}`)")
        else:
            line("WARN", f"{host}: not installed — skipped")
    # 6. DB writable
    if cfg.database_url.startswith("sqlite:///"):
        db_file = Path(cfg.database_url.removeprefix("sqlite:///"))
        try:
            db_file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_file)
            conn.execute("SELECT 1")
            conn.close()
            line("PASS", f"sqlite writable: {db_file}")
        except sqlite3.Error as exc:
            line("FAIL", f"sqlite not writable: {exc}")
    else:
        line("WARN", f"DATABASE_URL is non-sqlite ({cfg.database_url.split(':')[0]}); "
                     "reachability not checked")
    raise typer.Exit(1 if failures else 0)


# --- uninstall / update ---------------------------------------------------------------

@app.command()
def uninstall(
    host: str = typer.Option(None, "--host", help="Restrict to one host."),
    purge: bool = typer.Option(False, "--purge",
                               help="Also delete seats.yaml + state.db (needs --yes)."),
    yes: bool = typer.Option(False, "--yes", help="No prompts (non-interactive)."),
) -> None:
    """Remove our MCP entries + agent files (fingerprint-scoped)."""
    if host and host not in KNOWN_HOSTS:
        console.print(f"[red]unknown host {host!r}; known: {list(KNOWN_HOSTS)}[/red]")
        raise typer.Exit(1)
    scans = {h: _scan_host(h) for h in ([host] if host else KNOWN_HOSTS)}
    wired = [h for h, info in scans.items() if info["keys"] or info["agents"]]
    if not wired:
        console.print("nothing to remove (no fingerprint entries, no agents found)")
    selected: list[str]
    if yes:
        selected = wired
    else:
        if not wired:
            selected = []
        else:
            selected = questionary.checkbox(
                "Remove from which hosts?",
                choices=[questionary.Choice(
                    f"{h} (keys={scans[h]['keys']}, agents={len(scans[h]['agents'])})",
                    value=h, checked=True) for h in wired]).ask() or []
    removed: list[str] = []
    for h in selected:
        binding = get_host_binding(h)
        keys = binding.uninstall()
        agents = remove_agents(binding)
        removed += [f"{h}: keys {keys}"] + [f"{h}/agents/{a}" for a in agents]
    if removed:
        console.print("[green]removed:[/green]")
        for r in removed:
            console.print(f"  {r}")
    cfg = _cfg()
    if purge:
        if not yes:
            console.print("[red]--purge requires --yes[/red]")
            raise typer.Exit(1)
        for target in (Path(cfg.seats_file),
                       Path(cfg.database_url.removeprefix("sqlite:///"))):
            if target.exists():
                target.unlink()
                console.print(f"  deleted {target}")
    else:
        answer = False
        if wired and not yes:
            answer = bool(questionary.confirm(
                "Also delete seats.yaml (contains your API keys) and state.db?",
                default=False).ask())
        if answer:
            for target in (Path(cfg.seats_file),
                           Path(cfg.database_url.removeprefix("sqlite:///"))):
                if target.exists():
                    target.unlink()
                    console.print(f"  deleted {target}")
        else:
            console.print(f"kept {cfg.seats_file} and state.db "
                          "(use --purge --yes to delete)")


@app.command()
def update() -> None:
    """Upgrade hint + re-run the server spawn check."""
    console.print(
        "uvx resolves the latest version on cold cache. To force an upgrade:\n"
        "  uv cache clean blessthis-llm-council\n"
        "  # or, if installed as a tool:\n"
        "  uv tool upgrade blessthis-llm-council\n"
        "then re-run `install` to refresh MCP entries + agent files."
    )
    ok, err = _spawn_check(_cfg().seats_file)
    if ok:
        console.print("[green]server spawn check ok[/green]")
    else:
        console.print(f"[red]server spawn check failed:[/red] {err}")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
