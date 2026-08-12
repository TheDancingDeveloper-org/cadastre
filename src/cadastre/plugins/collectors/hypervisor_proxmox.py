"""Hypervisor collector (Proxmox).

Host and VM facts: what exists, what it is called, and how much of it there is.
Nothing is installed on the hypervisor — this calls its API from the collector
host.

Auth is an API token, which Proxmox sends in an `Authorization` header with its
own scheme; both are configurable rather than hard-coded, since the same shape
covers several appliances.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "hypervisor-proxmox"
VERSION = "1"
CAPABILITIES = ("Inventory",)

_BYTES_PER_GB = 1024**3


def _gb(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, round(number / _BYTES_PER_GB)) if number else None


def transform(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Cluster resources -> host entities, guests and nodes alike."""
    hypervisor = options.get("hypervisor")
    network = options.get("network")
    items = payload.get("data") if isinstance(payload, dict) else payload
    hosts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind not in ("qemu", "lxc", "node"):
            continue
        name = str(item.get("name") or item.get("node") or "")
        if not name:
            continue
        entity: dict[str, Any] = {
            "id": name,
            "role": "hypervisor" if kind == "node" else "server",
        }
        if kind != "node" and hypervisor:
            entity["hosted_in"] = str(hypervisor)
        if network:
            entity["reachable_from"] = [str(network)]
        resources = {
            "cpu_cores": item.get("maxcpu"),
            "memory_gb": _gb(item.get("maxmem")),
            "disk_gb": _gb(item.get("maxdisk")),
        }
        resources = {k: v for k, v in resources.items() if v}
        if resources:
            entity["resources"] = resources
        if item.get("template"):
            entity["tags"] = ["template"]
        hosts.append(entity)
    hosts.sort(key=lambda h: str(h["id"]))
    return {"entities": {"host": hosts}}


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config(
        request.config,
        default_scheme=str(request.config.get("auth_scheme", "PVEAPIToken=")),
    )
    payload = get_json(endpoint, "/api2/json/cluster/resources")
    return ok(
        transform(payload, request.config), format_timestamp(datetime.now(tz=UTC))
    )


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
