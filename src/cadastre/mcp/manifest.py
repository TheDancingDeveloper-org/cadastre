"""Manifest MCP wrappers over the canonical query service."""

from __future__ import annotations

from collections.abc import Callable


def manifest_brief() -> str:
    """Summarize the enabled Manifest work register."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/manifest/brief", token=token
        ),
        lambda: server._queries().manifest_brief(),
    )


def manifest_projects() -> str:
    """Show one deterministic row per declared repository and checkout state."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/manifest/projects", token=token
        ),
        lambda: server._queries().manifest_projects(),
    )


def manifest_backlog(
    state: str | None = None,
    initiative: str | None = None,
    repo: str | None = None,
    limit: int = 10,
) -> str:
    """List ranked Manifest work with optional bounded filters."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/manifest/backlog",
            query={
                "state": state,
                "initiative": initiative,
                "repo": repo,
                "limit": str(limit),
            },
            token=token,
        ),
        lambda: server._queries().manifest_backlog(
            state=state, initiative=initiative, repo=repo, limit=limit
        ),
    )


def manifest_next(limit: int = 10) -> str:
    """List the top eligible, unblocked Manifest work items."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/manifest/next", query={"limit": str(limit)}, token=token
        ),
        lambda: server._queries().manifest_next(limit=limit),
    )


def manifest_drift(repo: str | None = None) -> str:
    """Report the closed set of Manifest drift categories, optionally by repo."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/manifest/drift", query={"repo": repo}, token=token
        ),
        lambda: server._queries().manifest_drift(repo=repo),
    )


def manifest_repo(repo: str) -> str:
    """Show Manifest work, checkouts, and drift for one repository."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, f"/manifest/repo/{repo}", token=token
        ),
        lambda: server._queries().manifest_repo(repo),
    )


def manifest_why(entity_id: str) -> str:
    """Explain every deterministic contribution to one Manifest score."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, f"/manifest/why/{entity_id}", token=token
        ),
        lambda: server._queries().manifest_why(entity_id),
    )


Tool = Callable[..., str]


def enabled_tools(root: str) -> tuple[Tool, ...]:
    from pathlib import Path

    from cadastre.modules.config import load_modules

    if not load_modules(Path(root)).enabled("manifest"):
        return ()
    return (
        manifest_brief,
        manifest_projects,
        manifest_backlog,
        manifest_next,
        manifest_drift,
        manifest_repo,
        manifest_why,
    )
