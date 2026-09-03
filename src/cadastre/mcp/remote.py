"""Remote-only stdio MCP bridge for clients without native HTTP support."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from cadastre import __version__
from cadastre.adapters import client
from cadastre.core.errors import CadastreError

MCP_URL_ENV = "CADASTRE_MCP_URL"


def _endpoint() -> str:
    endpoint = os.environ.get(MCP_URL_ENV, "").strip()
    if not endpoint:
        raise CadastreError(f"{MCP_URL_ENV} is required")
    return client.mcp_endpoint(endpoint, remote_only=True)


def _token() -> str | None:
    return client.token_from_file()


def _remote_tool(name: str, arguments: dict[str, Any]) -> str:
    bridge = client.StreamableClient(_endpoint(), _token())
    return bridge.tool(name, arguments)


def _release(value: str) -> tuple[int, ...] | None:
    parts = value.strip().split(".")
    if not all(part.isdigit() for part in parts) or not parts:
        return None
    return tuple(int(part) for part in parts)


def warn_if_below_minimum_client() -> None:
    """Tell the operator once, on stderr, that this bridge is behind its server.

    MCP gives a server no way to push an update notice, so the bridge asks. Four
    rules hold this together, and each is a way it otherwise goes wrong:

    stdout is the MCP framing channel, so the notice goes to stderr or the
    session is corrupted. Version skew never fails startup — a bridge that
    refuses to run on a cosmetic bump is worse than a stale one. A server
    predating the `version` tool answers with an error envelope, which is not a
    problem to report. And any failure of the probe itself — network, auth,
    parse — is swallowed: the bridge's job is to proxy, and it must proxy even
    when it cannot introspect.
    """
    try:
        payload = json.loads(_remote_tool("version", {}))
        if not isinstance(payload, dict) or "error" in payload:
            return
        minimum = payload.get("minimum_client_version")
        if not isinstance(minimum, str):
            return
        required = _release(minimum)
        running = _release(__version__)
        if required is None or running is None or running >= required:
            return
        print(
            f"cadastre-mcp-remote {__version__} is older than this server's "
            f"minimum supported client {minimum}. "
            "Upgrade with: uv tool upgrade cadastre",
            file=sys.stderr,
        )
    except Exception:
        # Introspection must never become a second way startup can fail.
        return


def build_server() -> Any:
    """Build the normal stdio MCP server with every tool forced remote."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer
        except ImportError as exc:
            raise CadastreError(
                "the remote MCP bridge needs the optional dependency: "
                "`uv tool install 'cadastre[mcp-client]'`"
            ) from exc
        server = MCPServer("cadastre", version=__version__)
    else:
        server = FastMCP("cadastre", version=__version__)
    from cadastre.mcp import server as tools

    def brief() -> str:
        return _remote_tool("brief", {})

    def version() -> str:
        return _remote_tool("version", {})

    def context_for(intent: str) -> str:
        return _remote_tool("context_for", {"intent": intent})

    def check(artifact: str, kind: str | None = None) -> str:
        try:
            content = Path(artifact).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise CadastreError(
                f"cannot read check artifact {artifact!r}: {exc}"
            ) from exc
        return _remote_tool(
            "check",
            {"artifact": content, "kind": kind, "path": Path(artifact).name},
        )

    def lookup(entity_id: str, kind: str | None = None) -> str:
        return _remote_tool("lookup", {"entity_id": entity_id, "kind": kind})

    def drift(
        category: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        summary_only: bool = False,
    ) -> str:
        return _remote_tool(
            "drift",
            {
                "category": category,
                "kind": kind,
                "source": source,
                "entity_id": entity_id,
                "limit": limit,
                "cursor": cursor,
                "summary_only": summary_only,
            },
        )

    def question(
        question_id: str, subject: str | None = None, value: str | None = None
    ) -> str:
        return _remote_tool(
            "question",
            {"question_id": question_id, "subject": subject, "value": value},
        )

    def observations(
        source: str | None = None,
        method: str | None = None,
        key: str | None = None,
        limit: int | None = None,
        summary_only: bool = False,
    ) -> str:
        return _remote_tool(
            "observations",
            {
                "source": source,
                "method": method,
                "key": key,
                "limit": limit,
                "summary_only": summary_only,
            },
        )

    def manifest_brief() -> str:
        return _remote_tool("manifest_brief", {})

    def manifest_backlog(
        state: str | None = None,
        initiative: str | None = None,
        repo: str | None = None,
        limit: int = 10,
    ) -> str:
        return _remote_tool(
            "manifest_backlog",
            {"state": state, "initiative": initiative, "repo": repo, "limit": limit},
        )

    def manifest_next(limit: int = 10) -> str:
        return _remote_tool("manifest_next", {"limit": limit})

    def manifest_drift(repo: str | None = None) -> str:
        return _remote_tool("manifest_drift", {"repo": repo})

    def manifest_repo(repo: str) -> str:
        return _remote_tool("manifest_repo", {"repo": repo})

    def manifest_why(entity_id: str) -> str:
        return _remote_tool("manifest_why", {"entity_id": entity_id})

    def add(kind: str, record: dict[str, Any], reason: str = "MCP catalog edit") -> str:
        return _remote_tool("add", {"kind": kind, "record": record, "reason": reason})

    def update(
        kind: str, id: str, record: dict[str, Any], reason: str = "MCP catalog edit"
    ) -> str:
        return _remote_tool(
            "update", {"kind": kind, "id": id, "record": record, "reason": reason}
        )

    def annotate(
        kind: str, id: str, record: dict[str, Any], reason: str = "MCP catalog edit"
    ) -> str:
        return _remote_tool(
            "annotate", {"kind": kind, "id": id, "record": record, "reason": reason}
        )

    def accept(
        target: str,
        source: str,
        field: str | None = None,
        reason: str = "MCP catalog edit",
    ) -> str:
        return _remote_tool(
            "accept",
            {"target": target, "source": source, "field": field, "reason": reason},
        )

    def leave_contested(
        target: str,
        source: str,
        field: str | None = None,
        reason: str = "MCP catalog edit",
    ) -> str:
        return _remote_tool(
            "leave_contested",
            {"target": target, "source": source, "field": field, "reason": reason},
        )

    def acknowledge(
        target: str, source: str, until: str, reason: str = "MCP catalog edit"
    ) -> str:
        return _remote_tool(
            "acknowledge",
            {"target": target, "source": source, "until": until, "reason": reason},
        )

    base_functions = (
        brief,
        version,
        context_for,
        check,
        lookup,
        drift,
        question,
        observations,
    )
    manifest_functions = (
        manifest_brief,
        manifest_backlog,
        manifest_next,
        manifest_drift,
        manifest_repo,
        manifest_why,
    )
    write_functions = (add, update, annotate, accept, leave_contested, acknowledge)
    for function in base_functions:
        original = next(
            item for item in tools.TOOLS if item.__name__ == function.__name__
        )
        function.__doc__ = original.__doc__
        server.add_tool(function)
    # Registering a manifest_* or write tool the remote server doesn't
    # actually serve would look like activation and fail on every call,
    # which is worse than not offering it — so this bridge asks the remote
    # server what it has, the same way the version-skew check above never
    # blocks startup on a failed network call. A write tool is absent from
    # `tools/list` whenever the remote was started without `--allow-write`
    # or this bridge's token lacks `catalog.write` (§1/§4), so the bridge
    # inherits exactly what the remote principal is scoped for.
    try:
        from cadastre.mcp import manifest as manifest_tools
        from cadastre.mcp import writes as write_tools

        remote_names = {
            item.get("name")
            for item in client.StreamableClient(_endpoint(), _token()).list_tools()
        }
        originals: dict[str, Any] = {
            item.__name__: item
            for item in (
                manifest_tools.manifest_brief,
                manifest_tools.manifest_backlog,
                manifest_tools.manifest_next,
                manifest_tools.manifest_drift,
                manifest_tools.manifest_repo,
                manifest_tools.manifest_why,
            )
            + write_tools.WRITE_TOOLS
        }
        for conditional_function in manifest_functions + write_functions:
            if conditional_function.__name__ in remote_names:
                conditional_function.__doc__ = originals[
                    conditional_function.__name__
                ].__doc__
                server.add_tool(conditional_function)
    except CadastreError:
        pass
    warn_if_below_minimum_client()
    return server


def main() -> int:  # pragma: no cover - runs a server loop
    try:
        _endpoint()
        build_server().run()
    except (CadastreError, ImportError) as exc:
        print(f"cadastre-mcp-remote: {exc}", file=sys.stderr)
        return 2
    return 0
