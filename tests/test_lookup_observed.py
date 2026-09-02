"""GitHub #19 — observed-only entities must be reachable, and honestly labelled.

Cadastre answered `missing_entity` for infrastructure it had itself observed,
from a fresh collector run, and told the caller the catalog was wrong. The
evidence was retained the whole time, reachable only inside a ~165 KB `drift`
dump. These tests pin the query path, and pin that reaching an observation is
not the same as promoting it (DESIGN §1.3).
"""

from __future__ import annotations

import dataclasses

import pytest

from cadastre.cli.lookup import lookup
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import AmbiguousEntityError, MissingEntityError
from cadastre.core.observed import ObservedSource
from cadastre.render.text import render


def render_text(document: object) -> str:
    """Rendered text with wrapping collapsed, so an assertion about a phrase
    is not really an assertion about the terminal width."""
    return " ".join(render(document).split())  # type: ignore[arg-type]


AS_OF = "2026-08-07T11:55:00Z"

#: The shape the orchestrator collector emits: one entity per compose stack,
#: with the constituent container names one altitude down.
LOKI_STACK = model.Service(
    id="grafanaloki",
    extra={
        "x-orchestrator": {
            "compose_services": [
                {"name": "loki"},
                {"name": "promtail"},
            ],
            "host_attribution": "unknown",
        }
    },
)


def _observed(*entities: model.Entity, source: str = "orchestrator") -> ObservedSource:
    by_kind: dict[str, list[model.Entity]] = {}
    for entity in entities:
        by_kind.setdefault(_kind_of(entity), []).append(entity)
    return ObservedSource(
        source=source,
        plugin="orchestrator-gitops",
        as_of=AS_OF,
        capabilities=("inventory.list",),
        entities=by_kind,
    )


def _kind_of(entity: model.Entity) -> str:
    return {
        model.Service: "service",
        model.Host: "host",
        model.Endpoint: "endpoint",
    }[type(entity)]


def _with(session: Session, *sources: ObservedSource) -> Session:
    return dataclasses.replace(session, observed=tuple(sources))


def test_an_observed_only_entity_is_reachable_at_all(session: Session) -> None:
    """The defect: `lookup grafanaloki` was `missing_entity` while the same
    catalog held the observation."""
    document = lookup(_with(session, _observed(LOKI_STACK)), "grafanaloki")

    assert document.data["kind"] == "service"
    assert document.data["resolution"] == "observed-only"
    assert document.data["observed"][0]["source"] == "orchestrator"
    assert document.data["observed"][0]["as_of"] == AS_OF


def test_an_observation_is_never_presented_as_a_declaration(session: Session) -> None:
    """Reachable is not reconciled. §1.3 forbids promotion, not reporting."""
    document = lookup(_with(session, _observed(LOKI_STACK)), "grafanaloki")

    assert document.data["declared"] is False
    assert document.data["declared_at"] is None
    assert document.data["relations"] == []
    text = render_text(document)
    assert "observed, not declared" in text
    assert "does not promote" in text
    assert "cadastre add" in text


def test_a_declared_entity_still_answers_as_declared(session: Session) -> None:
    document = lookup(_with(session, _observed(LOKI_STACK)), "notes-api")

    assert document.data["resolution"] == "declared"
    assert document.data["declared"] is True
    assert document.data["entity"]["id"] == "notes-api"


def test_a_name_inside_a_stack_resolves_to_the_stack(session: Session) -> None:
    """The name a human knows the workload by was not a name the catalog held:
    `lookup loki` failed even once `grafanaloki` was known."""
    document = lookup(_with(session, _observed(LOKI_STACK)), "loki")

    assert document.data["resolution"] == "contained-in"
    assert document.data["query"] == "loki"
    assert document.data["contained_in"] == {
        "kind": "service",
        "id": "grafanaloki",
        "declared": False,
        "via": "x-orchestrator.compose_services",
        "source": "orchestrator",
    }
    text = render_text(document)
    assert "is not an entity" in text
    assert "Containment, not identity" in text


