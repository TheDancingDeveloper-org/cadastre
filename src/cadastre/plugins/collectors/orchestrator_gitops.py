"""Orchestrator collector (GitOps controller).

Added by the estate audit: a GitOps controller already reconciles a desired-state
repository against live container state, so it is the deployment source of
truth. **Cadastre sits above it.** The reconciliation niche is occupied, and
DESIGN §1.3 is not negotiable here — this reads the ops repo and reports; it
never writes to it and never reconciles.

**D2 is resolved to `observed/`.** The ops repository is another system's
desired state, not ours. Putting it in `declared/` would make Cadastre's own
statement of intent a copy of somebody else's, and drift between the two would
become invisible — which is precisely the divergence worth seeing.

Config:

```yaml
config:
  path: /srv/ops-repo        # a local checkout, read-only
  host_from: directory       # or: label — where the target host comes from
```
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre.core.artifacts import CADASTRE_KEY
from cadastre.core.provenance import format_timestamp
from cadastre.core.yamlio import load_yaml
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import HttpError
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "orchestrator-gitops"
VERSION = "1"
CAPABILITIES = ("Inventory",)

COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def transform_stack(
    data: Any, *, stack: str, host: str | None, options: dict[str, Any]
) -> dict[str, Any] | None:
    """One compose file from the ops repo -> one stack-level service entity.

    The catalog's declared services are curated at estate altitude; emitting
    one observed service per compose service produced 122 rows of
    compose-service-name noise that could never converge (§2e). The compose
    file's service/container inventory survives, just one altitude down, in
    the `x-orchestrator` attribute block.
    """
    if not isinstance(data, dict):
        return None
    top = data.get(CADASTRE_KEY) if isinstance(data.get(CADASTRE_KEY), dict) else {}
    top = top or {}
    services = data.get("services")
    if not isinstance(services, dict):
        return None
    compose_services: list[dict[str, Any]] = []
    for name, raw in sorted(services.items()):
        if not isinstance(raw, dict):
            continue
        nested = raw.get(CADASTRE_KEY)
        per_service = nested if isinstance(nested, dict) else {}
        target = per_service.get("host") or top.get("host") or host
        entry: dict[str, Any] = {"name": str(per_service.get("name") or name)}
        if target:
            entry["runs_on"] = str(target)
        expose = per_service.get("expose") or top.get("expose")
        if expose:
            entry["expose"] = str(expose)
        if options.get("repo"):
            entry["repo"] = str(options["repo"])
        compose_services.append(entry)
    if not compose_services:
        return None
    stack_host = top.get("host") or host
    entity: dict[str, Any] = {
        "id": stack,
        "x-orchestrator": {"compose_services": compose_services},
    }
    if stack_host:
        entity["runs_on"] = str(stack_host)
    expose = top.get("expose")
    if expose:
        entity["expose"] = str(expose)
    if options.get("repo"):
        entity["repo"] = str(options["repo"])
    return entity


def scan(root: Path, options: dict[str, Any]) -> dict[str, Any]:
    """Walk a local checkout of the ops repo. Read-only, and it says so."""
    services: list[dict[str, Any]] = []
    for name in COMPOSE_NAMES:
        for path in sorted(root.rglob(name)):
            relative = path.relative_to(root)
            stack = relative.parent.name or "root"
            # `runs_on` is NOT derived from the directory unless asked for.
            # A GitOps repo's directory name is the stack, and only sometimes
            # also the host — in the estate this was built against it never is,
            # so the default produced `runs_on: forgejo` for the forgejo stack
            # and a false divergence for every service. Guessing a host from a
            # path is precisely the plausible-and-wrong answer this project
            # exists to replace, so it is opt-in: set `host_from: directory`
            # where the layout really does name hosts.
            host = stack if options.get("host_from") == "directory" else None
            entity = transform_stack(
                load_yaml(path, rel=str(relative)),
                stack=stack,
                host=host,
                options=options,
            )
            if entity is not None:
                services.append(entity)
    seen: set[str] = set()
    unique = []
    for service in sorted(services, key=lambda s: str(s["id"])):
        if service["id"] in seen:
            continue
        seen.add(str(service["id"]))
        unique.append(service)
    return {"entities": {"service": unique}}


def _collect(request: Request) -> Reply:
    raw_path = request.config.get("path")
    if not raw_path:
        raise HttpError(
            "invalid_config", "config.path (the ops repo checkout) is required"
        )
    root = Path(str(raw_path)).expanduser()
    if not root.is_dir():
        raise HttpError("unreachable", f"no such directory: {root}")
    return ok(scan(root, request.config), format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "inventory.list": _collect,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
