"""Installer: per-host MCP registration + agent file deployment (PLAN P4a).

Decision #10: canonical server registration name is `llm-council`.
Decision #11: CLI where available, read→modify→write file-merge everywhere.
Decision #12: ownership = content fingerprint, never `_managed_by` keys.
Decision #13: agent files use the `blessthis-council-*` prefix.
"""

from llm_council.installer.fingerprint import find_ours, is_ours
from llm_council.installer.hosts import (
    RegistrationConflict,
    get_host_binding,
)

__all__ = [
    "find_ours",
    "is_ours",
    "RegistrationConflict",
    "get_host_binding",
]
