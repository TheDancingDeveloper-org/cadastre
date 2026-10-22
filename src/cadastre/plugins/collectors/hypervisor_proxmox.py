"""Hypervisor collector (Proxmox).

Host and VM facts: what exists, what it is called, and how much of it there is.
Nothing is installed on the hypervisor — this calls its API from the collector
host.

Auth is an API token, which Proxmox sends in an `Authorization` header with its
own scheme; both are configurable rather than hard-coded, since the same shape
covers several appliances. A `PVEAuditor` token is enough for everything read
here.

Beyond the neutral `host` fields, each entity carries an `x-proxmox` block:
guests their vmid, kind, status, tags, live usage and addresses (from the QEMU
guest agent or the LXC interface list); nodes their storage pools and disks
with SMART health. Address and disk calls are best-effort per guest or node: a
guest without a running agent simply has no addresses, and never fails the
collection.

Guests are linked to other sources by id: a guest's Proxmox name is its host
id, which is what the Komodo and tailnet collectors use too. Where the names
differ, `config.id_map` renames a Proxmox name to the catalog id.
"""

from __future__ import annotations

import ipaddress
import sys
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "hypervisor-proxmox"
VERSION = "2"
CAPABILITIES = ("Inventory",)

_BYTES_PER_GB = 1024**3

#: Fetch a PVE API path (under /api2/json) and return its `data`.
Fetch = Callable[[str], Any]


def _gb(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, round(number / _BYTES_PER_GB)) if number else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tags(raw: Any) -> list[str]:
    # PVE joins tags with `;` (and accepts `,` or spaces on input).
    text = str(raw or "").replace(",", ";").replace(" ", ";")
    return sorted({t for t in text.split(";") if t})


def _usable_address(text: str) -> str | None:
    address = text.split("/", 1)[0]
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_link_local:
        return None
    return str(parsed)


def _agent_addresses(payload: Any) -> list[str]:
    """QEMU guest agent `network-get-interfaces` -> addresses."""
    if isinstance(payload, dict):
        payload = payload.get("result")
    found: set[str] = set()
    for iface in payload or []:
        if not isinstance(iface, dict):
            continue
        for entry in iface.get("ip-addresses") or []:
            if isinstance(entry, dict):
                address = _usable_address(str(entry.get("ip-address") or ""))
                if address:
                    found.add(address)
    return sorted(found)


def _lxc_addresses(payload: Any) -> list[str]:
    """LXC `/interfaces` -> addresses."""
    found: set[str] = set()
    for iface in payload or []:
        if not isinstance(iface, dict):
            continue
        for key in ("inet", "inet6"):
            address = _usable_address(str(iface.get(key) or ""))
            if address:
                found.add(address)
        for entry in iface.get("ip-addresses") or []:
            if isinstance(entry, dict):
                address = _usable_address(str(entry.get("ip-address") or ""))
                if address:
                    found.add(address)
    return sorted(found)


def _disks(payload: Any) -> list[dict[str, Any]]:
    disks = []
    for disk in payload or []:
        if not isinstance(disk, dict) or not disk.get("devpath"):
            continue
        entry: dict[str, Any] = {"devpath": str(disk["devpath"])}
        for key in ("model", "serial", "type", "health"):
            if disk.get(key):
                entry[key] = str(disk[key])
        size = _gb(disk.get("size"))
        if size:
            entry["size_gb"] = size
        wearout = _int(disk.get("wearout"))
        if wearout is not None:
            entry["wearout"] = wearout
        disks.append(entry)
    disks.sort(key=lambda d: str(d["devpath"]))
    return disks


