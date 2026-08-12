"""Catalog write use cases shared by CLI and network adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cadastre.application.context import ApplicationContext
from cadastre.cli import trust
from cadastre.cli.write import _document
from cadastre.core import writes
from cadastre.render.document import Document


@dataclass(frozen=True)
class WriteService:
    context: ApplicationContext

    def catalog(
        self,
        operation: str,
        kind: str,
        *,
        ident: str | None = None,
        record: dict[str, Any] | None = None,
        principal: str,
        reason: str,
    ) -> Document:
        session = self.context.service_session()
        result = writes.write(
            session.root,
            operation,
            kind,
            ident,
            record or {},
            principal=principal,
            reason=reason,
            now=session.now,
        )
        return _document(session, result, reason)

    def resolve(
        self,
        action: str,
        target: str,
        *,
        source: str,
        field: str | None,
        principal: str,
        reason: str,
    ) -> Document:
        return trust.resolve(
            self.context.service_session(),
            action,
            target,
            source=source,
            field=field,
            principal=principal,
            reason=reason,
        )

    def acknowledge(
        self,
        target: str,
        *,
        source: str,
        until: str,
        principal: str,
        reason: str,
    ) -> Document:
        return trust.acknowledge(
            self.context.service_session(),
            target,
            source=source,
            until=until,
            principal=principal,
            reason=reason,
        )

    def dispatch(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        principal: str,
        reason: str,
    ) -> Document:
        """Dispatch a registry write operation with one canonical boundary.

        Mirrors `QueryService.dispatch()`: every transport (HTTP, stdio MCP,
        Streamable HTTP MCP) routes a mutation through this one function, so
        there is exactly one place that decides what `add`/`update`/... mean.
        `principal` is a keyword-only parameter supplied by the caller's
        authentication, never read out of `arguments` — a `principal` key in
        `arguments` would let any `mcp`-scoped caller forge the §2.3
        provenance stamp.
        """
        values = arguments or {}
        if name in {"add", "update", "annotate"}:
            return self.catalog(
                name,
                str(values["kind"]),
                ident=str(values["id"]) if name != "add" else None,
                record=dict(values["record"]),
                principal=principal,
                reason=reason,
            )
        if name in {"accept", "leave_contested"}:
            return self.resolve(
                "accept-observed" if name == "accept" else "leave-contested",
                str(values["target"]),
                source=str(values["source"]),
                field=values.get("field"),
                principal=principal,
                reason=reason,
            )
        if name == "acknowledge":
            return self.acknowledge(
                str(values["target"]),
                source=str(values["source"]),
                until=str(values["until"]),
                principal=principal,
                reason=reason,
            )
        from cadastre.core.errors import UsageError

        raise UsageError(f"unknown write operation `{name}`")
