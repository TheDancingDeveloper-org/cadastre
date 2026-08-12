"""Self-hosted forge collector (Forgejo/Gitea API).

The primary forge in the reference estate, and the origin side of every
dual-homed repository. Read-only: repository search and, if the token can see
them, the *names* in the repository secret store.

Names only. `secret.list` returns references and existence; no value ever
transits the query layer (DESIGN §1.3).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "forge-forgejo"
VERSION = "1"
CAPABILITIES = ("VCS", "SecretRef")

PAGE_SIZE = 50
MAX_PAGES = 20


def transform_repos(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Forgejo repo search -> repo entities."""
    forge = str(options.get("forge") or "forge-selfhosted")
    mirror_to = options.get("mirror_to")
    items = payload.get("data") if isinstance(payload, dict) else payload
    repos = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or item.get("name") or "")
        if not full_name:
            continue
        entity: dict[str, Any] = {
            "id": full_name.replace("/", "-"),
            "remotes": [
                {
                    "forge": forge,
                    "url": str(item.get("clone_url") or ""),
                    "role": "mirror" if item.get("mirror") else "origin",
                }
            ],
        }
        if item.get("mirror"):
            # The estate mirrors in both directions depending on the repo; which
            # way round is a fact about this repo, not a global setting.
            entity["mirror_from"] = str(options.get("mirror_from") or "forge-public")
        elif mirror_to:
            entity["mirror_to"] = str(mirror_to)
        if item.get("archived"):
            entity["tags"] = ["archived"]
        repos.append(entity)
    return {"entities": {"repo": sorted(repos, key=lambda r: str(r["id"]))}}


def transform_secret_names(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Secret *names* in the forge's own store, for the cross-store diff."""
    store = str(options.get("store") or "forge-secrets")
    items = payload.get("data") if isinstance(payload, dict) else payload
    names = sorted(
        {str(item.get("name")) for item in items or [] if isinstance(item, dict)}
    )
    return {"extra": {"secret_names": {store: names}}}


def _paged(endpoint: Endpoint, path: str) -> list[Any]:
    out: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        payload = get_json(endpoint, path, {"page": page, "limit": PAGE_SIZE})
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not items:
            break
        out.extend(items)
        if len(items) < PAGE_SIZE:
            break
    return out


def _repos(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config, default_scheme="token")
    items = _paged(endpoint, "/api/v1/repos/search")
    return ok(
        transform_repos({"data": items}, request.config),
        format_timestamp(datetime.now(tz=UTC)),
    )


def _secrets(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config, default_scheme="token")
    org = request.config.get("org")
    if not org:
        raise HttpError(
            "invalid_config",
            "config.org is required for secret.list — organisation secrets are "
            "the only ones a read-only token can enumerate",
        )
    payload = get_json(endpoint, f"/api/v1/orgs/{org}/actions/secrets")
    return ok(
        transform_secret_names(payload, request.config),
        format_timestamp(datetime.now(tz=UTC)),
    )


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "vcs.repos": _repos,
            "secret.list": _secrets,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
