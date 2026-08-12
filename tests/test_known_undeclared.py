from __future__ import annotations

from pathlib import Path

from cadastre.cli.drift import drift
from cadastre.cli.session import Session
from tests.conftest import NOW


def test_known_undeclared_is_a_reasoned_review_queue_exemption(
    catalog_copy: Path,
) -> None:
    policy = catalog_copy / "declared" / "policy" / "undeclared.yaml"
    policy.write_text(
        "known_undeclared:\n"
        "  - source: fixture\n"
        "    kind: host\n"
        "    ids: [app-99]\n"
        "    reason: ephemeral CI worker pool\n",
        encoding="utf-8",
    )
    plugins = catalog_copy / "declared" / "plugins.yaml"
    plugins.write_text(
        "sources:\n  - id: fixture\n    command: [true]\n",
        encoding="utf-8",
    )
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "fixture.json").write_text(
        '{"v": 1, "source": "fixture", "plugin": "fixture", '
        '"as_of": "2026-08-07T12:00:00Z", "ok": true, '
        '"capabilities": ["inventory.list"], "entities": '
        '{"host": [{"id": "app-99", "role": "server"}]}}',
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW)
    document = drift(session)
    assert not any(
        divergence["category"] == "undeclared" and divergence["id"] == "app-99"
        for divergence in document.data["divergences"]
    )
    assert document.data["known_undeclared"][0]["reason"] == (
        "ephemeral CI worker pool"
    )
