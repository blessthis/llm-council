"""pi CLI seat runner (P3c, seatspec.md §3 "pi").

Spawns the `pi` binary (or any `pi`-compatible CLI named by seat.agent.bin)
in JSONL streaming mode and normalizes its output into InvokeResult.

Key facts:
- session ids come from the first ``{"type": "session", "id": ...}`` JSONL line
  and are reported via ``on_session`` IMMEDIATELY (council_ask needs them).
- resume appends ``--session <uuid>`` as two discrete argv tokens (never
  ``--no-session`` — sessions are required for cross-examination).
  ``--session`` accepts a session path or id (partial uuid ok).
  ``--session-id <id>`` is the deterministic-resume alternative (creates
  the session if missing) — switch if deterministic ids become needed.
- the working dir is the subprocess **cwd** (``invoke`` passes
  ``cwd=workdir`` to ``create_subprocess_exec``); pi has no ``--add-dir``.
  Safety flags (``--no-extensions``, ``--no-skills``,
  ``--no-prompt-templates``, ``--no-context-files``, ``--offline``) are
  the seat author's responsibility via ``agent.args`` — the runner does
  NOT inject them (args are user-owned).
- usage is BEST-EFFORT: if ``agent_end`` carries usage/token fields they are
  parsed; otherwise zeros are returned. (PLAN.md Q17 spike pending — zeros
  are permitted and degrade only leaderboard data, not correctness.)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from ..base import (
    PROBE_TIMEOUT,
    InvokeResult,
    OnSession,
    ProbeResult,
    Progress,
    Seat,
    Usage,
)

_STDERR_TAIL = 500
_PROBE_TIMEOUT = PROBE_TIMEOUT

RUNNER_CLASS = "PiRunner"  # registry hook (seats/runners/__init__.py)


def _content_text(content: Any) -> str:
    """Assistant message content: plain string, or a list of blocks whose
    text fields are concatenated (block = {"type": "text", "text": ...})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _parse_usage(evt: dict[str, Any]) -> Usage:
    """Best-effort usage extraction from an agent_end event (Q17 pending)."""
    u = evt.get("usage")
    if not isinstance(u, dict):
        u = evt  # some shapes carry the token fields at top level

    def _get(*names: str) -> int:
        for n in names:
            v = u.get(n)  # type: ignore[union-attr]
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return Usage(
        input_tokens=_get("input_tokens", "inputTokens", "prompt_tokens"),
        output_tokens=_get("output_tokens", "outputTokens", "completion_tokens"),
        cache_read_tokens=_get("cache_read_tokens", "cacheReadTokens"),
        cache_write_tokens=_get("cache_write_tokens", "cacheWriteTokens"),
    )


