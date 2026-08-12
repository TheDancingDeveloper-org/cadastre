"""Ingress collector (Caddy).

First real collector, and the best value-to-effort in the set: the admin API
returns the full running config as JSON, so there is no scraping and no parsing
of a config language. It unblocks the hostname-collision rule in `check`, which
is the most immediately useful validation Cadastre has.

Read-only: it GETs `/config/` and nothing else. The admin API is unauthenticated
on localhost by default, so the usual deployment is a collector running beside
the proxy, or an SSH tunnel — hence `token_env` being optional here.

Config:

```yaml
config:
  endpoint: http://127.0.0.1:2019
  network: edge-net          # which declared network the listeners are on
  ingress_service: ingress   # the declared service doing the fronting
  token_env: CADASTRE_P_INGRESS_TOKEN   # optional
```
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "ingress-caddy"
VERSION = "1"
CAPABILITIES = ("Endpoint",)

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")


def _upstreams(handlers: Any) -> list[str]:
    """Dial addresses behind a route, however deeply the handler nests them."""
    found: list[str] = []
    if isinstance(handlers, dict):
        for upstream in handlers.get("upstreams") or []:
            if isinstance(upstream, dict) and upstream.get("dial"):
                found.append(str(upstream["dial"]))
        for value in handlers.values():
            found.extend(_upstreams(value))
    elif isinstance(handlers, list):
        for item in handlers:
            found.extend(_upstreams(item))
    return found


def _listen_ports(server: dict[str, Any]) -> list[int]:
    ports = []
    for listen in server.get("listen") or []:
        text = str(listen).rsplit(":", 1)[-1]
        if text.isdigit():
            ports.append(int(text))
    return ports or [443]


def transform(config: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Running Caddy config -> endpoint entities.

    Pure: a recorded config in, entities out. This is the whole reason the
    fixture tests can cover the collector without a live proxy.
    """
    network = str(options.get("network") or "")
    fronted_by = options.get("ingress_service")
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    endpoints: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(servers, dict):
        return {"entities": {}}
    for server_name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            continue
        ports = _listen_ports(server)
        for route in server.get("routes") or []:
            if not isinstance(route, dict):
                continue
            upstreams = _upstreams(route.get("handle"))
            for match in route.get("match") or []:
                if not isinstance(match, dict):
                    continue
                for hostname in match.get("host") or []:
                    ident = _slug(str(hostname))
                    if ident in seen:
                        continue
                    seen.add(ident)
                    entity: dict[str, Any] = {
                        "id": ident,
                        "address": str(hostname),
                        "port": ports[0],
                        "protocol": "https" if 443 in ports else "http",
                    }
                    if network:
                        entity["network"] = network
                    if fronted_by:
                        entity["fronted_by"] = str(fronted_by)
                    if upstreams:
                        # Observed text, rendered as inert data downstream. The
                        # upstream is evidence about the join, not the join.
                        entity["notes"] = (
                            f"served by {server_name}; upstream "
                            + ", ".join(sorted(set(upstreams)))
                        )
                    endpoints.append(entity)
    return {"entities": {"endpoint": endpoints}}


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config, required=False)
    config = get_json(endpoint, "/config/")
    if not isinstance(config, dict):
        raise HttpError("internal", "admin API did not return a config object")
    return ok(transform(config, request.config), format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "endpoint.list": _collect,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
