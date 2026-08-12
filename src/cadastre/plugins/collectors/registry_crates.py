"""Registry collector (crates.io).

Publish state only, and no auth: the public index says which versions exist.
Compared against workspace manifests it detects "released but not tagged"
drift, which is cheap and occasionally embarrassing.

Carried under `extra` rather than as entities. There is no `package` entity in
the model and adding one would need a question that cannot be answered without
it — publish state is not that question yet.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "registry-crates"
VERSION = "1"
CAPABILITIES = ()

DEFAULT_ENDPOINT = "https://crates.io"
#: crates.io asks for an identifying user agent, and refuses anonymous ones.
USER_AGENT = "cadastre-catalog (https://github.com/cadastre-catalog/cadastre)"


def transform(payload: Any) -> dict[str, Any]:
    crate = payload.get("crate") if isinstance(payload, dict) else None
    if not isinstance(crate, dict):
        return {}
    versions = [
        str(version.get("num"))
        for version in (payload.get("versions") or [])
        if isinstance(version, dict)
        and version.get("num")
        and not version.get("yanked")
    ]
    return {
        "name": str(crate.get("name") or ""),
        "max_version": str(
            crate.get("max_stable_version") or crate.get("max_version") or ""
        ),
        "updated_at": str(crate.get("updated_at") or "")[:10],
        "versions": versions,
    }


def _collect(request: Request) -> Reply:
    names = request.config.get("crates") or []
    if not names:
        raise HttpError("invalid_config", "config.crates must list crate names")
    endpoint = Endpoint.from_config(
        {"endpoint": DEFAULT_ENDPOINT, **request.config}, required=False
    )
    published = {}
    for name in names:
        payload = get_json(
            endpoint, f"/api/v1/crates/{name}", headers={"User-Agent": USER_AGENT}
        )
        published[str(name)] = transform(payload)
    return ok(
        {"extra": {"published": published}}, format_timestamp(datetime.now(tz=UTC))
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
