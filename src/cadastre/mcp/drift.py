"""MCP drift tool kept separate so the stdio adapter remains a thin shim."""

from __future__ import annotations


def drift(
    category: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    entity_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    summary_only: bool = False,
) -> str:
    """Report declared and observed divergence without resolving conflicts or
    selecting an authority."""
    # Import at call time to avoid a module cycle: server registers this tool.
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(endpoint, "/drift", token=token),
        lambda: server._queries().drift(
            category=category,
            kind=kind,
            source=source,
            entity_id=entity_id,
            limit=limit,
            cursor=cursor,
            summary_only=summary_only,
        ),
    )
