"""VPN collector (Tailscale).

Supplies `network` membership and `reachable_from`. Note the vocabulary
boundary: the word *tailnet* appears in this file and nowhere else. What leaves
is `network: {class: private}` (DESIGN §2.4).

Must run where it can reach the API; the device list comes from the control
plane, not from the local daemon, so the collector host does not need to be on
the tailnet itself.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "vpn-tailscale"
VERSION = "1"
CAPABILITIES = ("Network",)

DEFAULT_ENDPOINT = "https://api.tailscale.com"


def transform(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Device list -> a network entity and the hosts reachable from it."""
    network_id = str(options.get("network") or "vpn-0")
    devices = payload.get("devices") if isinstance(payload, dict) else payload
    hosts = []
    for item in devices or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("hostname") or item.get("name") or "")
        if not name:
            continue
        entity: dict[str, Any] = {
            "id": name.split(".")[0],
            "role": str(options.get("role") or "server"),
            "reachable_from": [network_id],
        }
        tags = [
            str(tag).removeprefix("tag:")
            for tag in item.get("tags") or []
            if isinstance(tag, str)
        ]
        if tags:
            entity["tags"] = sorted(tags)
        hosts.append(entity)
    hosts.sort(key=lambda h: str(h["id"]))
    return {
        "entities": {
            "network": [{"id": network_id, "class": "private"}],
            "host": hosts,
        }
    }


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config({"endpoint": DEFAULT_ENDPOINT, **request.config})
    scope = request.config.get("tailnet") or "-"
    payload = get_json(endpoint, f"/api/v2/tailnet/{scope}/devices")
    return ok(
        transform(payload, request.config), format_timestamp(datetime.now(tz=UTC))
    )


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "network.list": _collect,
            "network.members": _collect,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
