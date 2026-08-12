"""Remote stdio bridge contract tests independent of the optional SDK."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from cadastre.adapters.client import RemoteClientError, StreamableClient, mcp_endpoint
from cadastre.adapters.security import MCP_SCOPE, WRITE_SCOPE, credential
from cadastre.core.errors import CadastreError
from cadastre.mcp.remote import (
    _endpoint,
    build_server,
    warn_if_below_minimum_client,
)
from cadastre.mcp.streamable import MCPHTTPServer
from tests.conftest import console_script


def test_mcp_endpoint_requires_the_standard_path() -> None:
    assert mcp_endpoint("https://catalog.example/mcp") == "https://catalog.example/mcp"
    with pytest.raises(RemoteClientError, match="path must be `/mcp`"):
        mcp_endpoint("https://catalog.example")
    with pytest.raises(RemoteClientError, match="path must be `/mcp`"):
        mcp_endpoint("https://catalog.example/api")
    with pytest.raises(RemoteClientError, match="query"):
        mcp_endpoint("https://catalog.example/mcp?token=secret")
    with pytest.raises(RemoteClientError, match="userinfo"):
        mcp_endpoint("https://agent:secret@catalog.example/mcp")


def test_remote_only_rejects_plaintext_nonlocal_endpoints() -> None:
    with pytest.raises(RemoteClientError, match="requires an https"):
        StreamableClient("http://catalog.example/mcp")


def test_unavailable_mcp_endpoint_is_reported_without_fallback() -> None:
    bridge = StreamableClient("https://127.0.0.1:1/mcp")
    with pytest.raises(RemoteClientError, match="could not reach"):
        bridge.tool("brief")


def test_remote_bridge_fails_closed_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CADASTRE_MCP_URL", raising=False)
    monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
    with pytest.raises(CadastreError, match="required"):
        _endpoint()


def test_remote_bridge_rejects_api_endpoint_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CADASTRE_MCP_URL", raising=False)
    monkeypatch.setenv("CADASTRE_HTTP_URL", "https://catalog.example")
    with pytest.raises(CadastreError, match="CADASTRE_MCP_URL is required"):
        _endpoint()


def test_remote_bridge_never_uses_catalog_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CADASTRE_MCP_URL", "https://catalog.example/mcp")
    monkeypatch.setenv("CADASTRE_CATALOG", "/should/not/be/opened")
    bridge = StreamableClient("https://catalog.example/mcp")
    assert bridge.endpoint == "https://catalog.example/mcp"
    assert bridge.session_id is None


def test_client_state_keeps_token_out_of_endpoint() -> None:
    token = "operator-secret-token"
    bridge = StreamableClient("https://catalog.example/mcp", token)
    assert token not in bridge.endpoint
    assert json.dumps({"endpoint": bridge.endpoint}) == (
        '{"endpoint": "https://catalog.example/mcp"}'
    )


def test_streamable_client_round_trips_initialize_and_brief(catalog_copy: Any) -> None:
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        tokens={"mcp-token": credential("agent", scopes={MCP_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bridge = StreamableClient(
            f"http://127.0.0.1:{server.server_port}/mcp", "mcp-token"
        )
        assert {item["name"] for item in bridge.list_tools()} == {
            "brief",
            "context_for",
            "check",
            "lookup",
            "drift",
            "question",
            "observations",
            "version",
        }
        payload = json.loads(bridge.tool("brief"))
        assert payload["command"] == "cadastre brief"
        assert payload["provenance"] is not None
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_streamable_client_reports_auth_denial_without_local_fallback(
    catalog_copy: Any,
) -> None:
    server = MCPHTTPServer(("127.0.0.1", 0), catalog_copy, require_auth=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bridge = StreamableClient(f"http://127.0.0.1:{server.server_port}/mcp")
        with pytest.raises(RemoteClientError, match="HTTP 403"):
            bridge.tool("brief")
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_remote_bridge_registers_the_canonical_tool_surface() -> None:
    optional_mcp = pytest.importorskip("mcp")
    del optional_mcp
    bridge = build_server()
    registered = getattr(bridge, "_tool_manager", None)
    if registered is not None:
        names = set(getattr(registered, "_tools", {}))
    else:
        names = {item.__name__ for item in bridge._tool_manager.list_tools()}
    assert names == {
        "brief",
        "context_for",
        "check",
        "lookup",
        "drift",
        "question",
        "observations",
        "version",
    }


def _bridge_tool_names(bridge: Any) -> set[str]:
    registered = getattr(bridge, "_tool_manager", None)
    if registered is not None:
        return set(getattr(registered, "_tools", {}))
    return {item.__name__ for item in bridge._tool_manager.list_tools()}


def test_remote_bridge_hides_write_tools_the_remote_does_not_serve(
    monkeypatch: pytest.MonkeyPatch, catalog_copy: Any
) -> None:
    """L4C, §1: the bridge asks the remote server what it has (the same
    mechanism already proven for Manifest tools) — a remote started without
    `--allow-write` must not have its write surface offered by the bridge."""
    pytest.importorskip("mcp")
    server = MCPHTTPServer(("127.0.0.1", 0), catalog_copy, require_auth=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "CADASTRE_MCP_URL", f"http://127.0.0.1:{server.server_port}/mcp"
        )
        monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
        names = _bridge_tool_names(build_server())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert "add" not in names
    assert not ({"add", "update", "annotate", "accept", "leave_contested"} & names)


def test_remote_bridge_exposes_write_tools_the_remote_does_serve(
    monkeypatch: pytest.MonkeyPatch, catalog_copy: Any, tmp_path: Path
) -> None:
    """A write must be attributable even when the bridge inherits it: the
    remote needs an actual `catalog.write`-scoped token, not just
    `--allow-write` (a write over an otherwise-unauthenticated endpoint is
    refused — see the sibling refusal test in test_streamable.py)."""
    pytest.importorskip("mcp")
    token_file = tmp_path / "token"
    token_file.write_text("writer-token", encoding="utf-8")
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={"writer-token": credential("agent", scopes={MCP_SCOPE, WRITE_SCOPE})},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "CADASTRE_MCP_URL", f"http://127.0.0.1:{server.server_port}/mcp"
        )
        monkeypatch.setenv("CADASTRE_HTTP_TOKEN_FILE", str(token_file))
        monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
        names = _bridge_tool_names(build_server())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert {
        "add",
        "update",
        "annotate",
        "accept",
        "leave_contested",
        "acknowledge",
    } <= names
    assert "delete" not in names


def test_remote_bridge_executable_is_remote_only() -> None:
    command = console_script("cadastre-mcp-remote")
    if command is None:
        pytest.skip("install the package to test the console script")
    env = {
        **os.environ,
        "CADASTRE_MCP_URL": "",
        "CADASTRE_HTTP_URL": "",
        "CADASTRE_CATALOG": "/must-not-be-opened",
    }
    result = subprocess.run(
        [command], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 2
    assert "required" in result.stderr
    assert "must-not-be-opened" not in result.stderr


def _probe(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    def fake(name: str, arguments: dict[str, Any]) -> str:
        assert name == "version"
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)

    monkeypatch.setattr("cadastre.mcp.remote._remote_tool", fake)


def test_bridge_warns_once_when_older_than_the_minimum_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cadastre.mcp.remote.__version__", "0.2.0")
    _probe(monkeypatch, {"version": "0.3.0", "minimum_client_version": "0.3.0"})
    warn_if_below_minimum_client()
    captured = capsys.readouterr()
    # stdout is the MCP framing channel; a diagnostic there corrupts the session.
    assert captured.out == ""
    assert captured.err.count("cadastre-mcp-remote 0.2.0 is older") == 1
    assert "minimum supported client 0.3.0" in captured.err
    assert "uv tool upgrade cadastre" in captured.err


def test_bridge_is_silent_when_at_or_above_the_minimum_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cadastre.mcp.remote.__version__", "0.3.0")
    _probe(monkeypatch, {"version": "0.4.0", "minimum_client_version": "0.3.0"})
    warn_if_below_minimum_client()
    assert capsys.readouterr() == ("", "")


def test_bridge_is_silent_against_a_server_without_the_version_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cadastre.mcp.remote.__version__", "0.2.0")
    _probe(monkeypatch, {"error": {"kind": "usage", "message": "unknown tool"}})
    warn_if_below_minimum_client()
    assert capsys.readouterr() == ("", "")


def test_bridge_is_silent_when_the_probe_itself_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cadastre.mcp.remote.__version__", "0.2.0")
    _probe(monkeypatch, RemoteClientError("could not reach the endpoint"))
    warn_if_below_minimum_client()
    assert capsys.readouterr() == ("", "")


def test_bridge_still_starts_when_it_is_below_the_minimum_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusing to start on a cosmetic bump is worse than a stale bridge."""
    pytest.importorskip("mcp")
    monkeypatch.setattr("cadastre.mcp.remote.__version__", "0.2.0")
    _probe(monkeypatch, {"minimum_client_version": "9.9.9"})
    assert build_server() is not None
    assert "is older than this server's" in capsys.readouterr().err
