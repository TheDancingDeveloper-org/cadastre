"""MCP observations tool, kept beside `drift` so the stdio adapter stays thin."""

from __future__ import annotations


def observations(
    source: str | None = None,
    method: str | None = None,
    key: str | None = None,
    limit: int | None = None,
    summary_only: bool = False,
) -> str:
    """List retained collector evidence that has no entity form, bounded and
    with provenance. Returns plugin data framed as untrusted; never policy."""
    # Import at call time to avoid a module cycle: server registers this tool.
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/observations",
            query={
                "source": source,
                "method": method,
                "key": key,
                "limit": str(limit) if limit is not None else None,
                "summary_only": "true" if summary_only else None,
            },
            token=token,
        ),
        lambda: server._queries().observations(
            source=source,
            method=method,
            key=key,
            limit=limit,
            summary_only=summary_only,
        ),
    )
