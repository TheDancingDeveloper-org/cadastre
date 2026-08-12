"""Trust-state query and resolution adapters."""

from __future__ import annotations

from typing import Any

from cadastre.cli.session import Session
from cadastre.core.errors import UsageError
from cadastre.core.trust import (
    active_acknowledgements,
    presented_records,
    record_for,
    unverified_sources,
)
from cadastre.core.writes import parse_target
from cadastre.render.document import Bullets, Document, Section, Table


def stale(session: Session) -> Document:
    records = presented_records(session.root, session.now)
    acknowledgements = active_acknowledgements(session.root, session.now)
    acknowledged_keys = {
        (str(item.get("kind")), str(item.get("id")), str(item.get("source")))
        for item in acknowledgements
    }
    rows = []
    data: list[dict[str, Any]] = []
    for provenance in session.provenance():
        if provenance.stale:
            rows.append(
                (
                    "stale",
                    provenance.source,
                    provenance.as_of,
                    provenance.error or "expired",
                )
            )
            data.append(
                {
                    "state": "stale",
                    "source": provenance.source,
                    "as_of": provenance.as_of,
                    "error": provenance.error,
                }
            )
    configured = tuple(
        source.id for source in session.plugins.sources if source.enabled
    )
    observed = tuple(source.source for source in session.observed)
    for source in unverified_sources(session.root, configured, observed):
        rows.append(("unverified", source, "", "never confirmed"))
        data.append({"state": "unverified", "source": source})
    for record in records:
        if record.state != "contested":
            continue
        if (record.kind, record.id, record.source) in acknowledged_keys:
            continue
        rows.append(
            (
                "contested",
                f"{record.kind}:{record.id}",
                record.source,
                record.first_seen,
            )
        )
        data.append({"state": "contested", **record.to_dict()})
    for item in acknowledgements:
        data.append({"state": "acknowledged", **item})
    sections = [
        Section(
            "Attention",
            (Table(("state", "subject", "source", "since"), tuple(rows)),),
            note=(
                "Stale, unverified, and contested are independent trust axes. "
                "An empty table means no recorded attention items."
            ),
        )
    ]
    return Document(
        title="cadastre stale",
        sections=tuple(sections),
        provenance=session.provenance(),
        data={"items": data, "acknowledgements": len(acknowledgements)},
    )


def resolve(
    session: Session,
    action: str,
    target: str,
    *,
    source: str,
    field: str | None = None,
    principal: str,
    reason: str,
) -> Document:
    kind, ident = parse_target(target, registry=session.registry)
    if action not in {"accept-observed", "leave-contested"}:
        raise UsageError(f"unknown resolution: {action}")
    if record_for(session.root, kind, ident, field, source) is None:
        raise UsageError(
            f"no recorded contest for {kind}:{ident} field={field or '<entity>'} "
            f"source={source}"
        )
    # Resolutions are catalog-owned metadata and use the SQLite write gate.
    values = {
        "kind": kind,
        "id": ident,
        "field": field or "",
        "source": source,
        "action": action,
        "principal": principal,
        "reason": reason,
    }
    return _resolution_write(session, values, principal=principal, reason=reason)


def acknowledge(
    session: Session,
    target: str,
    *,
    source: str,
    until: str,
    reason: str,
    principal: str,
) -> Document:
    kind, ident = parse_target(target, registry=session.registry)
    values = {
        "kind": kind,
        "id": ident,
        "source": source,
        "until": until,
        "reason": reason,
        "principal": principal,
    }
    return _acknowledgement_write(session, values, principal=principal, reason=reason)


def _resolution_write(
    session: Session, values: dict[str, Any], *, principal: str, reason: str
) -> Document:
    from cadastre.core.writes import write_metadata

    result = write_metadata(
        session.root,
        "resolutions",
        values,
        principal=principal,
        reason=reason,
        now=session.now,
    )
    return Document(
        title="cadastre resolve",
        sections=(
            Section(
                "Committed",
                (
                    Bullets(
                        (f"revision {result.database_revision}: {values['action']}",)
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "action": values["action"],
            "target": {"kind": values["kind"], "id": values["id"]},
            "database_revision": result.database_revision,
            "transaction_id": result.transaction_id,
            "audit_id": result.audit_id,
        },
    )


def _acknowledgement_write(
    session: Session, values: dict[str, Any], *, principal: str, reason: str
) -> Document:
    from cadastre.core.writes import write_metadata

    result = write_metadata(
        session.root,
        "acknowledgements",
        values,
        principal=principal,
        reason=reason,
        now=session.now,
    )
    return Document(
        title="cadastre acknowledge",
        sections=(
            Section(
                "Committed",
                (
                    Bullets(
                        (
                            f"revision {result.database_revision}: acknowledged until "
                            f"{values['until']}",
                        )
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "target": {"kind": values["kind"], "id": values["id"]},
            "until": values["until"],
            "database_revision": result.database_revision,
            "transaction_id": result.transaction_id,
            "audit_id": result.audit_id,
        },
    )
