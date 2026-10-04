"""WI-848: the DNS chain check — record -> ingress edge -> node -> service.

The 2026-08-26 production outage: a proxied public hostname whose record
pointed at the workload node instead of the ingress edge (100.92.54.45) that
fronts it. Everything needed to see it was in the catalog; nothing joined it.
The first fixture below is that misconfiguration.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from cadastre.cli.dns_chain import dns_chain, normalize_hostname
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.catalog import Catalog
from cadastre.core.errors import UsageError
from cadastre.core.observed import ObservedSource
from cadastre.render.text import render

AS_OF = "2026-08-26T09:00:00Z"
EDGE_IP = "100.92.54.45"
WORKLOAD_IP = "100.64.0.20"
HOSTNAME = "vogt.example.com"


def _catalog(*, direct_endpoint: bool = True) -> Catalog:
    endpoints = [
        model.Endpoint(
            id="caddy-edge-tailnet",
            service="caddy-edge",
            network="tailnet",
            address=EDGE_IP,
            port=443,
        ),
        model.Endpoint(
            id="vogt-public",
            service="vogt",
            network="internet",
            address=HOSTNAME,
            port=443,
            fronted_by="caddy-edge",
        ),
    ]
    if direct_endpoint:
        endpoints.append(
            model.Endpoint(
                id="vogt-direct",
                service="vogt",
                network="tailnet",
                address=WORKLOAD_IP,
                port=8080,
            )
        )
    return Catalog(
        root=Path("."),
        entities={
            "host": {
                "edgehost": model.Host(id="edgehost"),
                "node-w": model.Host(id="node-w", role="container-host"),
            },
            "service": {
                "caddy-edge": model.Service(id="caddy-edge", runs_on="edgehost"),
                "vogt": model.Service(id="vogt", runs_on="node-w"),
            },
            "endpoint": {endpoint.id: endpoint for endpoint in endpoints},
        },
    )


def _dns(*records: model.Domain) -> ObservedSource:
    return ObservedSource(
        source="dns",
        plugin="dns-cloudflare",
        as_of=AS_OF,
        capabilities=("dns.records",),
        entities={"domain": list(records)},
    )


def _a(name: str, value: str, *, proxied: bool = True) -> model.Domain:
    return model.Domain(
        id=f"{name.replace('.', '-')}-a",
        zone="example.com",
        name=name,
        type="A",
        value=value,
        tags=("proxied",) if proxied else (),
    )


def _estate(
    session: Session, *sources: ObservedSource, direct_endpoint: bool = True
) -> Session:
    return dataclasses.replace(
        session, catalog=_catalog(direct_endpoint=direct_endpoint), observed=sources
    )


def test_the_2026_08_26_misconfiguration_is_flagged(session: Session) -> None:
    document = dns_chain(_estate(session, _dns(_a(HOSTNAME, WORKLOAD_IP))), HOSTNAME)

    assert document.data["verdict"] == "mismatch"
    codes = [finding["code"] for finding in document.data["findings"]]
    assert "points_at_workload" in codes
    message = next(
        f["message"]
        for f in document.data["findings"]
        if f["code"] == "points_at_workload"
    )
    assert "Proxied hostname vogt.example.com points at workload node node-w" in (
        message
    )
    assert "instead of the ingress edge" in message
    assert EDGE_IP in message
    assert document.data["expected"]["ingress"] == ["caddy-edge"]
    assert document.exit_code == 1
    assert "points_at_workload" in render(document)


def test_the_workload_node_is_recognised_from_vpn_evidence_too(
    session: Session,
) -> None:
    """No endpoint carries the workload address; a VPN collector's host
    evidence does."""
    vpn = ObservedSource(
        source="vpn",
        plugin="vpn-tailscale",
        as_of=AS_OF,
        capabilities=("network.list",),
        entities={
            "host": [
                model.Host(
                    id="node-w",
                    extra={"x-tailscale": {"addresses": [f"{WORKLOAD_IP}/32"]}},
                )
            ]
        },
    )
    estate = _estate(
        session, _dns(_a(HOSTNAME, WORKLOAD_IP)), vpn, direct_endpoint=False
    )
    document = dns_chain(estate, HOSTNAME)
    assert [f["code"] for f in document.data["findings"]] == ["points_at_workload"]


def test_the_correct_chain_is_ok(session: Session) -> None:
    document = dns_chain(_estate(session, _dns(_a(HOSTNAME, EDGE_IP))), HOSTNAME)

    assert document.data["verdict"] == "ok", document.data["findings"]
    steps = [hop["step"] for hop in document.data["chain"]]
    assert steps == ["dns", "address", "ingress", "service"]
    assert document.data["chain"][1]["edge"] == ["edgehost"]
    assert document.exit_code == 0


def test_a_cname_is_followed_to_the_edge(session: Session) -> None:
    cname = model.Domain(
        id="vogt-cname",
        zone="example.com",
        name=HOSTNAME,
        type="CNAME",
        value="edge.example.com.",
    )
    estate = _estate(session, _dns(cname, _a("edge.example.com", EDGE_IP)))
    document = dns_chain(estate, f"https://{HOSTNAME}/path")

    assert document.data["verdict"] == "ok", document.data["findings"]
    assert [hop.get("type") for hop in document.data["chain"][:2]] == [
        "CNAME",
        "A",
    ]


def test_a_wildcard_record_covers_the_name(session: Session) -> None:
    estate = _estate(session, _dns(_a("*.example.com", WORKLOAD_IP)))
    document = dns_chain(estate, HOSTNAME)
    assert document.data["chain"][0]["record_name"] == "*.example.com"
    assert "points_at_workload" in [f["code"] for f in document.data["findings"]]


def test_no_record_is_reported_not_guessed(session: Session) -> None:
    document = dns_chain(_estate(session, _dns()), HOSTNAME)
    assert document.data["verdict"] == "unverified"
    assert "no_record" in [f["code"] for f in document.data["findings"]]


def test_an_unowned_address_is_unverified(session: Session) -> None:
    document = dns_chain(_estate(session, _dns(_a(HOSTNAME, "198.51.100.7"))), HOSTNAME)
    assert document.data["verdict"] == "unverified"
    assert "unknown_address" in [f["code"] for f in document.data["findings"]]


def test_declared_and_observed_records_that_differ_are_reported(
    session: Session,
) -> None:
    estate = _estate(session, _dns(_a(HOSTNAME, EDGE_IP)))
    declared = dict(estate.catalog.entities)
    declared["domain"] = {"vogt-declared": _a(HOSTNAME, WORKLOAD_IP)}
    estate = dataclasses.replace(
        estate, catalog=dataclasses.replace(estate.catalog, entities=declared)
    )
    codes = [f["code"] for f in dns_chain(estate, HOSTNAME).data["findings"]]
    assert "declared_observed_differ" in codes


def test_the_limits_of_a_catalog_only_check_are_stated(session: Session) -> None:
    document = dns_chain(_estate(session, _dns(_a(HOSTNAME, EDGE_IP))), HOSTNAME)
    assert "no live resolution" in document.data["limitations"]
    assert "dig @1.1.1.1" in document.data["limitations"]


def test_hostname_is_required_and_normalized(session: Session) -> None:
    assert normalize_hostname("HTTPS://Vogt.Example.com:443/x") == HOSTNAME
    with pytest.raises(UsageError, match="hostname"):
        dns_chain(session, "")
