"""Black-box checks for standard MCP Streamable HTTP."""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from cadastre.adapters.security import MCP_SCOPE, WRITE_SCOPE, credential
from cadastre.core.storage import CatalogStore
from cadastre.mcp.streamable import MCPHTTPServer


def _post(
    server: MCPHTTPServer,
    payload: dict[str, Any],
    *,
    session: str | None = None,
) -> tuple[int, dict[str, Any], str | None]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer mcp",
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


def test_streamable_initialize_list_and_call(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    status, payload, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert status == 200
    assert payload["result"]["serverInfo"]["name"] == "cadastre"
    assert session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        session=session,
    )
    assert status == 200
    assert {tool["name"] for tool in payload["result"]["tools"]} == {
        "brief",
        "context_for",
        "check",
        "lookup",
        "drift",
        "question",
        "observations",
        "version",
    }


def test_streamable_rejects_missing_scope_and_wrong_origin(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"read": credential("agent", scopes={"catalog.read"})},
        allowed_origins=("https://expected.invalid",),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/mcp",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer read",
                "Origin": "https://wrong.invalid",
            },
        )
        response = connection.getresponse()
        assert response.status == 403
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_streamable_rejects_unsupported_protocol_version(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    status, payload, session = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1900-01-01"},
        },
    )
    assert status == 200
    assert payload["error"]["code"] == -32602
    assert session is None


def test_streamable_delete_invalidates_session(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    _, _, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    server.session_seen[session] = time.monotonic()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "DELETE",
            "/mcp",
            headers={
                "Authorization": "Bearer mcp",
                "Mcp-Session-Id": session,
            },
        )
        assert connection.getresponse().status == 204
        connection.close()
        assert session not in server.sessions
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_streamable_expires_idle_sessions(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
        session_ttl_seconds=0,
    )
    _, _, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
        session_ttl_seconds=0,
    )
    server.sessions.add(session)
    server.session_seen[session] = time.monotonic() - 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/mcp",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(),
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer mcp",
                "Mcp-Session-Id": session,
            },
        )
        response = connection.getresponse()
        status = response.status
        payload = json.loads(response.read())
        connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert status == 400
    assert payload["error"]["code"] == -32602
    assert session not in server.sessions


def test_streamable_check_preserves_requested_display_path(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    content = "services: {}\n"
    status, payload, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert status == 200 and session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "check",
                "arguments": {
                    "artifact": content,
                    "kind": "compose",
                    "path": "proposal.yaml",
                },
            },
        },
        session=session,
    )
    assert status == 200
    artifact = payload["result"]["structuredContent"]["result"]["artifact"]
    assert artifact["path"] == "proposal.yaml"


def test_streamable_marks_tool_validation_errors(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    _, _, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "question", "arguments": {"question_id": "Q-X99"}},
        },
        session=session,
    )
    assert status == 200
    assert payload["result"]["isError"] is True
    assert payload["result"]["structuredContent"]["error"]["kind"] == (
        "invalid_argument"
    )


def test_streamable_tool_errors_use_one_structured_contract(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    _, _, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert session
    cases = (
        ("lookup", {"entity_id": "absent"}, "missing_entity"),
        ("lookup", {"entity_id": "app-01", "kind": "not-a-kind"}, "unknown_kind"),
        ("check", {"artifact": "[not valid"}, "invalid_argument"),
        ("question", {"question_id": "Q-X99"}, "invalid_argument"),
        (
            "question",
            {"question_id": "Q-P02", "value": "not-a-number"},
            "invalid_value",
        ),
    )
    for name, arguments, error_kind in cases:
        server = MCPHTTPServer(
            ("127.0.0.1", 0),
            catalog_copy,
            tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
        )
        server.sessions.add(session)
        status, payload, _ = _post(
            server,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session=session,
        )
        assert status == 200
        result = payload["result"]
        assert result["isError"] is True
        assert set(result["structuredContent"]) == {"error"}
        assert result["structuredContent"]["error"]["kind"] == error_kind
        assert isinstance(result["structuredContent"]["error"]["message"], str)


def test_streamable_drift_filters_pages_and_compacts_text(catalog_copy: Path) -> None:
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "static.json").write_text(
        json.dumps(
            {
                "v": 1,
                "source": "static",
                "plugin": "fixture",
                "as_of": "2026-08-07T12:00:00Z",
                "ok": True,
                "entities": {"host": [{"id": "one"}, {"id": "two"}, {"id": "three"}]},
            }
        ),
        encoding="utf-8",
    )
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    _, _, session = _post(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert session
    arguments = {"category": "undeclared", "kind": "host", "limit": 2}
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "drift", "arguments": arguments},
        },
        session=session,
    )
    assert status == 200
    result = payload["result"]
    page = result["structuredContent"]["result"]
    assert result["content"] == [{"type": "text", "text": "cadastre drift"}]
    assert page["counts"]["undeclared"] == 3
    assert len(page["divergences"]) == 2
    assert page["pagination"]["total"] == 3
    cursor = page["pagination"]["next_cursor"]
    assert cursor

    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "drift",
                "arguments": {**arguments, "cursor": cursor},
            },
        },
        session=session,
    )
    assert status == 200
    second = payload["result"]["structuredContent"]["result"]
    assert [row["id"] for row in second["divergences"]] == ["two"]
    assert second["pagination"]["next_cursor"] is None


