from pathlib import Path

import pytest

from cadastre.core.errors import CatalogError
from cadastre.core.loader import load_catalog
from cadastre.core.storage import CatalogStore, initialize
from cadastre.manifest.model import WorkItem
from cadastre.modules.config import load_modules
from cadastre.modules.registry import EntityRegistry, active_registry


def manifest_root(tmp_path: Path) -> tuple[Path, EntityRegistry]:
    (tmp_path / "declared" / "work-items").mkdir(parents=True)
    (tmp_path / "declared" / "work-links").mkdir()
    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    return tmp_path, active_registry(load_modules(tmp_path))


def test_enabled_manifest_catalog_loads_and_preserves_kind(tmp_path: Path) -> None:
    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )

    catalog = load_catalog(root, registry=registry)

    item = catalog.get("work_item", "a")
    assert item is not None
    assert item.kind == "work_item"
    assert catalog.registry.kinds[-7:] == (
        "work_initiative",
        "work_item",
        "work_link",
        "forge_item",
        "markdown_finding",
        "repo_checkout",
        "revision_check",
    )


@pytest.mark.parametrize(
    "items, expected",
    [
        (
            "- id: a\n  title: A\n  state: open\n  priority: p1\n  "
            "created_at: '2026-08-10T00:00:00Z'\n  blocked_by: [b]\n"
            "- id: b\n  title: B\n  state: open\n  priority: p1\n  "
            "created_at: '2026-08-10T00:00:00Z'\n  blocked_by: [a]\n",
            "dependency cycle",
        ),
        (
            "id: a\ntitle: A\nstate: open\npriority: p1\ncreated_at: 'not-a-date'\n",
            "RFC 3339",
        ),
    ],
)
def test_manifest_invariants_are_rejected(
    tmp_path: Path, items: str, expected: str
) -> None:
    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(items, encoding="utf-8")

    with pytest.raises(CatalogError, match=expected):
        load_catalog(root, registry=registry)


def test_manifest_rows_survive_an_unrelated_write_transaction(tmp_path: Path) -> None:
    """Regression: `write()` used to snapshot only base kinds, so any add,
    update, delete, or annotate through the write gate silently deleted every
    Manifest-owned row from SQLite on its next `apply_catalog_transaction`."""
    from cadastre.core.writes import write

    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )
    catalog = load_catalog(root, registry=registry)
    initialize(root)
    with CatalogStore.open(root, create=True) as store:
        store.apply_catalog_transaction(
            catalog,
            principal="test",
            reason="seed",
            operation="import",
            changed=(("work_item", "a"),),
        )

    write(
        root,
        "add",
        "work_initiative",
        values={"id": "init-1", "title": "Q3", "weight": 5},
        principal="test",
        reason="unrelated write",
    )

    with CatalogStore.open(root, read_only=True) as store:
        loaded = store.read_catalog(registry=registry)
    assert loaded.get("work_item", "a") is not None
    assert loaded.get("work_initiative", "init-1") is not None


def test_export_bundle_includes_manifest_kinds(tmp_path: Path) -> None:
    from cadastre.core.storage import export_bundle

    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )
    catalog = load_catalog(root, registry=registry)
    initialize(root)
    with CatalogStore.open(root, create=True) as store:
        store.apply_catalog_transaction(
            catalog,
            principal="test",
            reason="seed",
            operation="import",
            changed=(("work_item", "a"),),
        )

    export_bundle(root, root / "bundle", include_observed=False)

    import json

    declared = json.loads((root / "bundle" / "declared.json").read_text())
    assert declared["work_item"][0]["id"] == "a"


def test_disabling_manifest_after_writing_refuses_writes_and_export(
    tmp_path: Path,
) -> None:
    """F03: a database that holds Manifest rows must refuse to write/export
    while the module is disabled, instead of silently dropping those rows
    (the bug fixed alongside this marker) or serving a smaller catalog."""
    from cadastre.core.errors import UsageError
    from cadastre.core.storage import export_bundle
    from cadastre.core.writes import write

    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )
    catalog = load_catalog(root, registry=registry)
    initialize(root)
    with CatalogStore.open(root, create=True) as store:
        store.apply_catalog_transaction(
            catalog,
            principal="test",
            reason="seed",
            operation="import",
            changed=(("work_item", "a"),),
        )
        assert "manifest" in store.required_modules

    (root / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: false\n", encoding="utf-8"
    )

    with pytest.raises(UsageError, match="REFUSED write"):
        write(
            root,
            "add",
            "work_initiative",
            values={"id": "init-1", "title": "Q3", "weight": 5},
            principal="test",
            reason="should be refused",
        )
    with pytest.raises(UsageError, match="REFUSED export"):
        export_bundle(root, root / "bundle")

    from cadastre.core.writes import write_metadata

    with pytest.raises(UsageError, match="REFUSED acknowledgements"):
        write_metadata(
            root,
            "acknowledgements",
            {
                "kind": "work_item",
                "id": "a",
                "field": "",
                "source": "test",
                "until": "2099-01-01T00:00:00Z",
            },
            principal="test",
            reason="should be refused",
        )


