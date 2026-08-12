"""`observed/` — collector output. Read-only evidence, never hand-edited.

Declared is authoritative; this is what the world appears to say (DESIGN §2.1).
Two consequences shape this module:

* Observed entities are **not** reference-checked against `declared/`. An
  observed service on an undeclared host is the finding, not an error.
* Every source carries its own `as_of` and its own failure state. A source that
  could not be refreshed is stale, not absent — absence would silently look
  like "nothing there".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cadastre.core import model
from cadastre.core.errors import CatalogError, CatalogIssue, Located
from cadastre.core.loader import IssueCollector, parse_entity
from cadastre.core.provenance import Provenance, evaluate, ttl_for
from cadastre.modules.registry import EntityRegistry, base_registry

OBSERVED_VERSION = 1


@dataclass(frozen=True)
class ObservedSource:
    """One collector's output for one source."""

    source: str
    plugin: str
    as_of: str
    capabilities: tuple[str, ...] = ()
    entities: dict[str, list[model.Entity]] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    #: Plugin-shaped payload kept verbatim for capabilities that have no
    #: entity representation yet (secret name lists, CI status).
    extra: dict[str, Any] = field(default_factory=dict)
    #: Per-kind source scope retained with evidence, so drift can tell a
    #: scoped collector from a global source after configuration changes.
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Declared namespaced fields accepted for each entity kind. Persisting the
    #: names keeps a valid collected snapshot valid when it is loaded later.
    extensions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    registry: EntityRegistry = field(default_factory=base_registry)

    def provenance(self, *, ttl_overrides: dict[str, int] | None = None) -> Provenance:
        capability = self.capabilities[0] if self.capabilities else "inventory.list"
        return Provenance(
            source=self.source,
            plugin=self.plugin,
            as_of=self.as_of,
            ttl_seconds=ttl_for(capability, ttl_overrides),
            stale=not self.ok,
            error=self.error,
        )

    def all_entities(self) -> list[model.Entity]:
        out: list[model.Entity] = []
        for kind in self.registry.kinds:
            out.extend(self.entities.get(kind, []))
        return out


def source_to_dict(source: ObservedSource) -> dict[str, Any]:
    """Canonical on-disk form. Stable key order so a re-collect that found
    nothing new produces an identical snapshot."""
    from cadastre.core.serialize import entities_to_documents

    payload: dict[str, Any] = {
        "v": OBSERVED_VERSION,
        "source": source.source,
        "plugin": source.plugin,
        "as_of": source.as_of,
        "ok": source.ok,
        "capabilities": sorted(source.capabilities),
        "entities": {
            kind: entities_to_documents(source.entities[kind], registry=source.registry)
            for kind in source.registry.kinds
            if kind in source.entities
        },
    }
    if source.error:
        payload["error"] = source.error
    if source.extra:
        payload["extra"] = {k: source.extra[k] for k in sorted(source.extra)}
    if source.coverage:
        payload["coverage"] = {
            key: source.coverage[key] for key in sorted(source.coverage)
        }
    if source.extensions:
        payload["extensions"] = {
            kind: sorted(source.extensions[kind]) for kind in sorted(source.extensions)
        }
    return payload


