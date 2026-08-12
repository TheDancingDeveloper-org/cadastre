"""Self-hosted CI collector (Woodpecker).

The primary CI, and one side of policy-scoped secret-store replication checks.
The collector returns names and references only, never secret values.

Read-only, and `ci.trigger` is deliberately not implemented — the runner
refuses write methods anyway, but a collector should not carry the code.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "ci-woodpecker"
VERSION = "1"
CAPABILITIES = ("CI", "SecretRef")


def transform_pipelines(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Repo list -> pipeline entities, one per configured pipeline file."""
    system = str(options.get("system") or "ci-selfhosted")
    pipelines = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or item.get("name") or "")
        if not full_name:
            continue
        repo_id = full_name.replace("/", "-")
        pipelines.append(
            {
                "id": f"{repo_id}-{system}",
                "repo": repo_id,
                "system": system,
                "file": str(item.get("config_file") or ".woodpecker.yaml"),
            }
        )
    return {"entities": {"pipeline": sorted(pipelines, key=lambda p: str(p["id"]))}}


def transform_secret_names(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    store = str(options.get("store") or "ci-store")
    # Same reason as the secrets collector: the estate decides what a reference
    # looks like. Without a prefix this emits bare `git_auth_token`, which is a
    # different spelling of a secret the manager holds as
    # `infisical://cicd/prod/GIT_AUTH_TOKEN` — and a catalog that declares the
    # prefixed form then sees both spellings in the same store.
    prefix = str(options.get("ref_prefix") or "")
    names = sorted(
        {
            prefix + str(item.get("name"))
            for item in payload or []
            if isinstance(item, dict)
        }
    )
    return {"extra": {"secret_names": {store: names}}}


def _repos(endpoint: Endpoint) -> list[Any]:
    payload = get_json(endpoint, "/api/user/repos")
    return payload if isinstance(payload, list) else []


def _pipelines(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config)
    return ok(
        transform_pipelines(_repos(endpoint), request.config),
        format_timestamp(datetime.now(tz=UTC)),
    )


def _secrets(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config)
    scope = request.config.get("org")
    path = f"/api/orgs/{scope}/secrets" if scope else "/api/secrets"
    payload = get_json(endpoint, path)
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
            "ci.pipelines": _pipelines,
            "secret.list": _secrets,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
