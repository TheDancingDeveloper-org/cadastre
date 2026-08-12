"""`cadastre sources` — what is configured, and what it says about itself.

The `plugin.info` handshake, run against every configured source. The first
thing to reach for when a collector is quiet: it separates "not configured"
from "configured and failing", which the observed files alone cannot.
"""

from __future__ import annotations

from typing import Any

from cadastre.cli.session import Session
from cadastre.plugins import runner
from cadastre.render.document import Document, Section, Table


def sources(session: Session) -> Document:
    rows = []
    data: list[dict[str, Any]] = []
    for source in session.plugins.sources:
        if not source.enabled:
            rows.append((source.id, "disabled", "", ""))
            data.append({"id": source.id, "state": "disabled"})
            continue
        outcome = runner.info(source)
        if outcome.ok and outcome.reply is not None:
            result = outcome.reply.result
            capabilities = ",".join(result.get("capabilities") or [])
            rows.append(
                (
                    source.id,
                    "ok",
                    f"{result.get('name', '?')} {result.get('version', '')}".strip(),
                    capabilities,
                )
            )
            data.append(
                {
                    "id": source.id,
                    "state": "ok",
                    "name": result.get("name"),
                    "version": result.get("version"),
                    "capabilities": result.get("capabilities") or [],
                    "methods": result.get("methods") or [],
                }
            )
        else:
            rows.append((source.id, "FAILED", outcome.message or "", ""))
            data.append({"id": source.id, "state": "failed", "error": outcome.message})
    return Document(
        title="cadastre sources",
        sections=(
            Section(
                "Configured plugins",
                (
                    Table(
                        ("source", "state", "plugin", "capabilities"),
                        tuple(rows),
                        empty_note=(
                            "(none configured — add sources to declared/plugins.yaml)"
                        ),
                    ),
                ),
                note=(
                    "A failing source is not an outage of Cadastre. It becomes a stale "
                    "source in every answer that depends on it."
                ),
            ),
        ),
        provenance=session.provenance(),
        data={"sources": data},
    )
