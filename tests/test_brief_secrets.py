"""GitHub #24 — `brief` must not present declared secret_refs as the whole set.

An agent treats `brief` as the estate map. When it lists only the declared
subset and hides the observed-only secrets a collector has seen, the map lies
by omission and the session concludes "no such credential" for a secret already
in the store.
"""

from __future__ import annotations

import dataclasses

from cadastre.cli.brief import brief
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.observed import ObservedSource
from cadastre.render.text import render

AS_OF = "2026-09-02T07:00:15Z"


def _observed_secrets(*secrets: model.Secret) -> ObservedSource:
    return ObservedSource(
        source="secrets-apps",
        plugin="secrets-infisical",
        as_of=AS_OF,
        capabilities=("secret.list",),
        entities={"secret": list(secrets)},
    )


def _with_observed(session: Session, source: ObservedSource) -> Session:
    return dataclasses.replace(session, observed=(source,))


def test_observed_only_secrets_are_counted_in_brief(session: Session) -> None:
    declared = len(session.catalog.secrets)
    source = _observed_secrets(
        model.Secret(
            id="infisical:apps-homelab-farmeggs-dev-api-secret",
            ref="infisical://apps/prod/HOMELAB_FARMEGGS_DEV_API_SECRET",
            store="infisical:apps",
        ),
        model.Secret(
            id="infisical:apps-linear-api-key",
            ref="infisical://apps/prod/LINEAR_API_KEY",
            store="infisical:apps",
        ),
    )
    document = brief(_with_observed(session, source))

    assert document.data["secrets"]["declared"] == declared
    assert document.data["secrets"]["observed_only_total"] == 2
    assert document.data["secrets"]["observed_only_by_store"] == {"infisical:apps": 2}
    text = " ".join(render(document).split())
    assert f"{declared} declared / 2 observed-only" in text


def test_a_declared_ref_is_not_double_counted_as_observed_only(
    session: Session,
) -> None:
    """A secret the collector observed that a declaration already names is not
    observed-only — it is confirmation, and must not inflate the count."""
    declared_secret = session.catalog.secrets[0]
    source = _observed_secrets(
        model.Secret(
            id=declared_secret.id,
            ref=declared_secret.ref,
            store=declared_secret.store,
        )
    )
    document = brief(_with_observed(session, source))
    assert document.data["secrets"]["observed_only_total"] == 0


def test_no_observed_secrets_keeps_the_plain_note(session: Session) -> None:
    document = brief(session)
    assert document.data["secrets"]["observed_only_total"] == 0
    text = " ".join(render(document).split())
    assert "No value ever transits this layer." in text