# --------------------------------------------------------------------------
# MCP write surface (§1)
# --------------------------------------------------------------------------


def _topology_record(entity_id: str) -> dict[str, Any]:
    """`deployment_topology` is purely catalog-declared — no built-in plugin
    claims source authority for it, so `add` is never refused here."""
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


def _initialized_session(server: MCPHTTPServer) -> str:
    _, _, session = _post(
        server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert session
    return session


def test_write_tools_are_hidden_without_allow_write(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    session = _initialized_session(server)
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session=session
    )
    assert status == 200
    assert "add" not in {tool["name"] for tool in payload["result"]["tools"]}


def test_write_tool_call_refused_without_allow_write(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    session = _initialized_session(server)
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "add",
                "arguments": {
                    "kind": "deployment_topology",
                    "record": _topology_record("x"),
                },
            },
        },
        session=session,
    )
    assert status == 403
    assert "allow-write" in payload["error"]["message"]


def test_write_tools_are_hidden_without_catalog_write_scope(catalog_copy: Path) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    session = _initialized_session(server)
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session=session
    )
    assert status == 200
    assert "add" not in {tool["name"] for tool in payload["result"]["tools"]}

    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "add",
                "arguments": {
                    "kind": "deployment_topology",
                    "record": _topology_record("x"),
                },
            },
        },
        session=session,
    )
    assert status == 403
    assert "catalog.write" in payload["error"]["message"]


def test_add_lands_in_the_catalog_with_authenticated_principal_and_reason(
    catalog_copy: Path,
) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("mcp-agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    session = _initialized_session(server)
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("mcp-agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "add",
                "arguments": {
                    "kind": "deployment_topology",
                    "record": _topology_record("mcp-write-coverage"),
                    "reason": "MCP write coverage",
                },
            },
        },
        session=session,
    )
    assert status == 200
    assert payload["result"].get("isError") is not True
    result = payload["result"]["structuredContent"]["result"]
    assert result["target"] == {
        "kind": "deployment_topology",
        "id": "mcp-write-coverage",
    }
    assert result["principal"] == "mcp-agent"
    assert result["reason"] == "MCP write coverage"
    with CatalogStore.open(catalog_copy, read_only=True) as store:
        audit_entry = store.audit()[-1]
    assert audit_entry["principal"] == "mcp-agent"
    assert audit_entry["reason"] == "MCP write coverage"


def test_add_refuses_source_authoritative_kind_with_structured_envelope(
    catalog_copy: Path,
) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    session = _initialized_session(server)
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "add",
                "arguments": {
                    "kind": "host",
                    "record": {"id": "mcp-new-host", "role": "server"},
                },
            },
        },
        session=session,
    )
    assert status == 200
    result = payload["result"]
    assert result["isError"] is True
    message = result["structuredContent"]["error"]["message"]
    assert "REFUSED" in message
    assert "cadastre collect --source" in message


def test_write_tool_rejects_a_caller_supplied_principal_argument(
    catalog_copy: Path,
) -> None:
    """§1: `principal` comes from authentication only — a caller supplying
    one as a tool argument must be rejected, not silently accepted, or any
    `mcp`-scoped caller could forge the §2.3 provenance stamp."""
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    session = _initialized_session(server)
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"mcp": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    server.sessions.add(session)
    status, payload, _ = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "add",
                "arguments": {
                    "kind": "deployment_topology",
                    "record": _topology_record("y"),
                    "principal": "someone-else",
                },
            },
        },
        session=session,
    )
    assert status == 200
    result = payload["result"]
    assert result["isError"] is True
    assert "principal" in result["structuredContent"]["error"]["message"]