def test_containment_reaches_a_declared_container_too(session: Session) -> None:
    """The extension block indexes the same way whichever side carries it."""
    declared = session.catalog.of("service")["notes-api"]
    stack = dataclasses.replace(
        declared,
        extra={"x-orchestrator": {"compose_services": [{"name": "notes-worker"}]}},
    )
    catalog = dataclasses.replace(
        session.catalog,
        entities={
            **session.catalog.entities,
            "service": {**session.catalog.of("service"), "notes-api": stack},
        },
    )
    document = lookup(dataclasses.replace(session, catalog=catalog), "notes-worker")

    assert document.data["contained_in"]["id"] == "notes-api"
    assert document.data["contained_in"]["declared"] is True
    assert document.data["contained_in"]["source"] is None


def test_a_name_in_several_stacks_lists_them_rather_than_guessing(
    session: Session,
) -> None:
    other = model.Service(
        id="lokitwo",
        extra={"x-orchestrator": {"compose_services": [{"name": "loki"}]}},
    )
    document = lookup(_with(session, _observed(LOKI_STACK, other)), "loki")

    assert document.data["resolution"] == "contained-in"
    assert [row["id"] for row in document.data["containers"]] == [
        "grafanaloki",
        "lokitwo",
    ]


def test_a_genuinely_absent_id_still_says_so(session: Session) -> None:
    """The message stays honest in both directions: when nothing was declared
    AND nothing was observed, the caller should be told the name is unknown."""
    with pytest.raises(MissingEntityError) as caught:
        lookup(_with(session, _observed(LOKI_STACK)), "nothing-here")

    message = str(caught.value)
    assert "no entity with id" in message
    assert "no collector has observed one" in message


def test_kind_narrows_the_observed_side_as_well(session: Session) -> None:
    with pytest.raises(MissingEntityError):
        lookup(_with(session, _observed(LOKI_STACK)), "grafanaloki", kind="host")
    document = lookup(
        _with(session, _observed(LOKI_STACK)), "grafanaloki", kind="service"
    )
    assert document.data["resolution"] == "observed-only"


def test_an_id_observed_as_two_kinds_asks_for_kind(session: Session) -> None:
    source = _observed(
        model.Service(id="collide"),
        model.Host(id="collide"),
    )
    with pytest.raises(AmbiguousEntityError, match="--kind"):
        lookup(_with(session, source), "collide")


def test_an_unattributed_service_says_so_rather_than_reading_as_host_less(
    session: Session,
) -> None:
    document = lookup(_with(session, _observed(LOKI_STACK)), "grafanaloki")
    text = render_text(document)
    assert "unattributed, not host-less" in text


def test_a_host_lists_what_collectors_place_on_it(session: Session) -> None:
    placed = model.Service(id="placed-stack", runs_on="app-01")
    document = lookup(_with(session, _observed(placed, LOKI_STACK)), "app-01")

    assert document.data["observed_on_host"] == [
        {"source": "orchestrator", "kind": "service", "id": "placed-stack"}
    ]
    assert document.data["unattributed_observations"] == [
        {"source": "orchestrator", "count": 1}
    ]
    text = render_text(document)
    assert "Observed on this host" in text
    # "nothing runs here" and "nobody could tell me" are different answers.
    assert "lower bound" in text


def test_a_host_with_nothing_observed_does_not_invent_a_warning(
    session: Session,
) -> None:
    placed = model.Service(id="placed-stack", runs_on="app-01")
    document = lookup(_with(session, _observed(placed)), "app-01")

    assert document.data["unattributed_observations"] == []
    assert "lower bound" not in render_text(document)


# -- GitHub #23: observed-only entities found by natural name or ref ---------

FARMEGGS_SECRET = model.Secret(
    id="infisical:apps-homelab-farmeggs-dev-api-secret",
    ref="infisical://apps/prod/HOMELAB_FARMEGGS_DEV_API_SECRET",
    store="infisical:apps",
)


def _observed_secret(*secrets: model.Secret) -> ObservedSource:
    return ObservedSource(
        source="secrets-apps",
        plugin="secrets-infisical",
        as_of=AS_OF,
        capabilities=("secret.list",),
        entities={"secret": list(secrets)},
    )


