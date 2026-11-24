"""access(host): user, credential, path, Tailscale SSH and skill, never guessed."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from cadastre.cli.access import access
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import AmbiguousEntityError, MissingEntityError, UsageError


def _with_host(session: Session, **changes: object) -> Session:
    host = session.catalog.get("host", "app-01")
    assert isinstance(host, model.Host)
    session.catalog.entities["host"]["app-01"] = replace(host, **changes)  # type: ignore[arg-type]
    return session


def test_undeclared_facts_are_named_not_guessed(session: Session) -> None:
    data = access(session, "app-01").data
    assert data["host"] == "app-01"
    assert data["user"] is None
    assert data["skill"] is None
    assert data["tailscale_ssh"] is None
    missing = " ".join(data["missing"])
    for fact in ("login user", "skill", "tailscale ssh"):
        assert fact in missing
    assert data["access"][0]["via"] == "ssh-ca"
    assert data["networks"][0] == {"id": "lab-net", "class": "private"}


def test_declared_tags_fill_user_skill_and_tailscale(session: Session) -> None:
    host = session.catalog.get("host", "app-01")
    assert isinstance(host, model.Host)
    _with_host(
        session,
        tags=(*host.tags, "ssh-user:deploy", "skill:host-ssh", "tailscale-ssh"),
        aliases=("app-01.tail1234.ts.net", "100.64.1.2", "192.168.1.20", "app01"),
    )
    data = access(session, "app-01").data
    assert data["user"] == "deploy"
    assert data["skill"] == "host-ssh"
    assert data["tailscale_ssh"] is True
    assert data["tailnet"] is True
    scopes = {a["value"]: a["scope"] for a in data["addresses"]}
    assert scopes == {
        "app-01.tail1234.ts.net": "tailnet",
        "100.64.1.2": "tailnet",
        "192.168.1.20": "lan",
        "app01": "name",
    }
    assert "login user" not in " ".join(data["missing"])


def test_resolves_through_an_alias(session: Session) -> None:
    _with_host(session, aliases=("app01.lan",))
    assert access(session, "app01.lan").data["host"] == "app-01"


def test_shared_alias_is_ambiguous(session: Session) -> None:
    _with_host(session, aliases=("shared",))
    other = session.catalog.get("host", "app-02")
    assert isinstance(other, model.Host)
    session.catalog.entities["host"]["app-02"] = replace(other, aliases=("shared",))
    with pytest.raises(AmbiguousEntityError):
        access(session, "shared")


def test_unknown_host_and_empty_host_are_errors(session: Session) -> None:
    with pytest.raises(MissingEntityError):
        access(session, "nosuchhost")
    with pytest.raises(UsageError):
        access(session, " ")


def test_credential_names_a_secret_that_carries_the_host(session: Session) -> None:
    session.catalog.entities.setdefault("secret", {})["app-01-ssh-key"] = model.Secret(
        id="app-01-ssh-key",
        ref="infisical://ops/prod/APP_01_SSH_KEY",
        store="infisical",
    )
    data = access(session, "app-01").data
    ids = [c["id"] for c in data["credentials"]]
    assert ids == ["app-01-ssh-key"]
    assert data["credentials"][0]["ref"] == "infisical://ops/prod/APP_01_SSH_KEY"
    # No source configuration is visible here, so the line cannot be built.
    assert data["credentials"][0]["manifest_line"] is None
    assert "project id is unknown" in " ".join(data["missing"])


def test_never_carries_a_value_and_is_json(session: Session) -> None:
    json.dumps(access(session, "app-01").data)
