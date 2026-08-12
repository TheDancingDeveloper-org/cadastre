"""JSON Schema, emitted from `spec.py`.

Published in-repo (`schema/catalog.schema.json`) so a plugin can validate its
output against the same declaration the loader parses from. Hand-maintaining a
second copy of the model is how the two quietly diverge, so nothing here is
hand-maintained — `cadastre schema` regenerates it and CI fails if the checked-in
copy is stale.
"""

from __future__ import annotations

import json
from typing import Any

from cadastre.core.spec import (
    CONVENTIONS,
    EXPOSURE_TIER,
    GRANT,
    KNOWN_UNDECLARED,
    REPLICATION_CONTRACT,
    FieldSpec,
)
from cadastre.modules.registry import EntityRegistry, base_registry

SCHEMA_ID = "https://cadastre.invalid/schema/catalog.schema.json"

_SCALAR_TYPES = {"str": "string", "int": "integer", "bool": "boolean"}


def _field_schema(fs: FieldSpec) -> dict[str, Any]:
    node: dict[str, Any]
    if fs.type in _SCALAR_TYPES:
        node = {"type": _SCALAR_TYPES[fs.type]}
        if fs.enum:
            node["enum"] = list(fs.enum)
    elif fs.type == "ref":
        node = {"type": "string", "x-cadastre-ref": fs.ref}
    elif fs.type == "list[str]":
        node = {"type": "array", "items": {"type": "string"}}
    elif fs.type == "list[ref]":
        node = {"type": "array", "items": {"type": "string", "x-cadastre-ref": fs.ref}}
    elif fs.type == "obj":
        node = _object_schema(fs.fields)
    elif fs.type == "list[obj]":
        node = {"type": "array", "items": _object_schema(fs.fields)}
    elif fs.type == "mapping":
        node = {"type": "object", "additionalProperties": True}
    elif fs.type == "mapping[str]":
        node = {"type": "object", "additionalProperties": {"type": "string"}}
    else:  # pragma: no cover - unreachable while SCALARS is closed
        raise AssertionError(f"unhandled field type {fs.type}")
    if fs.description:
        node["description"] = fs.description
    return node


def _object_schema(fields: tuple[FieldSpec, ...]) -> dict[str, Any]:
    published = tuple(f for f in fields if f.key != "extra")
    required = [f.key for f in published if f.required]
    node: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {f.key: _field_schema(f) for f in published},
    }
    if required:
        node["required"] = required
    return node


def catalog_schema(*, registry: EntityRegistry | None = None) -> dict[str, Any]:
    """The whole model as one schema document."""
    registry = registry or base_registry()
    definitions = {
        kind: dict(_object_schema(es.fields), title=kind, description=es.description)
        for kind, es in registry.specs.items()
    }
    definitions["exposure_tier"] = _object_schema(EXPOSURE_TIER)
    definitions["conventions"] = _object_schema(CONVENTIONS)
    definitions["grant"] = _object_schema(GRANT)
    definitions["known_undeclared"] = _object_schema(KNOWN_UNDECLARED)
    definitions["replication_contract"] = _object_schema(REPLICATION_CONTRACT)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Cadastre catalog",
        "description": (
            "Entities in declared/. One file per kind-directory, each holding a "
            "list of entities of that kind."
        ),
        "$defs": definitions,
        "properties": {
            kind: {"type": "array", "items": {"$ref": f"#/$defs/{kind}"}}
            for kind in registry.specs
        },
        "x-cadastre-relations": [
            {"relation": r, "from": f, "field": a, "to": t}
            for r, f, a, t in registry.relations
        ],
        "type": "object",
        "additionalProperties": False,
    }


def render_schema(*, registry: EntityRegistry | None = None) -> str:
    """Stable text. Two-space indent, trailing newline, no key reordering."""
    return (
        json.dumps(catalog_schema(registry=registry), indent=2, sort_keys=False) + "\n"
    )
