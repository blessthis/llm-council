"""ClaudeRunner — claude CLI seat runner (P3b).

Refactor of `claude_seat.py` onto the SeatRunner Protocol (docs/seatspec.md §3):
ONE seat, ONE env dict, ONE model per invoke — the native/gateway split and the
model fallback chains are gone (they live in seats.yaml now, Decision #16 /
seatspec §5 Q1). Preserved behavior: transient-retry budget + backoff, <think>
stripping, capture-to-file ANSWER.md flow, on_session reporting, timeout-kill
as attempt failure, and seatspec §1 error semantics (RuntimeError only on total
failure).
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import uuid

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

RUNNER_CLASS = "ClaudeRunner"

# Some models (MiniMax) emit their chain-of-thought as literal <think>...</think>
# text in the final message — strip it so the council sees only the conclusion.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

# Transient network/infra errors worth retrying in place. Deliberately does NOT
# match `error_max_turns` (budget problem) or our subprocess timeout (not a blip).
_RETRYABLE_RE = re.compile(
    r"(econnreset|socket (?:connection|hang up)|connection (?:closed|reset|error)|"
    r"\b5(?:0[0-4]|24)\b|\b429\b|rate.?limit|cooling down|overloaded|"
    r"temporarily unavailable|service unavailable|timeout occurred|"
    r"the connection was closed)",
    re.IGNORECASE,
)

_DEFAULT_TIMEOUT = 1500
_DEFAULT_MAX_TURNS = 80
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_S = (5, 15, 30)

_CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def _clean(text: str | None) -> str:
    return _THINK_RE.sub("", (text or "").strip()).strip()


def _last_json(text: str) -> dict | None:
    for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _usage(data: dict) -> Usage:
    u = data.get("usage") or {}
    return Usage(
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
    )


class _AttemptFailure(RuntimeError):
    """One attempt died (nonzero exit / no parseable JSON). Carries diagnostics."""

    def __init__(self, msg: str, *, rc: int | None = None, stderr: str = ""):
        super().__init__(msg)
        self.rc = rc
        self.stderr = stderr


class ClaudeRunner:
    """Drives `claude -p --output-format json` seats via an exec-array spawn."""

    def __init__(
        self,
        *,
        transient_retries: int = _MAX_TRANSIENT_RETRIES,
        retry_backoff_s: tuple[int, ...] = _RETRY_BACKOFF_S,
    ) -> None:
        self.transient_retries = transient_retries
        self.retry_backoff_s = retry_backoff_s

    @property
    def runner_kind(self) -> str:
        return "claude"

    # --- argv rendering (Q4: exec-array, never a shell string) ----------------

    @staticmethod
    def render_argv(
        seat: Seat,
        model: str,
        prompt: str,
        workdir: str,
        *,
        resume: str | None = None,
        session_id: str | None = None,
        system_prompt: str = "",
        max_turns: int = _DEFAULT_MAX_TURNS,
        extra: dict[str, str] | None = None,
    ) -> list[str]:
        subs = {
            "{prompt}": prompt,
            "{model}": model,
            "{workdir}": workdir,
            "{session_id}": session_id or "",
        }
        argv = [seat.agent.bin]
        for a in seat.agent.args:
            for k, v in subs.items():
                a = a.replace(k, v)
            argv.append(a)
        if resume:
            argv += ["--resume", resume]
        elif session_id:
            argv += ["--session-id", session_id]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        for key, value in (extra or {}).items():
            argv += [f"--{key.replace('_', '-')}", value]
        return argv

    @staticmethod
    def _subprocess_env(seat: Seat) -> dict[str, str]:
        env = dict(os.environ)
        env["CI"] = "1"  # keep the CLI non-interactive / quiet
        env.update(seat.agent.env)
        return env

    # --- one subprocess run ----------------------------------------------------

    async def _run_once(
        self,
        seat: Seat,
        model: str,
        prompt: str,
        workdir: str,
        *,
        resume: str | None,
        session_id: str | None,
        system_prompt: str,
        max_turns: int,
        timeout: int,
        extra: dict[str, str] | None,
    ) -> dict:
        argv = self.render_argv(
            seat,
            model,
            prompt,
            workdir,
            resume=resume,
            session_id=session_id,
            system_prompt=system_prompt,
            max_turns=max_turns,
            extra=extra,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=self._subprocess_env(seat),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude seat timed out after {timeout}s (model={model})") from None

        stdout = raw_out.decode("utf-8", "replace")
        stderr = raw_err.decode("utf-8", "replace")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = _last_json(stdout)
        if not isinstance(data, dict):
            raise _AttemptFailure(
                f"claude produced no JSON (model={model}, rc={proc.returncode}): "
                f"{stdout[:600]!r} / stderr={stderr[:400]!r}",
                rc=proc.returncode,
                stderr=stderr,
            )
        return data

    @staticmethod
    def _read_answer_file(path: str | None) -> str:
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return _clean(fh.read())
        except FileNotFoundError:
            return ""
        except Exception:  # noqa: BLE001 — a bad read must not sink the seat
            logger.exception("claude runner: failed reading answer file %s", path)
            return ""

    @staticmethod
    async def _fire(on_session: OnSession | None, session_id: str) -> None:
        if not on_session:
            return
        try:
            await on_session(session_id)
        except Exception:  # noqa: BLE001 — bookkeeping must never sink the seat
            logger.exception("claude runner: on_session callback failed")

    # --- Protocol: invoke --------------------------------------------------------

    async def invoke(
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
        max_turns: int = _DEFAULT_MAX_TURNS,
        timeout: int = _DEFAULT_TIMEOUT,
        extra: dict[str, str] | None = None,
    ) -> InvokeResult:
        scratch_dir: str | None = None
        answer_path: str | None = None
        seat_prompt = prompt
        if capture_to_file:
            scratch_dir = tempfile.mkdtemp(prefix="council-seat-")
            answer_path = os.path.join(scratch_dir, "ANSWER.md")
            extra = dict(extra or {})
            extra.setdefault("add-dir", scratch_dir)
            seat_prompt = prompt + seat_working_instruction(answer_path)
        try:
            sid = session_id or (None if resume else str(uuid.uuid4()))
            active = resume or sid
            if active:
                await self._fire(on_session, active)

            budget = self.transient_retries
            step_resume, step_sid = resume, (None if resume else sid)
            data: dict = {}
            failure: _AttemptFailure | RuntimeError | None = None
            while True:
                used = self.transient_retries - budget
                try:
                    data = await self._run_once(
                        seat,
                        model,
                        seat_prompt,
                        workdir,
                        resume=step_resume,
                        session_id=step_sid,
                        system_prompt=system_prompt,
                        max_turns=max_turns,
                        timeout=timeout,
                        extra=extra,
                    )
                except (_AttemptFailure, RuntimeError) as e:  # noqa: BLE001
                    failure = e
                    transient = bool(_RETRYABLE_RE.search(str(e)))
                    if not transient or budget <= 0:
                        break
                    budget -= 1
                    await asyncio.sleep(
                        self.retry_backoff_s[min(used, len(self.retry_backoff_s) - 1)]
                    )
                    logger.warning(
                        "claude runner invoke crashed (%r) — transient retry, budget left %d",
                        e,
                        budget,
                    )
                    step_resume, step_sid = active, None  # resume our pinned session
                    continue

                failure = None
                result = _clean(data.get("result"))
                file_answer = self._read_answer_file(answer_path)
                usable = len(file_answer) >= _MIN_FILE_ANSWER or (
                    result and not data.get("is_error")
                )
                if usable and not data.get("is_error"):
                    break
                transient = bool(data.get("is_error")) and bool(_RETRYABLE_RE.search(result))
                if not transient or budget <= 0:
                    break
                budget -= 1
                active = data.get("session_id") or active
                step_resume, step_sid = active, None  # resume warm context
                await asyncio.sleep(self.retry_backoff_s[min(used, len(self.retry_backoff_s) - 1)])
                logger.warning(
                    "claude runner transient error (model=%s): %s — retry, budget left %d",
                    model,
                    result[:120],
                    budget,
                )

            # fresh run: report the CLI-assigned id as soon as parsed
            if data.get("session_id") and data["session_id"] != active:
                await self._fire(on_session, data["session_id"])

            file_answer = self._read_answer_file(answer_path)
            recovered = len(file_answer) >= _MIN_FILE_ANSWER
            result = _clean(data.get("result")) if data else ""

            if not data and not recovered:
                # Nothing parseable from any attempt — surface the diagnostics.
                if failure is None:
                    raise RuntimeError(f"claude seat total failure (model={model}): no output")
                if isinstance(failure, _AttemptFailure) and failure.stderr.strip():
                    return InvokeResult(
                        response=f"(seat produced no answer — "
                        f"exit_{failure.rc}) stderr: {failure.stderr.strip()}",
                        usage=Usage(),
                        session_id=None,
                        model=model,
                        is_error=True,
                        subtype=f"exit_{failure.rc}",
                    )
                raise RuntimeError(f"claude seat total failure (model={model}): {failure}")

            if recovered:
                response = file_answer
            elif result:
                response = result
            else:
                subtype = (data.get("subtype") if data else None) or "no_output"
                response = f"(seat produced no answer — {subtype})"
            if data:
                is_error = bool(data.get("is_error")) and not recovered
            else:
                is_error = not recovered
            return InvokeResult(
                response=response,
                usage=_usage(data) if data else Usage(),
                session_id=data.get("session_id"),
                model=model,
                # a recovered full file means the seat succeeded even if the CLI
                # flagged an end-of-run error
                is_error=is_error,
                num_turns=data.get("num_turns"),
                subtype=data.get("subtype"),
            )
        finally:
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)

    # --- Protocol: probe ---------------------------------------------------------

    async def probe(self, seat: Seat, model: str) -> ProbeResult:
        try:
            res = await self.invoke(
                seat,
                model,
                "Reply with just: ok",
                os.getcwd(),
                max_turns=1,
                timeout=PROBE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 — probe never raises
            return self._probe_error(model, repr(e))
        if res.is_error:
            return self._probe_error(model, res.response)
        return ProbeResult(ok=True, model=model)

    @staticmethod
    def _probe_error(model: str, error: str) -> ProbeResult:
        reason = None
        try:  # classify without creating an import cycle
            from ...availability import classify
        except ImportError:
            classify = None
        reason = classify(error) if classify else None
        return ProbeResult(ok=False, model=model, error=error[:400], reason=reason)

    # --- Protocol: progress ------------------------------------------------------

    def supports_progress(self) -> bool:
        return True

    def read_progress(self, session_id: str) -> Progress | None:
        if not session_id:
            return None
        matches = glob.glob(os.path.join(_CLAUDE_PROJECTS, "*", f"{session_id}.jsonl"))
        if not matches:
            return None
        turns = 0
        in_tokens = 0
        out_tokens = 0
        last_event_type: str | None = None
        try:
            with open(matches[0], encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    last_event_type = ev.get("type") or last_event_type
                    if ev.get("type") != "assistant":
                        continue
                    turns += 1
                    u = (ev.get("message") or {}).get("usage") or {}
                    in_tokens += int(u.get("input_tokens") or 0)
                    out_tokens += int(u.get("output_tokens") or 0)
        except OSError:
            return None
        return Progress(
            turns=turns,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            last_event_type=last_event_type,
        )

    # --- parse -------------------------------------------------------------------

    def parse(self, line: str) -> InvokeResult:
        """Single JSON result line → InvokeResult."""
        data: dict = {}
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                data = obj
        except json.JSONDecodeError:
            data = _last_json(line) or {}
        result = _clean(data.get("result"))
        subtype = data.get("subtype") or "no_output"
        return InvokeResult(
            response=result or f"(seat produced no answer — {subtype})",
            usage=_usage(data),
            session_id=data.get("session_id"),
            model=data.get("model") or "",
            is_error=bool(data.get("is_error")),
            num_turns=data.get("num_turns"),
            subtype=data.get("subtype"),
        )
