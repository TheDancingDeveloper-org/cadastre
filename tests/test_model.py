"""M1 — the entity model.

The parity test is the important one: `spec.py` drives parsing and the emitted
JSON Schema, `model.py` drives the type checker. If they diverge, one of the
two is lying to whoever reads it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from cadastre.core import model, spec
from cadastre.core.errors import CatalogError
from cadastre.core.loader import load_catalog
from cadastre.core.schema import catalog_schema
from cadastre.core.serialize import entities_to_documents
from cadastre.core.yamlio import dump_yaml
from cadastre.manifest import model as manifest_model
from cadastre.manifest import spec as manifest_spec
from tests.conftest import EXAMPLE_CATALOG


def _dataclass_fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


@pytest.mark.parametrize("kind", sorted(spec.ENTITY_SPECS))
def test_spec_matches_dataclass(kind: str) -> None:
    entity_spec = spec.ENTITY_SPECS[kind]
    assert {f.attr for f in entity_spec.fields} == _dataclass_fields(entity_spec.cls)


@pytest.mark.parametrize("kind", sorted(manifest_spec.ENTITY_SPECS))
def test_manifest_spec_matches_dataclass(kind: str) -> None:
    entity_spec = manifest_spec.ENTITY_SPECS[kind]
    assert {f.attr for f in entity_spec.fields} == _dataclass_fields(entity_spec.cls)


@pytest.mark.parametrize(
    ("fields", "cls"),
    [
        (spec.RESOURCES, model.Resources),
        (spec.ACCESS, model.Access),
        (spec.REMOTE, model.Remote),
        (spec.DEPLOYMENT, model.Deployment),
        (spec.EXPOSURE_TIER, model.ExposureTier),
        (spec.CONVENTIONS, model.Conventions),
        (spec.GRANT, model.Grant),
        (spec.KNOWN_UNDECLARED, model.KnownUndeclared),
        (spec.REPLICATION_CONTRACT, model.ReplicationContract),
        (manifest_spec.ORIGIN, manifest_model.WorkOrigin),
    ],
)
def test_nested_spec_matches_dataclass(fields: tuple, cls: type) -> None:
    assert {f.attr for f in fields} == _dataclass_fields(cls)


def test_every_relation_names_a_real_field() -> None:
    for relation, from_kind, attr, to_kind in model.RELATIONS:
        assert from_kind in spec.ENTITY_SPECS, relation
        assert to_kind in spec.ENTITY_SPECS, relation
        assert attr in _dataclass_fields(spec.ENTITY_SPECS[from_kind].cls), relation


def test_example_catalog_loads(example_catalog: Path) -> None:
    catalog = load_catalog(example_catalog)
    assert catalog.counts()["host"] == 7
    assert catalog.get("host", "app-01") is not None


def test_round_trip_is_byte_identical(example_catalog: Path) -> None:
    """M1's definition of done: load, validate, serialise — byte-identically."""
    catalog = load_catalog(example_catalog)
    for kind, dirname in model.KIND_DIRS.items():
        entities = catalog.all(kind)
        if not entities:
            continue
        path = example_catalog / "declared" / dirname / f"{dirname}.yaml"
        assert path.read_text(encoding="utf-8") == dump_yaml(
            entities_to_documents(entities)
        ), f"{path} is not in canonical form; run `cadastre fmt`"


def test_schema_is_json_serialisable_and_covers_every_kind() -> None:
    schema = catalog_schema()
    json.dumps(schema)
    assert set(schema["$defs"]) >= set(spec.ENTITY_SPECS)
    assert schema["$defs"]["network"]["properties"]["class"]["enum"] == [
        "private",
        "public",
        "mixed",
    ]


def test_checked_in_schema_is_current() -> None:
    """CI fails if the published schema drifts from the model."""
    from cadastre.core.schema import render_schema

    published = EXAMPLE_CATALOG.parent.parent / "schema" / "catalog.schema.json"
    assert published.exists(), "run `cadastre schema > schema/catalog.schema.json`"
    assert published.read_text(encoding="utf-8") == render_schema()


