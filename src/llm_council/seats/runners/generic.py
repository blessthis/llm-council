"""GenericRunner — last-resort seat runner for unknown CLI binaries (P3b).

No session semantics, no usage accounting: spawn `bin + args`, read stdout to
EOF as the answer. A nonzero exit is a seat-level error (is_error=True with
stderr surfaced), never a raised exception unless the binary can't be spawned
at all.
"""

from __future__ import annotations

import asyncio
import os

from ..base import InvokeResult, OnSession, ProbeResult, Progress, Seat, Usage

RUNNER_CLASS = "GenericRunner"


class GenericRunner:
    """Dumb exec-array runner: bin+args, stdout is the answer."""

    @property
    def runner_kind(self) -> str:
        return "generic"

    @staticmethod
    def render_argv(
        seat: Seat,
        model: str,
        prompt: str,
        workdir: str,
        *,
        session_id: str | None = None,
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
        argv.extend(f"{v}" for v in (extra or {}).values())
        return argv

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
        max_turns: int = 80,
        timeout: int = 1500,
        extra: dict[str, str] | None = None,
    ) -> InvokeResult:
        del resume, system_prompt, capture_to_file, on_session, max_turns
        argv = self.render_argv(seat, model, prompt, workdir, session_id=session_id, extra=extra)
        env = dict(os.environ)
        env.update(seat.agent.env)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return InvokeResult(
                response=f"(generic seat timed out after {timeout}s)",
                usage=Usage(),
                session_id=None,
                model=model,
                is_error=True,
                subtype="timeout",
            )
        stdout = raw_out.decode("utf-8", "replace")
        stderr = raw_err.decode("utf-8", "replace")
        if proc.returncode != 0:
            return InvokeResult(
                response=f"(generic seat exited {proc.returncode}) stderr: {stderr.strip()[:600]}",
                usage=Usage(),
                session_id=None,
                model=model,
                is_error=True,
                subtype=f"exit_{proc.returncode}",
            )
        return InvokeResult(
            response=stdout.strip(),
            usage=Usage(),
            session_id=None,
            model=model,
            is_error=not stdout.strip(),
            subtype=None,
        )

    async def probe(self, seat: Seat, model: str) -> ProbeResult:
        try:
            res = await self.invoke(seat, model, "ping", os.getcwd(), timeout=60)
        except Exception as e:  # noqa: BLE001 — probe never raises
            return ProbeResult(ok=False, model=model, error=repr(e)[:400])
        if res.is_error:
            return ProbeResult(ok=False, model=model, error=res.response[:400])
        return ProbeResult(ok=True, model=model)

    def supports_progress(self) -> bool:
        return False

    def read_progress(self, session_id: str) -> Progress | None:
        del session_id
        return None
