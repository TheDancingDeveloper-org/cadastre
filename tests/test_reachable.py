"""`reachable`: NetworkPolicy egress evaluation from the GitOps source (#71)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from cadastre.cli import reachable as reachable_cmd
from cadastre.cli.session import Session
from cadastre.core.errors import UsageError
from cadastre.core.netpol import Policy, evaluate_egress

GITOPS = Path(__file__).parent / "fixtures" / "gitops"
CALLER = "k8s:vogt-sessions"


@pytest.fixture
def policies() -> list[Policy]:
    return reachable_cmd.load_policies(GITOPS)


def _answer(policies: list[Policy], to: str, caller: str = CALLER) -> dict[str, Any]:
    return reachable_cmd.reachable_from_policies(policies, caller, to).data


def test_loads_both_policies_into_the_directory_namespace(
    policies: list[Policy],
) -> None:
    assert sorted(p.name for p in policies) == ["default-deny", "session-egress"]
    assert {p.namespace for p in policies} == {"vogt-sessions"}


def test_tailnet_ssh_is_allowed_by_the_tailnet_rule(policies: list[Policy]) -> None:
    data = _answer(policies, "100.92.54.45:22")
    assert data["decision"] == "allow"
    [match] = data["matches"]
    assert match["policy"] == "session-egress"
    assert match["peer"] == {"ipBlock": {"cidr": "100.64.0.0/10"}}
    assert match["port"] == {"port": 22, "protocol": "TCP"}
    assert match["origin"] == "apps/vogt-sessions/networkpolicy.yaml"


def test_lan_ssh_is_denied_citing_session_egress(policies: list[Policy]) -> None:
    document = reachable_cmd.reachable_from_policies(policies, CALLER, "192.168.1.5:22")
    assert document.data["decision"] == "deny"
    assert document.data["matches"] == []
    assert "session-egress" in document.data["isolating_policies"]
    assert "session-egress" in document.data["reason"]
    assert document.exit_code == 1


def test_tailnet_https_is_allowed_by_the_node_b_rule(policies: list[Policy]) -> None:
    data = _answer(policies, "100.92.54.45:443")
    assert data["decision"] == "allow"
    assert data["matches"][0]["peer"] == {"ipBlock": {"cidr": "100.92.54.45/32"}}


def test_public_ssh_allowed_but_excepted_ranges_are_not(policies: list[Policy]) -> None:
    assert _answer(policies, "140.82.112.3:22")["decision"] == "allow"
    assert _answer(policies, "10.1.2.3:443")["decision"] == "deny"


def test_namespace_without_policies_is_not_isolated(policies: list[Policy]) -> None:
    data = _answer(policies, "192.168.1.5:22", caller="k8s:other")
    assert data["decision"] == "allow"
    assert data["isolating_policies"] == []


def test_named_and_ranged_ports_and_label_dependent_selectors() -> None:
    spec = {
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [{"ports": [{"port": "https"}, {"port": 8000, "endPort": 8010}]}],
    }
    labelled = {
        "podSelector": {"matchLabels": {"app": "x"}},
        "policyTypes": ["Egress"],
    }
    policies = [
        Policy("ranged", "ns", spec),
        Policy("labelled", "ns", labelled),
    ]
    import ipaddress

    address = ipaddress.ip_address("1.2.3.4")
    assert (
        evaluate_egress(policies, namespace="ns", address=address, port=8005).decision
        == "allow"
    )
    verdict = evaluate_egress(policies, namespace="ns", address=address, port=443)
    assert verdict.decision == "deny"
    assert verdict.conditional == ("labelled",)
    with_labels = evaluate_egress(
        policies, namespace="ns", address=address, port=8005, pod_labels={"app": "y"}
    )
    assert with_labels.isolating == ("ranged",)


@pytest.mark.parametrize(
    ("caller", "to"),
    [
        ("vogt-sessions", "100.92.54.45:22"),
        ("tailnet:node", "100.92.54.45:22"),
        (CALLER, "winrarhost:22"),
        (CALLER, "100.92.54.45"),
        (CALLER, "100.92.54.45:70000"),
    ],
)
def test_malformed_arguments_are_usage_errors(
    policies: list[Policy], caller: str, to: str
) -> None:
    with pytest.raises(UsageError):
        reachable_cmd.reachable_from_policies(policies, caller, to)


def test_checkout_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(reachable_cmd.GITOPS_ENV, str(GITOPS))

    class _Plugins:
        sources = ()

    class _Session:
        root = tmp_path
        plugins = _Plugins()

        def provenance(self) -> tuple[()]:
            return ()

    document = reachable_cmd.reachable(
        cast(Session, _Session()), CALLER, "100.92.54.45:22"
    )
    assert document.data["decision"] == "allow"
    assert document.data["source"] == str(GITOPS)


def test_no_checkout_configured_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(reachable_cmd.GITOPS_ENV, raising=False)

    class _Plugins:
        sources = ()

    class _Session:
        root = tmp_path
        plugins = _Plugins()

    with pytest.raises(UsageError, match="CADASTRE_GITOPS_PATH"):
        reachable_cmd.reachable(cast(Session, _Session()), CALLER, "100.92.54.45:22")
