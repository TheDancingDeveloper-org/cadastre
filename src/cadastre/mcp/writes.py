"""MCP write tools, kept separate so the stdio adapter stays thin (§1).

Registered only while write mode is enabled (`server.build_server`, and the
Streamable HTTP `--allow-write` flag) — off by default (DESIGN §2.4: write
endpoints are separate explicit opt-ins). `delete` is deliberately absent —
least reversible, lowest agent need.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from cadastre.application.context import ApplicationContext
from cadastre.application.writes import WriteService

#: Env var gating registration of these tools in stdio mode (Streamable HTTP
#: has its own `--allow-write` server flag).
ALLOW_WRITE_ENV = "CADASTRE_MCP_ALLOW_WRITE"
#: Local-mode principal, defaulting like the CLI's `--principal` (`agent`).
#: Remote mode carries no such default: the bearer token on the forwarded
#: HTTP request supplies the principal server-side (§2.3), and a
#: client-chosen principal here would let any caller forge that stamp.
PRINCIPAL_ENV = "CADASTRE_MCP_PRINCIPAL"


def write_mode_enabled() -> bool:
    return os.environ.get(ALLOW_WRITE_ENV, "").lower() in {"1", "true", "yes"}


def _local_principal() -> str:
    return os.environ.get(PRINCIPAL_ENV, "agent")


def _writes() -> WriteService:
    from cadastre.mcp import server

    return WriteService(ApplicationContext.open(server._root(), runtime=False))


def add(kind: str, record: dict[str, Any], reason: str = "MCP catalog edit") -> str:
    """Declare a new catalog record. Refused for source-authoritative kinds,
    which must be collected instead."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/add",
            method="POST",
            body={"kind": kind, "record": record, "reason": reason},
            token=token,
        ),
        lambda: _writes().dispatch(
            "add",
            {"kind": kind, "record": record},
            principal=_local_principal(),
            reason=reason,
        ),
    )


def update(
    kind: str, id: str, record: dict[str, Any], reason: str = "MCP catalog edit"
) -> str:
    """Update an existing declared record's catalog-owned fields."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/update",
            method="POST",
            body={"kind": kind, "id": id, "record": record, "reason": reason},
            token=token,
        ),
        lambda: _writes().dispatch(
            "update",
            {"kind": kind, "id": id, "record": record},
            principal=_local_principal(),
            reason=reason,
        ),
    )


def annotate(
    kind: str, id: str, record: dict[str, Any], reason: str = "MCP catalog edit"
) -> str:
    """Annotate an entity's catalog-owned fields (tags, notes) regardless of
    which plugin is authoritative for the rest of it."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/annotate",
            method="POST",
            body={"kind": kind, "id": id, "record": record, "reason": reason},
            token=token,
        ),
        lambda: _writes().dispatch(
            "annotate",
            {"kind": kind, "id": id, "record": record},
            principal=_local_principal(),
            reason=reason,
        ),
    )


def accept(
    target: str,
    source: str,
    field: str | None = None,
    reason: str = "MCP catalog edit",
) -> str:
    """Accept observed evidence over declared state for one contested field."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/accept",
            method="POST",
            body={"target": target, "source": source, "field": field, "reason": reason},
            token=token,
        ),
        lambda: _writes().dispatch(
            "accept",
            {"target": target, "source": source, "field": field},
            principal=_local_principal(),
            reason=reason,
        ),
    )


def leave_contested(
    target: str,
    source: str,
    field: str | None = None,
    reason: str = "MCP catalog edit",
) -> str:
    """Record that a contest between declared and observed state remains
    unresolved."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/leave-contested",
            method="POST",
            body={"target": target, "source": source, "field": field, "reason": reason},
            token=token,
        ),
        lambda: _writes().dispatch(
            "leave_contested",
            {"target": target, "source": source, "field": field},
            principal=_local_principal(),
            reason=reason,
        ),
    )


def acknowledge(
    target: str, source: str, until: str, reason: str = "MCP catalog edit"
) -> str:
    """Defer a contest until a stated date."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/acknowledge",
            method="POST",
            body={"target": target, "source": source, "until": until, "reason": reason},
            token=token,
        ),
        lambda: _writes().dispatch(
            "acknowledge",
            {"target": target, "source": source, "until": until},
            principal=_local_principal(),
            reason=reason,
        ),
    )


WRITE_TOOLS: tuple[Callable[..., str], ...] = (
    add,
    update,
    annotate,
    accept,
    leave_contested,
    acknowledge,
)
