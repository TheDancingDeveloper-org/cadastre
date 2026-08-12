"""Operator diagnostic for the selected network security profile."""

from __future__ import annotations

from pathlib import Path

from cadastre.adapters.security import proxy_from_file, security_report
from cadastre.render.document import Document


def security_check(
    *,
    bind: str,
    profile: str,
    require_auth: bool,
    scopes: tuple[str, ...],
    certfile: Path | None = None,
    keyfile: Path | None = None,
    ca_file: Path | None = None,
    proxy_networks: tuple[str, ...] = (),
    proxy_secret_file: Path | None = None,
) -> Document:
    report = security_report(
        bind=bind,
        tls=certfile is not None and keyfile is not None,
        profile=profile,
        require_auth=require_auth,
        scopes=set(scopes),
        certfile=certfile,
        keyfile=keyfile,
        ca_file=ca_file,
        proxy=proxy_from_file(list(proxy_networks), proxy_secret_file),
    )
    return Document(
        title="cadastre security-check",
        data=report,
        exit_code=0 if report["ready"] else 1,
    )
