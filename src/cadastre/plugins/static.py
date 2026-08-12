"""The `static` plugin — reads the configured catalog.

Makes Cadastre useful with zero integrations (DESIGN §4.4), and gives `collect` a
source that always answers, so the observed pipeline can be exercised before a
single real credential exists.

For a runtime catalog, provenance comes from SQLite metadata. File-tree
catalogs remain explicit interchange fixtures only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from cadastre.core.loader import declared_as_of, load_catalog
from cadastre.core.serialize import entities_to_documents
from cadastre.core.storage import CatalogStore
from cadastre.plugins.contract import default_entity_declaration
from cadastre.plugins.harness import Handler, serve
from cadastre.plugins.protocol import Reply, Request, fail, ok

NAME = "static"
VERSION = "1"
CAPABILITIES = ("Inventory", "Network", "Endpoint", "SecretRef", "VCS", "CI")

_METHOD_KINDS: dict[str, tuple[str, ...]] = {
    "inventory.list": ("host", "service"),
    "network.list": ("network",),
    "network.members": ("host",),
    "endpoint.list": ("endpoint",),
    "dns.records": ("domain",),
    "secret.list": ("secret",),
    "vcs.repos": ("repo",),
    "ci.pipelines": ("pipeline",),
    "ci.status": ("ci_executor", "ci_pool"),
}


def _root(request: Request) -> Path:
    return Path(str(request.config.get("catalog", "."))).expanduser()


def _as_of(root: Path) -> str:
    return declared_as_of(root)


def _entities(request: Request, kinds: tuple[str, ...]) -> Reply:
    root = _root(request)
    try:
        if (root / "catalog.sqlite3").exists():
            with CatalogStore.open(root, read_only=True) as store:
                catalog = store.read_catalog()
        else:
            catalog = load_catalog(root)
    except Exception as exc:
        return fail("invalid_config", f"cannot load {root}: {exc}")
    payload: dict[str, Any] = {
        "entities": {
            kind: entities_to_documents(catalog.all(kind))
            for kind in kinds
            if catalog.all(kind)
        }
    }
    return ok(payload, _as_of(root))


def _info(request: Request) -> Reply:
    return ok(
        {
            "name": NAME,
            "version": VERSION,
            "capabilities": list(CAPABILITIES),
            "methods": sorted(_METHOD_KINDS),
            "entities": [
                {
                    "kind": declaration.kind,
                    "authority": "catalog",
                    "reflected": list(declaration.reflected),
                    "intended": list(declaration.intended),
                    "annotated": list(declaration.annotated),
                    "identity": list(declaration.identity),
                    "attributes": declaration.attributes,
                    "on_contest": declaration.on_contest,
                    "empty_expected": declaration.empty_expected,
                    "coverage": declaration.coverage,
                }
                for declaration in (
                    default_entity_declaration(kind, authority="catalog")
                    for kind in sorted(
                        {kind for kinds in _METHOD_KINDS.values() for kind in kinds}
                    )
                )
            ],
        },
        _as_of(_root(request)),
    )


def _handler(kinds: tuple[str, ...]) -> Handler:
    def run(request: Request) -> Reply:
        return _entities(request, kinds)

    return run


HANDLERS = {method: _handler(kinds) for method, kinds in _METHOD_KINDS.items()}


def main() -> int:
    return serve(HANDLERS, info=_info)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
