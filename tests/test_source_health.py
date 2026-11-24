"""WI-4558 — a source stale past 2x its TTL, or erroring, is an alert."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cadastre.cli.collect import collect
from cadastre.cli.drift import drift
from cadastre.core.observed import ObservedSource
from cadastre.core.source_health import assess, render_prometheus
from tests.conftest import NOW
from tests.test_collect_drift import _configure, _session


def _source(age: timedelta, error: str | None = None) -> ObservedSource:
    return ObservedSource(
        source="pve",
        plugin="hypervisor-proxmox",
        as_of=(NOW - age).isoformat().replace("+00:00", "Z"),
        capabilities=("inventory.list",),
        ok=error is None,
        error=error,
    )


def test_fresh_source_does_not_alert() -> None:
    (health,) = assess([_source(timedelta(hours=1))], NOW)
    assert not health.stale and not health.alerting


def test_stale_within_twice_ttl_is_not_yet_an_alert() -> None:
    (health,) = assess([_source(timedelta(hours=30))], NOW)
    assert health.stale and not health.overdue and not health.alerting


def test_stale_past_twice_ttl_alerts() -> None:
    (health,) = assess([_source(timedelta(hours=49))], NOW)
    assert health.overdue and health.alerting


def test_error_on_fresh_evidence_is_not_persistent() -> None:
    (health,) = assess([_source(timedelta(hours=1), "boom")], NOW)
    assert not health.alerting


def test_error_past_ttl_alerts() -> None:
    (health,) = assess([_source(timedelta(hours=25), "boom")], NOW)
    assert health.erroring and health.alerting


def test_ttl_overrides_apply() -> None:
    (health,) = assess(
        [_source(timedelta(hours=3))], NOW, ttl_overrides={"inventory.list": 3600}
    )
    assert health.overdue


def test_prometheus_exposition() -> None:
    text = render_prometheus(assess([_source(timedelta(hours=49), "x")], NOW))
    assert 'cadastre_source_overdue{source="pve",plugin="hypervisor-proxmox"} 1' in text
    assert 'cadastre_source_error{source="pve",plugin="hypervisor-proxmox"} 1' in text
    assert "# TYPE cadastre_source_age_seconds gauge" in text


def test_drift_reports_source_stale(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    collect(_session(catalog_copy))
    # The fixture collector stamps 09:00; the freshness TTL is one hour.
    fresh = drift(_session(catalog_copy, now=NOW - timedelta(hours=2, minutes=30)))
    assert "source-stale" not in fresh.data["counts"]

    late = _session(catalog_copy, now=NOW)
    document = drift(late, exit_code=True)
    rows = [d for d in document.data["divergences"] if d["category"] == "source-stale"]
    assert [r["id"] for r in rows] == ["fixture"]
    assert document.exit_code == 1
