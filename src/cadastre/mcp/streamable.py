"""Standard MCP Streamable HTTP transport.

This is a transport adapter, not a second Cadastre API.  JSON-RPC requests are
translated directly to the same tool callables used by stdio MCP; the
ordinary HTTP adapter remains a separate sibling endpoint.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cadastre import __version__
from cadastre.adapters.security import (
    MCP_SCOPE,
    AuditLog,
    AuthConfig,
    Authorizer,
    ProxyConfig,
    RateLimiter,
    TokenCredential,
    certificate_common_name,
)
from cadastre.api.registry import (
    ARGUMENT_TYPES,
    MANIFEST_MCP_OPERATIONS,
    MCP_OPERATIONS,
    MCP_WRITE_OPERATIONS,
    argument_type,
)
from cadastre.application.checks import CheckService
from cadastre.application.context import ApplicationContext
from cadastre.application.queries import QueryService
from cadastre.application.writes import WriteService
from cadastre.core.errors import CadastreError, UsageError
from cadastre.mcp import server as tool_server
from cadastre.mcp.sdk import error_kind
from cadastre.render import json_out

SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-11-25", "2026-07-28"})


def _manifest_enabled(root: Path) -> bool:
    from cadastre.modules.config import load_modules

    return load_modules(root).enabled("manifest")


def _tools(server: MCPHTTPServer, principal: str | None) -> list[dict[str, Any]]:
    """`tools/list` filters by the authenticated principal's scopes.

    Write tools are advertised only to an authenticated principal holding
    `catalog.write`, and only while the server was started with
    `--allow-write` — ignoring the principal here was safe only while every
    tool was read (§1/§4). A mutation must always be attributable, so an
    unauthenticated caller (possible when the whole endpoint runs with
    `require_auth=False`) never sees a write tool, even if the scope check
    itself would otherwise pass.
    """
    from cadastre.mcp.manifest import enabled_tools

    root = server.root
    manifest_functions = enabled_tools(str(root)) if _manifest_enabled(root) else ()
    functions = {
        function.__name__: function
        for function in tool_server.TOOLS + tool_server.WRITE_TOOLS + manifest_functions
    }
    operations: tuple[Any, ...] = MCP_OPERATIONS + (
        MANIFEST_MCP_OPERATIONS if manifest_functions else ()
    )
    if (
        server.allow_write
        and principal is not None
        and server.permits(principal, "catalog.write")
    ):
        operations = operations + MCP_WRITE_OPERATIONS
    result = []
    for operation in operations:
        function = functions[operation.name]
        result.append(
            {
                "name": operation.name,
                "description": function.__doc__ or function.__name__,
                "inputSchema": operation.input_schema(),
            }
        )
    return result


def _jsonrpc_error(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }


def _tool_error(exc: Exception) -> dict[str, Any]:
    """The stable error envelope returned by every failed tool call.

    MCP distinguishes a failed *tool invocation* (`isError`) from an invalid
    JSON-RPC request.  The former carries this schema in `structuredContent`;
    the latter uses JSON-RPC `error.data` with the same shape.
    """
    kind = error_kind(exc)
    if kind not in {
        "unknown_kind",
        "missing_entity",
        "ambiguous_entity",
        "invalid_argument",
    }:
        kind = "catalog_error"
    return {"error": {"kind": kind, "message": str(exc)}}


class _Handler(BaseHTTPRequestHandler):
    server: MCPHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        session: str | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=False).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)

    def _security(self) -> str | None:
        self.server.validate_request(self.headers, self.client_address[0])
        principal = self.server.principal(
            self.headers, self.client_address[0], self.connection
        )
        if not self.server.permits(principal, MCP_SCOPE):
            self.server.audit.record(
                principal=principal,
                operation="mcp",
                target="/mcp",
                decision="deny",
                request_material=self.path.encode(),
                catalog_revision=self.server.catalog_revision(),
                result="denied",
            )
            raise PermissionError("operation requires scope `mcp`")
        self.server.audit.record(
            principal=principal,
            operation="mcp",
            target="/mcp",
            decision="allow",
            request_material=self.path.encode(),
            catalog_revision=self.server.catalog_revision(),
            result="authorized",
        )
        return principal

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/mcp":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                _jsonrpc_error(None, -32004, "MCP endpoint is /mcp"),
            )
            return
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            _jsonrpc_error(
                None,
                -32000,
                "MCP server-to-client GET streams are not enabled",
            ),
        )

    def do_DELETE(self) -> None:
        try:
            if self.path.split("?", 1)[0] != "/mcp":
                raise UsageError("MCP endpoint is /mcp")
            self._security()
            session = self.headers.get("Mcp-Session-Id")
            if not session or session not in self.server.sessions:
                raise UsageError("DELETE needs a valid Mcp-Session-Id")
            self.server.sessions.discard(session)
            self.server.session_seen.pop(session, None)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN, _jsonrpc_error(None, -32001, str(exc))
            )
        except (CadastreError, ValueError, TypeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32602, str(exc))
            )

    def do_POST(self) -> None:
        request_id: Any = None
        try:
            if self.path.split("?", 1)[0] != "/mcp":
                raise UsageError("MCP endpoint is /mcp")
            principal = self._security()
            length = int(self.headers.get("Content-Length", "0"))
            if length > self.server.max_body_bytes:
                raise UsageError("request body exceeds the configured size limit")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                raise UsageError("MCP request must be a JSON-RPC 2.0 object")
            request_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params") or {}
            if method == "initialize":
                requested_version = params.get("protocolVersion", "2025-06-18")
                if requested_version not in SUPPORTED_PROTOCOL_VERSIONS:
                    self._send_json(
                        HTTPStatus.OK,
                        _jsonrpc_error(
                            request_id,
                            -32602,
                            "unsupported MCP protocol version; supported versions: "
                            + ", ".join(sorted(SUPPORTED_PROTOCOL_VERSIONS)),
                        ),
                    )
                    return
                session = secrets.token_urlsafe(18)
                self.server.sessions.add(session)
                self.server.session_seen[session] = time.monotonic()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": requested_version,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "cadastre", "version": __version__},
                        },
                    },
                    session=session,
                )
                return
            requested_session = self.headers.get("Mcp-Session-Id")
            if requested_session not in self.server.sessions:
                raise UsageError("MCP request needs a valid Mcp-Session-Id")
            assert requested_session is not None
            seen = self.server.session_seen.setdefault(
                requested_session, time.monotonic()
            )
            if time.monotonic() - seen > self.server.session_ttl_seconds:
                self.server.sessions.discard(requested_session)
                self.server.session_seen.pop(requested_session, None)
                raise UsageError("MCP session has expired")
            self.server.session_seen[requested_session] = time.monotonic()
            session = requested_session
            if method == "notifications/initialized":
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
                return
            if method == "tools/list":
                result = {"tools": _tools(self.server, principal)}
            elif method == "tools/call":
                result = self._call_tool(params, principal)
            else:
                self._send_json(
                    HTTPStatus.OK,
                    _jsonrpc_error(request_id, -32601, "method not found"),
                    session=session,
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {"jsonrpc": "2.0", "id": request_id, "result": result},
                session=session,
            )
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                _jsonrpc_error(request_id, -32001, str(exc)),
            )
        except (CadastreError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _jsonrpc_error(request_id, -32602, str(exc)),
            )
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                _jsonrpc_error(request_id, -32603, str(exc)),
            )

    def _call_tool(self, params: Any, principal: str | None) -> dict[str, Any]:
        try:
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise UsageError("tools/call needs a tool name")
            name = params["name"]
            from cadastre.mcp.manifest import enabled_tools

            manifest_functions = (
                enabled_tools(str(self.server.root))
                if _manifest_enabled(self.server.root)
                else ()
            )
            function = next(
                (
                    item
                    for item in tool_server.TOOLS
                    + tool_server.WRITE_TOOLS
                    + manifest_functions
                    if item.__name__ == name
                ),
                None,
            )
            if function is None:
                raise UsageError(f"unknown MCP tool `{name}`")
            write_operation = next(
                (op for op in MCP_WRITE_OPERATIONS if op.name == name), None
            )
            if write_operation is not None:
                allowed = (
                    self.server.allow_write
                    and principal is not None
                    and self.server.permits(principal, write_operation.scope)
                )
                self.server.audit.record(
                    principal=principal,
                    operation=f"mcp.tools/call.{name}",
                    target=name,
                    decision="allow" if allowed else "deny",
                    request_material=json.dumps(params).encode("utf-8"),
                    catalog_revision=self.server.catalog_revision(),
                )
                if not self.server.allow_write:
                    raise PermissionError(
                        "MCP writes are disabled; restart with --allow-write"
                    )
                # A write must always be attributable, even when the whole
                # endpoint runs unauthenticated (loopback development,
                # `require_auth=False`, where a read-scope check would
                # otherwise pass with no identity) — matching the plain HTTP
                # `/add` et al. routes, which refuse the same way.
                if principal is None:
                    raise PermissionError(
                        "a valid scoped bearer token is required for writes"
                    )
                if not self.server.permits(principal, write_operation.scope):
                    raise PermissionError(
                        f"operation requires scope `{write_operation.scope}`"
                    )
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise UsageError("tools/call arguments must be an object")
            self._validate_tool_arguments(name, arguments)
            text = self.server.call_tool(name, arguments, principal=principal)
            structured = json.loads(text)
        except (CadastreError, ValueError, TypeError) as exc:
            structured = _tool_error(exc)
            return {
                "content": [{"type": "text", "text": structured["error"]["message"]}],
                "structuredContent": structured,
                "isError": True,
            }
        result_data = structured.get("result") if isinstance(structured, dict) else None
        if isinstance(result_data, dict) and isinstance(result_data.get("error"), dict):
            # Query contracts can reject a typed argument without raising so
            # CLI callers retain their explanatory document.  At the MCP
            # boundary it is still a failed tool invocation.
            structured = {"error": result_data["error"]}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": structured["error"].get("message", "tool failed"),
                    }
                ],
                "structuredContent": structured,
                "isError": True,
            }
        return {
            # The JSON structure is authoritative. A short text summary avoids
            # duplicating large drift reports in every MCP response. Other
            # tools retain their text form for older MCP clients.
            "content": [
                {
                    "type": "text",
                    "text": structured.get("command", name)
                    if name == "drift"
                    else text,
                }
            ],
            "structuredContent": structured,
        }

    @staticmethod
    def _validate_tool_arguments(name: str, arguments: dict[str, Any]) -> None:
        operation = next(
            item
            for item in MCP_OPERATIONS + MANIFEST_MCP_OPERATIONS + MCP_WRITE_OPERATIONS
            if item.name == name
        )
        unexpected = sorted(set(arguments) - set(operation.arguments))
        if unexpected:
            raise UsageError(f"{name} does not accept argument {unexpected[0]!r}")
        required = operation.required_argument_names()
        missing = [
            key
            for key in (operation.required_arguments or ())
            if key not in arguments or arguments[key] is None
        ]
        if missing:
            raise UsageError(f"{name} needs argument {missing[0]!r}")
        for key, value in arguments.items():
            if value is None and key not in required:
                # The schema publishes optional arguments as nullable with a
                # null default, and the core accepts None. Rejecting the value
                # a client was told to send is the transport disagreeing with
                # its own advertisement.
                continue
            expected = argument_type(key)
            if not isinstance(value, expected) or (
                expected is int and isinstance(value, bool)
            ):
                raise UsageError(
                    f"{name} argument {key!r} must be a "
                    f"{ARGUMENT_TYPES.get(key, 'string')}"
                )


class MCPHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        *,
        allow_write: bool = False,
        tokens: dict[str, TokenCredential] | None = None,
        require_auth: bool = True,
        allowed_hosts: tuple[str, ...] = (),
        allowed_origins: tuple[str, ...] = (),
        max_body_bytes: int = 1_048_576,
        rate_limit: int = 120,
        audit_path: Path | None = None,
        audit_required: bool | None = None,
        proxy: ProxyConfig | None = None,
        proxy_scopes: dict[str, frozenset[str]] | None = None,
        mtls_scopes: dict[str, frozenset[str]] | None = None,
        audience: str = "cadastre",
        session_ttl_seconds: int = 3600,
    ) -> None:
        super().__init__(address, _Handler)
        if address[0] not in {"127.0.0.1", "::1", "localhost"} and not require_auth:
            raise UsageError("remote MCP requires authentication")
        self.root = root
        self.allow_write = allow_write
        self.tokens = tokens or {}
        self.require_auth = require_auth
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins
        self.max_body_bytes = max_body_bytes
        self.rate_limiter = RateLimiter(rate_limit)
        self.audit = AuditLog(
            audit_path,
            required=(
                address[0] not in {"127.0.0.1", "::1", "localhost"}
                if audit_required is None
                else audit_required
            ),
        )
        self.proxy = proxy
        self.proxy_scopes = proxy_scopes or {}
        self.mtls_scopes = mtls_scopes or {}
        self.audience = audience
        self.authorizer = Authorizer(
            AuthConfig(require_auth=require_auth, audience=audience),
            tokens=self.tokens,
            proxy_scopes=self.proxy_scopes,
            mtls_scopes=self.mtls_scopes,
        )
        self.sessions: set[str] = set()
        self.session_seen: dict[str, float] = {}
        self.session_ttl_seconds = session_ttl_seconds

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        principal: str | None = None,
    ) -> str:
        context = ApplicationContext.open(self.root, now=datetime.now(tz=UTC))
        write_operation = next(
            (op for op in MCP_WRITE_OPERATIONS if op.name == name), None
        )
        if write_operation is not None:
            # `_call_tool` checks `--allow-write` and the `catalog.write`
            # scope before this runs, so `principal` is always authenticated
            # here. `principal` never comes from `arguments` (§2.3): a
            # caller-supplied one would let any `mcp`-scoped token forge the
            # provenance stamp.
            assert principal is not None
            reason = str(arguments.get("reason", "MCP catalog edit"))
            document = WriteService(context).dispatch(
                name, arguments, principal=principal, reason=reason
            )
        elif name == "check":
            content = arguments.get("artifact")
            if not isinstance(content, str):
                raise UsageError("check needs an artifact string")
            document = CheckService(context).artifact(
                content,
                kind=arguments.get("kind"),
                display_path=(
                    Path(str(arguments["path"])).name
                    if isinstance(arguments.get("path"), str) and arguments["path"]
                    else None
                ),
            )
        else:
            document = QueryService(context).dispatch(name, arguments)
        return json_out.render(document)

    def validate_request(self, headers: Any, address: str) -> None:
        if not self.rate_limiter.allow(address):
            raise PermissionError("request rate limit exceeded")
        host = str(headers.get("Host", "")).split(":", 1)[0].lower()
        if self.allowed_hosts and host not in {
            item.lower() for item in self.allowed_hosts
        }:
            raise PermissionError("request Host is not configured for this endpoint")
        origin = headers.get("Origin")
        if origin and self.allowed_origins and origin not in self.allowed_origins:
            raise PermissionError("request Origin is not configured for this endpoint")

    def principal(
        self, headers: Any, address: str, connection: Any | None = None
    ) -> str | None:
        if self.proxy is not None:
            forwarded = self.proxy.principal(dict(headers), address)
            if forwarded:
                return forwarded
        if connection is not None and self.mtls_scopes:
            name = certificate_common_name(connection)
            if name in self.mtls_scopes:
                return name
        header = headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        item = self.tokens.get(header[7:])
        if item is None or not item.valid(
            now=datetime.now(tz=UTC), audience=self.audience
        ):
            return None
        return item.principal

    def permits(self, principal: str | None, scope: str) -> bool:
        from cadastre.adapters.security import Identity, RequestContext

        identity = Identity(principal, "transport") if principal else None
        return self.authorizer.decide(
            RequestContext(identity, "unknown", "unknown"), scope
        ).allowed

    def catalog_revision(self) -> int | None:
        try:
            from cadastre.core.storage import CatalogStore

            with CatalogStore.open(self.root, read_only=True) as store:
                return store.revision
        except Exception:
            return None


__all__ = ["MCPHTTPServer"]
