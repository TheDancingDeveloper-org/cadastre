"""Hypervisor collector (Hyper-V).

The Proxmox collector's twin for a Hyper-V estate (#35). Host and guest facts:
what exists, what it is called, and how much of it there is. Nothing is
installed on the hypervisor — this reads a JSON inventory of the host's guests
from the collector host.

Hyper-V has no first-party JSON/REST inventory of its own; the canonical
enumeration is PowerShell (`Get-VM | ConvertTo-Json`), which a small read-only
shim on or beside the host exposes over HTTP. The transform is a pure function
of that payload, so the fixture-based test needs no Windows and no network.

Vendor nouns (`Msvm_*`, `Get-VM`, `ComputerName`) stay in this file. What leaves
is `host`, exactly as the Proxmox collector emits it: id, role, `hosted_in`, and
resources. Run-state beyond existence is deliberately not reflected — the
neutral `host` model carries no such field, and `hypervisor-proxmox` reflects
none either; adding one here would put the two hypervisor collectors at odds
over what a `host` observation means.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "hypervisor-hyperv"
VERSION = "1"
CAPABILITIES = ("Inventory",)

_BYTES_PER_GB = 1024**3


def _gb(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, round(number / _BYTES_PER_GB)) if number else None


def _memory_bytes(item: dict[str, Any]) -> Any:
    """Assigned memory is 0 while a guest is off; fall back to the configured
    startup memory so a stopped guest still reports the size it will take."""
    assigned = item.get("MemoryAssigned")
    if _gb(assigned):
        return assigned
    return item.get("MemoryStartup")


def transform(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """`Get-VM` output -> host entities: each guest, and the host it runs on."""
    default_host = options.get("hypervisor")
    network = options.get("network")
    if isinstance(payload, dict):
        items = payload.get("value") or payload.get("data") or payload.get("vms")
        if items is None:
            items = [payload] if payload.get("Name") else []
    else:
        items = payload
    hosts: dict[str, dict[str, Any]] = {}
    hypervisors: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or item.get("VMName") or "")
        if not name:
            continue
        hypervisor = str(item.get("ComputerName") or default_host or "")
        entity: dict[str, Any] = {"id": name, "role": "server"}
        if hypervisor:
            entity["hosted_in"] = hypervisor
            hypervisors.add(hypervisor)
        if network:
            entity["reachable_from"] = [str(network)]
        resources = {
            "cpu_cores": item.get("ProcessorCount"),
            "memory_gb": _gb(_memory_bytes(item)),
            "disk_gb": _gb(item.get("DiskSizeBytes")),
        }
        resources = {k: v for k, v in resources.items() if v}
        if resources:
            entity["resources"] = resources
        hosts[name] = entity
    for hypervisor in hypervisors:
        # A guest may already carry the hypervisor's name (a host running on
        # itself is not a thing); only synthesise the node when nothing else
        # claimed the id.
        hosts.setdefault(hypervisor, {"id": hypervisor, "role": "hypervisor"})
        if hosts[hypervisor].get("role") != "hypervisor":
            hosts[hypervisor]["role"] = "hypervisor"
            hosts[hypervisor].pop("hosted_in", None)
    ordered = sorted(hosts.values(), key=lambda h: str(h["id"]))
    return {"entities": {"host": ordered}}


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config)
    path = str(request.config.get("path") or "/vms")
    payload = get_json(endpoint, path)
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
