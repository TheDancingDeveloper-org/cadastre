"""Optional official-SDK subprocess acceptance for the stdio MCP adapter."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp", reason="install the optional mcp extra")
import httpx2  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.types import TextContent  # noqa: E402

from cadastre.adapters.security import (  # noqa: E402
    MCP_SCOPE,
    WRITE_SCOPE,
    credential,
)
from cadastre.core.storage import import_legacy, initialize  # noqa: E402
from cadastre.mcp.streamable import MCPHTTPServer  # noqa: E402
from tests.conftest import console_script  # noqa: E402

_TOPOLOGY_RECORD = {
    "id": "sdk-write-coverage",
    "repo": "notes-api-repo",
    "path_pattern": "deploy/compose.yaml",
    "pipeline": "notes-api-selfhosted",
    "produces": "registry.example.invalid/notes-api:{{ git_sha }}",
    "registry": "registry.example.invalid",
    "target_kind": "service",
    "target": "notes-api",
    "node": "app-01",
    "artifact": "compose",
    "exposure": "internal",
}


def test_streamable_http_official_sdk_round_trip(tmp_path: Path) -> None:
    """Exercise the server directly with the official Streamable HTTP SDK."""
    root = tmp_path / "runtime"
    source = Path(__file__).parents[1] / "examples" / "catalog"
    shutil.copytree(source, root)
    initialize(root)
    import_legacy(root, root)
    server = MCPHTTPServer(("127.0.0.1", 0), root, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        async def run() -> None:
            async with (
                streamable_http_client(
                    f"http://127.0.0.1:{server.server_port}/mcp"
                ) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                initialized = await session.initialize()
                tools = await session.list_tools()
                answer = await session.call_tool("brief", {})

                assert initialized.server_info.name == "cadastre"
                assert {tool.name for tool in tools.tools} == {
                    "brief",
                    "check",
                    "context_for",
                    "drift",
                    "lookup",
                    "observations",
                    "version",
                    "question",
                }
                assert answer.is_error is False
                assert len(answer.content) == 1
                assert isinstance(answer.content[0], TextContent)
                assert '"provenance"' in answer.content[0].text

        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_manifest_streamable_http_official_sdk_round_trip(tmp_path: Path) -> None:
    """The Manifest tools must be real, callable MCP tools too, not just
    listed — call_tool exercises the actual dispatch path, which
    tools/list alone does not."""
    root = tmp_path / "runtime"
    (root / "declared" / "work-items").mkdir(parents=True)
    (root / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (root / "declared" / "work-items" / "a.yaml").write_text(
        "id: a\ntitle: Ship it\nstate: open\npriority: p1\n"
        "created_at: '2026-08-01T00:00:00Z'\n",
        encoding="utf-8",
    )
    initialize(root)
    import_legacy(root, root)
    server = MCPHTTPServer(("127.0.0.1", 0), root, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        async def run() -> None:
            async with (
                streamable_http_client(
                    f"http://127.0.0.1:{server.server_port}/mcp"
                ) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "manifest_brief" in names
                assert "manifest_why" in names

                brief = await session.call_tool("manifest_brief", {})
                assert brief.is_error is False
                assert isinstance(brief.content[0], TextContent)
                assert '"work_items": 1' in brief.content[0].text

                why = await session.call_tool("manifest_why", {"entity_id": "a"})
                assert why.is_error is False
                assert isinstance(why.content[0], TextContent)
                assert '"contributions"' in why.content[0].text

        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_streamable_http_write_tools_gated_by_allow_write_and_scope(
    tmp_path: Path,
) -> None:
    """§1/§4: `--allow-write` and a `catalog.write`-scoped principal are both
    required for a write tool to be listed or callable; a real MCP client
    exercises the actual dispatch path, not just the tool inventory."""
    root = tmp_path / "runtime"
    source = Path(__file__).parents[1] / "examples" / "catalog"
    shutil.copytree(source, root)
    initialize(root)
    import_legacy(root, root)

    async def list_tool_names(server: MCPHTTPServer, token: str) -> set[str]:
        client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with (
            streamable_http_client(
                f"http://127.0.0.1:{server.server_port}/mcp",
                http_client=client,
            ) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}

    # Read-only server: write tools are never listed, regardless of scope.
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        names = asyncio.run(list_tool_names(server, "mcp"))
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert "add" not in names

    # Write-enabled server, read-only token: still hidden.
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        names = asyncio.run(list_tool_names(server, "mcp"))
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert "add" not in names

    # Write-enabled server, catalog.write token: listed and callable.
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        async def run() -> None:
            client = httpx2.AsyncClient(headers={"Authorization": "Bearer mcp"})
            async with (
                streamable_http_client(
                    f"http://127.0.0.1:{server.server_port}/mcp",
                    http_client=client,
                ) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {
                    "add",
                    "update",
                    "annotate",
                    "accept",
                    "leave_contested",
                    "acknowledge",
                } <= names
                assert "delete" not in names

                answer = await session.call_tool(
                    "add",
                    {
                        "kind": "deployment_topology",
                        "record": _TOPOLOGY_RECORD,
                        "reason": "SDK write coverage",
                    },
                )
                assert answer.is_error is False
                assert isinstance(answer.content[0], TextContent)
                assert '"principal": "agent"' in answer.content[0].text

        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_stdio_write_tools_gated_by_allow_write_env(tmp_path: Path) -> None:
    """§4.2c: write tools register on the stdio adapter only with
    `CADASTRE_MCP_ALLOW_WRITE` set — local trust context, matching the CLI's
    `--principal` default (`agent`) since no bearer token authenticates a
    local stdio caller."""
    root = tmp_path / "runtime"
    source = Path(__file__).parents[1] / "examples" / "catalog"
    shutil.copytree(source, root)
    initialize(root)
    import_legacy(root, root)
    command = console_script("cadastre-mcp")
    if command is None:
        pytest.skip("cadastre-mcp is not installed in the MCP test environment")

    async def list_tool_names(env: dict[str, str]) -> set[str]:
        parameters = StdioServerParameters(command=command, args=[], env=env)
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}

    default_env = {**os.environ, "CADASTRE_CATALOG": str(root)}
    default_env.pop("CADASTRE_MCP_ALLOW_WRITE", None)
    names = asyncio.run(list_tool_names(default_env))
    assert "add" not in names

    write_env = {**default_env, "CADASTRE_MCP_ALLOW_WRITE": "1"}
    names = asyncio.run(list_tool_names(write_env))
    assert {
        "add",
        "update",
        "annotate",
        "accept",
        "leave_contested",
        "acknowledge",
    } <= (names)
    assert "delete" not in names

    async def add() -> None:
        parameters = StdioServerParameters(command=command, args=[], env=write_env)
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            answer = await session.call_tool(
                "add",
                {
                    "kind": "deployment_topology",
                    "record": {**_TOPOLOGY_RECORD, "id": "stdio-write-coverage"},
                },
            )
            assert answer.is_error is False
            assert isinstance(answer.content[0], TextContent)
            assert '"principal": "agent"' in answer.content[0].text

    asyncio.run(add())


def test_stdio_mcp_official_sdk_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    source = Path(__file__).parents[1] / "examples" / "catalog"
    shutil.copytree(source, root)
    initialize(root)
    import_legacy(root, root)
    command = console_script("cadastre-mcp")
    if command is None:
        pytest.skip("cadastre-mcp is not installed in the MCP test environment")

    async def run() -> None:
        parameters = StdioServerParameters(
            command=command,
            args=[],
            env={**os.environ, "CADASTRE_CATALOG": str(root)},
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            answer = await session.call_tool(
                "question", {"question_id": "Q-H03", "subject": "app-01"}
            )

            assert initialized.server_info.name == "cadastre"
            assert {tool.name for tool in tools.tools} == {
                "brief",
                "check",
                "context_for",
                "drift",
                "lookup",
                "observations",
                "version",
                "question",
            }
            assert answer.is_error is False
            assert len(answer.content) == 1
            assert isinstance(answer.content[0], TextContent)
            assert '"provenance"' in answer.content[0].text

            failure = await session.call_tool("lookup", {"entity_id": "absent"})
            assert failure.is_error is True
            assert failure.structured_content == {
                "error": {
                    "kind": "missing_entity",
                    "message": failure.structured_content["error"]["message"],
                }
            }

    asyncio.run(run())


def test_remote_bridge_official_sdk_round_trip(tmp_path: Path) -> None:
    """Exercise the shipped stdio bridge against the standard HTTP transport."""
    root = tmp_path / "runtime"
    source = Path(__file__).parents[1] / "examples" / "catalog"
    shutil.copytree(source, root)
    initialize(root)
    import_legacy(root, root)
    command = console_script("cadastre-mcp-remote")
    if command is None:
        pytest.skip("cadastre-mcp-remote is not installed in the MCP test environment")
    server = MCPHTTPServer(("127.0.0.1", 0), root, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        async def run() -> None:
            environment = dict(os.environ)
            for name in (
                "CADASTRE_CATALOG",
                "CADASTRE_HTTP_URL",
                "CADASTRE_HTTP_TOKEN_FILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "CADASTRE_MCP_URL": (f"http://127.0.0.1:{server.server_port}/mcp"),
                    "CADASTRE_REMOTE_ONLY": "1",
                }
            )
            parameters = StdioServerParameters(
                command=command, args=[], env=environment
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                answer = await session.call_tool("brief", {})
                assert {tool.name for tool in tools.tools} == {
                    "brief",
                    "check",
                    "context_for",
                    "drift",
                    "lookup",
                    "observations",
                    "version",
                    "question",
                }
                assert answer.is_error is False
                assert isinstance(answer.content[0], TextContent)
                assert '"provenance"' in answer.content[0].text

        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_remote_bridge_discovers_manifest_tools_from_the_server(
    tmp_path: Path,
) -> None:
    """The remote bridge has no local catalog to read modules.yaml from, so
    it must ask the remote server which tools it actually serves — and must
    not register a manifest_* tool the server doesn't have."""
    root = tmp_path / "runtime"
    (root / "declared" / "work-items").mkdir(parents=True)
    (root / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (root / "declared" / "work-items" / "a.yaml").write_text(
        "id: a\ntitle: Ship it\nstate: open\npriority: p1\n"
        "created_at: '2026-08-01T00:00:00Z'\n",
        encoding="utf-8",
    )
    initialize(root)
    import_legacy(root, root)
    command = console_script("cadastre-mcp-remote")
    if command is None:
        pytest.skip("cadastre-mcp-remote is not installed in the MCP test environment")
    server = MCPHTTPServer(("127.0.0.1", 0), root, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        async def run() -> None:
            environment = dict(os.environ)
            for name in (
                "CADASTRE_CATALOG",
                "CADASTRE_HTTP_URL",
                "CADASTRE_HTTP_TOKEN_FILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "CADASTRE_MCP_URL": (f"http://127.0.0.1:{server.server_port}/mcp"),
                    "CADASTRE_REMOTE_ONLY": "1",
                }
            )
            parameters = StdioServerParameters(
                command=command, args=[], env=environment
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "manifest_brief" in names
                assert "manifest_why" in names

                answer = await session.call_tool("manifest_brief", {})
                assert answer.is_error is False
                assert isinstance(answer.content[0], TextContent)
                assert '"work_items": 1' in answer.content[0].text

        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_remote_bridge_exposes_write_tools_iff_the_remote_lists_them(
    tmp_path: Path,
) -> None:
    """L4C, §1: the bridge builds its tool list from the remote server's
    `tools/list` — it must not offer a write tool the remote won't actually
    serve, and must offer the full write set once the remote does. A write
    must always be attributable, so the remote needs a real
    `catalog.write`-scoped token even with `--allow-write` set."""
    root = tmp_path / "runtime"
    source = Path(__file__).parents[1] / "examples" / "catalog"
    shutil.copytree(source, root)
    initialize(root)
    import_legacy(root, root)
    command = console_script("cadastre-mcp-remote")
    if command is None:
        pytest.skip("cadastre-mcp-remote is not installed in the MCP test environment")
    token_file = tmp_path / "token"
    token_file.write_text("writer-token", encoding="utf-8")

    async def list_tool_names(port: int, *, with_token: bool) -> set[str]:
        environment = dict(os.environ)
        for name in (
            "CADASTRE_CATALOG",
            "CADASTRE_HTTP_URL",
            "CADASTRE_HTTP_TOKEN_FILE",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "CADASTRE_MCP_URL": f"http://127.0.0.1:{port}/mcp",
                "CADASTRE_REMOTE_ONLY": "1",
            }
        )
        if with_token:
            environment["CADASTRE_HTTP_TOKEN_FILE"] = str(token_file)
        parameters = StdioServerParameters(command=command, args=[], env=environment)
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}

    # Remote without --allow-write: the bridge must not offer write tools,
    # even with a catalog.write token.
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        tokens={"writer-token": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        names = asyncio.run(list_tool_names(server.server_port, with_token=True))
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert "add" not in names

    # Remote with --allow-write but no token: the bridge still can't see
    # write tools (a mutation is never attributable to an anonymous caller).
    server = MCPHTTPServer(("127.0.0.1", 0), root, allow_write=True, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        names = asyncio.run(list_tool_names(server.server_port, with_token=False))
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert "add" not in names

    # Remote with --allow-write and a catalog.write token: the bridge
    # inherits the full write set and can actually call one.
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        allow_write=True,
        tokens={"writer-token": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        names = asyncio.run(list_tool_names(server.server_port, with_token=True))
        assert {
            "add",
            "update",
            "annotate",
            "accept",
            "leave_contested",
            "acknowledge",
        } <= names
        assert "delete" not in names

        async def add() -> None:
            environment = dict(os.environ)
            for name in (
                "CADASTRE_CATALOG",
                "CADASTRE_HTTP_URL",
                "CADASTRE_HTTP_TOKEN_FILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "CADASTRE_MCP_URL": f"http://127.0.0.1:{server.server_port}/mcp",
                    "CADASTRE_REMOTE_ONLY": "1",
                    "CADASTRE_HTTP_TOKEN_FILE": str(token_file),
                }
            )
            parameters = StdioServerParameters(
                command=command, args=[], env=environment
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                answer = await session.call_tool(
                    "add",
                    {
                        "kind": "deployment_topology",
                        "record": {**_TOPOLOGY_RECORD, "id": "bridge-write-coverage"},
                    },
                )
                assert answer.is_error is False
                assert isinstance(answer.content[0], TextContent)
                assert '"database_revision"' in answer.content[0].text

        asyncio.run(add())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
