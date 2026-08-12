"""Collectors — read-only observers of real systems.

Each is a separate executable speaking the wire protocol, so core takes no
vendor SDK dependency and a plugin crash is a stale source rather than a core
crash (DESIGN §4.1). Built-ins live in-tree for distribution; nothing about the
protocol requires that, and a third-party plugin is a peer, not a special case.

House rules, enforced by review and by `tests/test_collectors.py`:

* **Read-only credentials, scoped per capability.** A DNS collector gets
  zone-read, never zone-edit. No collector here calls a write endpoint.
* **The credential arrives by environment variable named in `config`.** Never
  in `params`, which may be logged.
* **The transform is a pure function of the payload.** That is what makes a
  fixture-based test possible, and a test suite that needs network access is a
  test suite nobody runs.
* **Vendor nouns stay here.** `tailnet`, `zone_id`, `vmid` are plugin
  vocabulary; what leaves is `network`, `domain`, `host`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors.http import HttpError
from cadastre.plugins.contract import declaration_for
from cadastre.plugins.harness import serve
from cadastre.plugins.protocol import METHOD_ENTITY_KINDS, Reply, Request, ok

Handler = Callable[[Request], Reply]


def serve_collector(
    *,
    name: str,
    version: str,
    capabilities: tuple[str, ...],
    methods: dict[str, Handler],
    entities: tuple[str, ...] = (),
) -> int:
    """Run a collector: handshake, error guard, wire loop.

    A function rather than a base class, and deliberately. Plugins are
    external processes speaking JSON over stdio (DESIGN §4.1), so the contract
    a collector satisfies is a *wire protocol*, not an interface it inherits.
    A base class would only be inheritable by plugins written in Python and
    living in this tree, which is exactly the privilege the protocol exists to
    avoid — a third-party plugin is a peer, not a special case.

    What it removes is real duplication: every collector had its own copy of
    the same HttpError guard (in two spellings, already drifting apart) and its
    own hand-written `plugin.info`.

    `plugin.info` is now DERIVED from `methods`. It was previously a
    hand-maintained list beside the handler dict, so the two could disagree —
    a collector could advertise a method it did not implement, and the
    negotiation would believe it.
    """

    def guard(handler: Handler) -> Handler:
        def run(request: Request) -> Reply:
            try:
                return handler(request)
            except HttpError as exc:
                return exc.as_reply()

        return run

    declared_entities = entities or tuple(
        sorted(
            {kind for method in methods for kind in METHOD_ENTITY_KINDS.get(method, ())}
        )
    )

    def info(_: Request) -> Reply:
        return ok(
            {
                "name": name,
                "version": version,
                "capabilities": list(capabilities),
                "methods": sorted(methods),
                "entities": [
                    _declaration_dict(declaration_for(kind, plugin=name))
                    for kind in declared_entities
                ],
            },
            format_timestamp(datetime.now(tz=UTC)),
        )

    handlers: dict[str, Handler] = {}
    handlers.update({method: guard(h) for method, h in methods.items()})
    return serve(handlers, info=info)


def _declaration_dict(declaration: Any) -> dict[str, Any]:
    return {
        "kind": declaration.kind,
        "authority": declaration.authority,
        "reflected": list(declaration.reflected),
        "intended": list(declaration.intended),
        "annotated": list(declaration.annotated),
        "identity": list(declaration.identity),
        "attributes": declaration.attributes,
        "on_contest": declaration.on_contest,
        "empty_expected": declaration.empty_expected,
        "coverage": declaration.coverage,
    }
