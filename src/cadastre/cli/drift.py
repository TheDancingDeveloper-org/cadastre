"""`cadastre drift` — where declared and observed disagree.

Surfaces divergence. It does not fix either side, and neither should the reader
without deciding which side is wrong: "fixing" the declaration to match reality
is how an undocumented change becomes documented policy.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cadastre.cli.session import Session
from cadastre.core.drift import (
    Divergence,
    compare,
    known_undeclared,
    unobservable,
)
from cadastre.core.errors import UsageError
from cadastre.core.topology import drift as topology_drift
from cadastre.render.document import Document, Para, Section, Table

_EXPLANATION = {
    "undeclared": (
        "Observed in the estate, absent from declared/. Either it should be "
        "declared, or it should not exist."
    ),
    "missing": (
        "Declared, and the collector that reports this kind did not see it. "
        "Either it is gone, or the collector cannot see all of it."
    ),
    "differs": (
        "Both sides know about it and disagree. The declared value is the "
        "intent; the observed value is what is running."
    ),
    "secret-only-in": (
        "A reference exists in one store and not the other. Names only — no "
        "value was read."
    ),
}


def _table(rows: list[Divergence]) -> Table:
    return Table(
        ("kind", "id", "field", "declared", "observed", "source"),
        tuple(
            (
                d.kind,
                d.id,
                d.field or "",
                d.declared or "",
                d.observed or "",
                d.source,
            )
            for d in rows
        ),
    )


def _cursor(filters: dict[str, str | None], offset: int) -> str:
    """Make an opaque cursor which cannot be reused for another filter."""
    fingerprint = hashlib.sha256(
        json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = json.dumps({"v": 1, "f": fingerprint, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _cursor_offset(cursor: str, filters: dict[str, str | None]) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or decoded.get("v") != 1:
            raise ValueError
        offset = decoded.get("o")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError
        expected = _cursor(filters, 0)
        expected_raw = base64.urlsafe_b64decode(expected + "=" * (-len(expected) % 4))
        if json.loads(expected_raw).get("f") != decoded.get("f"):
            raise ValueError
        return offset
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError("invalid drift cursor") from exc


def _filtered(
    rows: list[Divergence], filters: dict[str, str | None]
) -> list[Divergence]:
    return [
        row
        for row in rows
        if all(
            value is None or getattr(row, name) == value
            for name, value in filters.items()
        )
    ]


def drift(
    session: Session,
    *,
    exit_code: bool = False,
    category: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    entity_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    summary_only: bool = False,
) -> Document:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        raise UsageError("drift limit must be an integer")
    if limit is not None and not 1 <= limit <= 1000:
        raise UsageError("drift limit must be between 1 and 1000")
    if not isinstance(summary_only, bool):
        raise UsageError("drift summary_only must be a boolean")
    divergences = compare(session.catalog, list(session.observed))
    filters = {
        "category": category,
        "kind": kind,
        "source": source,
        "id": entity_id,
    }
    filtered = _filtered(divergences, filters)
    paged = (
        any(filters.values()) or limit is not None or cursor is not None or summary_only
    )
    offset = _cursor_offset(cursor, filters) if cursor is not None else 0
    page_limit = limit if limit is not None else (10 if summary_only else None)
    displayed = (
        filtered[offset:]
        if page_limit is None
        else filtered[offset : offset + page_limit]
    )
    next_cursor = (
        _cursor(filters, offset + len(displayed))
        if page_limit is not None and offset + len(displayed) < len(filtered)
        else None
    )
    known = known_undeclared(session.catalog, list(session.observed))
    blind = unobservable(session.catalog, list(session.observed))
    grouped: dict[str, list[Divergence]] = {}
    for divergence in displayed:
        grouped.setdefault(divergence.category, []).append(divergence)

    sections = []
    if not session.observed:
        sections.append(
            Section(
                "No evidence",
                (
                    Para(
                        "Nothing has been collected. `cadastre drift` compares "
                        "declared/ "
                        "with observed/, and observed/ is empty — that is not the same "
                        "as agreement. Run `cadastre collect` on the collector host."
                    ),
                ),
            )
        )
    for category in ("undeclared", "missing", "differs", "secret-only-in"):
        rows = grouped.get(category)
        if not rows:
            continue
        sections.append(Section(category, (_table(rows),), note=_EXPLANATION[category]))
    topology_findings = topology_drift(session.catalog)
    if topology_findings:
        sections.append(Section("Deployment topology", tuple(topology_findings)))
    if blind:
        sections.append(
            Section(
                "Declared, but nothing looks for it",
                (
                    Table(
                        ("kind", "id", "collectors of this kind"),
                        tuple((r["kind"], r["id"], r["sources"]) for r in blind),
                    ),
                ),
                note=(
                    "These fall outside the scope every collector of their kind "
                    "declared, so NO row above can be about them — not "
                    "`missing`, not `differs`. Absence of drift here means "
                    "nobody looked, which is not agreement. Usually a field "
                    "that has to match a collector's configuration and does "
                    "not: a secret `store` that names no configured source, a "
                    "zone outside `zones:`, an org nobody scans. Either widen "
                    "the source (or its credential), or correct the declared "
                    "value."
                ),
            )
        )
    if known:
        sections.append(
            Section(
                "Known, deliberately not declared",
                (
                    Table(
                        ("kind", "id", "source", "reason"),
                        tuple(
                            (row["kind"], row["id"], row["source"], row["reason"])
                            for row in known
                        ),
                    ),
                ),
                note="These are review-queue exemptions, not resolved observations.",
            )
        )
    if session.observed and not displayed:
        sections.append(
            Section(
                "No divergence",
                (
                    Para(
                        "Every collected source agrees with declared/. This is a "
                        "statement about what the collectors can see, not about "
                        "everything that exists."
                    ),
                ),
            )
        )

    data: dict[str, Any] = {
        "divergences": [d.to_dict() for d in displayed],
        "topology_findings": [f.to_dict() for f in topology_findings],
        "counts": {k: len(v) for k, v in sorted(grouped.items())},
        "known_undeclared": known,
        "unobservable": blind,
    }
    if paged:
        full_counts: dict[str, int] = {}
        for divergence in filtered:
            full_counts[divergence.category] = (
                full_counts.get(divergence.category, 0) + 1
            )
        data["counts"] = dict(sorted(full_counts.items()))
        data["pagination"] = {
            "total": len(filtered),
            "limit": page_limit,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "summary_only": summary_only,
        }
    return Document(
        title="cadastre drift",
        sections=tuple(sections),
        provenance=session.provenance(),
        data=data,
        exit_code=1 if (exit_code and any(d.actionable for d in filtered)) else 0,
    )
