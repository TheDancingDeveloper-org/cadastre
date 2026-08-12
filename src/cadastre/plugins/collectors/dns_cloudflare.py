"""DNS collector (Cloudflare).

**First among the remaining plugins, and the highest-value credential to
create for this project.** Where DNS is dashboard-managed there is no
machine-readable desired state at all, and it is the system where a wrong agent
guess is both most likely and most externally visible.

Needs a read-only zone-read token. Not zone-edit — a collector credential that
can edit DNS turns a topology disclosure into a mutation (DESIGN §6).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "dns-cloudflare"
VERSION = "1"
CAPABILITIES = ("DNS",)

DEFAULT_ENDPOINT = "https://api.cloudflare.com"
PAGE_SIZE = 100
MAX_PAGES = 20

_SLUG = re.compile(r"[^a-z0-9]+")

#: Record types Cadastre models. A TXT record's content is attacker-controllable
#: text that would land in a model's context, so it is carried as data and
#: rendered inert (DESIGN §6) — never dropped, never interpolated.
KEPT_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "SRV", "NS")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")


def transform_zones(payload: Any) -> dict[str, Any]:
    zones = payload.get("result") if isinstance(payload, dict) else payload
    return {
        "extra": {
            "zones": sorted(
                str(zone.get("name"))
                for zone in zones or []
                if isinstance(zone, dict) and zone.get("name")
            )
        }
    }


def transform_records(payload: Any, zone_name: str) -> dict[str, Any]:
    records = payload.get("result") if isinstance(payload, dict) else payload
    candidates: list[tuple[str, dict[str, Any]]] = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        record_type = str(item.get("type") or "")
        if record_type not in KEPT_TYPES:
            continue
        name = str(item.get("name") or "")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity:
            identity = hashlib.sha256(
                json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:12]
        base = f"{_slug(name)}-{record_type.lower()}"
        entity: dict[str, Any] = {
            "id": f"{base}-{_slug(identity)}",
            "zone": zone_name,
            "name": name,
            "type": record_type,
        }
        content = item.get("content")
        if isinstance(content, str) and content:
            entity["value"] = content
        if item.get("proxied"):
            entity["tags"] = ["proxied"]
        candidates.append((base, entity))
    # Preserve existing compact IDs for singleton name/type pairs.  Multiple
    # records retain distinct, stable upstream-ID/digest suffixes instead of
    # collapsing into one catalog entity.
    counts: dict[str, int] = {}
    for base, _ in candidates:
        counts[base] = counts.get(base, 0) + 1
    domains = []
    for base, entity in candidates:
        if counts[base] == 1:
            entity["id"] = base
        domains.append(entity)
    domains.sort(key=lambda d: str(d["id"]))
    return {"entities": {"domain": domains}}


def _endpoint(config: dict[str, Any]) -> Endpoint:
    return Endpoint.from_config({"endpoint": DEFAULT_ENDPOINT, **config})


def _zones(endpoint: Endpoint, config: dict[str, Any]) -> list[dict[str, Any]]:
    payload = get_json(endpoint, "/client/v4/zones", {"per_page": PAGE_SIZE})
    zones = payload.get("result") if isinstance(payload, dict) else payload
    wanted = {str(z) for z in (config.get("zones") or ())}
    return [
        zone
        for zone in zones or []
        if isinstance(zone, dict) and (not wanted or str(zone.get("name")) in wanted)
    ]


def _list_zones(request: Request) -> Reply:
    endpoint = _endpoint(request.config)
    payload = get_json(endpoint, "/client/v4/zones", {"per_page": PAGE_SIZE})
    return ok(transform_zones(payload), format_timestamp(datetime.now(tz=UTC)))


def _list_records(request: Request) -> Reply:
    endpoint = _endpoint(request.config)
    domains: list[Any] = []
    visited: set[str] = set()
    for zone in _zones(endpoint, request.config):
        zone_id, zone_name = zone.get("id"), str(zone.get("name"))
        if not zone_id:
            continue
        visited.add(zone_name)
        for page in range(1, MAX_PAGES + 1):
            payload = get_json(
                endpoint,
                f"/client/v4/zones/{zone_id}/dns_records",
                {"per_page": PAGE_SIZE, "page": page},
            )
            batch = transform_records(payload, zone_name)["entities"]["domain"]
            domains.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
    # Absence is only evidence for the zones actually enumerated. A token
    # scoped to one zone (or a `zones:` narrowing in plugins.yaml) must not
    # make every declared record in every OTHER zone read as `missing`.
    result: dict[str, Any] = {"entities": {"domain": domains}}
    if visited:
        result["coverage"] = {"domain": {"where": {"zone": sorted(visited)}}}
    return ok(result, format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "dns.zones": _list_zones,
            "dns.records": _list_records,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