def test_observed_secret_is_found_by_its_natural_name(session: Session) -> None:
    """The FarmEggs incident: `lookup homelab-farmeggs-dev-api-secret` said the
    catalog had never observed it, while the store-keyed id resolved fine."""
    document = lookup(
        _with(session, _observed_secret(FARMEGGS_SECRET)),
        "homelab-farmeggs-dev-api-secret",
        kind="secret",
    )

    assert document.data["resolution"] == "name-match"
    assert document.data["declared"] is False
    ids = [candidate["id"] for candidate in document.data["candidates"]]
    assert "infisical:apps-homelab-farmeggs-dev-api-secret" in ids


def test_observed_secret_is_found_by_its_reference_key(session: Session) -> None:
    """The uppercase env-key form folds to the same name and still resolves."""
    document = lookup(
        _with(session, _observed_secret(FARMEGGS_SECRET)),
        "HOMELAB_FARMEGGS_DEV_API_SECRET",
    )
    assert document.data["resolution"] == "name-match"
    assert document.data["candidates"][0]["id"] == FARMEGGS_SECRET.id


def test_ref_only_match_reports_it_matched_on_ref(session: Session) -> None:
    """A secret whose id does not contain the key is still found via the ref."""
    opaque = model.Secret(
        id="infisical:apps-secret-0007",
        ref="infisical://apps/prod/STRIPE_WEBHOOK_SIGNING_KEY",
        store="infisical:apps",
    )
    document = lookup(
        _with(session, _observed_secret(opaque)), "stripe-webhook-signing-key"
    )
    assert document.data["resolution"] == "name-match"
    assert document.data["candidates"][0]["matched_on"] == "ref"


def test_exact_store_id_still_resolves_as_observed_only(session: Session) -> None:
    """The fallback does not disturb the exact-id path that already worked."""
    document = lookup(
        _with(session, _observed_secret(FARMEGGS_SECRET)),
        "infisical:apps-homelab-farmeggs-dev-api-secret",
    )
    assert document.data["resolution"] == "observed-only"


def test_a_truly_unknown_name_still_says_nothing_observed(session: Session) -> None:
    with pytest.raises(MissingEntityError) as caught:
        lookup(_with(session, _observed_secret(FARMEGGS_SECRET)), "no-such-thing-here")
    assert "no collector has observed one" in str(caught.value)


def test_a_short_query_does_not_drag_in_everything(session: Session) -> None:
    """A two-character query must not name-match the whole estate."""
    with pytest.raises(MissingEntityError):
        lookup(_with(session, _observed_secret(FARMEGGS_SECRET)), "ap")


# -- GitHub #28: declared is not the same as collector-confirmed -------------


def _a_declared_host(session: Session) -> str:
    return next(iter(session.catalog.of("host")))


def test_declared_host_with_no_collector_is_marked_unobserved(
    session: Session,
) -> None:
    """A declared host no collector reports must not read as live truth."""
    host_id = _a_declared_host(session)
    document = lookup(session, host_id)  # no observed sources at all

    assert document.data["resolution"] == "declared"
    assert document.data["confirmation"]["status"] == "unobserved"
    text = render_text(document)
    assert "Not confirmed by a collector" in text
    assert "declaration only" in text


def test_declared_host_a_collector_confirms_is_confirmed(session: Session) -> None:
    host_id = _a_declared_host(session)
    observed_host = model.Host(id=host_id)
    document = lookup(_with(session, _observed(observed_host)), host_id)

    assert document.data["confirmation"]["status"] == "confirmed"
    assert document.data["confirmation"]["collectors"] == ["orchestrator"]
    assert "Not confirmed by a collector" not in render_text(document)


def test_declared_host_a_collector_ran_but_missed_is_unconfirmed(
    session: Session,
) -> None:
    host_id = _a_declared_host(session)
    other = model.Host(id="some-other-host-entirely")
    document = lookup(_with(session, _observed(other)), host_id)

    assert document.data["confirmation"]["status"] == "unconfirmed"
    assert document.data["confirmation"]["collectors"] == ["orchestrator"]
    assert "none reported this id" in render_text(document)
