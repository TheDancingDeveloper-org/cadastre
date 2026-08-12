"""M6 — placement.

Plain constraint filtering with unit tests. No solver, no model call. The
exclusion assertions matter as much as the candidate ones: an exclusion the
reader believes is wrong is how a wrong catalog becomes visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cadastre.cli.session import Session
from cadastre.core.catalog import Catalog
from cadastre.core.placement import parse_intent, placement_for
from tests.conftest import DECLARED_AS_OF, NOW


def _catalog(session: Session) -> Catalog:
    return session.catalog


def _excluded(session: Session, intent: str) -> dict[str, str]:
    placement = placement_for(_catalog(session), intent)
    return {e.host: f"{e.reason}: {e.detail}" for e in placement.exclusions}


def _candidates(session: Session, intent: str) -> list[str]:
    return [c.host for c in placement_for(_catalog(session), intent).candidates]


# -- intent parsing ---------------------------------------------------------


def test_intent_parsing_is_keywords_only(session: Session) -> None:
    requirements = parse_intent(
        "deploy an internal service with a gpu on port 9000 named reports",
        _catalog(session),
    )
    assert requirements.expose == "internal"
    assert requirements.needs_gpu
    assert requirements.port == 9000
    assert requirements.name == "reports"


def test_sizes_are_read_from_the_intent(session: Session) -> None:
    requirements = parse_intent("needs 24 gb of ram and 8 cores", _catalog(session))
    assert requirements.memory_gb == 24
    assert requirements.cpu_cores == 8


def test_words_it_did_not_understand_are_reported(session: Session) -> None:
    requirements = parse_intent("deploy a low-latency kafka broker", _catalog(session))
    assert "kafka" in requirements.unrecognised
    assert "low-latency" in requirements.unrecognised


def test_public_synonyms_resolve_to_the_declared_tier(session: Session) -> None:
    assert parse_intent("something internet-facing", _catalog(session)).expose == (
        "public"
    )


@pytest.mark.parametrize(
    "intent",
    [
        "deploy a service that must not be public",
        "deploy a service without a gpu",
        "deploy anywhere except host-a",
        "deploy on host-b, not host-a",
        "deploy a public overlay-only service",
    ],
)
def test_negated_or_contradictory_constraints_fail_closed(
    session: Session, intent: str
) -> None:
    requirements = parse_intent(intent, _catalog(session))
    placement = placement_for(_catalog(session), intent)
    assert requirements.parse_conflicts
    assert not placement.candidates


def test_tags_are_taken_from_the_catalog_not_a_fixed_list(session: Session) -> None:
    assert parse_intent("an app-tier service", _catalog(session)).tags == ("app-tier",)


def test_explicit_host_selection_accepts_the_human_spaced_form(
    session: Session,
) -> None:
    intent = "deploy an internal service on app 01"
    assert parse_intent(intent, _catalog(session)).target_host == "app-01"
    assert _candidates(session, intent) == ["app-01"]
    assert "not requested host" in _excluded(session, intent)["app-02"]


# -- filtering --------------------------------------------------------------


def test_a_gpu_workload_lands_on_the_accelerated_host(session: Session) -> None:
    assert _candidates(session, "an internal batch worker that needs a gpu") == [
        "app-02"
    ]


def test_every_rejection_says_why(session: Session) -> None:
    placement = placement_for(_catalog(session), "an internal service with a gpu")
    assert placement.exclusions
    for exclusion in placement.exclusions:
        assert exclusion.reason
        assert exclusion.detail


def test_a_workstation_is_never_a_placement_target(session: Session) -> None:
    assert (
        "not a deployment target" in _excluded(session, "an internal service")["ws-01"]
    )


def test_a_hypervisor_is_excluded_in_favour_of_its_guests(session: Session) -> None:
    assert "place on a guest" in _excluded(session, "an internal service")["hv-01"]


def test_exposure_excludes_hosts_with_no_matching_network_class(
    session: Session,
) -> None:
    reason = _excluded(session, "a public service")["app-01"]
    assert "no network of class `public`" in reason
    assert "lab-net (private)" in reason


def test_exposure_tier_requires_its_exact_network_not_just_its_class(
    catalog_copy: Path,
) -> None:
    policy = catalog_copy / "declared" / "policy" / "exposure.yaml"
    policy.write_text(
        policy.read_text(encoding="utf-8")
        + "- name: overlay\n  network_class: private\n  network: mgmt-net\n",
        encoding="utf-8",
    )
    hosts = catalog_copy / "declared" / "hosts" / "hosts.yaml"
    hosts.write_text(
        hosts.read_text(encoding="utf-8")
        .replace(
            "- id: app-02\n  tags:\n    - app-tier\n    - container-host\n    - gpu\n",
            "- id: app-02\n  tags:\n    - app-tier\n    - container-host\n    - gpu\n",
        )
        .replace(
            "  reachable_from:\n    - lab-net\n  resources:\n    cpu_cores: 16",
            "  reachable_from:\n    - mgmt-net\n  resources:\n    cpu_cores: 16",
        ),
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    assert _candidates(session, "an overlay service") == ["app-02"]
    detail = _excluded(session, "an overlay service")["app-01"]
    assert "requires network `mgmt-net`" in detail


def test_a_grant_deny_keeps_a_host_out_of_the_candidate_list(
    session: Session,
) -> None:
    """`context-for` should not propose what the Broker will later refuse."""
    reason = _excluded(session, "an internal service")["data-01"]
    assert "denied by grant `g-001`" in reason
    assert "persistent-data" in reason


def test_asking_for_the_denied_tag_explicitly_lifts_the_exclusion(
    session: Session,
) -> None:
    assert "data-01" in _candidates(session, "an internal persistent-data service")


def test_an_unknown_resource_is_not_treated_as_zero_or_as_enough(
    session: Session,
) -> None:
    reason = _excluded(session, "an internal service with 4 cores")["rtr-01"]
    assert "role `router`" in reason
    placement = placement_for(_catalog(session), "an internal service with 64 gb")
    assert "app-01" in {e.host for e in placement.exclusions}


def test_insufficient_resources_report_both_numbers(session: Session) -> None:
    assert (
        "has 32, needs 64"
        in _excluded(session, "an internal service with 64 gb")["app-01"]
    )


def test_a_candidate_explains_why_it_qualifies(session: Session) -> None:
    placement = placement_for(_catalog(session), "a public service")
    assert placement.candidates
    assert "class public" in " ".join(placement.candidates[0].because)


# -- conflicts --------------------------------------------------------------


def test_a_taken_port_is_reported_as_a_conflict(session: Session) -> None:
    placement = placement_for(_catalog(session), "an internal service on port 8080")
    assert any(c.kind == "port" and "app-01" in c.subject for c in placement.conflicts)


def test_a_taken_hostname_is_reported_as_a_conflict(session: Session) -> None:
    placement = placement_for(
        _catalog(session), "an internal service at notes.internal.example.invalid"
    )
    assert any(c.kind == "hostname" for c in placement.conflicts)


def test_a_taken_service_name_is_reported_as_a_conflict(session: Session) -> None:
    placement = placement_for(_catalog(session), "an internal service named notes-api")
    assert any(c.kind == "service name" for c in placement.conflicts)


# -- the visibly-wrong-catalog property -------------------------------------


def test_a_seeded_wrong_fact_produces_a_visibly_wrong_exclusion(
    catalog_copy: Path,
) -> None:
    """M6's definition of done. Remove the GPU from the only accelerated host
    and the tool must exclude it *for that stated reason*, rather than quietly
    proposing somewhere else."""
    from tests.conftest import DECLARED_AS_OF, NOW

    hosts = catalog_copy / "declared" / "hosts" / "hosts.yaml"
    hosts.write_text(
        hosts.read_text(encoding="utf-8").replace(
            "    gpu: generic-accelerator-16gb\n", ""
        ),
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    placement = placement_for(session.catalog, "an internal worker that needs a gpu")
    assert placement.candidates == ()
    assert _excluded(session, "an internal worker that needs a gpu")[
        "app-02"
    ].startswith("no GPU")
