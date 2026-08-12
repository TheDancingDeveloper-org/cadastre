"""Rendering observed text as data, never as instruction.

A container label, a DNS TXT record, a repo description or a commit message is
attacker-controllable text that lands in a model's context (DESIGN §6). It is
rendered quoted, on one line, with control characters stripped, so it cannot
occupy a position where it reads as a directive.

This is not sanitisation — the text is not altered beyond flattening, because
hiding an injection attempt is worse than showing it. It is framing.
"""

from __future__ import annotations

_CONTROL = {c: None for c in range(0x20) if c not in (0x09,)}
_CONTROL[0x7F] = None

#: Marker prefix that would let untrusted text imitate Cadastre's own output.
_STRUCTURE = ("#", ">", "!!", "error", "warn", "```")

MAX_LENGTH = 240


def flatten(text: str) -> str:
    """One line, no control characters, no ANSI escapes."""
    collapsed = " ".join(text.replace("\t", " ").split())
    return collapsed.translate(_CONTROL)


def inert(text: str | None, *, max_length: int = MAX_LENGTH) -> str:
    """Quote untrusted text so it is unmistakably a value, not a line of output."""
    if text is None:
        return ""
    flat = flatten(text)
    if len(flat) > max_length:
        flat = flat[: max_length - 1] + "…"
    escaped = flat.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def looks_like_instruction(text: str | None) -> bool:
    """Whether observed text is shaped like a directive.

    Used only to annotate output — never to drop the text. The reader is told
    that a field contains instruction-shaped content and that it was ignored.
    """
    if not text:
        return False
    flat = flatten(text).lower()
    needles = (
        "ignore previous",
        "ignore all previous",
        "disregard the above",
        "you are an ai",
        "system prompt",
        "new instructions",
        "instead, run",
        "execute the following",
    )
    return any(needle in flat for needle in needles) or flat.startswith(_STRUCTURE)
