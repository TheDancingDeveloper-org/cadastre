"""Secret-manager collector (Infisical).

**Names and existence only.** The API returns values; this collector drops them
before anything else in the process can see them, and there is a test that
fails if a value ever reaches the output. No exceptions, including "just for
local dev" (AGENTS.md, the lines that do not move).

What it delivers is the other half of the secret-name diff: references present
in the secret manager versus references present in the CI secret store.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "secrets-infisical"
VERSION = "1"
CAPABILITIES = ("SecretRef",)

#: Every key an API response might carry a secret value in. Dropped by name
#: rather than filtered by shape: a value that changes shape must not slip
#: through, and a new key that carries values is a change to this list.
_VALUE_KEYS = frozenset(
    {
        "secretValue",
        "secret_value",
        "value",
        "plaintext",
        "secretValueCiphertext",
        "secretValueIV",
        "secretValueTag",
    }
)


def transform(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """API response -> secret entities. Names, paths, rotation dates. No values."""
    store = str(options.get("store") or "secrets-manager")
    environment = str(options.get("environment") or "prod")
    # The estate decides what a secret reference looks like, not this collector.
    # Default "/" keeps the bare `/env/path/KEY` shape; an estate whose
    # convention names the store and project sets it to e.g.
    # "infisical://cicd/" and gets refs that match its own `secret_ref` regex.
    prefix = str(options.get("ref_prefix") or "/")
    items = payload.get("secrets") if isinstance(payload, dict) else payload
    secrets = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("secretKey") or item.get("key") or "")
        if not key:
            continue
        path = str(item.get("secretPath") or options.get("path") or "/")
        ref = "/".join(part for part in [environment, path.strip("/"), key] if part)
        entity: dict[str, Any] = {
            "id": f"{store}-{key.lower()}".replace("_", "-"),
            "ref": prefix + ref.lstrip("/"),
            "store": store,
        }
        updated = item.get("updatedAt") or item.get("updated_at")
        if isinstance(updated, str) and updated:
            entity["last_rotated"] = updated[:10]
        secrets.append(entity)
    secrets.sort(key=lambda s: str(s["ref"]))
    return {
        "entities": {"secret": secrets},
        "extra": {"secret_names": {store: [str(s["ref"]) for s in secrets]}},
        # One Infisical project is not evidence about another. Without this,
        # an estate with three projects has each declared secret checked
        # against all three sources and reported `missing` from the two it was
        # never in — absence claimed well outside what this token can see.
        "coverage": {"secret": {"where": {"store": store}}},
    }


def _assert_no_values(payload: Any) -> None:
    """Belt and braces: fail loudly rather than emit a value by accident."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _VALUE_KEYS:
                raise HttpError(
                    "internal",
                    "a secret value reached the transform output; refusing to "
                    "return it (DESIGN §1.3)",
                )
            _assert_no_values(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_values(item)


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config)
    workspace = request.config.get("workspace_id")
    if not workspace:
        raise HttpError("invalid_config", "config.workspace_id is required")
    payload = get_json(
        endpoint,
        "/api/v3/secrets/raw",
        {
            "workspaceId": workspace,
            "environment": request.config.get("environment", "prod"),
            "secretPath": request.config.get("path", "/"),
        },
    )
    result = transform(payload, request.config)
    _assert_no_values(result)
    return ok(result, format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "secret.list": _collect,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
