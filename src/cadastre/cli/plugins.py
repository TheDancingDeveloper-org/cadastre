"""`cadastre plugins` — registered plugins and their active state."""

from __future__ import annotations

from typing import Any

from cadastre.cli.session import Session
from cadastre.plugins import PluginRegistry
from cadastre.render.document import Document, Section, Table


def plugins(session: Session) -> Document:
    configured = {source.plugin for source in session.plugins.sources}
    registry = PluginRegistry.discover(session.root)
    rows: list[tuple[str, str, str, str]] = []
    data: list[dict[str, Any]] = []
    for registered in registry.plugins:
        active = registered.name in configured
        state = "active" if active else "registered-inactive"
        kinds = ", ".join(item.kind for item in registered.info.entities)
        rows.append((registered.name, state, registered.info.version, kinds))
        data.append(
            {
                "name": registered.name,
                "state": state,
                "version": registered.info.version,
                "capabilities": list(registered.info.capabilities),
                "entities": [item.kind for item in registered.info.entities],
                "origin": registered.origin,
            }
        )
    return Document(
        title="cadastre plugins",
        sections=(
            Section(
                "Registered plugins",
                (Table(("plugin", "state", "version", "entities"), tuple(rows)),),
                note=(
                    "Unconfigured plugins are registered-inactive, not errors. "
                    "Configure a source in plugins.yaml to activate one."
                ),
            ),
        ),
        provenance=session.provenance(),
        data={"plugins": data},
    )
