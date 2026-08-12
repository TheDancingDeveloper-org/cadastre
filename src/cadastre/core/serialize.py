"""Entities to plain data, in one canonical shape.

Field order follows `spec.py`, not the mapping order of the source file, and
empty values are omitted. So the serialisation of a catalog is a function of
its content alone — which is what makes the round-trip test meaningful and the
rendered `--json` output diffable.
"""

from __future__ import annotations

from typing import Any

from cadastre.core import model
from cadastre.core.spec import FieldSpec
from cadastre.modules.registry import EntityRegistry, base_registry


def _object_to_dict(fields: tuple[FieldSpec, ...], obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fs in fields:
        if fs.key == "extra":
            continue
        value = getattr(obj, fs.attr, None)
        if value is None or value == () or value == []:
            continue
        out[fs.key] = _value_to_data(fs, value)
    return out


def _value_to_data(fs: FieldSpec, value: Any) -> Any:
    if fs.type == "obj":
        return _object_to_dict(fs.fields, value)
    if fs.type == "list[obj]":
        return [_object_to_dict(fs.fields, item) for item in value]
    if fs.is_list:
        return list(value)
    return value


def entity_to_dict(
    entity: model.Entity, *, registry: EntityRegistry | None = None
) -> dict[str, Any]:
    """One entity as plain data, in spec field order."""
    registry = registry or base_registry()
    result = _object_to_dict(
        registry.specs[registry.kind_for_class(type(entity))].fields, entity
    )
    result.update(entity.extra)
    return result


def entities_to_documents(
    entities: list[model.Entity], *, registry: EntityRegistry | None = None
) -> list[dict[str, Any]]:
    """A file's worth of entities, ordered by id so the file is stable."""
    return [
        entity_to_dict(e, registry=registry)
        for e in sorted(entities, key=lambda e: e.id)
    ]