# -- located errors ---------------------------------------------------------


def _write(root: Path, relative: str, text: str) -> None:
    path = root / "declared" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_bad_enum_names_file_line_and_expected_shape(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "networks/networks.yaml",
        "- id: n1\n  class: private\n- id: n2\n  class: sideways\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    rendered = caught.value.render()
    assert "declared/networks/networks.yaml:4" in rendered
    assert "network[n2].class" in rendered
    assert "one of: private, public, mixed" in rendered


def test_missing_required_field_is_located(tmp_path: Path) -> None:
    _write(tmp_path, "networks/networks.yaml", "- id: n1\n")
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert "missing required field" in caught.value.render()
    assert "network[n1].class" in caught.value.render()


def test_unknown_field_is_reported_with_the_known_ones(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "networks/networks.yaml",
        "- id: n1\n  class: private\n  clas: private\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    rendered = caught.value.render()
    assert "unknown field" in rendered
    assert "declared/networks/networks.yaml:3" in rendered


def test_unresolvable_reference_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "networks/networks.yaml", "- id: lab\n  class: private\n")
    _write(
        tmp_path,
        "hosts/hosts.yaml",
        "- id: h1\n  role: server\n  reachable_from:\n    - nowhere\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    rendered = caught.value.render()
    assert "no such network: 'nowhere'" in rendered
    assert "one of: lab" in rendered


def test_duplicate_id_names_the_first_declaration(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "networks/networks.yaml",
        "- id: n1\n  class: private\n- id: n1\n  class: public\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert "duplicate id" in caught.value.render()


def test_a_boolean_is_not_an_integer_port(tmp_path: Path) -> None:
    _write(tmp_path, "networks/networks.yaml", "- id: lab\n  class: private\n")
    _write(tmp_path, "hosts/hosts.yaml", "- id: h1\n  role: server\n")
    _write(tmp_path, "services/services.yaml", "- id: s1\n  runs_on: h1\n")
    _write(
        tmp_path,
        "endpoints/endpoints.yaml",
        "- id: e1\n  service: s1\n  network: lab\n  address: a\n  port: true\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert "endpoint[e1].port" in caught.value.render()


def test_every_problem_is_reported_not_just_the_first(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "networks/networks.yaml",
        "- id: n1\n  class: sideways\n- id: n2\n  class: diagonal\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert len(caught.value.issues) == 2


def test_unknown_exposure_tier_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "hosts/hosts.yaml", "- id: h1\n  role: server\n")
    _write(
        tmp_path, "services/services.yaml", "- id: s1\n  runs_on: h1\n  expose: hmm\n"
    )
    _write(
        tmp_path,
        "policy/exposure.yaml",
        "tiers:\n- name: internal\n  network_class: private\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert "unknown exposure tier" in caught.value.render()


def test_replication_contract_loads_selectors_and_mappings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "policy/replication.yaml",
        """\
replication:
- source: secrets-manager
  target: ci-store
  selectors: [/prod/ci/*]
  mappings: {DEPLOY_TOKEN: FORGE_DEPLOY_TOKEN}
""",
    )
    contract = load_catalog(tmp_path).policy.replication[0]
    assert contract == model.ReplicationContract(
        "secrets-manager",
        "ci-store",
        selectors=("/prod/ci/*",),
        mappings={"DEPLOY_TOKEN": "FORGE_DEPLOY_TOKEN"},
    )


def test_replication_contract_rejects_non_string_mappings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "policy/replication.yaml",
        "replication:\n- source: a\n  target: b\n  mappings: {DEPLOY_TOKEN: 42}\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert "mapping of string keys to string values" in caught.value.render()


def test_replication_contract_rejects_self_replication(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "policy/replication.yaml",
        "replication:\n- source: secrets-manager\n  target: secrets-manager\n",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    assert "source and target must differ" in caught.value.render()
