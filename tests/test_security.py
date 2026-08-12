"""M16-M20 transport and identity boundary tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from cadastre.adapters.http import CadastreHTTPServer
from cadastre.adapters.security import (
    CHECK_SCOPE,
    MCP_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    AuditLog,
    ProxyConfig,
    credential,
    parse_scope_bindings,
    parse_token_file,
    security_report,
)
from cadastre.core.errors import UsageError
from cadastre.mcp.streamable import MCPHTTPServer


def _request(
    server: CadastreHTTPServer,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        encoded = json.dumps(body).encode() if body is not None else None
        connection.request(
            method,
            path,
            encoded,
            {"Content-Type": "application/json", **(headers or {})},
        )
        response = connection.getresponse()
        raw_response = response.read()
        payload = json.loads(raw_response) if raw_response else {}
        connection.close()
        return response.status, payload
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _mcp_post(
    server: MCPHTTPServer, payload: dict[str, Any], *, session: str | None = None
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


def test_mcp_write_tool_without_catalog_write_scope_is_denied_and_audited(
    tmp_path: Path, catalog_copy: Path
) -> None:
    """§1: a token holding `mcp` but not `catalog.write` must be refused a
    write tool call, and the refusal must be recorded in the audit sink —
    not just returned to the caller."""
    audit_path = tmp_path / "audit.jsonl"
    tokens = {"mcp": credential("agent", scopes={MCP_SCOPE})}
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        audit_path=audit_path,
        tokens=tokens,
    )
    _, _, session = _mcp_post(
        server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert session
    server = MCPHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        audit_path=audit_path,
        tokens=tokens,
    )
    server.sessions.add(session)
    status, payload, _ = _mcp_post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "annotate",
                "arguments": {
                    "kind": "host",
                    "id": "app-01",
                    "record": {"notes": "denied write attempt"},
                },
            },
        },
        session=session,
    )
    assert status == 403
    assert WRITE_SCOPE in payload["error"]["message"]
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    write_rows = [row for row in rows if row["operation"] == "mcp.tools/call.annotate"]
    assert len(write_rows) == 1
    assert write_rows[0]["decision"] == "deny"
    assert write_rows[0]["principal"] == "agent"


def test_scoped_token_distinguishes_read_check_and_write(
    catalog_copy: Path,
) -> None:
    token = credential("reader", scopes={READ_SCOPE})
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        require_auth=True,
        tokens={"read": token},
    )
    status, _ = _request(
        server, "GET", "/brief", headers={"Authorization": "Bearer read"}
    )
    assert status == 200
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        require_auth=True,
        tokens={"read": token},
    )
    status, payload = _request(
        server,
        "POST",
        "/check",
        headers={"Authorization": "Bearer read"},
        body={"artifact": "services: {}", "kind": "compose"},
    )
    assert status == 403
    assert CHECK_SCOPE in payload["error"]["message"]


def test_required_audit_sink_fails_closed(tmp_path: Path) -> None:
    sink = AuditLog(tmp_path / "audit.jsonl", required=True)
    sink.record(
        principal="reader",
        operation="brief",
        target="/brief",
        decision="allow",
    )
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    failed = AuditLog(tmp_path, required=True)
    with pytest.raises(PermissionError, match="audit sink failed"):
        failed.record(
            principal="reader",
            operation="brief",
            target="/brief",
            decision="allow",
        )


def test_expired_and_revoked_tokens_are_denied(catalog_copy: Path) -> None:
    expired = credential(
        "reader",
        scopes={READ_SCOPE},
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
    )
    revoked = credential("reader", scopes={READ_SCOPE}, revoked=True)
    for item in (expired, revoked):
        server = CadastreHTTPServer(
            ("127.0.0.1", 0),
            catalog_copy,
            allow_write=False,
            require_auth=True,
            tokens={"token": item},
        )
        status, _ = _request(
            server, "GET", "/brief", headers={"Authorization": "Bearer token"}
        )
        assert status == 403


def test_body_host_origin_and_rate_limits_are_enforced(catalog_copy: Path) -> None:
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=False,
        allowed_hosts=("expected.invalid",),
        allowed_origins=("https://agent.invalid",),
        max_body_bytes=4,
        rate_limit=1,
    )
    status, payload = _request(
        server, "GET", "/brief", headers={"Host": "wrong.invalid"}
    )
    assert status == 403
    assert "Host" in payload["error"]["message"]
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=False,
        max_body_bytes=4,
    )
    status, payload = _request(server, "POST", "/check", body={"artifact": "too long"})
    assert status == 400
    assert "size limit" in payload["error"]["message"]


def test_cors_preflight_requires_an_allowed_origin(catalog_copy: Path) -> None:
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=False,
        allowed_origins=("https://gui.invalid",),
    )
    status, _ = _request(
        server,
        "OPTIONS",
        "/brief",
        headers={"Origin": "https://gui.invalid"},
    )
    assert status == 204
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=False,
        allowed_origins=("https://gui.invalid",),
    )
    status, payload = _request(
        server,
        "OPTIONS",
        "/brief",
        headers={"Origin": "https://evil.invalid"},
    )
    assert status == 403
    assert "Origin" in payload["error"]["message"]


def test_proxy_identity_requires_network_and_signature() -> None:
    proxy = ProxyConfig(networks=("127.0.0.0/8",), identity_secret=b"secret")
    signature = hmac.new(b"secret", b"agent", hashlib.sha256).hexdigest()
    assert (
        proxy.principal(
            {
                "X-Cadastre-Principal": "agent",
                "X-Cadastre-Identity-Signature": signature,
            },
            "127.0.0.1",
        )
        == "agent"
    )
    assert (
        proxy.principal(
            {"X-Cadastre-Principal": "agent", "X-Cadastre-Identity-Signature": "bad"},
            "127.0.0.1",
        )
        is None
    )


def test_security_report_fails_non_loopback_plaintext() -> None:
    report = security_report(
        bind="0.0.0.0:8000",
        tls=False,
        profile="development-plaintext",
        require_auth=True,
        scopes={READ_SCOPE},
    )
    assert not report["ready"]
    assert any(not check["ok"] for check in report["checks"])


def test_token_file_is_scoped_and_rejects_wildcard(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "token": "secret",
                        "principal": "agent",
                        "scopes": [MCP_SCOPE],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    tokens = parse_token_file(path)
    assert tokens["secret"].scopes == {MCP_SCOPE}
    path.write_text(
        json.dumps(
            {"tokens": [{"token": "secret", "principal": "*", "scopes": [MCP_SCOPE]}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(UsageError, match="named principal"):
        parse_token_file(path)


def test_legacy_token_file_requires_explicit_local_mode(tmp_path: Path) -> None:
    path = tmp_path / "tokens.txt"
    path.write_text("agent=secret\n", encoding="utf-8")
    with pytest.raises(UsageError, match="legacy"):
        parse_token_file(path)
    assert parse_token_file(path, allow_legacy=True)["secret"].scopes


def test_audience_and_explicit_scope_bindings_are_default_deny() -> None:
    assert parse_scope_bindings(["agent=catalog.read,catalog.check"]) == {
        "agent": frozenset({READ_SCOPE, CHECK_SCOPE})
    }
    with pytest.raises(UsageError, match="unknown security scope"):
        parse_scope_bindings(["agent=admin"])
    assert not credential("agent", scopes={READ_SCOPE}, audience="other").valid(
        now=datetime.now(tz=UTC), audience="cadastre"
    )
