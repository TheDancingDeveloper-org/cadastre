"""M13 deployment-topology coverage."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cadastre.cli.context_for import context_for
from cadastre.cli.session import Session
from cadastre.core.artifacts import parse
from cadastre.core.loader import load_catalog
from cadastre.core.storage import CatalogStore
from cadastre.core.topology import check_artifact, drift
from cadastre.core.writes import write
from cadastre.render import text
from tests.conftest import DECLARED_AS_OF, EXAMPLE_CATALOG, NOW


@pytest.fixture
def sqlite_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, root)
    from cadastre.core.storage import initialize

    initialize(root)
    from cadastre.core.storage import CatalogStore

    with CatalogStore.open(root) as store:
        store.apply_catalog_transaction(
            load_catalog(root),
            principal="fixture",
            reason="fixture",
            operation="seed",
            changed=(),
        )
    return root


def _topology_yaml(*, node: str = "app-01") -> str:
    return (
        "- id: notes-api-compose\n"
        "  repo: notes-api-repo\n"
        "  path_pattern: deploy/compose.yaml\n"
        "  pipeline: notes-api-selfhosted\n"
        "  produces: registry.example.invalid/notes-api:{{ git_sha }}\n"
        "  registry: registry.example.invalid\n"
        "  target_kind: service\n"
        "  target: notes-api\n"
        f"  node: {node}\n"
        "  artifact: compose\n"
        "  exposure: internal\n"
        "  hostname_pattern: notes.example.invalid\n"
        "  secret_ref_format: /prod/notes-api/{name}\n"
    )


def _add_topology(root: Path, *, node: str = "app-01") -> None:
    path = root / "declared" / "topologies" / "topologies.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_topology_yaml(node=node), encoding="utf-8")


def test_topology_loads_and_is_available_as_a_typed_collection(
    catalog_copy: Path,
) -> None:
    _add_topology(catalog_copy)
    catalog = load_catalog(catalog_copy)
    assert catalog.deployment_topologies[0].target == "notes-api"


def test_context_for_includes_matching_topology(catalog_copy: Path) -> None:
    _add_topology(catalog_copy)
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    document = context_for(session, "deploy notes-api using notes-api-compose")
    assert document.data["topologies"][0]["id"] == "notes-api-compose"
    assert "notes-api-selfhosted" in text.render(document)


def test_topology_drift_reports_missing_node_without_rejecting_catalog(
    catalog_copy: Path,
) -> None:
    _add_topology(catalog_copy, node="decommissioned-01")
    catalog = load_catalog(catalog_copy)
    findings = drift(catalog)
    assert [(finding.code, finding.subject) for finding in findings] == [
        ("topology-missing-node", "notes-api-compose")
    ]


def test_topology_uses_the_generic_gated_crud_path(sqlite_catalog: Path) -> None:
    result = write(
        sqlite_catalog,
        "add",
        "deployment_topology",
        values={
            "id": "notes-api-compose",
            "repo": "notes-api-repo",
            "path_pattern": "deploy/compose.yaml",
            "pipeline": "notes-api-selfhosted",
            "produces": "registry.example.invalid/notes-api:{{ git_sha }}",
            "registry": "registry.example.invalid",
            "target_kind": "service",
            "target": "notes-api",
            "node": "app-01",
            "artifact": "compose",
            "exposure": "internal",
        },
        principal="agent-7",
        reason="record deployment path",
        now=NOW,
    )
    assert "deployment_topology:notes-api-compose" in result.paths
    assert (
        CatalogStore.open(sqlite_catalog, read_only=True)
        .read_catalog()
        .get("deployment_topology", "notes-api-compose")
        is not None
    )


def test_check_validates_an_artifact_topology_claim(catalog_copy: Path) -> None:
    _add_topology(catalog_copy)
    artifact_path = catalog_copy / "compose.yaml"
    artifact_path.write_text(
        "x-cadastre:\n"
        "  repo: notes-api-repo\n"
        "  topology: notes-api-compose\n"
        "services:\n"
        "  notes-api:\n"
        "    image: notes-api:latest\n",
        encoding="utf-8",
    )
    artifact = parse(artifact_path)
    assert check_artifact(load_catalog(catalog_copy), artifact) == ()
