"""GeminiRunner — seat runner for the Gemini CLI.

No session resume support (the CLI has no stable session id on stdout), no
usage accounting: spawn `bin + args`, read stdout to EOF as the answer. A
nonzero exit is a seat-level error (is_error=True with stderr surfaced).

Differs from GenericRunner only in kind name; kept separate so future
gemini-specific semantics (session resume, usage parse) have a home.
"""

from __future__ import annotations

from .generic import GenericRunner

RUNNER_CLASS = "GeminiRunner"


class GeminiRunner(GenericRunner):
    """Gemini CLI: exec-array spawn, stdout is the answer."""

    @property
    def runner_kind(self) -> str:
        return "gemini"
