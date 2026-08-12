"""Operational SQLite lifecycle and interchange tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from cadastre.core import model
from cadastre.core.catalog import Catalog
from cadastre.core.loader import load_catalog
from cadastre.core.observed import ObservedSource
from cadastre.core.observed_db import history, record_source
from cadastre.core.storage import (
    CatalogStore,
    RuntimeStore,
    backup,
    export_bundle,
    import_bundle,
    initialize,
    integrity,
    restore,
)
from tests.conftest import EXAMPLE_CATALOG


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    shutil.copytree(EXAMPLE_CATALOG, root)
    initialize(root)
    with CatalogStore.open(root) as store:
        store.apply_catalog_transaction(
            load_catalog(root),
            principal="fixture",
            reason="seed",
            operation="seed",
            changed=(),
        )
    return root


def _secret_stores(catalog: Catalog) -> set[str]:
    """`Catalog.all` is typed to the Entity base, which has no `store`."""
    return {s.store for s in catalog.all("secret") if isinstance(s, model.Secret)}


def test_blank_initialization_is_durable_and_separate(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    result = initialize(root)
    assert result["empty"] is True
    assert (root / "catalog.sqlite3").exists()
    assert (root / "observed.sqlite3").exists()
    with CatalogStore.open(root, read_only=True) as store:
        assert store.revision == 0
        assert store.read_catalog().all("host") == []
    assert integrity(root)["schema_version"] == 1
    with RuntimeStore.open(root) as runtime:
        assert runtime.revisions() == {"catalog.sqlite3": 0, "observed.sqlite3": 0}


def test_load_declared_updates_fields_the_write_gate_refuses(tmp_path: Path) -> None:
    """The missing half of the round trip.

    The fields most likely to be wrong on a first run — a secret's `store`,
    a repo's forge id — are exactly the ones a source owns, so `update`
    refuses them. Without a reload there is no in-band fix at all: only
    export, hand-edit JSON, recompute the manifest checksum, import.
    """
    from cadastre.core.storage import load_declared

    root = _runtime(tmp_path)
    secrets = root / "declared" / "secrets" / "secrets.yaml"
    secrets.write_text(
        secrets.read_text().replace("store: secrets-manager", "store: infisical:cicd"),
        encoding="utf-8",
    )

    result = load_declared(root, root, principal="operator", reason="qualify stores")

    assert result["removed"] == []
    with CatalogStore.open(root, read_only=True) as store:
        stores = _secret_stores(store.read_catalog())
    assert "infisical:cicd" in stores
    assert "secrets-manager" not in stores


def test_load_declared_preserves_annotations_it_does_not_replace(
    tmp_path: Path,
) -> None:
    """The transaction underneath replaces the catalog wholesale.

    Annotations are catalog-owned edits made through the write gate and
    usually have no representation in `declared/`, so a reload would quietly
    delete every one of them — including the record of why someone last
    touched an entity.
    """
    from cadastre.core.storage import load_declared
    from cadastre.core.writes import write

    root = _runtime(tmp_path)
    write(
        root,
        "annotate",
        "host",
        "app-01",
        {"notes": "keep me"},
        principal="operator",
        reason="test",
    )

    result = load_declared(root, root)

    assert result["annotations_preserved"] == ["host:app-01"]
    with CatalogStore.open(root, read_only=True) as store:
        host = store.read_catalog().get("host", "app-01")
    assert host is not None
    assert host.notes == "keep me"


def test_load_declared_never_touches_collected_evidence(tmp_path: Path) -> None:
    """Intent in, evidence untouched.

    `import_legacy` also ingests `<root>/observed/*.json` because it seeds a
    fresh database from a whole file-tree catalog. Doing that on a reload
    merges a checkout's stale files into a live observed database that
    collectors own — resurrecting retired sources and inflating every
    `undeclared` count.
    """
    from cadastre.core.observed_db import load_sources
    from cadastre.core.storage import load_declared

    root = _runtime(tmp_path)
    record_source(
        root,
        ObservedSource(
            source="live-collector",
            plugin="fixture",
            as_of="2026-08-07T09:00:00Z",
            capabilities=("inventory.list",),
            entities={},
        ),
    )
    (root / "observed").mkdir(exist_ok=True)
    (root / "observed" / "retired.json").write_text(
        json.dumps(
            {
                "v": 1,
                "source": "retired-collector",
                "as_of": "2020-01-01T00:00:00Z",
                "ok": True,
                "capabilities": ["inventory.list"],
                "entities": {},
            }
        ),
        encoding="utf-8",
    )

    load_declared(root, root)

    assert {s.source for s in load_sources(root)} == {"live-collector"}


def test_load_declared_dry_run_reports_without_writing(tmp_path: Path) -> None:
    from cadastre.core.storage import load_declared

    root = _runtime(tmp_path)
    secrets = root / "declared" / "secrets" / "secrets.yaml"
    secrets.write_text(
        secrets.read_text().replace("store: secrets-manager", "store: infisical:cicd"),
        encoding="utf-8",
    )
    with CatalogStore.open(root, read_only=True) as store:
        before = store.revision

    result = load_declared(root, root, dry_run=True)

    assert result["dry_run"] is True
    assert result["updated"]
    with CatalogStore.open(root, read_only=True) as store:
        assert store.revision == before
        assert "secrets-manager" in _secret_stores(store.read_catalog())


def test_a_failed_init_leaves_no_data_directory_behind(tmp_path: Path) -> None:
    """`init` is all-or-nothing.

    A half-initialized directory is worse than none: the catalog is valid but
    EMPTY, every later command accepts it, and `drift` then reports the entire
    estate as `undeclared` — a confident, completely wrong answer. The obvious
    first-run mistake (`--from-bundle` at a `declared/` tree) used to produce
    exactly that.
    """
    from cadastre.cli.main import main

    root = tmp_path / "fresh"
    not_a_bundle = tmp_path / "declared"
    not_a_bundle.mkdir()

    assert main(["init", "--data-dir", str(root), "--from-bundle", str(not_a_bundle)])
    assert not root.exists()


def test_a_failed_init_never_deletes_an_existing_catalog(tmp_path: Path) -> None:
    """Rollback touches only what this run created."""
    from cadastre.cli.main import main

    root = tmp_path / "populated"
    initialize(root)
    before = (root / "catalog.sqlite3").read_bytes()
    not_a_bundle = tmp_path / "declared"
    not_a_bundle.mkdir()

    assert main(["init", "--data-dir", str(root), "--from-bundle", str(not_a_bundle)])
    assert (root / "catalog.sqlite3").read_bytes() == before


def test_runtime_store_rejects_partial_and_newer_observed_databases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "partial"
    initialize(root)
    (root / "observed.sqlite3").unlink()
    with pytest.raises(Exception, match="incomplete"):
        RuntimeStore.open(root)

    initialize(root)
    import sqlite3

    with sqlite3.connect(root / "observed.sqlite3") as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='format_version'")
        connection.commit()
    with pytest.raises(Exception, match="newer"):
        RuntimeStore.open(root)


def test_startup_health_does_not_expose_the_runtime_path(tmp_path: Path) -> None:
    from cadastre.core.storage import startup_check

    root = tmp_path / "private-runtime"
    initialize(root)
    result = startup_check(root)
    assert "data_dir" not in result
    assert result["lifecycle"]["state"] == "ready"


def test_export_is_deterministic_and_round_trips_history(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    host = load_catalog(root).get("host", "app-01")
    assert host is not None
    source = ObservedSource(
        source="fixture",
        plugin="fixture",
        as_of="2026-08-07T09:00:00Z",
        entities={"host": [host]},
    )
    record_source(root, source)
    with RuntimeStore.open(root) as runtime:
        assert runtime.observed_revision == 1
    first = tmp_path / "bundle-a"
    second = tmp_path / "bundle-b"
    export_bundle(root, first)
    export_bundle(root, second)
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.iterdir()
    } == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.iterdir()
    }
    assert history(root, source="fixture")
    restored = tmp_path / "restored"
    initialize(restored)
    import_bundle(restored, first)
    assert history(restored, source="fixture")
    assert integrity(restored)["databases"]["catalog.sqlite3"]["integrity"] == "ok"


def test_backup_restore_and_secret_rejection(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    backup_dir = tmp_path / "backup"
    manifest = backup(root, backup_dir)
    assert manifest["files"]
    assert set(manifest["revisions"]) == {"catalog.sqlite3", "observed.sqlite3"}
    restored = tmp_path / "restored"
    restore(restored, backup_dir)
    with CatalogStore.open(restored, read_only=True) as store:
        assert store.read_catalog().get("host", "app-01") is not None

    bundle = tmp_path / "secret-bundle"
    export_bundle(root, bundle)
    declared = json.loads((bundle / "declared.json").read_text())
    declared["host"][0]["password"] = "must-not-enter-catalog"
    (bundle / "declared.json").write_text(json.dumps(declared, indent=2) + "\n")
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["checksums"]["declared.json"] = hashlib.sha256(
        (bundle / "declared.json").read_bytes()
    ).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with pytest.raises(Exception, match="secret"):
        import_bundle(tmp_path / "secret-target", bundle)
