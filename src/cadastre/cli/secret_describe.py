"""`cadastre secret-describe` — everything about a secret except its value.

Production deploy #49 failed on a secret holding twelve raw newlines. Finding
that took a hand-written script that read the value and printed counts, and a
raw project listing to learn the project id the store's API wanted, because
`lookup` answered with a reference and nothing else.

This answers both from evidence the catalog already holds:

* **where** — store, project id and slug, environment, path, server;
* **shape** — length, newline/CR counts, whether it parses as JSON
  (`core.secretshape`), as the secrets collector computed it *inside its own
  process* from the value its list call already returns;
* **history** — version, updated/created timestamps, last rotation;
* **consumers** — declared `consumes_secret` and every observed stack that
  interpolates the secret's name.

The value never reaches this process. There is nothing here to redact because
there is nothing here to leak: the collector reduces the value to counts and
drops it before anything is written (DESIGN §1.3, §8).
"""

from __future__ import annotations

from typing import Any

from cadastre.cli.search import Hit, all_hits, normalize, secret_location
from cadastre.cli.session import Session
from cadastre.core.errors import MissingEntityError, UsageError
from cadastre.core.secretshape import shape_problems
from cadastre.render.document import Document, Fields, Para, Section, Table

MAX_MATCHES = 10


def _matches(session: Session, ref: str) -> list[Hit]:
    """By exact reference, by id, then by bare key name (case-insensitive)."""
    hits = all_hits(session, kind="secret")
    exact = [
        hit
        for hit in hits
        if ref
        in {
            hit.id,
            *(str(getattr(e, "ref", "") or "") for e in [hit.entity, *hit.observed]),
            *(e.id for e in hit.observed),
        }
    ]
    if exact:
        return exact
    want = normalize(ref.rstrip("/").split("/")[-1])
    return [
        hit
        for hit in hits
        if want
        in {
            normalize(str(secret_location(session, hit)["key"] or "")),
            normalize(hit.id),
        }
    ]


def _consumers(session: Session, hit: Hit, key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ids = {hit.id, *(entity.id for entity in hit.observed)}
    for service in session.catalog.services:
        if ids & set(service.consumes_secret):
            out.append(
                {
                    "kind": "service",
                    "id": service.id,
                    "via": "consumes_secret",
                    "source": "declared",
                }
            )
    wanted = normalize(key)
    for source in session.observed:
        for kind in session.registry.kinds:
            for entity in source.entities.get(kind, []):
                for block_name, block in entity.extra.items():
                    if not isinstance(block, dict):
                        continue
                    names = block.get("variable_refs")
                    if isinstance(names, list) and any(
                        isinstance(name, str) and normalize(name) == wanted
                        for name in names
                    ):
                        out.append(
                            {
                                "kind": kind,
                                "id": entity.id,
                                "via": f"{block_name}.variable_refs",
                                "source": source.source,
                            }
                        )
    return out


def _describe(session: Session, hit: Hit) -> dict[str, Any]:
    location = secret_location(session, hit)
    block: dict[str, Any] = {}
    observed_by = []
    for source, entity in zip(hit.sources, hit.observed, strict=True):
        candidate = entity.extra.get("x-secret-store")
        if isinstance(candidate, dict) and not block:
            block = candidate
        observed_by.append(
            {
                "source": source.source,
                "as_of": source.as_of,
                "stale": source.provenance(
                    ttl_overrides=session.plugins.freshness
                ).stale,
                "id": entity.id,
            }
        )
    declared = hit.declared_entity
    shape = block.get("shape") if isinstance(block.get("shape"), dict) else None
    last_rotated = next(
        (
            entity.last_rotated  # type: ignore[attr-defined]
            for entity in [*(hit.observed), *([declared] if declared else [])]
            if getattr(entity, "last_rotated", None)
        ),
        None,
    )
    described: dict[str, Any] = {
        **location,
        "declared_id": declared.id if declared else None,
        "observed_by": observed_by,
        "version": block.get("version"),
        "updated_at": block.get("updated_at"),
        "created_at": block.get("created_at"),
        "last_rotated": last_rotated,
        "value_shape": shape,
        "value_shape_problems": shape_problems(shape) if shape else [],
        "consumers": _consumers(session, hit, str(location["key"] or "")),
    }
    if shape is None:
        described["value_shape_note"] = (
            "No collector reported this secret's shape: the store is not "
            "collected, or its collector predates `x-secret-store`. Re-run "
            "`cadastre collect` with a current secrets collector."
        )
    return described


def secret_describe(session: Session, ref: str) -> Document:
    ref = (ref or "").strip()
    if not ref:
        raise UsageError(
            "secret_describe needs `ref`: a secret reference "
            "(scheme://project/env/KEY), a catalog id, or a bare key name"
        )
    hits = _matches(session, ref)
    if not hits:
        raise MissingEntityError(
            f"no declared or observed secret matches {ref!r} by reference, id, "
            "or key name. `lookup(query=...)` searches by words."
        )
    shown = hits[:MAX_MATCHES]
    described = [_describe(session, hit) for hit in shown]
    sections = []
    for item in described:
        shape = item["value_shape"] or {}
        fields = (
            ("ref", str(item["ref"] or "")),
            ("store", str(item["store"] or "")),
            ("project", f"{item['project_slug'] or '?'} ({item['project_id'] or '?'})"),
            ("environment / path", f"{item['environment'] or '?'} {item['path']}"),
            ("server", str(item["server"] or "?")),
            (
                "version / updated",
                f"{item['version'] or '?'} / {item['updated_at'] or '?'}",
            ),
            ("length", str(shape.get("length", "?"))),
            (
                "newlines / CR",
                f"{shape.get('newlines', '?')} / {shape.get('carriage_returns', '?')}",
            ),
            ("parses as JSON", str(shape.get("parses_as_json", "?")).lower()),
            ("manifest line", str(item["manifest_line"] or "(project id unknown)")),
        )
        blocks: list[Any] = [Fields(fields)]
        if item["value_shape_problems"]:
            blocks.append(
                Para("Shape problems: " + "; ".join(item["value_shape_problems"]) + ".")
            )
        blocks.append(
            Table(
                ("consumer", "kind", "via", "source"),
                tuple(
                    (c["id"], c["kind"], c["via"], c["source"])
                    for c in item["consumers"]
                ),
                empty_note="(no consumer declared or observed)",
            )
        )
        sections.append(Section(f"secret {item['key']}", tuple(blocks)))
    return Document(
        title=f"cadastre secret-describe {ref}",
        sections=tuple(sections),
        provenance=session.provenance(),
        data={
            "query": ref,
            "total": len(hits),
            "secrets": described,
            "value": "never returned (DESIGN §1.3)",
        },
    )