def _storage(item: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": str(item.get("storage") or item.get("id"))}
    for key in ("plugintype", "status", "content"):
        if item.get(key):
            entry[key] = str(item[key])
    used, total = _int(item.get("disk")), _int(item.get("maxdisk"))
    if total:
        entry["total_gb"] = _gb(total)
        entry["used_gb"] = round((used or 0) / _BYTES_PER_GB)
        entry["used_percent"] = round(100 * (used or 0) / total, 1)
    if item.get("shared"):
        entry["shared"] = True
    return entry


def _guest_x(item: dict[str, Any], kind: str) -> dict[str, Any]:
    x: dict[str, Any] = {"type": kind}
    vmid = _int(item.get("vmid"))
    if vmid is not None:
        x["vmid"] = vmid
    for key in ("node", "status"):
        if item.get(key):
            x[key] = str(item[key])
    tags = _tags(item.get("tags"))
    if tags:
        x["tags"] = tags
    usage: dict[str, Any] = {}
    cpu = item.get("cpu")
    if isinstance(cpu, (int, float)):
        usage["cpu_percent"] = round(100 * float(cpu), 1)
    for key, out in (("mem", "memory_gb_used"), ("disk", "disk_gb_used")):
        value = _int(item.get(key))
        if value:
            usage[out] = round(value / _BYTES_PER_GB, 1)
    uptime = _int(item.get("uptime"))
    if uptime:
        usage["uptime_seconds"] = uptime
    if usage:
        x["usage"] = usage
    return x


def transform(
    payload: Any,
    options: dict[str, Any],
    *,
    fetch: Fetch | None = None,
) -> dict[str, Any]:
    """Cluster resources -> host entities, guests and nodes alike.

    With `fetch`, each running guest is asked for its addresses and each node
    for its disks; without it only `/cluster/resources` is read.
    """
    hypervisor = options.get("hypervisor")
    network = options.get("network")
    id_map = {str(k): str(v) for k, v in (options.get("id_map") or {}).items()}
    items = payload.get("data") if isinstance(payload, dict) else payload
    items = [item for item in items or [] if isinstance(item, dict)]

    storage_by_node: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("type") == "storage" and item.get("node"):
            storage_by_node.setdefault(str(item["node"]), []).append(_storage(item))

    hosts = []
    for item in items:
        kind = str(item.get("type") or "")
        if kind not in ("qemu", "lxc", "node"):
            continue
        name = str(item.get("name") or item.get("node") or "")
        if not name:
            continue
        entity: dict[str, Any] = {
            "id": id_map.get(name, name),
            "role": "hypervisor" if kind == "node" else "server",
        }
        if kind != "node":
            parent = hypervisor or item.get("node")
            if parent:
                entity["hosted_in"] = id_map.get(str(parent), str(parent))
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

        if kind == "node":
            x: dict[str, Any] = {"type": "node"}
            if item.get("status"):
                x["status"] = str(item["status"])
            storage = sorted(storage_by_node.get(name, []), key=lambda s: s["name"])
            if storage:
                x["storage"] = storage
            if fetch is not None:
                disks = _try(fetch, f"/nodes/{_q(name)}/disks/list")
                if disks is not None:
                    x["disks"] = _disks(disks)
        else:
            x = _guest_x(item, kind)
            if (
                fetch is not None
                and item.get("status") == "running"
                and item.get("node")
                and item.get("vmid") is not None
                and not item.get("template")
            ):
                base = f"/nodes/{_q(item['node'])}/{kind}/{_q(item['vmid'])}"
                if kind == "qemu":
                    found = _try(fetch, base + "/agent/network-get-interfaces")
                    addresses = _agent_addresses(found)
                else:
                    addresses = _lxc_addresses(_try(fetch, base + "/interfaces"))
                if addresses:
                    x["addresses"] = addresses
        entity["x-proxmox"] = x
        hosts.append(entity)
    hosts.sort(key=lambda h: str(h["id"]))
    return {"entities": {"host": hosts}}


def _q(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


def _try(fetch: Fetch, path: str) -> Any:
    """Best-effort detail call: a missing agent or permission is not fatal."""
    try:
        return fetch(path)
    except HttpError:
        return None


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config(
        request.config,
        default_scheme=str(request.config.get("auth_scheme", "PVEAPIToken=")),
    )

    def fetch(path: str) -> Any:
        body = get_json(endpoint, "/api2/json" + path)
        return body.get("data") if isinstance(body, dict) else body

    payload = get_json(endpoint, "/api2/json/cluster/resources")
    return ok(
        transform(payload, request.config, fetch=fetch),
        format_timestamp(datetime.now(tz=UTC)),
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
