"""CodexRunner — codex CLI seat runner (P5, seatspec.md §3 "codex").

Spawns the `codex` binary in JSONL streaming mode (`codex exec --json ...`)
and normalizes its output into InvokeResult.

Key facts (seatspec §3 / PLAN.md §5g):
- thread ids come from the first ``{"type": "thread.started", "thread_id": ...}``
  JSONL line and are reported via ``on_session`` IMMEDIATELY (council_ask
  needs them).
- resume is a SUBCOMMAND, not a flag: ``codex exec resume <thread_id> <prompt>``.
  The runner renders the exec-array from seats.yaml, then inserts ``resume``
  + thread_id as discrete tokens before the positional prompt token (Q4).
- usage comes from ``{"type": "turn.completed", "usage": {...}}`` (codex v0.147+),
  normalized from OpenAI-ish field names.
- capture_to_file uses the uniform prompt-level ``seat_working_instruction``
  (same as claude/pi runners) — NOT codex's ``-o`` flag, because the argv
  belongs to seats.yaml. The answer file is read after completion and
  preferred when it meets ``_MIN_FILE_ANSWER``.
- codex is OpenAI-wire: non-OpenAI models need an OpenAI-compatible gateway
  (``OPENAI_BASE_URL`` + key) in the seat's ``agent.env``; auth failures
  surface as stderr in an error InvokeResult (Q12).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile

from ..base import (
    _MIN_FILE_ANSWER,
    PROBE_TIMEOUT,
    InvokeResult,
    OnSession,
    ProbeResult,
    Progress,
    Seat,
    Usage,
    seat_working_instruction,
)

logger = logging.getLogger(__name__)

RUNNER_CLASS = "CodexRunner"  # registry hook (seats/runners/__init__.py)

_STDERR_TAIL = 500
_STDOUT_TAIL = 500
_PROBE_TIMEOUT = PROBE_TIMEOUT


def _parse_usage(u: object) -> Usage:
    """Normalize OpenAI-ish token fields from a turn.completed usage dict."""
    if not isinstance(u, dict):
        return Usage()

    def _get(*names: str) -> int:
        for n in names:
            v = u.get(n)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return Usage(
        input_tokens=_get("input_tokens", "inputTokens", "prompt_tokens"),
        output_tokens=_get("output_tokens", "outputTokens", "completion_tokens"),
        cache_read_tokens=_get(
            "cached_input_tokens", "cache_read_tokens", "prompt_tokens_details.cached_tokens"
        ),
        cache_write_tokens=_get("cache_write_tokens", "cacheWriteTokens"),
    )


def _insert_resume_tokens(argv: list[str], prompt: str, thread_id: str) -> list[str]:
    """Render the resume SUBCOMMAND form: insert ``resume <thread_id>`` as
    discrete tokens after ``exec`` — located via the positional prompt token
    (the rendered argv token that equals the prompt)."""
    idx = None
    if "exec" in argv[1:]:
        idx = argv.index("exec", 1) + 1  # subcommand goes right after `exec`
    elif prompt:
        for i in range(1, len(argv)):
            if argv[i] == prompt:
                idx = i
                break
    if idx is None:
        idx = len(argv)
    return argv[:idx] + ["resume", thread_id] + argv[idx:]


class CodexRunner:
    """SeatRunner for the OpenAI `codex` coding-agent CLI."""

    @property
    def runner_kind(self) -> str:
        return "codex"

    # --- argv rendering (Q4: exec-array, never a shell string) ----------------

    def _render_argv(
        self,
        seat: Seat,
        model: str,
        prompt: str,
        workdir: str,
        resume: str | None,
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
            argv = _insert_resume_tokens(argv, prompt, resume)
        return argv

    @staticmethod
    def _env(seat: Seat, extra: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(seat.agent.env)
        if extra:
            env.update(extra)
        return env

    # --- Protocol: invoke --------------------------------------------------------

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
        scratch_dir: str | None = None
        answer_path: str | None = None
        seat_prompt = prompt
        if capture_to_file:
            scratch_dir = tempfile.mkdtemp(prefix="council-seat-")
            answer_path = os.path.join(scratch_dir, "ANSWER.md")
            seat_prompt = prompt + seat_working_instruction(answer_path)

        argv = self._render_argv(seat, model, seat_prompt, workdir, resume)
        env = self._env(seat, extra)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir or None,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)
            raise RuntimeError(
                f"codex runner: cannot spawn seat binary '{seat.agent.bin}': {e!r}"
            ) from e

        sid: str | None = session_id
        parts: list[str] = []
        usage = Usage()
        error_message: str | None = None
        stderr_task = asyncio.create_task(proc.stderr.read())

        async def _stream() -> None:
            nonlocal sid, usage, error_message
            assert proc.stdout is not None
            async for raw in proc.stdout:
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
                if evt_type == "thread.started":
                    tid = evt.get("thread_id")
                    if isinstance(tid, str) and tid and not sid:
                        sid = tid
                        if on_session is not None:
                            try:
                                await on_session(sid)  # IMMEDIATELY, per contract
                            except Exception:  # noqa: BLE001
                                logger.exception("codex runner: on_session callback failed")
                elif evt_type == "turn.completed":
                    usage = _parse_usage(evt.get("usage"))
                elif evt_type == "item.completed":
                    item = evt.get("item")
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "agent_message"
                        and isinstance(item.get("text"), str)
                    ):
                        parts.append(item["text"])
                elif evt_type == "error":
                    msg = evt.get("message")
                    error_message = msg if isinstance(msg, str) else json.dumps(evt)

        try:
            await asyncio.wait_for(_stream(), timeout)
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await stderr_task
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)
            return InvokeResult(
                response=f"(codex seat timed out after {timeout}s)",
                usage=usage,
                session_id=sid,
                model=model,
                is_error=True,
                subtype="timeout",
            )

        stderr_bytes = await stderr_task
        stderr_tail = stderr_bytes.decode("utf-8", errors="replace")[-_STDERR_TAIL:].strip()
        file_answer = ""
        if answer_path:
            try:
                with open(answer_path, encoding="utf-8", errors="replace") as fh:
                    file_answer = fh.read().strip()
            except OSError:
                file_answer = ""
        if scratch_dir:
            shutil.rmtree(scratch_dir, ignore_errors=True)

        recovered = len(file_answer) >= _MIN_FILE_ANSWER
        answer = "".join(parts).strip()

        if error_message is not None and not recovered:
            return InvokeResult(
                response=f"(codex stream error): {error_message}",
                usage=usage,
                session_id=sid,
                model=model,
                is_error=True,
                subtype="cli_error",
            )
        if proc.returncode != 0 and not recovered:
            return InvokeResult(
                response=(
                    f"codex exited {proc.returncode}: "
                    f"{stderr_tail or answer[-_STDOUT_TAIL:] or '(no stderr)'}"
                ),
                usage=usage,
                session_id=sid,
                model=model,
                is_error=True,
                subtype="cli_error",
            )
        if recovered:
            return InvokeResult(
                response=file_answer,
                usage=usage,
                session_id=sid,
                model=model,
                is_error=False,
            )
        if not answer:
            return InvokeResult(
                response="(codex produced no answer — no agent_message item in stream)",
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

    # --- Protocol: probe ---------------------------------------------------------

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

    # --- Protocol: progress ------------------------------------------------------

    def supports_progress(self) -> bool:  # noqa: D102
        # seatspec §3: Default False until verified — rollout file path not
        # stable/parsed for v1.
        return False

    def read_progress(self, session_id: str) -> Progress | None:  # noqa: D102
        return None
