"""Artifact checking service shared by CLI, HTTP, and MCP."""

from __future__ import annotations

import tempfile
from pathlib import Path

from cadastre.application.context import ApplicationContext
from cadastre.cli import check
from cadastre.render.document import Document


class CheckService:
    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def artifact(
        self,
        content: str,
        *,
        kind: str | None = None,
        display_path: str | None = None,
    ) -> Document:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", encoding="utf-8", delete=True
        ) as handle:
            handle.write(content)
            handle.flush()
            return check.check(
                self.context.service_session(),
                Path(handle.name),
                kind=kind,
                display_path=display_path,
            )
