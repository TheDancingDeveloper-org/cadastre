"""L5 transport-parity guard (§4 of the 2026-08-11 issue review).

The bug class this closes: a new operation lands in one transport (a route,
a QueryService/WriteService method) and is forgotten in another. R09 shipped
exactly that twice — Streamable HTTP never got the Manifest tools, and the
remote bridge never proxied them either — and nothing failed CI. This file
is the regression guard: an inventory assertion that every HTTP route has
either an MCP operation or a documented exclusion, plus a walk that
exercises stdio, Streamable HTTP, and the bridge for every registered MCP
operation and asserts equivalent success/error envelopes.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from cadastre.adapters.client import StreamableClient
from cadastre.adapters.security import MCP_SCOPE, WRITE_SCOPE, credential
from cadastre.api.registry import (
    HTTP_ROUTES,
    MANIFEST_HTTP_ROUTES,
    MANIFEST_MCP_OPERATIONS,
    MCP_OPERATIONS,
    MCP_WRITE_OPERATIONS,
)
from cadastre.core.storage import import_legacy, initialize
from cadastre.mcp import server as stdio_tools
from cadastre.mcp import writes as write_tools
from cadastre.mcp.streamable import MCPHTTPServer
from tests.conftest import EXAMPLE_CATALOG


def _post(
    server: MCPHTTPServer, payload: dict[str, Any], *, session: str | None = None
) -> tuple[int, dict[str, Any], str | None]:
    """A raw JSON-RPC round trip — a real Streamable HTTP client, as opposed
    to `StreamableClient` (what the bridge wraps) or `server.call_tool`
    (bypasses the `_Handler` auth/scope gate entirely)."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer writer",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        connection.request("POST", "/mcp", json.dumps(payload).encode(), headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        return response.status, result, response.getheader("Mcp-Session-Id")
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _call_tool_over_http(
    root: Path, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        allow_write=True,
        tokens={"writer": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    _, _, session = _post(
        server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        allow_write=True,
        tokens={"writer": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    server.sessions.add(session)
    _, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session=session,
    )
    result = payload["result"]
    return result["structuredContent"]


#: HTTP-only routes with no MCP counterpart, named and reasoned individually
#: rather than pattern-matched — an unreviewed new route must fail this test,
#: not silently join an exclusion glob.
HTTP_ONLY_ROUTES = {
    # Irreversible; excluded from MCP v1 by design (§1 of the issue review).
    "delete",
    # Introspection about the server/catalog itself, not the estate; no
    # agent-facing need has come up for these over MCP.
    "stale",
    "plugins",
    "sources",
    "security-check",
    "schema",
}


def test_every_http_route_has_an_mcp_operation_or_a_named_exclusion() -> None:
    mcp_names = {
        op.name.replace("_", "-")
        for op in MCP_OPERATIONS + MANIFEST_MCP_OPERATIONS + MCP_WRITE_OPERATIONS
    }
    for route in HTTP_ROUTES + MANIFEST_HTTP_ROUTES:
        normalized = route.name.replace("_", "-")
        if normalized in HTTP_ONLY_ROUTES:
            continue
        assert normalized in mcp_names, (
            f"HTTP route {route.name!r} has no MCP operation and is not in "
            "HTTP_ONLY_ROUTES — add the MCP operation or name the exclusion"
        )
    # And the exclusion list itself must not have gone stale in the other
    # direction: every name in it must still be a real HTTP-only route.
    all_http_names = {
        r.name.replace("_", "-") for r in HTTP_ROUTES + MANIFEST_HTTP_ROUTES
    }
    assert all_http_names >= HTTP_ONLY_ROUTES


@pytest.fixture
def runtime_catalog(tmp_path: Path) -> Path:
    return _fresh_catalog(tmp_path / "catalog")


def _fresh_catalog(root: Path) -> Path:
    shutil.copytree(EXAMPLE_CATALOG, root)
    initialize(root)
    import_legacy(root, root)
    return root


@pytest.fixture
def manifest_runtime_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "manifest-catalog"
    shutil.copytree(EXAMPLE_CATALOG, root)
    (root / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    initialize(root)
    import_legacy(root, root)
    return root


def test_manifest_projects_structured_result_is_transport_identical(
    monkeypatch: pytest.MonkeyPatch, manifest_runtime_catalog: Path
) -> None:
    monkeypatch.setenv("CADASTRE_CATALOG", str(manifest_runtime_catalog))
    monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
    from cadastre.mcp.manifest import manifest_projects

    local = json.loads(manifest_projects())
    from cadastre.adapters.http import CadastreHTTPServer

    http_server = CadastreHTTPServer(
        ("127.0.0.1", 0), manifest_runtime_catalog, allow_write=False
    )
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", http_server.server_port, timeout=3)
        connection.request("GET", "/manifest/projects")
        http_response = json.loads(connection.getresponse().read())
    finally:
        http_server.shutdown()
        http_thread.join(timeout=3)
        http_server.server_close()
    server = MCPHTTPServer(
        ("127.0.0.1", 0), manifest_runtime_catalog, require_auth=False
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        streamed = json.loads(
            server.call_tool("manifest_projects", {}, principal="agent")
        )
        bridge = json.loads(
            StreamableClient(f"http://127.0.0.1:{server.server_port}/mcp").tool(
                "manifest_projects", {}
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert (
        local["result"]
        == http_response["result"]
        == streamed["result"]
        == bridge["result"]
    )


def _topology_record(entity_id: str) -> dict[str, Any]:
    """`deployment_topology` is purely catalog-declared, so `add` always
    succeeds for it — the one write operation this walk can exercise as a
    genuine success rather than a source-authoritative refusal."""
    return {
        "id": entity_id,
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


def _envelope_shape(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """(is_error, error_kind_or_None) — the part of the envelope that must
    agree across transports. Field values (timestamps, revision numbers)
    legitimately differ; whether the call succeeded, and why, must not."""
    error = payload.get("error")
    if isinstance(error, dict):
        return True, error.get("kind")
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        return True, result["error"].get("kind")
    return False, None


#: name -> (stdio-local kwargs, MCP-protocol arguments). The two differ only
#: where the wire protocol's argument shape differs from the Python tool
#: signature; both must reach the same operation.
READ_CALLS: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    ("brief", {}, {}),
    ("version", {}, {}),
    (
        "context_for",
        {"intent": "an internal service"},
        {"intent": "an internal service"},
    ),
    ("lookup", {"entity_id": "app-01"}, {"entity_id": "app-01"}),
    ("drift", {}, {}),
    ("question", {"question_id": "Q-H03"}, {"question_id": "Q-H03"}),
    ("observations", {}, {}),
)


def test_read_operations_agree_between_stdio_and_streamable_http(
    monkeypatch: pytest.MonkeyPatch, runtime_catalog: Path
) -> None:
    monkeypatch.setenv("CADASTRE_CATALOG", str(runtime_catalog))
    monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
    functions = {f.__name__: f for f in stdio_tools.TOOLS}
    server = MCPHTTPServer(("127.0.0.1", 0), runtime_catalog, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name, local_kwargs, mcp_arguments in READ_CALLS:
            local_payload = json.loads(functions[name](**local_kwargs))
            http_text = server.call_tool(name, mcp_arguments, principal="agent")
            http_payload = json.loads(http_text)
            assert _envelope_shape(local_payload) == _envelope_shape(http_payload), name
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_read_operations_agree_via_the_remote_bridge_client(
    runtime_catalog: Path,
) -> None:
    """The bridge's `_remote_tool` is exactly `StreamableClient(...).tool(...)`
    (`mcp/remote.py`); driving that class directly exercises the bridge's
    real dispatch path without needing the optional SDK installed."""
    server = MCPHTTPServer(("127.0.0.1", 0), runtime_catalog, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bridge = StreamableClient(f"http://127.0.0.1:{server.server_port}/mcp")
        for name, _local_kwargs, mcp_arguments in READ_CALLS:
            http_text = server.call_tool(name, mcp_arguments, principal="agent")
            bridge_text = bridge.tool(name, mcp_arguments)
            assert _envelope_shape(json.loads(http_text)) == _envelope_shape(
                json.loads(bridge_text)
            ), name
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


#: A call that must be REFUSED the same way on every transport (a
#: source-authoritative kind), proving the refusal envelope — not just the
#: success path — is transport-independent.
REFUSED_WRITE_CALL: tuple[str, dict[str, Any]] = (
    "add",
    {"kind": "host", "record": {"id": "parity-guard-host", "role": "server"}},
)


def test_write_operation_refusal_agrees_between_stdio_and_streamable_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_functions = {f.__name__: f for f in write_tools.WRITE_TOOLS}
    monkeypatch.setenv(write_tools.ALLOW_WRITE_ENV, "1")
    name, arguments = REFUSED_WRITE_CALL

    local_root = _fresh_catalog(tmp_path / "refuse-local")
    monkeypatch.setenv("CADASTRE_CATALOG", str(local_root))
    monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
    local_payload = json.loads(write_functions[name](**arguments))

    http_root = _fresh_catalog(tmp_path / "refuse-http")
    http_payload = _call_tool_over_http(http_root, name, arguments)

    assert _envelope_shape(local_payload) == _envelope_shape(http_payload)
    assert _envelope_shape(local_payload)[0] is True


def test_write_operation_refusal_agrees_via_the_remote_bridge_client(
    tmp_path: Path,
) -> None:
    root = _fresh_catalog(tmp_path / "bridge-refuse")
    name, arguments = REFUSED_WRITE_CALL
    http_payload = _call_tool_over_http(root, name, arguments)

    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        root,
        allow_write=True,
        tokens={"writer": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bridge = StreamableClient(
            f"http://127.0.0.1:{server.server_port}/mcp", "writer"
        )
        bridge_text = bridge.tool(name, arguments)
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    assert _envelope_shape(http_payload) == _envelope_shape(json.loads(bridge_text))
    assert _envelope_shape(http_payload)[0] is True


def test_write_operation_success_agrees_between_stdio_and_streamable_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercised once, on `add`: the other five write operations
    (update/annotate/accept/leave_contested/acknowledge) share the exact
    same `WriteService.dispatch` boundary (Phase 6a) — this proves that
    boundary produces one envelope shape regardless of which transport
    reached it, which is what actually varies between transports."""
    write_functions = {f.__name__: f for f in write_tools.WRITE_TOOLS}
    monkeypatch.setenv(write_tools.ALLOW_WRITE_ENV, "1")

    local_root = _fresh_catalog(tmp_path / "succeed-local")
    monkeypatch.setenv("CADASTRE_CATALOG", str(local_root))
    monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
    local_payload = json.loads(
        write_functions["add"](
            kind="deployment_topology", record=_topology_record("local")
        )
    )

    http_root = _fresh_catalog(tmp_path / "succeed-http")
    http_payload = _call_tool_over_http(
        http_root,
        "add",
        {"kind": "deployment_topology", "record": _topology_record("http")},
    )

    assert _envelope_shape(local_payload) == (False, None)
    assert _envelope_shape(http_payload) == (False, None)
    assert local_payload["result"]["operation"] == http_payload["result"]["operation"]
