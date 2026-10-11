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
        WriteRefused, match=r"REFUSED  add hypervisor-hyperv\.host"
    ) as refused:
        write(
            sqlite_catalog,
            "add",
            "host",
            values={"id": "new-node", "role": "server"},
        )
    assert "cadastre collect --source hypervisor-hyperv" in str(refused.value)
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


# -- WI-1257: a third-party provider has no source of record but the catalog --


def _add(root: Path, kind: str, values: dict[str, object]) -> None:
    write(root, "add", kind, values=values, principal="test", reason="provider")


def test_an_external_provider_is_catalog_authored(sqlite_catalog: Path) -> None:
    """No collector reflects a vendor endpoint we call, so host role
    `external`, a service on it, and that service's endpoint are authored
    through the gated write path rather than refused (DESIGN §2.1)."""
    _add(
        sqlite_catalog,
        "host",
        {"id": "llm-vendor", "role": "external", "reachable_from": ["edge-net"]},
    )
    _add(
        sqlite_catalog,
        "service",
        {"id": "llm-vendor-api", "runs_on": "llm-vendor", "tags": ["llm"]},
    )
    _add(
        sqlite_catalog,
        "endpoint",
        {
            "id": "llm-vendor-https",
            "service": "llm-vendor-api",
            "network": "edge-net",
            "address": "api.vendor.example.invalid",
            "protocol": "https",
        },
    )
    catalog = CatalogStore.open(sqlite_catalog, read_only=True).read_catalog()
    assert catalog.get("host", "llm-vendor") is not None
    assert catalog.get("service", "llm-vendor-api") is not None
    assert catalog.get("endpoint", "llm-vendor-https") is not None

    write(sqlite_catalog, "delete", "endpoint", "llm-vendor-https")
    catalog = CatalogStore.open(sqlite_catalog, read_only=True).read_catalog()
    assert catalog.get("endpoint", "llm-vendor-https") is None


def test_the_external_exemption_does_not_reach_estate_entities(
    sqlite_catalog: Path,
) -> None:
    """A service on one of our hosts, and a secret even for a provider, are
    still owned by their collectors."""
    _add(sqlite_catalog, "host", {"id": "llm-vendor", "role": "external"})
    with pytest.raises(WriteRefused, match=r"REFUSED  add orchestrator-gitops"):
        _add(sqlite_catalog, "service", {"id": "rogue", "runs_on": "app-01"})
    with pytest.raises(WriteRefused, match=r"REFUSED  add .*\.secret"):
        _add(
            sqlite_catalog,
            "secret",
            {"id": "llm-key", "ref": "/prod/llm/key", "store": "secrets-manager"},
        )
    with pytest.raises(WriteRefused, match=r"REFUSED  delete .*\.host"):
        write(sqlite_catalog, "delete", "host", "app-01")


def test_a_malformed_add_on_a_source_kind_is_still_refused(
    sqlite_catalog: Path,
) -> None:
    with pytest.raises(WriteRefused, match=r"REFUSED  add"):
        _add(sqlite_catalog, "host", {"id": "x", "role": "not-a-role"})


def test_a_provider_is_updatable_but_cannot_be_used_to_launder_authority(
    sqlite_catalog: Path,
) -> None:
    """The review case: relabelling an estate host `external` would make it
    deletable. `role` stays reflected for estate hosts, and an update only
    keeps the exemption if the entity is a provider's before and after."""
    _add(sqlite_catalog, "host", {"id": "llm-vendor", "role": "external"})
    _add(sqlite_catalog, "service", {"id": "llm-api", "runs_on": "llm-vendor"})
    write(sqlite_catalog, "update", "service", "llm-api", {"expose": "none"})
    catalog = CatalogStore.open(sqlite_catalog, read_only=True).read_catalog()
    assert catalog.get("service", "llm-api").expose == "none"  # type: ignore[union-attr]

    with pytest.raises(WriteRefused, match=r"REFUSED update host"):
        write(sqlite_catalog, "update", "host", "app-01", {"role": "external"})
    with pytest.raises(WriteRefused, match=r"REFUSED update service"):
        write(sqlite_catalog, "update", "service", "llm-api", {"runs_on": "app-01"})
    with pytest.raises(WriteRefused, match=r"REFUSED update host"):
        write(sqlite_catalog, "update", "host", "llm-vendor", {"role": "server"})
