"""WI-1257 / WI-1258 — a third-party provider, declared and then found.

An LLM or API vendor is something the estate depends on but does not run.
Declaring one used to mean inventing a pseudo-host with a role that
misdescribed it (`appliance`), and once declared nothing session-facing could
find it without guessing its id: `context_for` is a placement query and
`lookup` matched ids and names, never tags.

These tests pin both halves: host role `external` loads, checks clean and is
never a placement target; `lookup tag:<x>` and the `brief` services table
make a tagged provider discoverable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cadastre.cli.brief import brief
from cadastre.cli.lookup import lookup
from cadastre.cli.session import Session
from cadastre.core.errors import UsageError
from cadastre.core.placement import placement_for
from cadastre.core.rules import check_catalog
from cadastre.render.text import render
from tests.conftest import DECLARED_AS_OF, NOW

PROVIDER_HOST = """\
- id: llm-vendor
  tags:
    - llm
  notes: Third-party LLM API. Not a machine we run.
  role: external
  reachable_from:
    - edge-net
"""

PROVIDER_SERVICE = """\
- id: llm-vendor-api
  tags:
    - llm
    - openai-compatible
  notes: 'Models: model-a, model-b. Status: working.'
  runs_on: llm-vendor
"""

PROVIDER_ENDPOINT = """\
- id: llm-vendor-https
  service: llm-vendor-api
  network: edge-net
  address: api.vendor.example.invalid
  port: 443
  protocol: https
"""

PROVIDER_SECRET = """\
- id: llm-vendor-api-key
  tags:
    - llm
  notes: Reference only.
  ref: /prod/llm-vendor/api-key
  store: secrets-manager
"""


def _append(root: Path, relative: str, text: str) -> None:
    path = root / "declared" / relative
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


@pytest.fixture
def provider_session(catalog_copy: Path) -> Session:
    _append(catalog_copy, "hosts/hosts.yaml", PROVIDER_HOST)
    _append(catalog_copy, "services/services.yaml", PROVIDER_SERVICE)
    _append(catalog_copy, "endpoints/endpoints.yaml", PROVIDER_ENDPOINT)
    _append(catalog_copy, "secrets/secrets.yaml", PROVIDER_SECRET)
    return Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)


def _flat(document: object) -> str:
    return " ".join(render(document).split())  # type: ignore[arg-type]


# -- WI-1257: host role `external` --------------------------------------------


def test_an_external_provider_loads_and_checks_clean(
    provider_session: Session,
) -> None:
    host = provider_session.catalog.get("host", "llm-vendor")
    assert host is not None
    assert host.role == "external"  # type: ignore[attr-defined]
    errors = [
        finding
        for finding in check_catalog(provider_session.catalog)
        if finding.level == "error"
    ]
    assert errors == []


def test_an_external_host_is_never_a_placement_target(
    provider_session: Session,
) -> None:
    """Reachable from the public network, so only its role keeps it out of a
    public placement — and the exclusion has to say so truthfully."""
    placement = placement_for(provider_session.catalog, "a public web app")
    assert "llm-vendor" not in {c.host for c in placement.candidates}
    excluded = {e.host: f"{e.reason}: {e.detail}" for e in placement.exclusions}
    assert excluded["llm-vendor"] == (
        "role `external`: third-party provider; not a placement target"
    )


# -- WI-1258: tag discovery ---------------------------------------------------


def test_lookup_by_tag_lists_every_declared_entity_carrying_it(
    provider_session: Session,
) -> None:
    document = lookup(provider_session, "tag:llm")
    assert document.data["resolution"] == "tag-match"
    assert [(m["kind"], m["id"]) for m in document.data["matches"]] == [
        ("host", "llm-vendor"),
        ("service", "llm-vendor-api"),
        ("secret", "llm-vendor-api-key"),
    ]
    assert document.provenance
    text = _flat(document)
    assert "3 declared entities carry tag 'llm'" in text
    assert "runs_on=llm-vendor" in text
    assert "ref=/prod/llm-vendor/api-key" in text
    # Notes are free text: quoted as data, never a bare line.
    assert '"Models: model-a, model-b. Status: working."' in text


def test_lookup_by_tag_narrows_by_kind(provider_session: Session) -> None:
    document = lookup(provider_session, "tag:llm", kind="service")
    assert [m["id"] for m in document.data["matches"]] == ["llm-vendor-api"]
    assert "1 declared service entity carries tag 'llm'" in _flat(document)


def test_lookup_by_an_unused_tag_is_an_honest_empty_answer(
    session: Session,
) -> None:
    document = lookup(session, "tag:no-such-tag")
    assert document.data["matches"] == []
    assert "Nothing declared carries tag 'no-such-tag'" in _flat(document)


def test_lookup_by_an_empty_tag_is_a_usage_error(session: Session) -> None:
    with pytest.raises(UsageError, match="tag:llm"):
        lookup(session, "tag:")


def test_brief_shows_service_tags(provider_session: Session) -> None:
    document = brief(provider_session)
    rows = {row["id"]: row for row in document.data["services"]}
    assert rows["llm-vendor-api"]["tags"] == ["llm", "openai-compatible"]
    assert "llm, openai-compatible" in render(document)
