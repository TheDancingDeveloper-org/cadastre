"""M12 durable trust-state tests."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cadastre.cli.session import Session
from cadastre.cli.trust import acknowledge, resolve, stale
from cadastre.core.loader import load_catalog
from cadastre.core.observed import ObservedSource, load_observed
from cadastre.core.trust import (
    active_acknowledgements,
    load_records,
    presented_records,
    unverified_sources,
    update_records,
    write_records,
)
from tests.conftest import EXAMPLE_CATALOG

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def _sqlite_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, root)
    from cadastre.core.storage import CatalogStore, initialize

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


def _source(root: Path, note: str, role: str = "server") -> ObservedSource:
    payload = {
        "v": 1,
        "source": "fixture",
        "plugin": "fixture",
        "as_of": "2026-08-07T12:00:00Z",
        "ok": True,
        "capabilities": ["inventory.list"],
        "entities": {"host": [{"id": "app-01", "role": role, "notes": note}]},
    }
    from cadastre.core.storage import record_observed

    record_observed(root, payload)
    return load_observed(root, now=NOW)[0]


def test_first_seen_is_stable_across_repeated_contests(tmp_path: Path) -> None:
    root = _sqlite_catalog(tmp_path)
    from cadastre.core.loader import load_catalog

    catalog = load_catalog(root)
    first = update_records(root, catalog, _source(root, "changed"), NOW)
    write_records(root, first)
    later = update_records(
        root, catalog, _source(root, "changed"), NOW + timedelta(days=1)
    )
    record = later[0]
    assert record.state == "contested"
    assert record.first_seen == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert record.last_seen == "2026-08-08T12:00:00Z"


def test_flapping_is_reported_separately(tmp_path: Path) -> None:
    root = _sqlite_catalog(tmp_path)
    from cadastre.core.loader import load_catalog

    catalog = load_catalog(root)
    write_records(root, update_records(root, catalog, _source(root, "one"), NOW))
    records = update_records(
        root,
        catalog,
        _source(root, "two", role="workstation"),
        NOW + timedelta(hours=1),
    )
    role = next(record for record in records if record.field == "role")
    assert role.flapping is True
    assert records[0].first_seen == "2026-08-07T12:00:00Z"


def test_accept_observed_is_explicit_and_persists(tmp_path: Path) -> None:
    root = _sqlite_catalog(tmp_path)
    from cadastre.core.loader import load_catalog

    catalog = load_catalog(root)
    records = update_records(root, catalog, _source(root, "changed"), NOW)
    write_records(root, records)
    session = Session.open(root, now=NOW, as_of="2026-08-07T08:00:00Z")
    result = resolve(
        session,
        "accept-observed",
        "host:app-01",
        source="fixture",
        field="role",
        principal="reviewer",
        reason="verified upstream",
    )
    assert result.data["action"] == "accept-observed"
    assert presented_records(root, NOW)[0].state == "agreed"
    assert load_records(root)[0].state == "contested"


def test_expired_acknowledgement_is_not_active(tmp_path: Path) -> None:
    root = _sqlite_catalog(tmp_path)
    session = Session.open(root, now=NOW, as_of="2026-08-07T08:00:00Z")
    acknowledge(
        session,
        "host:app-01",
        source="fixture",
        until="2026-08-08T12:00:00Z",
        reason="waiting for upstream owner",
        principal="reviewer",
    )
    assert len(active_acknowledgements(root, NOW)) == 1
    assert not active_acknowledgements(root, NOW + timedelta(days=2))


def test_unverified_is_separate_from_contested() -> None:
    assert unverified_sources(Path("/unused"), ("dns", "vpn"), ("dns",)) == ("vpn",)


def test_stale_reports_unverified_configured_source(catalog_copy: Path) -> None:
    (catalog_copy / "declared" / "plugins.yaml").write_text(
        "sources:\n  - id: never-seen\n    command: [true]\n",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of="2026-08-07T08:00:00Z")
    document = stale(session)
    assert {
        item["source"]
        for item in document.data["items"]
        if item["state"] == "unverified"
    } == {"never-seen"}