def write_source(root: Path, source: ObservedSource) -> Path:
    directory = root / "observed"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source.source}.json"
    path.write_text(
        json.dumps(source_to_dict(source), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def parse_source(
    payload: Any,
    where: Located,
    *,
    extensions: dict[str, set[str]] | None = None,
    registry: EntityRegistry | None = None,
) -> ObservedSource:
    """Parse one observed document. Entities go through the same spec parser as
    `declared/`, so a plugin cannot invent a field the model does not have."""
    registry = registry or base_registry()
    issues = IssueCollector()
    if not isinstance(payload, dict):
        raise CatalogError(
            [CatalogIssue(where, "<observed>", "not an object", "a JSON object")]
        )
    raw_extensions = payload.get("extensions") or {}
    if not isinstance(raw_extensions, dict):
        issues.add(where, "extensions", "not an object", "kind -> list of x-* fields")
        raw_extensions = {}
    stored_extensions: dict[str, set[str]] = {}
    for kind, names in raw_extensions.items():
        if kind not in registry.specs:
            issues.add(
                where,
                f"extensions.{kind}",
                "unknown kind",
                "one of: " + ", ".join(sorted(registry.specs)),
            )
            continue
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name.startswith("x-") for name in names
        ):
            issues.add(
                where,
                f"extensions.{kind}",
                "invalid extension names",
                "a list of x-* field names",
            )
            continue
        stored_extensions[str(kind)] = set(names)
    accepted_extensions = {
        kind: set(names) for kind, names in (extensions or {}).items()
    }
    for kind, names in stored_extensions.items():
        accepted_extensions.setdefault(kind, set()).update(names)

    raw_coverage = payload.get("coverage") or {}
    if not isinstance(raw_coverage, dict):
        issues.add(where, "coverage", "not an object", "kind -> coverage mapping")
        raw_coverage = {}
    coverage: dict[str, dict[str, Any]] = {}
    for kind, scope in raw_coverage.items():
        if kind not in registry.specs:
            issues.add(
                where,
                f"coverage.{kind}",
                "unknown kind",
                "one of: " + ", ".join(sorted(registry.specs)),
            )
            continue
        if not isinstance(scope, dict):
            issues.add(where, f"coverage.{kind}", "not an object", "ids and/or where")
            continue
        unknown = sorted(str(key) for key in scope if key not in {"ids", "where"})
        ids = scope.get("ids")
        where_clause = scope.get("where")
        if unknown:
            issues.add(
                where,
                f"coverage.{kind}",
                "unknown keys: " + ", ".join(unknown),
                "ids and/or where",
            )
        if ids is not None and (
            not isinstance(ids, list) or not all(isinstance(item, str) for item in ids)
        ):
            issues.add(
                where,
                f"coverage.{kind}.ids",
                "not a list of strings",
                "exact entity ids",
            )
        if where_clause is not None and not isinstance(where_clause, dict):
            issues.add(
                where,
                f"coverage.{kind}.where",
                "not an object",
                "entity field constraints",
            )
        known_fields = {field.key for field in registry.specs[kind].fields}
        unknown_fields = (
            sorted(str(key) for key in where_clause if key not in known_fields)
            if isinstance(where_clause, dict)
            else []
        )
        if unknown_fields:
            issues.add(
                where,
                f"coverage.{kind}.where",
                "unknown fields: " + ", ".join(unknown_fields),
                "declared entity fields",
            )
        if (
            not unknown
            and not unknown_fields
            and (
                ids is None
                or (
                    isinstance(ids, list) and all(isinstance(item, str) for item in ids)
                )
            )
            and (where_clause is None or isinstance(where_clause, dict))
        ):
            coverage[str(kind)] = dict(scope)

    entities: dict[str, list[model.Entity]] = {}
    raw_entities = payload.get("entities") or {}
    if not isinstance(raw_entities, dict):
        issues.add(where, "entities", "not an object", "kind -> list of entities")
        raw_entities = {}
    for kind, items in sorted(raw_entities.items()):
        if kind not in registry.specs:
            issues.add(
                where,
                f"entities.{kind}",
                "unknown kind",
                "one of: " + ", ".join(sorted(registry.specs)),
            )
            continue
        if not isinstance(items, list):
            issues.add(where, f"entities.{kind}", "not a list", "a list of entities")
            continue
        parsed = [
            parse_entity(
                registry.specs[kind],
                item,
                where,
                issues,
                strict=False,
                extensions=accepted_extensions.get(kind),
            )
            for item in items
        ]
        entities[kind] = [e for e in parsed if e is not None]
    issues.raise_if_any()
    return ObservedSource(
        source=str(payload.get("source") or where.path),
        plugin=str(payload.get("plugin") or "unknown"),
        as_of=str(payload.get("as_of") or ""),
        capabilities=tuple(payload.get("capabilities") or ()),
        entities=entities,
        ok=bool(payload.get("ok", True)),
        error=payload.get("error"),
        extra=dict(payload.get("extra") or {}),
        coverage=coverage,
        extensions={
            kind: tuple(sorted(names)) for kind, names in accepted_extensions.items()
        },
        registry=registry,
    )


def load_observed(
    root: Path,
    *,
    now: datetime | None = None,
    ttl_overrides: dict[str, int] | None = None,
    registry: EntityRegistry | None = None,
) -> list[ObservedSource]:
    """Every observed source, ordered by source id.

    `now` re-evaluates staleness. Passing it is how golden tests stay
    deterministic while the real CLI uses the wall clock.
    """
    directory = root / "observed"
    # SQLite is the query cache once it exists.  JSON remains the interchange
    # and recovery format: removing the cache makes the next query read the
    # snapshots, while collection repopulates both stores.  Reading snapshots
    # in preference to an existing cache would make M14's history pointless
    # and would leave drift querying a different store from collection.
    from cadastre.core.observed_db import database_path, load_sources

    if database_path(root).exists():
        if (root / "catalog.sqlite3").exists():
            from cadastre.core.storage import observed_payloads

            payloads = observed_payloads(root)
            if not payloads and directory.is_dir():
                from cadastre.core.observed_db import sync_snapshots

                sync_snapshots(root)
                payloads = observed_payloads(root)
            sources = [
                parse_source(
                    payload,
                    Located(f"sqlite:observed:{payload.get('source', '')}"),
                    registry=registry,
                )
                for payload in payloads
            ]
            return _evaluate_sources(sources, now=now, ttl_overrides=ttl_overrides)
        sources = load_sources(root)
        # A cache may have been created first by trust metadata or by an
        # explicit initialization step.  Populate it lazily from snapshots if
        # it has no current sources yet.
        if not sources and directory.is_dir():
            from cadastre.core.observed_db import sync_snapshots

            sync_snapshots(root)
            sources = load_sources(root)
    elif directory.is_dir():
        sources = load_snapshot_files(root, registry=registry)
    else:
        sources = []
    return _evaluate_sources(sources, now=now, ttl_overrides=ttl_overrides)


def load_snapshot_files(
    root: Path, *, registry: EntityRegistry | None = None
) -> list[ObservedSource]:
    """Read JSON interchange snapshots without consulting SQLite."""
    directory = root / "observed"
    if not directory.is_dir():
        return []
    sources: list[ObservedSource] = []
    for path in sorted(directory.glob("*.json")):
        rel = str(path.relative_to(root))
        where = Located(rel)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(
                [CatalogIssue(where, "<observed>", str(exc), "well-formed JSON")]
            ) from exc
        source = parse_source(payload, where, registry=registry)
        sources.append(source)
    return sources


def _evaluate_sources(
    sources: list[ObservedSource],
    *,
    now: datetime | None,
    ttl_overrides: dict[str, int] | None,
) -> list[ObservedSource]:
    if now is None:
        return sources
    evaluated: list[ObservedSource] = []
    for source in sources:
        provenance = evaluate(source.provenance(ttl_overrides=ttl_overrides), now)
        evaluated.append(
            ObservedSource(
                source=source.source,
                plugin=source.plugin,
                as_of=source.as_of,
                capabilities=source.capabilities,
                entities=source.entities,
                ok=source.ok and not provenance.stale,
                error=source.error,
                extra=source.extra,
                coverage=source.coverage,
                extensions=source.extensions,
                registry=source.registry,
            )
        )
    return evaluated


def provenance_of(
    sources: list[ObservedSource],
    *,
    now: datetime | None = None,
    ttl_overrides: dict[str, int] | None = None,
) -> tuple[Provenance, ...]:
    out = []
    for source in sources:
        provenance = source.provenance(ttl_overrides=ttl_overrides)
        out.append(evaluate(provenance, now) if now else provenance)
    return tuple(sorted(out, key=lambda p: (p.source, p.plugin)))
