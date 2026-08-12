"""M2 — provenance and staleness.

The property under test is not "provenance is usually present". It is that a
response without it cannot be constructed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cadastre.core.errors import CadastreError
from cadastre.core.provenance import (
    Provenance,
    ProvenanceSet,
    Response,
    evaluate,
    format_timestamp,
    parse_timestamp,
    ttl_for,
)

FRESH = Provenance("dns", "cloudflare", "2026-08-07T09:00:00Z", ttl_seconds=3600)
NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


def test_a_response_without_provenance_is_unrepresentable() -> None:
    with pytest.raises(CadastreError):
        Response(result={"hosts": []}, provenance=())


def test_a_response_with_provenance_is_fine() -> None:
    response: Response[dict[str, list[str]]] = Response(
        result={"hosts": []}, provenance=(FRESH,)
    )
    assert response.to_dict()["provenance"][0]["source"] == "dns"


def test_staleness_is_decided_against_a_clock() -> None:
    assert evaluate(FRESH, NOW).stale is False
    assert evaluate(FRESH, NOW + timedelta(hours=2)).stale is True


def test_a_source_already_stale_stays_stale_however_recent() -> None:
    """A failure to refresh is not cured by the timestamp being new."""
    failed = Provenance(
        "dns", "cloudflare", format_timestamp(NOW), stale=True, error="unreachable"
    )
    assert evaluate(failed, NOW).stale is True


def test_freshness_thresholds_are_per_capability() -> None:
    assert ttl_for("dns.records") < ttl_for("inventory.list")


def test_operator_overrides_win_over_defaults() -> None:
    assert ttl_for("dns.records", {"dns.records": 60}) == 60
    assert ttl_for("dns.records", {"dns": 120}) == 120
    assert ttl_for("anything.at.all", {"default": 7}) == 7


def test_timestamps_round_trip_through_z_suffix() -> None:
    assert format_timestamp(parse_timestamp("2026-08-07T09:00:00Z")) == (
        "2026-08-07T09:00:00Z"
    )
    assert parse_timestamp("2026-08-07T10:00:00+01:00") == parse_timestamp(
        "2026-08-07T09:00:00Z"
    )


def test_a_bad_timestamp_is_an_error_not_a_silent_zero() -> None:
    with pytest.raises(CadastreError):
        parse_timestamp("last tuesday")


def test_the_worst_news_about_a_source_survives_deduplication() -> None:
    collected = ProvenanceSet()
    collected.add(FRESH)
    collected.add(Provenance("dns", "cloudflare", FRESH.as_of, stale=True))
    assert collected.frozen()[0].stale is True
