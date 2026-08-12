"""CLI entry point for the optional HTTP adapter."""

from __future__ import annotations

from pathlib import Path

from cadastre.adapters.http import serve as run_server
from cadastre.adapters.security import (
    TokenCredential,
    parse_scope_bindings,
    parse_token_file,
    proxy_from_file,
)
from cadastre.render.document import Document


def serve(
    root: Path,
    *,
    bind: str,
    allow_write: bool,
    allow_non_loopback: bool,
    require_auth: bool,
    token_file: Path | None,
    tls_certfile: Path | None = None,
    tls_keyfile: Path | None = None,
    tls_ca_file: Path | None = None,
    require_client_cert: bool = False,
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
    audit_path: Path | None = None,
    profile: str = "loopback-development",
    proxy_networks: tuple[str, ...] = (),
    proxy_secret_file: Path | None = None,
    proxy_scopes: list[str] | None = None,
    mtls_scopes: list[str] | None = None,
    audience: str = "cadastre",
) -> Document:
    # A blank persistent volume is a valid installation.  Initialization is a
    # local storage operation, not an estate mutation, and is done before the
    # listener is opened so readiness cannot race schema creation.
    from cadastre.core.storage import initialize, startup_check

    initialize(root)
    startup_check(root)
    if profile == "mtls" and not require_client_cert:
        raise ValueError("the mtls profile requires --require-client-cert")
    if profile in {"direct-https", "mtls"} and not require_auth:
        raise ValueError(f"the {profile} profile requires --require-auth")
    if profile == "trusted-proxy" and not proxy_networks:
        raise ValueError("the trusted-proxy profile requires --proxy-network")
    tokens: dict[str, TokenCredential] = {}
    if token_file is not None:
        tokens = parse_token_file(
            token_file, allow_legacy=profile == "loopback-development"
        )
    run_server(
        root,
        bind=bind,
        allow_write=allow_write,
        allow_non_loopback=allow_non_loopback,
        require_auth=require_auth,
        tokens=tokens,
        tls_certfile=tls_certfile,
        tls_keyfile=tls_keyfile,
        tls_ca_file=tls_ca_file,
        require_client_cert=require_client_cert,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        audit_path=audit_path,
        profile=profile,
        proxy=proxy_from_file(list(proxy_networks), proxy_secret_file),
        proxy_scopes=parse_scope_bindings(proxy_scopes or []),
        mtls_scopes=parse_scope_bindings(mtls_scopes or []),
        audience=audience,
    )
    return Document(title="cadastre serve", data={"stopped": True})
