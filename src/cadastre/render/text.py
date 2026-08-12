"""Human- and model-readable text.

Deterministic by construction: nothing here reads a clock, a locale, an
environment variable, or a filesystem path. Same document in, byte-identical
text out (DESIGN §7).

Staleness is rendered at the top, before the answer, because a model reading
prose has to notice it before it acts on the numbers below (DESIGN §2.5).
"""

from __future__ import annotations

from cadastre.core.provenance import Provenance
from cadastre.render.document import (
    Block,
    Bullets,
    Document,
    Fields,
    Finding,
    Para,
    Section,
    Table,
)

WIDTH = 88

_LEVEL_LABEL = {"error": "ERROR", "warn": "WARN ", "info": "NOTE "}


def render(document: Document) -> str:
    lines: list[str] = []
    lines.append(document.title)
    lines.append("=" * len(document.title))
    lines.append("")
    lines.extend(_stale_banner(document.stale))
    for section in document.sections:
        lines.extend(_render_section(section))
    lines.extend(_provenance_block(document.provenance))
    return "\n".join(_rstrip(lines)).rstrip("\n") + "\n"


def _rstrip(lines: list[str]) -> list[str]:
    # Trailing whitespace is invisible and breaks byte-identity across editors.
    return [line.rstrip() for line in lines]


def _stale_banner(stale: tuple[Provenance, ...]) -> list[str]:
    if not stale:
        return []
    lines = ["!! STALE DATA — do not act on the sections below without saying so:"]
    for provenance in sorted(stale, key=lambda p: p.source):
        detail = (
            f"   !! {provenance.source} ({provenance.plugin}) "
            f"last read {provenance.as_of}"
        )
        if provenance.error:
            detail += f" — collector failed: {provenance.error}"
        lines.append(detail)
    lines.append("")
    return lines


def _render_section(section: Section) -> list[str]:
    lines = [f"## {section.title}", ""]
    if section.note:
        lines += _wrap(section.note) + [""]
    for block in section.blocks:
        lines += _render_block(block)
    return lines


def _render_block(block: Block) -> list[str]:
    if isinstance(block, Para):
        return _wrap(block.text) + [""]
    if isinstance(block, Fields):
        if not block.items:
            return []
        width = max(len(k) for k, _ in block.items)
        return [f"{k.ljust(width)}  {v}" for k, v in block.items] + [""]
    if isinstance(block, Bullets):
        if not block.items:
            return []
        return [f"{block.marker} {item}" for item in block.items] + [""]
    if isinstance(block, Table):
        return _render_table(block) + [""]
    if isinstance(block, Finding):
        return _render_finding(block) + [""]
    raise AssertionError(f"unrenderable block {type(block).__name__}")


def _render_table(table: Table) -> list[str]:
    if not table.rows:
        return [table.empty_note]
    columns = len(table.headers)
    widths = [len(h) for h in table.headers]
    for row in table.rows:
        for index in range(columns):
            widths[index] = max(widths[index], len(row[index]))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(table.headers)).rstrip()]
    out.append("  ".join("-" * widths[i] for i in range(columns)))
    for row in table.rows:
        out.append("  ".join(row[i].ljust(widths[i]) for i in range(columns)).rstrip())
    return out


def _render_finding(finding: Finding) -> list[str]:
    label = _LEVEL_LABEL.get(finding.level, finding.level.upper())
    out = [f"{label}  {finding.subject}", f"  {finding.message}"]
    if finding.why:
        out += [f"  {line}" for line in _wrap(finding.why, WIDTH - 2)]
    if finding.fix:
        out += [f"  Fix: {finding.fix}"]
    out += [f"  [{finding.code}]"]
    return out


def _wrap(text: str, width: int = WIDTH) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _provenance_block(provenance: tuple[Provenance, ...]) -> list[str]:
    if not provenance:
        return []
    lines = ["## Provenance", ""]
    rows = []
    for item in sorted(provenance, key=lambda p: (p.source, p.plugin)):
        state = "STALE" if item.stale else "fresh"
        note = f" ({item.error})" if item.error else ""
        rows.append((item.source, item.plugin, item.as_of, state + note))
    lines += _render_table(Table(("source", "plugin", "as_of", "state"), tuple(rows)))
    return lines
