"""context_for answers about an existing entity for access-verb intents."""

from __future__ import annotations

from cadastre.cli.context_for import context_for
from cadastre.cli.session import Session


def test_ssh_into_existing_host_answers_about_host(session: Session) -> None:
    document = context_for(session, "ssh into host app-01")
    assert "candidates" not in document.data
    assert "app-01" in document.title or "app-01" in str(document.data)


def test_connect_and_reach_verbs_resolve(session: Session) -> None:
    for intent in ("connect to app-01", "how do I reach app-01"):
        document = context_for(session, intent)
        assert "candidates" not in document.data


def test_unknown_host_does_not_fall_back_to_placement(session: Session) -> None:
    document = context_for(session, "ssh into host winrarhost")
    assert document.data["access"] is True
    assert document.data["matched"] is None
    assert "candidates" not in document.data
    assert "winrarhost" in document.data["names"]


def test_placement_intent_unchanged(session: Session) -> None:
    document = context_for(session, "stateful public service")
    assert "candidates" in document.data


def test_placement_with_access_word_still_places(session: Session) -> None:
    for intent in (
        "deploy a public service with ssh access",
        "new stateful service, console access for admins",
    ):
        document = context_for(session, intent)
        assert "candidates" in document.data, intent
        assert "access" not in document.data


def test_every_named_host_is_resolved_or_reported(session: Session) -> None:
    document = context_for(session, "ssh into app-01 and nosuchhost")
    assert document.data["access"] is True
    assert document.data["matched"] is not None
    assert document.data["missing"] == ["nosuchhost"]
    assert "candidates" not in document.data
