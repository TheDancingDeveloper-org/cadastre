"""CLI entry point for the standard MCP Streamable HTTP adapter."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from cadastre.adapters.security import (
    parse_scope_bindings,
    parse_token_file,
    proxy_from_file,
    tls_context,
)
from cadastre.mcp.streamable import MCPHTTPServer
from cadastre.render.document import Document


def serve(
    root: Path,
    *,
    bind: str,
    token_file: Path | None,
    allow_non_loopback: bool,
    allow_write: bool = False,
    tls_certfile: Path | None = None,
    tls_keyfile: Path | None = None,
    tls_ca_file: Path | None = None,
    require_client_cert: bool = False,
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
    audit_path: Path | None = None,
    proxy_networks: tuple[str, ...] = (),
    proxy_secret_file: Path | None = None,
    proxy_scopes: list[str] | None = None,
    mtls_scopes: list[str] | None = None,
    audience: str = "cadastre",
) -> Document:
    from cadastre.core.storage import initialize, startup_check

    initialize(root)
    startup_check(root)
    host, raw_port = bind.rsplit(":", 1)
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not allow_non_loopback:
        raise ValueError("remote MCP binds require --allow-non-loopback")
    if (tls_certfile is None) != (tls_keyfile is None):
        raise ValueError("MCP TLS requires both --tls-cert and --tls-key")
    if tls_certfile is not None:
        assert tls_keyfile is not None
    cert_path = tls_certfile
    key_path = tls_keyfile
    context = (
        tls_context(
            cast(Path, cert_path),
            cast(Path, key_path),
            ca_file=tls_ca_file,
            require_client_cert=require_client_cert,
        )
        if tls_certfile is not None
        else None
    )
    if not loopback and context is None:
        raise ValueError("remote MCP requires TLS")
    server = MCPHTTPServer(
        (host, int(raw_port)),
        root,
        allow_write=allow_write,
        tokens=(parse_token_file(token_file, allow_legacy=False) if token_file else {}),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        audit_path=audit_path,
        proxy=proxy_from_file(list(proxy_networks), proxy_secret_file),
        proxy_scopes=parse_scope_bindings(proxy_scopes or []),
        mtls_scopes=parse_scope_bindings(mtls_scopes or []),
        audience=audience,
    )
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return Document(title="cadastre mcp-http", data={"stopped": True})
