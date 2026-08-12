"""The single gated SQLite catalog write path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre.core import model, storage
from cadastre.core.catalog import Catalog
from cadastre.core.errors import CatalogError, CatalogIssue, Located, UsageError
from cadastre.core.loader import IssueCollector, parse_entity
from cadastre.core.provenance import format_timestamp, parse_timestamp
from cadastre.core.serialize import entity_to_dict
from cadastre.core.yamlio import load_yaml
from cadastre.modules.config import load_modules
from cadastre.modules.registry import EntityRegistry, active_registry, base_registry
from cadastre.plugins import PluginRegistry


class WriteRefused(UsageError):
    """A deliberate refusal with an actionable next step."""


@dataclass(frozen=True)
class WriteResult:
    operation: str
    kind: str
    ident: str
    principal: str
    reason: str
    database_revision: int
    transaction_id: str
    audit_id: str
    changed: tuple[tuple[str, str], ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f"{kind}:{ident}" for kind, ident in self.changed)


def read_record(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise UsageError(f"no such record file: {path}")
    import json

    try:
        raw = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.suffix.lower() == ".json"
            else load_yaml(path, rel=str(path))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{path}: invalid record: {exc}") from exc
    if not isinstance(raw, dict):
        raise UsageError(f"{path}: expected one entity mapping")
    return dict(raw)


def parse_target(
    raw: str, *, registry: EntityRegistry | None = None
) -> tuple[str, str]:
    registry = registry or base_registry()
    if ":" not in raw:
        raise UsageError("target must be KIND:ID, for example host:node-1")
    kind, ident = raw.split(":", 1)
    if kind not in registry.kinds or not ident:
        raise UsageError(
            f"target must name a known kind ({', '.join(registry.kinds)}) and id"
        )
    return kind, ident


def annotate_values(items: list[str]) -> dict[str, Any]:
    if not items:
        raise UsageError("annotate needs at least one key=value")
    result: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise UsageError(f"annotation must be key=value: {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise UsageError("annotation key cannot be empty")
        result[key] = (
            [part for part in value.split(",") if part] if key == "tags" else value
        )
    return result


def _parse_candidate(
    kind: str, raw: dict[str, Any], registry: EntityRegistry
) -> model.Entity:
    issues = IssueCollector()
    entity = parse_entity(
        registry.specs[kind], raw, Located(f"sqlite-write:{kind}"), issues
    )
    issues.raise_if_any()
    if entity is None:
        raise CatalogError(
            [CatalogIssue(Located(f"sqlite-write:{kind}"), kind, "invalid entity")]
        )
    return entity


def _source_owner(root: Path, kind: str) -> tuple[str, Any] | None:
    registry = PluginRegistry.discover(root)
    for registered in registry.plugins:
        declaration = registered.info.entity(kind)
        if declaration is not None and declaration.authority == "source":
            return registered.name, declaration
    return None


def _refuse_source(
    operation: str, kind: str, ident: str | None, owner: tuple[str, Any]
) -> None:
    plugin_name, _ = owner
    target = f"{kind}:{ident or '<id>'}"
    raise WriteRefused(
        f"REFUSED  {operation} {plugin_name}.{kind}\n"
        f"  Entity type `{kind}` is reflected from the `{plugin_name}` plugin; "
        "the catalog mirrors it.\n"
        f"  To change upstream truth, then: cadastre collect --source {plugin_name}\n"
        f"  You may annotate an existing entity: cadastre annotate {target} tags=..."
    )


def _validate_annotations(values: dict[str, Any]) -> None:
    unknown = sorted(set(values) - {"tags", "notes"})
    if unknown:
        raise WriteRefused(
            "REFUSED annotate\n  Only catalog annotation fields may be "
            "written: tags, notes.\n  Refused: " + ", ".join(unknown)
        )
    if "tags" in values and (
        not isinstance(values["tags"], (list, tuple))
        or not all(isinstance(tag, str) for tag in values["tags"])
    ):
        raise UsageError("annotation `tags` must be a list of strings")
    if "notes" in values and not isinstance(values["notes"], str):
        raise UsageError("annotation `notes` must be a string")


def _validate_catalog(catalog: Catalog) -> None:
    from cadastre.core.rules import check_catalog

    errors = [item for item in check_catalog(catalog) if item.level == "error"]
    if errors:
        raise UsageError(
            "catalog check failed: "
            + "; ".join(f"{item.code}: {item.message}" for item in errors)
        )


def write(
    root: Path,
    operation: str,
    kind: str,
    ident: str | None = None,
    values: dict[str, Any] | None = None,
    *,
    principal: str = "agent",
    reason: str = "catalog edit",
    now: datetime | None = None,
    store: str = "declared",
) -> WriteResult:
    if operation not in {"add", "update", "delete", "annotate"}:
        raise UsageError(f"unknown catalog operation: {operation}")
    if store != "declared":
        raise WriteRefused(
            "REFUSED catalog write\n  observed evidence is generated and "
            "cannot be edited.\n  Run the collector instead: "
            "cadastre collect --source <plugin>"
        )
    registry = active_registry(load_modules(root))
    with storage.CatalogStore.open(root) as db:
        storage.refuse_if_module_disabled(db, registry, "write")
        catalog = db.read_catalog(registry=registry)
        owner = _source_owner(root, kind)
        existing = catalog.get(kind, ident or "")
        if operation in {"add", "delete"} and owner is not None:
            _refuse_source(operation, kind, ident, owner)
        if operation == "add":
            if not values:
                raise UsageError("add needs an entity record")
            raw = dict(values)
            if ident:
                raw.setdefault("id", ident)
            candidate = _parse_candidate(kind, raw, registry)
            if catalog.get(kind, candidate.id) is not None:
                raise UsageError(f"{kind}:{candidate.id} already exists; use update")
            result_ident = candidate.id
            entities = {k: dict(catalog.of(k)) for k in registry.kinds}
            entities[kind][candidate.id] = candidate
        elif existing is None:
            raise UsageError(f"no such declared entity: {kind}:{ident}")
        elif operation == "delete":
            result_ident = ident or ""
            entities = {k: dict(catalog.of(k)) for k in registry.kinds}
            del entities[kind][result_ident]
        elif operation == "annotate":
            result_ident = ident or ""
            updates = dict(values or {})
            _validate_annotations(updates)
            current = dict(
                catalog.annotations.get((kind, result_ident), {}).get("values", {})
            )
            current.update(updates)
            annotations = dict(catalog.annotations)
            annotations[(kind, result_ident)] = {
                "kind": kind,
                "id": result_ident,
                "values": current,
                "principal": principal,
                "at": format_timestamp(now or datetime.now(tz=UTC)),
            }
            base = entity_to_dict(
                catalog.base_get(kind, result_ident) or existing, registry=registry
            )
            base.update(current)
            entities = {k: dict(catalog.of(k)) for k in registry.kinds}
            entities[kind][result_ident] = _parse_candidate(kind, base, registry)
            proposed = Catalog(
                root,
                entities,
                catalog.policy,
                catalog.locations,
                annotations,
                entities,
                registry=registry,
            )
            _validate_catalog(proposed)
            result = db.apply_catalog_transaction(
                proposed,
                principal=principal,
                reason=reason,
                operation=operation,
                changed=((kind, result_ident),),
            )
            return WriteResult(
                operation,
                kind,
                result_ident,
                principal,
                reason,
                result.database_revision,
                result.transaction_id,
                result.audit_id,
                result.changed,
            )
        else:
            updates = dict(values or {})
            if owner is not None:
                classes = owner[1].field_classes
                forbidden = sorted(
                    field
                    for field in updates
                    if classes.get(field, "reflected") == "reflected"
                )
                if forbidden:
                    # Naming `annotate` alone is a dead end: it does not accept
                    # reflected fields either, so an operator who declared one
                    # of these wrongly (a mistyped secret `store`, say) is told
                    # to use a verb that will refuse them too. Say which side
                    # actually owns the field and how each side is corrected.
                    annotated = ", ".join(sorted(owner[1].annotated)) or "none"
                    raise WriteRefused(
                        f"REFUSED update {kind}\n  Reflected fields cannot be "
                        f"edited in the catalog: {', '.join(forbidden)}.\n  "
                        f"These are owned by the source plugin {owner[0]!r}, "
                        "not the catalog — the value follows the estate.\n  "
                        "  - wrong in the estate: change it upstream, then "
                        "cadastre collect\n"
                        "    - wrong in the catalog: correct declared/, then "
                        "re-seed (cadastre export, edit, cadastre import "
                        "--mode replace)\n"
                        f"  annotate accepts only: {annotated}"
                    )
            updates.pop("id", None)
            merged = entity_to_dict(existing, registry=registry)
            merged.update(updates)
            candidate = _parse_candidate(kind, merged, registry)
            result_ident = ident or ""
            entities = {k: dict(catalog.of(k)) for k in registry.kinds}
            entities[kind][result_ident] = candidate
            annotations = dict(catalog.annotations)
            proposed = Catalog(
                root,
                entities,
                catalog.policy,
                catalog.locations,
                annotations,
                entities,
                registry=registry,
            )
            _validate_catalog(proposed)
            result = db.apply_catalog_transaction(
                proposed,
                principal=principal,
                reason=reason,
                operation=operation,
                changed=((kind, result_ident),),
            )
            return WriteResult(
                operation,
                kind,
                result_ident,
                principal,
                reason,
                result.database_revision,
                result.transaction_id,
                result.audit_id,
                result.changed,
            )
        annotations = dict(catalog.annotations)
        if operation == "delete":
            annotations.pop((kind, result_ident), None)
        proposed = Catalog(
            root,
            entities,
            catalog.policy,
            catalog.locations,
            annotations,
            entities,
            registry=registry,
        )
        _validate_catalog(proposed)
        result = db.apply_catalog_transaction(
            proposed,
            principal=principal,
            reason=reason,
            operation=operation,
            changed=((kind, result_ident),),
        )
        return WriteResult(
            operation,
            kind,
            result_ident,
            principal,
            reason,
            result.database_revision,
            result.transaction_id,
            result.audit_id,
            result.changed,
        )


def write_metadata(
    root: Path,
    collection: str,
    values: dict[str, Any],
    *,
    principal: str,
    reason: str,
    now: datetime | None = None,
) -> WriteResult:
    """Record a resolution/acknowledgement as audited database metadata."""
    if collection not in {"resolutions", "acknowledgements"}:
        raise UsageError(f"unknown metadata collection: {collection}")
    if collection == "acknowledgements":
        until = values.get("until")
        if not isinstance(until, str) or parse_timestamp(until) <= (
            now or datetime.now(tz=UTC)
        ):
            raise UsageError(
                "acknowledgement --until must be a future RFC 3339 timestamp"
            )
    registry = active_registry(load_modules(root))
    with storage.CatalogStore.open(root) as db:
        storage.refuse_if_module_disabled(db, registry, collection)
        existing = db.connection.execute(
            "SELECT value FROM metadata WHERE key=?", (collection,)
        ).fetchone()
        items = __import__("json").loads(existing[0]) if existing else []
        items = [item for item in items if item != values] + [values]
        db.connection.execute("BEGIN IMMEDIATE")
        try:
            db.connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                (collection, __import__("json").dumps(items, sort_keys=True)),
            )
            revision = db.revision + 1
            db.connection.execute(
                "UPDATE metadata SET value=? WHERE key='revision'", (str(revision),)
            )
            tx, audit = __import__("uuid").uuid4().hex, __import__("uuid").uuid4().hex
            at = format_timestamp(now or datetime.now(tz=UTC))
            db.connection.execute(
                "INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",
                (audit, tx, revision, principal, reason, collection, "[]", at),
            )
            db.connection.commit()
        except Exception:
            db.connection.rollback()
            raise
    return WriteResult(
        collection,
        "metadata",
        str(values.get("id", "")),
        principal,
        reason,
        revision,
        tx,
        audit,
        (),
    )