def test_update_and_annotate_work_through_the_write_gate(tmp_path: Path) -> None:
    """Regression: `update`/`annotate` merge the existing record through
    entity_to_dict() before re-validating it; that call defaulted to the
    base-only registry and crashed on any Manifest kind with
    "KeyError: <class 'cadastre.manifest.model.WorkItem'>"."""
    from cadastre.core.writes import write

    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )
    catalog = load_catalog(root, registry=registry)
    initialize(root)
    with CatalogStore.open(root, create=True) as store:
        store.apply_catalog_transaction(
            catalog,
            principal="test",
            reason="seed",
            operation="import",
            changed=(("work_item", "a"),),
        )

    write(
        root,
        "update",
        "work_item",
        "a",
        values={"priority": "p0"},
        principal="test",
        reason="reprioritize",
    )
    write(
        root,
        "annotate",
        "work_item",
        "a",
        values={"tags": ["urgent"]},
        principal="test",
        reason="tag",
    )

    with CatalogStore.open(root, read_only=True) as store:
        loaded = store.read_catalog(registry=registry)
    item = loaded.get("work_item", "a")
    assert isinstance(item, WorkItem)
    assert item.priority == "p0"
    assert item.tags == ("urgent",)


def test_enabled_manifest_rows_round_trip_through_sqlite(tmp_path: Path) -> None:
    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )
    catalog = load_catalog(root, registry=registry)
    initialize(root)
    with CatalogStore.open(root, create=True) as store:
        store.apply_catalog_transaction(
            catalog,
            principal="test",
            reason="Manifest round trip",
            operation="import",
            changed=(("work_item", "a"),),
        )
    with CatalogStore.open(root, read_only=True) as store:
        loaded = store.read_catalog(registry=registry)

    item = loaded.get("work_item", "a")
    assert isinstance(item, WorkItem)
    assert item.title == "Ship"


def test_work_item_origin_round_trips_yaml_and_sqlite(tmp_path: Path) -> None:
    from cadastre.core.serialize import entity_to_dict

    root, registry = manifest_root(tmp_path)
    digest = "a" * 64
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\norigin:\n"
        f"  - path: project/TODO.md\n    line: 7\n    digest: {digest}\n"
        "    run: migration-1\n"
        f"  - path: project/PLAN.md\n    line: 3\n    digest: {'b' * 64}\n"
        "    run: migration-1\n",
        encoding="utf-8",
    )
    catalog = load_catalog(root, registry=registry)
    item = catalog.get("work_item", "a")
    assert isinstance(item, WorkItem)
    assert [(origin.path, origin.line) for origin in item.origin] == [
        ("project/TODO.md", 7),
        ("project/PLAN.md", 3),
    ]
    initialize(root)
    with CatalogStore.open(root, create=True) as store:
        store.apply_catalog_transaction(
            catalog,
            principal="test",
            reason="origin round trip",
            operation="import",
            changed=(("work_item", "a"),),
        )
    with CatalogStore.open(root, read_only=True) as store:
        loaded = store.read_catalog(registry=registry)
    loaded_item = loaded.get("work_item", "a")
    assert isinstance(loaded_item, WorkItem)
    assert entity_to_dict(loaded_item, registry=registry) == entity_to_dict(
        item, registry=registry
    )


@pytest.mark.parametrize(
    ("entry", "field", "message", "line"),
    [
        (
            "path: /absolute.md\n    line: 1\n    digest: " + "a" * 64 + "\n    run: r",
            "path",
            "relative path",
            7,
        ),
        (
            "path: ../escape.md\n    line: 1\n    digest: " + "a" * 64 + "\n    run: r",
            "path",
            "relative path",
            7,
        ),
        (
            "path: TODO.md\n    line: 0\n    digest: " + "a" * 64 + "\n    run: r",
            "line",
            "must be positive",
            8,
        ),
        (
            "path: TODO.md\n    line: 1\n    digest: ABC\n    run: r",
            "digest",
            "lowercase SHA-256",
            9,
        ),
        (
            "path: TODO.md\n    line: 1\n    digest: " + "a" * 64 + "\n    run: ''",
            "run",
            "must not be empty",
            10,
        ),
    ],
)
def test_invalid_origin_is_rejected_at_its_nested_yaml_line(
    tmp_path: Path, entry: str, field: str, message: str, line: int
) -> None:
    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\norigin:\n  - " + entry + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(root, registry=registry)
    rendered = caught.value.render()
    assert f"declared/work-items/a.yaml:{line}" in rendered
    assert f"work_item[a].origin[0].{field}" in rendered
    assert message in rendered


def test_work_link_rejects_unknown_reflected_fields(tmp_path: Path) -> None:
    root, registry = manifest_root(tmp_path)
    (root / "declared/work-items/a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-10T00:00:00Z'\n",
        encoding="utf-8",
    )
    (root / "declared/work-links/l.yaml").write_text(
        "id: l\nwork_item: a\nforge: github\nrepo: org/repo\nkind: issue\n"
        "ref: '1'\ncompletion: closed\nreflect: [prioritry]\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="unsupported reflected fields"):
        load_catalog(root, registry=registry)
