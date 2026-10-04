"""Secret-manager collector (Infisical).

**Names and existence only.** The API returns values; this collector drops them
before anything else in the process can see them, and there is a test that
fails if a value ever reaches the output. No exceptions, including "just for
local dev" (AGENTS.md, the lines that do not move).

What it delivers is the other half of the secret-name diff: references present
in the secret manager versus references present in the CI secret store.

It also delivers, per secret, an `x-secret-store` block: where the secret lives
(project id and slug, environment, path, server) and the *shape* of its value
(length, newline/CR counts, whether it parses as JSON — `core.secretshape`).
The shape is computed here, in the collector process, from the value the list
call already returns, and the value is dropped in the same expression. The
query layer never sees a value (DESIGN §8: "Secret values never transit the
query layer"); `secret_describe` reads this block.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.core.secretshape import describe_value
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


def _location(options: dict[str, Any]) -> dict[str, Any]:
    """Where this source's secrets live: the facts a consumer needs to fetch one.

    The project id is the store's own identifier — the thing an API call or an
    agent-auth manifest line needs, and the thing a bare `store://project/...`
    reference does not carry.
    """
    location: dict[str, Any] = {}
    for key, option in (
        ("project_id", "workspace_id"),
        ("project_slug", "project_slug"),
        ("server", "endpoint"),
    ):
        value = options.get(option)
        if isinstance(value, str) and value:
            location[key] = value.rstrip("/") if key == "server" else value
    return location


def _store_block(
    item: dict[str, Any],
    key: str,
    path: str,
    environment: str,
    location: dict[str, Any],
) -> dict[str, Any]:
    block: dict[str, Any] = {
        **location,
        "environment": environment,
        "path": path,
        "key": key,
    }
    version = item.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        block["version"] = version
    for field, names in (
        ("updated_at", ("updatedAt", "updated_at")),
        ("created_at", ("createdAt", "created_at")),
    ):
        stamp = next(
            (item[name] for name in names if isinstance(item.get(name), str)), None
        )
        if stamp:
            block[field] = stamp
    raw = next(
        (
            item[name]
            for name in ("secretValue", "secret_value", "value")
            if name in item
        ),
        None,
    )
    # The value is read once, reduced to counts, and never bound to anything
    # that outlives this expression.
    if isinstance(raw, str):
        block["shape"] = describe_value(raw)
    return block


def transform(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """API response -> secret entities. Names, paths, rotation dates, shapes.

    No values: a value is reduced to `core.secretshape` counts inside
    `_store_block` and never copied.
    """
    location = _location(options)
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
            "x-secret-store": _store_block(item, key, path, environment, location),
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


def _project_slug(endpoint: Endpoint, workspace: str) -> str | None:
    """Best effort: the project's human slug. A token scoped to secrets only may
    not read project metadata, and that must not fail the collection."""
    try:
        payload = get_json(endpoint, f"/api/v1/workspace/{workspace}")
    except HttpError:
        return None
    project = payload.get("workspace") if isinstance(payload, dict) else None
    slug = project.get("slug") if isinstance(project, dict) else None
    return slug if isinstance(slug, str) and slug else None


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
    config = dict(request.config)
    if workspace and not config.get("project_slug"):
        slug = _project_slug(endpoint, str(workspace))
        if slug:
            config["project_slug"] = slug
    result = transform(payload, config)
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
