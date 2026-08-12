"""M11 write-path tests against throwaway SQLite catalogs."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cadastre.core.errors import CatalogError, UsageError
from cadastre.core.loader import load_catalog
from cadastre.core.storage import CatalogStore, initialize
from cadastre.core.writes import WriteRefused, write
from tests.conftest import EXAMPLE_CATALOG

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


@pytest.fixture
def sqlite_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, root)
    initialize(root)
    with CatalogStore.open(root) as store:
        store.apply_catalog_transaction(
            load_catalog(root),
            principal="fixture",
            reason="fixture",
            operation="seed",
            changed=(),
        )
    return root


def test_source_authoritative_add_refuses_without_mutation(
    sqlite_catalog: Path,
) -> None:
    before = CatalogStore.open(sqlite_catalog, read_only=True).revision
    with pytest.raises(
        WriteRefused, match=r"REFUSED  add hypervisor-proxmox\.host"
    ) as refused:
        write(
            sqlite_catalog,
            "add",
            "host",
            values={"id": "new-node", "role": "server"},
        )
    assert "cadastre collect --source hypervisor-proxmox" in str(refused.value)
    with CatalogStore.open(sqlite_catalog, read_only=True) as store:
        assert store.revision == before


def test_orchestrator_service_add_refuses_without_mutation(
    sqlite_catalog: Path,
) -> None:
    """The live service identity must arrive from the orchestrator collector."""
    before = CatalogStore.open(sqlite_catalog, read_only=True).revision
    with pytest.raises(
        WriteRefused, match=r"REFUSED  add orchestrator-gitops\.service"
    ) as refused:
        write(
            sqlite_catalog,
            "add",
            "service",
            "aidevenv-feat",
            values={"id": "aidevenv-feat", "runs_on": "node-b"},
        )
    assert "cadastre collect --source orchestrator-gitops" in str(refused.value)
    with CatalogStore.open(sqlite_catalog, read_only=True) as store:
        assert store.revision == before


def test_annotation_is_keyed_stamped_and_audited(sqlite_catalog: Path) -> None:
    result = write(
        sqlite_catalog,
        "annotate",
        "host",
        "app-01",
        {"tags": ["app-tier", "operator-owned"], "notes": "reviewed"},
        principal="agent-7",
        reason="placement review",
        now=NOW,
    )
    assert result.database_revision >= 2
    with CatalogStore.open(sqlite_catalog, read_only=True) as store:
        assert store.audit()[-1]["audit_id"] == result.audit_id
    catalog = CatalogStore.open(sqlite_catalog, read_only=True).read_catalog()
    entity = catalog.get("host", "app-01")
    assert entity is not None
    assert entity.tags[-1] == "operator-owned"
    assert catalog.annotations[("host", "app-01")]["principal"] == "agent-7"
    assert result.principal == "agent-7"


def test_orphaned_annotation_is_a_catalog_error(sqlite_catalog: Path) -> None:
    metadata = sqlite_catalog / "declared" / ".cadastre"
    metadata.mkdir(exist_ok=True)
    (metadata / "annotations.yaml").write_text(
        "annotations:\n"
        "  - kind: host\n"
        "    id: gone\n"
        "    values:\n"
        "      tags: [orphan]\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="orphaned annotation"):
        load_catalog(sqlite_catalog)


def test_stale_revision_is_rejected(sqlite_catalog: Path) -> None:
    with CatalogStore.open(sqlite_catalog) as store:
        catalog = store.read_catalog()
        with pytest.raises(UsageError, match="revision conflict"):
            store.apply_catalog_transaction(
                catalog,
                principal="test",
                reason="stale",
                operation="test",
                changed=(),
                expected_revision=0,
            )


def test_observed_store_is_always_refused(sqlite_catalog: Path) -> None:
    with pytest.raises(WriteRefused, match="observed evidence is generated"):
        write(
            sqlite_catalog,
            "annotate",
            "host",
            "app-01",
            {"tags": ["blocked"]},
            store="observed",
        )