class PiRunner:
    """SeatRunner for the `pi` coding-agent CLI."""

    @property
    def runner_kind(self) -> str:
        return "pi"

    def _render_argv(
        self, seat: Seat, model: str, prompt: str, workdir: str, resume: str | None
    ) -> list[str]:
        subs = {
            "{prompt}": prompt,
            "{model}": model,
            "{workdir}": workdir,
            "{session_id}": resume or "",
        }
        argv = [seat.agent.bin]
        for tok in seat.agent.args:
            for ph, val in subs.items():
                tok = tok.replace(ph, val)
            argv.append(tok)
        if resume:
            # discrete tokens, appended AFTER the rendered exec-array (Q4)
            argv += ["--session", resume]
        return argv

    def _env(self, seat: Seat) -> dict[str, str]:
        env = dict(os.environ)
        env.update(seat.agent.env)
        return env

    async def invoke(  # noqa: D102 (Protocol contract)
        self,
        seat: Seat,
        model: str,
        prompt: str,
        workdir: str,
        *,
        resume: str | None = None,
        session_id: str | None = None,
        system_prompt: str = "",
        capture_to_file: bool = False,
        on_session: OnSession | None = None,
        max_turns: int = 80,
        timeout: int = 1500,
        extra: dict[str, str] | None = None,
    ) -> InvokeResult:
        argv = self._render_argv(seat, model, prompt, workdir, resume)
        env = dict(self._env(seat))
        if extra:
            env.update(extra)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir or None,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # NOTE: pi --mode json emits ONE JSON LINE per event; a toolResult
                # embedding a big file read can exceed any fixed StreamReader
                # limit (default 64KB → "Separator is not found, and chunk exceed
                # the limit"). We therefore do NOT use readline()/async-for; the
                # stream loop below reads raw chunks and splits lines itself.
                limit=2 ** 30,
            )
        except (FileNotFoundError, PermissionError) as e:
            raise RuntimeError(
                f"pi runner: cannot spawn seat binary '{seat.agent.bin}': {e!r}"
            ) from e

        sid: str | None = session_id
        answer: str | None = None
        usage = Usage()
        stderr_task = asyncio.create_task(proc.stderr.read())

        async def _stream() -> None:
            nonlocal sid, answer, usage
            assert proc.stdout is not None
            buf = bytearray()
            # Manual chunked read: no readline limit — one JSON event line can
            # be arbitrarily large (huge toolResult payloads).
            while True:
                chunk = await proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw = bytes(buf[:nl])
                    del buf[:nl + 1]
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue  # non-JSON noise line — skip
                    if not isinstance(evt, dict):
                        continue
                    evt_type = evt.get("type")
                    if evt_type == "session" and evt.get("id") and not sid:
                        sid = str(evt["id"])
                        if on_session is not None:
                            await on_session(sid)  # IMMEDIATELY, per contract
                    elif evt_type == "agent_end":
                        for msg in reversed(evt.get("messages") or []):
                            if isinstance(msg, dict) and msg.get("role") == "assistant":
                                answer = _content_text(msg.get("content"))
                                break
                        usage = _parse_usage(evt)

        try:
            await asyncio.wait_for(_stream(), timeout)
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return InvokeResult(
                response=f"(pi seat timed out after {timeout}s)",
                usage=usage,
                session_id=sid,
                model=model,
                is_error=True,
                subtype="timeout",
            )

        stderr_tail = (await stderr_task).decode("utf-8", errors="replace")[-_STDERR_TAIL:].strip()

        if proc.returncode != 0:
            return InvokeResult(
                response=(f"pi exited {proc.returncode}: {stderr_tail or '(no stderr)'}"),
                usage=usage,
                session_id=sid,
                model=model,
                is_error=True,
                subtype="cli_error",
            )
        if answer is None:
            return InvokeResult(
                response="(pi produced no answer — no assistant message in agent_end)",
                usage=usage,
                session_id=sid,
                model=model,
                is_error=True,
                subtype="no_answer",
            )
        return InvokeResult(
            response=answer,
            usage=usage,
            session_id=sid,
            model=model,
            is_error=False,
        )

    async def probe(self, seat: Seat, model: str) -> ProbeResult:  # noqa: D102
        """Trivial 1-turn CLI spawn; NEVER raises (Q16: no HTTP anywhere)."""
        try:
            result = await self.invoke(
                seat,
                model,
                prompt="Reply with the single word: ok",
                workdir=os.getcwd(),
                timeout=_PROBE_TIMEOUT,
                max_turns=1,
            )
        except Exception as e:  # noqa: BLE001 — binary missing, spawn failure, ...
            return ProbeResult(ok=False, model=model, error=repr(e))
        if result.is_error:
            return ProbeResult(ok=False, model=model, error=result.response)
        return ProbeResult(ok=True, model=model)

    def supports_progress(self) -> bool:  # noqa: D102
        # seatspec §3: session file path not stable for v1 — fallback False.
        return False

    def read_progress(self, session_id: str) -> Progress | None:  # noqa: D102
        return None
