"""Terminal-safe rendering for untrusted text.

This module is a stdlib-only leaf so CLI, doctor, and lightweight HTTP clients
can share one control-sequence policy without importing each other.
"""

from __future__ import annotations

import re

__all__ = ["safe_terminal_text"]

# Strip complete OSC and CSI sequences, other two-byte ESC sequences, and C0/C1
# controls while preserving newlines and tabs. OSC must precede the generic ESC
# alternative so its payload is removed with its introducer and terminator.
_TERMINAL_CTRL_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC through BEL or ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI with the full ECMA-48 parameter class
    r"|\x1b[ -/]*[@-~]"  # other two-byte ESC sequences
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # C0/C1 controls (keep \n and \t)
)


def safe_terminal_text(value: str, *, cap: int = 2000) -> str:
    """Return bounded text that cannot execute complete terminal controls."""
    cleaned = _TERMINAL_CTRL_RE.sub("", value)
    if cap <= 0:
        return ""
    if len(cleaned) > cap:
        return cleaned[: cap - 1] + "…"
    return cleaned
