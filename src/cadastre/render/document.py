"""The intermediate document every command produces.

Commands compute; they do not format. They return a `Document`, and the text
and JSON renderers are the only code that decides what output looks like. One
consequence matters: the staleness banner is applied in exactly one place, so
no command can forget it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cadastre.core.provenance import Provenance


@dataclass(frozen=True)
class Block:
    """Base for renderable content."""


@dataclass(frozen=True)
class Para(Block):
    text: str


@dataclass(frozen=True)
class Fields(Block):
    """Aligned key/value pairs. Order is the caller's, and it is preserved."""

    items: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Table(Block):
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    empty_note: str = "(none)"


@dataclass(frozen=True)
class Bullets(Block):
    items: tuple[str, ...]
    marker: str = "-"


@dataclass(frozen=True)
class Finding(Block):
    """A violation, warning, or note with a fix.

    DESIGN §3.3 is the format contract: what is wrong, why, and the fix. An
    error of that shape gets self-corrected in one turn.
    """

    level: str  # error | warn | info
    code: str
    subject: str
    message: str
    why: str | None = None
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "level": self.level,
            "code": self.code,
            "subject": self.subject,
            "message": self.message,
        }
        if self.why:
            out["why"] = self.why
        if self.fix:
            out["fix"] = self.fix
        return out


@dataclass(frozen=True)
class Section:
    title: str
    blocks: tuple[Block, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class Document:
    """A rendered answer, plus the machine-readable form of the same answer."""

    title: str
    sections: tuple[Section, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    #: The `--json` payload. Structured for machines; not a transcription of
    #: the text form.
    data: dict[str, Any] = field(default_factory=dict)
    #: Non-zero exit request, for CI gates. None means success.
    exit_code: int = 0

    @property
    def stale(self) -> tuple[Provenance, ...]:
        return tuple(p for p in self.provenance if p.stale)
