"""Parity checks for the transport-neutral application boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cadastre import __version__
from cadastre.application.checks import CheckService
from cadastre.application.context import ApplicationContext
from cadastre.application.health import HealthService
from cadastre.application.queries import QueryService
from cadastre.application.writes import WriteService
from cadastre.cli import brief, context_for, drift, lookup
from cadastre.compatibility import compatibility
from cadastre.core.errors import UsageError
from cadastre.render.json_out import render


def test_query_service_matches_cli_documents(catalog_copy: Path) -> None:
    context = ApplicationContext.open(
        catalog_copy, now=datetime(2026, 8, 7, 12, tzinfo=UTC)
    )
    service = QueryService(context)
    session = context.session()
    assert render(service.brief()) == render(brief.brief(session))
    assert render(service.context_for("stateful public service")) == render(
        context_for.context_for(session, "stateful public service")
    )
    assert render(service.lookup("app-01", kind="host")) == render(
        lookup.lookup(session, "app-01", kind="host")
    )
    assert render(service.drift()) == render(drift.drift(session))


def test_check_service_uses_safe_display_path(catalog_copy: Path) -> None:
    result = CheckService(ApplicationContext.open(catalog_copy)).artifact(
        "services: {}\n", kind="compose", display_path="proposal.yaml"
    )
    payload = json.loads(render(result))
    assert payload["result"]["artifact"]["path"] == "proposal.yaml"


def test_query_service_dispatches_every_extended_read_operation(
    catalog_copy: Path,
) -> None:
    service = QueryService(ApplicationContext.open(catalog_copy))
    calls: tuple[tuple[str, dict[str, Any], str], ...] = (
        ("question", {"question_id": "Q-H03"}, "cadastre question Q-H03"),
        ("stale", {}, "cadastre stale"),
        ("sources", {}, "cadastre sources"),
        ("plugins", {}, "cadastre plugins"),
    )
    for name, arguments, command in calls:
        payload = json.loads(render(service.dispatch(name, arguments)))
        assert payload["command"] == command
        assert payload["provenance"]
    with pytest.raises(UsageError, match="unknown query operation"):
        service.dispatch("not-an-operation")


def test_write_service_dispatches_every_registered_write_operation(
    catalog_copy: Path,
) -> None:
    """Mirrors `QueryService.dispatch`: one boundary, every transport routes
    through it. `principal` and `reason` are keyword-only, never read out of
    `arguments` (§2.3 — a forgeable `arguments["principal"]` would let any
    `mcp`-scoped caller stamp someone else's provenance)."""
    service = WriteService(ApplicationContext.open(catalog_copy))
    document = service.dispatch(
        "annotate",
        {"kind": "host", "id": "app-01", "record": {"notes": "reviewed via dispatch"}},
        principal="agent-dispatch",
        reason="dispatch coverage",
    )
    payload = json.loads(render(document))
    assert payload["result"]["principal"] == "agent-dispatch"
    assert payload["result"]["reason"] == "dispatch coverage"

    document = service.dispatch(
        "acknowledge",
        {"target": "host:app-01", "source": "fixture", "until": "2027-01-01"},
        principal="agent-dispatch",
        reason="dispatch coverage",
    )
    payload = json.loads(render(document))
    assert payload["result"]["target"] == {"kind": "host", "id": "app-01"}
    assert payload["result"]["until"] == "2027-01-01"

    with pytest.raises(UsageError, match="unknown write operation"):
        service.dispatch("not-an-operation", {}, principal="x", reason="y")


def test_runtime_context_and_health_share_the_initialized_store(
    catalog_copy: Path,
) -> None:
    context = ApplicationContext.open(catalog_copy)
    runtime = context.runtime_session()
    try:
        assert runtime.store.revision == 1
        assert runtime.store.observed_revision == 0
        assert runtime.view.root == catalog_copy
    finally:
        runtime.store.close()

    health = HealthService(catalog_copy)
    assert health.live()["liveness"] is True
    ready = health.ready()
    assert ready["lifecycle"]["state"] == "ready"
    assert ready["lifecycle"]["checks"]["runtime_store"] == "sqlite"
    reported = health.version()
    # `name` and `version` are the established contract; everything else the
    # compatibility document adds is additive.
    assert reported["name"] == "cadastre"
    assert reported["version"] == __version__
    assert reported["application_version"] == __version__
    assert (
        reported["minimum_client_version"] == compatibility()["minimum_client_version"]
    )
    assert reported["release_url"].startswith("https://")

    fixture_context = ApplicationContext.open(catalog_copy, runtime=False)
    assert fixture_context.service_session().root == catalog_copy
