"""The optional, stateless HTTP adapter.

This module deliberately uses the standard library.  It is an adapter over
the CLI command functions, not a second implementation of catalog logic.  It
is loopback/read-only unless the operator explicitly opts into broader binds
and authenticated writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cadastre import __version__
from cadastre.adapters.security import (
    CHECK_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    AuditLog,
    AuthConfig,
    Authorizer,
    ProxyConfig,
    RateLimiter,
    TokenCredential,
    certificate_common_name,
    security_report,
    tls_context,
)
from cadastre.api.contract import error_response_schema
from cadastre.api.registry import HTTP_ROUTES, MANIFEST_HTTP_ROUTES
from cadastre.application.checks import CheckService
from cadastre.application.context import ApplicationContext
from cadastre.application.health import HealthService
from cadastre.application.queries import QueryService
from cadastre.application.writes import WriteService
from cadastre.core.errors import CadastreError, UsageError
from cadastre.core.schema import catalog_schema
from cadastre.core.yamlio import load_yaml
from cadastre.render.document import Document
from cadastre.render.json_out import to_dict


def openapi_schema(*, manifest_enabled: bool = False) -> dict[str, Any]:
    """Generate the OpenAPI document from the same entity schema definitions.

    `manifest_enabled` defaults to False so a disabled catalog's document is
    byte-identical to base Cadastre (MANIFEST.md R09's default-off
    contract). The runtime HTTP handler passes the catalog's real activation
    state; the GUI codegen script passes True so the checked-in TypeScript
    route contract always names the full possible surface, matching how
    auth-gated write routes are already listed unconditionally.
    """
    schema_registry = None
    if manifest_enabled:
        from cadastre.modules.config import ModuleConfig, ModulesFile
        from cadastre.modules.registry import active_registry

        schema_registry = active_registry(
            ModulesFile((ModuleConfig("manifest", enabled=True),))
        )
    schema = catalog_schema(registry=schema_registry)
    document_schema = {
        "type": "object",
        "required": ["command", "result", "provenance", "stale"],
        "properties": {
            "command": {"type": "string"},
            "result": {"type": "object"},
            "provenance": {"type": "array", "items": {"type": "object"}},
            "stale": {"type": "array", "items": {"type": "string"}},
        },
    }
    document_response = {"$ref": "#/components/responses/Document"}
    schema_response = {"$ref": "#/components/responses/Schema"}
    routes = HTTP_ROUTES + (MANIFEST_HTTP_ROUTES if manifest_enabled else ())
    return {
        "openapi": "3.1.0",
        "info": {"title": "Cadastre API", "version": __version__},
        "paths": {
            operation.route: _openapi_operation(
                operation, document_response, schema_response
            )
            for operation in routes
            if operation.route is not None
        }
        | {
            "/health/live": {
                "get": {"responses": {"200": {"description": "Liveness"}}}
            },
            "/health/ready": {
                "get": {"responses": {"200": {"description": "Readiness"}}}
            },
        },
        "components": {
            "schemas": {
                **schema["$defs"],
                "Catalog": schema,
                "Document": document_schema,
            },
            "responses": {
                "Document": {
                    "description": "A core command result",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Document"}
                        }
                    },
                },
                "Schema": {
                    "description": "The entity JSON Schema",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Catalog"}
                        }
                    },
                },
                "Error": {
                    "description": "A stable API error",
                    "content": {
                        "application/json": {"schema": error_response_schema()}
                    },
                },
            },
        },
    }


def _openapi_operation(
    operation: Any, document: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    method: dict[str, Any] = {
        "responses": {"200": schema if operation.name == "schema" else document}
    }
    if operation.method == "GET":
        method["parameters"] = [
            {
                "name": field,
                "in": "path" if f"{{{field}}}" in operation.route else "query",
                "required": field in operation.required_request_fields,
                "schema": {"type": "string"},
            }
            for field in operation.request_fields
        ]
    elif operation.request_fields:
        method["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": list(operation.required_request_fields),
                        "properties": {
                            field: {"type": "object" if field == "record" else "string"}
                            for field in operation.request_fields
                        },
                    }
                }
            },
        }
    return {operation.method.lower(): method}


def _write_operation(
    operation: str,
    *,
    requires_record: bool = False,
    requires_source: bool = False,
    requires_until: bool = False,
) -> dict[str, Any]:
    """Describe one JSON write request in the generated API document."""
    required: list[str] = []
    properties: dict[str, Any] = {"reason": {"type": "string"}}
    if operation in {"add", "update", "delete", "annotate"}:
        required.append("kind")
        properties["kind"] = {"type": "string"}
    if operation in {"update", "delete", "annotate"}:
        required.append("id")
        properties["id"] = {"type": "string"}
    if requires_record:
        required.append("record")
        properties["record"] = {"type": "object"}
    if requires_source:
        required.extend(("target", "source"))
        properties.update({"target": {"type": "string"}, "source": {"type": "string"}})
    if requires_until:
        required.append("until")
        properties["until"] = {"type": "string"}
    if operation in {"accept", "leave-contested"}:
        properties["field"] = {"type": "string"}
    return {
        "post": {
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": required,
                            "properties": properties,
                        }
                    }
                },
            },
            "responses": {"200": {"$ref": "#/components/responses/Document"}},
        }
    }


def _principals(root: Path) -> dict[str, str]:
    path = root / "declared/policy/principals.yaml"
    if not path.exists():
        return {}
    raw = load_yaml(path, rel="declared/policy/principals.yaml") or {}
    items = raw.get("principals", []) if isinstance(raw, dict) else []
    result: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        principal = item.get("principal", item.get("id"))
        token = item.get("token")
        digest = item.get("token_sha256")
        # Token values must not be stored in the catalog. The optional token
        # field is ignored; operators pass live tokens through the server
        # configuration instead.
        _ = token
        if isinstance(principal, str) and isinstance(digest, str):
            result[f"sha256:{digest}"] = principal
    return result


class _Handler(BaseHTTPRequestHandler):
    server: CadastreHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, value: Any) -> None:
        body = json.dumps(value, indent=2, sort_keys=False).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin and origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, content_type: str, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security(self, operation: str, scope: str | None = None) -> str | None:
        self.server.validate_request(self.headers, self.client_address[0])
        principal = self.server.principal(
            self.headers, self.client_address[0], self.connection
        )
        if scope is not None and not self.server.permits(principal, scope):
            self.server.audit.record(
                principal=principal,
                operation=operation,
                target=urlparse(self.path).path,
                decision="deny",
                request_material=self.path.encode(),
                catalog_revision=self.server.catalog_revision(),
                result="denied",
            )
            raise PermissionError(f"operation requires scope `{scope}`")
        self.server.audit.record(
            principal=principal,
            operation=operation,
            target=urlparse(self.path).path,
            decision="allow",
            request_material=self.path.encode(),
            catalog_revision=self.server.catalog_revision(),
            result="authorized",
        )
        return principal

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > self.server.max_body_bytes:
            raise UsageError("request body exceeds the configured size limit")
        raw = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(raw, dict):
            raise UsageError("request body must be a JSON object")
        return raw

    def _context(self) -> ApplicationContext:
        return ApplicationContext.open(self.server.root, now=datetime.now(tz=UTC))

    def _call(self, document: Any) -> None:
        self._send(HTTPStatus.OK, to_dict(document))

    def _error(self, status: int, exc: Exception) -> None:
        self._send(status, {"error": {"kind": type(exc).__name__, "message": str(exc)}})

    def _principal(self) -> str | None:
        return self.server.principal(
            self.headers, self.client_address[0], self.connection
        )

    def do_OPTIONS(self) -> None:
        try:
            self.server.validate_request(self.headers, self.client_address[0])
            origin = self.headers.get("Origin")
            if origin and origin not in self.server.allowed_origins:
                raise PermissionError(
                    "request Origin is not configured for this endpoint"
                )
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Allow", "GET, POST, OPTIONS")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers", "Authorization, Content-Type"
                )
                self.send_header("Vary", "Origin")
            self.end_headers()
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, exc)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path in {"/health/live", "/health/ready"}:
                try:
                    payload = (
                        HealthService(self.server.root).ready()
                        if path.endswith("/ready")
                        else HealthService(self.server.root).live()
                    )
                except Exception as exc:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, exc)
                    return
                self._send(HTTPStatus.OK, payload)
                return
            query = parse_qs(parsed.query)
            scope = CHECK_SCOPE if path == "/check" else READ_SCOPE
            self._security("http.get", scope)
            context = self._context()
            service = QueryService(context)
            manifest_enabled = context.service_session().modules.enabled("manifest")
            if path == "/openapi.json":
                self._send(
                    HTTPStatus.OK, openapi_schema(manifest_enabled=manifest_enabled)
                )
            elif path == "/docs":
                self._send_text(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>Cadastre API docs</title>"
                    "<h1>Cadastre API</h1><p>Generated OpenAPI contract:</p>"
                    "<p><a href='/openapi.json'>OpenAPI 3.1 JSON</a></p>",
                )
            elif path == "/version":
                self._send(HTTPStatus.OK, HealthService(self.server.root).version())
            elif path == "/schema":
                self._send(
                    HTTPStatus.OK,
                    catalog_schema(registry=context.service_session().registry),
                )
            elif path == "/brief":
                self._call(service.brief())
            elif path == "/manifest/brief":
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                self._call(service.manifest_brief())
            elif path == "/manifest/projects":
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                self._call(service.manifest_projects())
            elif path == "/manifest/backlog":
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                manifest_limit = query.get("limit", ["10"])[0]
                self._call(
                    service.manifest_backlog(
                        state=query.get("state", [None])[0],
                        initiative=query.get("initiative", [None])[0],
                        repo=query.get("repo", [None])[0],
                        limit=int(manifest_limit),
                    )
                )
            elif path == "/manifest/next":
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                next_limit = query.get("limit", ["10"])[0]
                self._call(service.manifest_next(limit=int(next_limit)))
            elif path == "/manifest/drift":
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                self._call(service.manifest_drift(repo=query.get("repo", [None])[0]))
            elif path.startswith("/manifest/repo/"):
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                self._call(service.manifest_repo(path.removeprefix("/manifest/repo/")))
            elif path.startswith("/manifest/why/"):
                if not manifest_enabled:
                    self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
                    return
                self._call(service.manifest_why(path.removeprefix("/manifest/why/")))
            elif path == "/context-for":
                self._call(service.context_for(query.get("intent", [""])[0]))
            elif path == "/question":
                self._call(
                    service.question(
                        query.get("id", [""])[0],
                        subject=query.get("subject", [None])[0],
                        value=query.get("value", [None])[0],
                    )
                )
            elif path.startswith("/lookup/"):
                self._call(
                    service.lookup(
                        path.removeprefix("/lookup/"),
                        kind=query.get("kind", [None])[0],
                    )
                )
            elif path == "/drift":
                self._call(service.drift())
            elif path == "/observations":
                limit: str | None = query.get("limit", [None])[0]
                self._call(
                    service.observations(
                        source=query.get("source", [None])[0],
                        method=query.get("method", [None])[0],
                        key=query.get("key", [None])[0],
                        limit=int(limit) if limit else None,
                        summary_only=query.get("summary_only", [""])[0]
                        in ("1", "true", "yes"),
                    )
                )
            elif path == "/stale":
                self._call(service.stale())
            elif path == "/plugins":
                self._call(service.plugins())
            elif path == "/sources":
                self._call(service.sources())
            elif path == "/security-check":
                self._call(
                    Document(
                        title="cadastre security-check",
                        data=security_report(**self.server.security_kwargs()),
                    )
                )
            else:
                self._error(HTTPStatus.NOT_FOUND, UsageError("unknown API route"))
        except CadastreError as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc)
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, exc)
        except Exception as exc:  # keep transport errors structured
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, exc)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path.rstrip("/")
            body = self._body()
            self._security(
                "http.post", CHECK_SCOPE if path == "/check" else WRITE_SCOPE
            )
            context = self._context()
            writes_service = WriteService(context)
            if path == "/check":
                self._check(body, context)
                return
            if not self.server.allow_write:
                raise PermissionError(
                    "HTTP writes are disabled; restart with --allow-write"
                )
            principal = self._principal()
            if principal is None:
                raise PermissionError(
                    "a valid scoped bearer token is required for writes"
                )
            reason = str(body.get("reason", "HTTP catalog edit"))
            if path == "/add":
                self._call(
                    writes_service.catalog(
                        "add",
                        str(body["kind"]),
                        record=dict(body["record"]),
                        principal=principal,
                        reason=reason,
                    )
                )
            elif path in {"/update", "/delete", "/annotate"}:
                operation = path.removeprefix("/")
                self._call(
                    writes_service.catalog(
                        operation,
                        str(body["kind"]),
                        ident=str(body["id"]),
                        record=dict(body.get("record", body.get("values", {}))),
                        principal=principal,
                        reason=reason,
                    )
                )
            elif path in {"/accept", "/leave-contested", "/acknowledge"}:
                if path == "/acknowledge":
                    document = writes_service.acknowledge(
                        str(body["target"]),
                        source=str(body["source"]),
                        until=str(body["until"]),
                        reason=reason,
                        principal=principal,
                    )
                else:
                    document = writes_service.resolve(
                        "accept-observed" if path == "/accept" else "leave-contested",
                        str(body["target"]),
                        source=str(body["source"]),
                        field=body.get("field"),
                        principal=principal,
                        reason=reason,
                    )
                self._call(document)
            else:
                raise UsageError("unknown API route")
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, exc)
        except (CadastreError, KeyError, TypeError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc)
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, exc)

    def _check(self, body: dict[str, Any], context: ApplicationContext) -> None:
        artifact = body.get("artifact")
        if not isinstance(artifact, str):
            raise UsageError("check needs an artifact string")
        display_path = body.get("path")
        self._call(
            CheckService(context).artifact(
                artifact,
                kind=body.get("kind"),
                display_path=(
                    Path(display_path).name
                    if isinstance(display_path, str) and display_path
                    else None
                ),
            )
        )


class CadastreHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        allow_write: bool,
        tokens: Mapping[str, TokenCredential] | None = None,
        require_auth: bool = False,
        *,
        allowed_hosts: tuple[str, ...] = (),
        allowed_origins: tuple[str, ...] = (),
        max_body_bytes: int = 1_048_576,
        rate_limit: int = 120,
        audit_path: Path | None = None,
        audit_required: bool | None = None,
        proxy: ProxyConfig | None = None,
        profile: str = "loopback-development",
        tls_enabled: bool = False,
        tls_certfile: Path | None = None,
        tls_keyfile: Path | None = None,
        tls_ca_file: Path | None = None,
        proxy_scopes: Mapping[str, frozenset[str]] | None = None,
        mtls_scopes: Mapping[str, frozenset[str]] | None = None,
        audience: str = "cadastre",
    ) -> None:
        super().__init__(address, _Handler)
        if address[0] not in {"127.0.0.1", "::1", "localhost"} and not require_auth:
            raise UsageError("non-loopback HTTP servers require authentication")
        self.root = root
        self.allow_write = allow_write
        self.tokens: dict[str, TokenCredential] = dict(tokens or {})
        self.require_auth = require_auth
        self.allowed_hosts = tuple(allowed_hosts)
        self.allowed_origins = tuple(allowed_origins)
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
        self.proxy_scopes = dict(proxy_scopes or {})
        self.mtls_scopes = dict(mtls_scopes or {})
        self.audience = audience
        self.authorizer = Authorizer(
            AuthConfig(require_auth=require_auth, audience=audience),
            tokens=self.tokens,
            proxy_scopes=self.proxy_scopes,
            mtls_scopes=self.mtls_scopes,
        )
        self.profile = profile
        self.tls_enabled = tls_enabled
        self.tls_certfile = tls_certfile
        self.tls_keyfile = tls_keyfile
        self.tls_ca_file = tls_ca_file

    def validate_request(self, headers: Any, address: str) -> None:
        if not self.rate_limiter.allow(address):
            raise PermissionError("request rate limit exceeded")
        host = str(headers.get("Host", "")).split(":", 1)[0].lower()
        allowed = {item.lower().split(":", 1)[0] for item in self.allowed_hosts}
        if allowed and host not in allowed:
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
        token = header[7:]
        credential = self.tokens.get(token)
        if credential is None:
            return None
        now = datetime.now(tz=UTC)
        requested_audience = headers.get("X-Cadastre-Audience", self.audience)
        if credential.valid(now=now, audience=requested_audience):
            return credential.principal
        return None

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

    def security_kwargs(self) -> dict[str, Any]:
        return {
            "bind": "{}:{}".format(*self.server_address[:2]),
            "tls": self.tls_enabled,
            "profile": self.profile,
            "require_auth": self.require_auth,
            "scopes": set().union(*(item.scopes for item in self.tokens.values())),
            "certfile": self.tls_certfile,
            "keyfile": self.tls_keyfile,
            "ca_file": self.tls_ca_file,
            "proxy": self.proxy,
            "audience": self.audience,
        }


def serve(
    root: Path,
    *,
    bind: str = "127.0.0.1:8000",
    allow_write: bool = False,
    allow_non_loopback: bool = False,
    require_auth: bool = False,
    tokens: Mapping[str, TokenCredential] | None = None,
    tls_certfile: Path | None = None,
    tls_keyfile: Path | None = None,
    tls_ca_file: Path | None = None,
    require_client_cert: bool = False,
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
    audit_path: Path | None = None,
    profile: str = "loopback-development",
    proxy: ProxyConfig | None = None,
    proxy_scopes: Mapping[str, frozenset[str]] | None = None,
    mtls_scopes: Mapping[str, frozenset[str]] | None = None,
    audience: str = "cadastre",
) -> None:
    host, raw_port = bind.rsplit(":", 1)
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    trusted_proxy = (
        profile == "trusted-proxy" and proxy is not None and bool(proxy.networks)
    )
    if not allow_non_loopback and not loopback:
        raise UsageError("non-loopback binds require an explicit operator decision")
    tls = tls_certfile is not None or tls_keyfile is not None
    if tls != (tls_certfile is not None and tls_keyfile is not None):
        raise UsageError("TLS requires both --tls-cert and --tls-key")
    if not loopback and not tls and not trusted_proxy:
        raise UsageError("non-loopback binds require TLS")
    context = (
        tls_context(
            tls_certfile,  # type: ignore[arg-type]
            tls_keyfile,  # type: ignore[arg-type]
            ca_file=tls_ca_file,
            require_client_cert=require_client_cert,
        )
        if tls
        else None
    )
    server = CadastreHTTPServer(
        (host, int(raw_port)),
        root,
        allow_write,
        tokens,
        require_auth,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        audit_path=audit_path,
        profile=profile,
        tls_enabled=tls,
        tls_certfile=tls_certfile,
        tls_keyfile=tls_keyfile,
        tls_ca_file=tls_ca_file,
        proxy=proxy,
        proxy_scopes=proxy_scopes,
        mtls_scopes=mtls_scopes,
        audience=audience,
    )
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = ["CadastreHTTPServer", "openapi_schema", "serve"]
