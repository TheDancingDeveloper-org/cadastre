"""CLI adapters for the gated catalog write transaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cadastre.cli.session import Session
from cadastre.core import writes
from cadastre.core.writes import WriteResult
from cadastre.modules.registry import EntityRegistry
from cadastre.render.document import Document, Fields, Section


def run(
    session: Session,
    operation: str,
    kind: str,
    ident: str | None = None,
    record: Path | None = None,
    values: dict[str, Any] | None = None,
    *,
    principal: str,
    reason: str,
    store: str = "declared",
) -> Document:
    payload = (
        values if values is not None else (writes.read_record(record) if record else {})
    )
    result = writes.write(
        session.root,
        operation,
        kind,
        ident,
        payload,
        principal=principal,
        reason=reason,
        now=session.now,
        store=store,
    )
    return _document(session, result, reason)


def parse_target(
    raw: str, *, registry: EntityRegistry | None = None
) -> tuple[str, str]:
    return writes.parse_target(raw, registry=registry)


def annotate_values(items: list[str]) -> dict[str, Any]:
    return writes.annotate_values(items)


def _document(session: Session, result: WriteResult, reason: str) -> Document:
    target = f"{result.kind}:{result.ident}"
    return Document(
        title=f"cadastre {result.operation} {target}",
        sections=(
            Section(
                "Committed",
                (
                    Fields(
                        (
                            ("operation", result.operation),
                            ("target", target),
                            ("principal", result.principal),
                            ("reason", reason),
                            ("database_revision", str(result.database_revision)),
                            ("transaction_id", result.transaction_id),
                            ("audit_id", result.audit_id),
                        )
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "operation": result.operation,
            "target": {"kind": result.kind, "id": result.ident},
            "principal": result.principal,
            "reason": reason,
            "database_revision": result.database_revision,
            "transaction_id": result.transaction_id,
            "audit_id": result.audit_id,
            "paths": list(result.paths),
        },
    )
