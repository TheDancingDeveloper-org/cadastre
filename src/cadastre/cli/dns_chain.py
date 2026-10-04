"""`cadastre dns-chain` — record -> ingress -> node -> service, checked.

The 2026-08-26 production outage was a public hostname whose record pointed
at the workload node rather than the ingress edge that fronts it. Every fact
needed to see that was already in the catalog — the DNS collector had the
record, the declared endpoint said which ingress fronts the hostname, and the
ingress service's own endpoint carried the edge address — but no command
joined them, so nobody looked until the site was down.

This joins them. It reads only the catalog (declared intent plus whatever the
DNS, orchestrator, hypervisor, VPN and ingress collectors last observed); it
never resolves a name live or probes anything (DESIGN §1.3). That is also its
stated limit: a split-horizon resolver can answer differently on the LAN, and
nothing here can see that.

The vocabulary stays neutral (DESIGN §2.4): an *ingress* is any service some
endpoint is `fronted_by`, or one tagged `ingress`/`edge`/`reverse-proxy`; an
*edge host* is a host running one, or a host whose role or tags say `edge`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import UsageError
from cadastre.render.document import Document, Finding, Para, Section, Table

INGRESS_TAGS = frozenset({"ingress", "edge", "reverse-proxy"})
MAX_CNAME_DEPTH = 8

LIMITATIONS = (
    "Catalog only: no live resolution was performed. The records are what "
    "the DNS collector last read from the provider, i.e. the public view. A "
    "LAN resolver with a wildcard or split-horizon zone may answer "
    "differently — compare a public resolver (`dig @1.1.1.1 NAME`) with the "
    "LAN resolver before trusting either alone."
)

_UPSTREAM = re.compile(r"upstream\s+(.+)$", re.IGNORECASE)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return True


def normalize_hostname(raw: str) -> str:
    text = (raw or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.rstrip(".")


@dataclass(frozen=True)
class _Record:
    name: str
    type: str
    value: str
    proxied: bool
    source: str
    id: str


@dataclass
class _Estate:
    """The joined view: who runs where, and which addresses belong to whom."""

    services: dict[str, model.Service]
    hosts: dict[str, model.Host]
    endpoints: list[tuple[model.Endpoint, str]]
    records: list[_Record]
    address_owner: dict[str, list[dict[str, str]]]
    ingress_services: set[str]
    edge_hosts: set[str]


def _collect(session: Session) -> _Estate:
    services: dict[str, model.Service] = {}
    hosts: dict[str, model.Host] = {}
    endpoints: list[tuple[model.Endpoint, str]] = []
    records: list[_Record] = []

    def add_record(domain: model.Domain, source: str) -> None:
        if domain.type not in {"A", "AAAA", "CNAME"} or not domain.value:
            return
        records.append(
            _Record(
                name=normalize_hostname(domain.name),
                type=domain.type,
                value=normalize_hostname(domain.value)
                if domain.type == "CNAME"
                else domain.value.strip(),
                proxied="proxied" in domain.tags,
                source=source,
                id=domain.id,
            )
        )

    # Declared first, so a declaration wins the id; observed fills the gaps.
    for service in session.catalog.services:
        services[service.id] = service
    for host in session.catalog.hosts:
        hosts[host.id] = host
    for endpoint in session.catalog.endpoints:
        endpoints.append((endpoint, "declared"))
    for domain in session.catalog.domains:
        add_record(domain, "declared")
    observed_hosts: list[tuple[model.Host, str]] = []
    for source in session.observed:
        for entity in source.entities.get("service", []):
            if isinstance(entity, model.Service):
                current = services.get(entity.id)
                if current is None or (not current.runs_on and entity.runs_on):
                    services[entity.id] = entity
        for entity in source.entities.get("host", []):
            if isinstance(entity, model.Host):
                hosts.setdefault(entity.id, entity)
                observed_hosts.append((entity, source.source))
        for entity in source.entities.get("endpoint", []):
            if isinstance(entity, model.Endpoint):
                endpoints.append((entity, source.source))
        for entity in source.entities.get("domain", []):
            if isinstance(entity, model.Domain):
                add_record(entity, source.source)

    owner: dict[str, list[dict[str, str]]] = {}

    def own(address: str, host: str | None, via: str, source: str) -> None:
        if not address or not _is_ip(address) or not host:
            return
        entry = {"host": host, "via": via, "source": source}
        if entry not in owner.setdefault(address, []):
            owner[address].append(entry)

    for endpoint, origin in endpoints:
        owner_host = endpoint.host or _runs_on(services, endpoint.service)
        own(endpoint.address, owner_host, f"endpoint {endpoint.id}", origin)
        if endpoint.bind_address:
            own(endpoint.bind_address, owner_host, f"endpoint {endpoint.id}", origin)
    declared_hosts = [(h, "declared") for h in session.catalog.hosts]
    for host_entity, origin in declared_hosts + observed_hosts:
        for block_name, block in host_entity.extra.items():
            for address in _addresses(block):
                own(address, host_entity.id, f"{block_name} addresses", origin)

    ingress = {endpoint.fronted_by for endpoint, _ in endpoints if endpoint.fronted_by}
    ingress |= {sid for sid, svc in services.items() if INGRESS_TAGS & set(svc.tags)}
    edge = {svc.runs_on for sid, svc in services.items() if sid in ingress}
    edge |= {
        hid
        for hid, host in hosts.items()
        if host.role == "edge" or INGRESS_TAGS & set(host.tags)
    }
    edge.discard("")
    return _Estate(services, hosts, endpoints, records, owner, ingress, edge)


def _addresses(block: Any) -> list[str]:
    if not isinstance(block, dict):
        return []
    found: list[str] = []
    for key in ("addresses", "ips", "ip_addresses"):
        value = block.get(key)
        if isinstance(value, list):
            found.extend(str(item) for item in value if isinstance(item, str))
    for key in ("address", "ip", "ip_address"):
        value = block.get(key)
        if isinstance(value, str):
            found.append(value)
    return [item.split("/", 1)[0] for item in found]


def _runs_on(services: dict[str, model.Service], service_id: str | None) -> str | None:
    if not service_id:
        return None
    service = services.get(service_id)
    return service.runs_on if service and service.runs_on else None


def _records_for(estate: _Estate, name: str) -> list[_Record]:
    exact = [record for record in estate.records if record.name == name]
    if exact:
        return exact
    labels = name.split(".")
    for index in range(1, len(labels)):
        wildcard = "*." + ".".join(labels[index:])
        matches = [record for record in estate.records if record.name == wildcard]
        if matches:
            return matches
    return []


def _upstreams(notes: str | None) -> list[str]:
    if not notes:
        return []
    match = _UPSTREAM.search(notes)
    if not match:
        return []
    out = []
    for item in match.group(1).split(","):
        dial = item.strip()
        if dial.startswith("[") and "]" in dial:
            out.append(dial[1 : dial.index("]")])
        else:
            out.append(dial.rsplit(":", 1)[0] if dial.count(":") == 1 else dial)
    return [item for item in out if item]


def dns_chain(session: Session, hostname: str) -> Document:
    name = normalize_hostname(hostname)
    if not name or "." not in name:
        raise UsageError(
            "dns_chain needs `hostname`, a fully qualified name such as "
            "'app.example.com'"
        )
    estate = _collect(session)
    hops: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []

    def finding(level: str, code: str, message: str) -> None:
        findings.append({"level": level, "code": code, "message": message})

    # 1. What the name should reach: endpoints that publish this hostname.
    published = [
        (endpoint, source)
        for endpoint, source in estate.endpoints
        if normalize_hostname(endpoint.address) == name
    ]
    expected_services = sorted({e.service for e, _ in published if e.service})
    expected_ingress = sorted({e.fronted_by for e, _ in published if e.fronted_by})
    ingress_hosts = {
        host
        for service in expected_ingress
        if (host := _runs_on(estate.services, service))
    }
    workload_hosts = {
        host
        for service in expected_services
        if (host := _runs_on(estate.services, service))
    }
    ingress_addresses = sorted(
        address
        for address, owners in estate.address_owner.items()
        if any(entry["host"] in ingress_hosts for entry in owners)
    )

    # 2. Walk the records.
    terminals: list[tuple[str, _Record]] = []
    frontier = [name]
    seen: set[str] = set()
    depth = 0
    proxied = False
    any_record = False
    while frontier and depth < MAX_CNAME_DEPTH:
        current = frontier.pop(0)
        if current in seen:
            finding("warn", "cname_loop", f"CNAME chain revisits {current}.")
            break
        seen.add(current)
        records = _records_for(estate, current)
        if not records and current != name:
            finding(
                "info",
                "external_target",
                f"{current} is outside every collected zone; the chain leaves "
                "the catalog there (a CDN, tunnel or third-party host).",
            )
        for record in records:
            any_record = True
            proxied = proxied or record.proxied
            hops.append(
                {
                    "step": "dns",
                    "name": current,
                    "record_name": record.name,
                    "type": record.type,
                    "value": record.value,
                    "proxied": record.proxied,
                    "source": record.source,
                    "record_id": record.id,
                }
            )
            if record.type == "CNAME":
                frontier.append(record.value)
            else:
                terminals.append((record.value, record))
        depth += 1
    if not any_record:
        finding(
            "warn",
            "no_record",
            f"No A/AAAA/CNAME record for {name} (or a wildcard covering it) in "
            "any collected zone. Either the zone is not collected or the name "
            "does not exist publicly.",
        )

    _declared_vs_observed(estate, name, finding)

    # 3. Where each address lands, judged against what the name should reach.
    for address, record in terminals:
        owners = estate.address_owner.get(address, [])
        landed = sorted({entry["host"] for entry in owners})
        hops.append(
            {
                "step": "address",
                "value": address,
                "lands_on": owners,
                "edge": sorted(set(landed) & estate.edge_hosts),
            }
        )
        if not owners:
            finding(
                "warn",
                "unknown_address",
                f"{name} -> {address}: no declared or observed host owns this "
                "address, so the chain cannot be verified past DNS.",
            )
            continue
        on_edge = set(landed) & estate.edge_hosts
        if ingress_hosts:
            if set(landed) & ingress_hosts:
                continue
            target = ", ".join(sorted(ingress_hosts))
            via = ", ".join(expected_ingress)
            wanted = ", ".join(ingress_addresses) or "its address"
            if on_edge:
                finding(
                    "error",
                    "wrong_ingress",
                    f"{name} -> {address} lands on edge host "
                    f"{', '.join(sorted(on_edge))}, but the hostname is fronted "
                    f"by {via} on {target} ({wanted}).",
                )
            else:
                finding(
                    "error",
                    "points_at_workload",
                    f"{'Proxied hostname' if record.proxied else 'Hostname'} "
                    f"{name} points at workload node {', '.join(landed)} "
                    f"({address}) instead of the ingress edge: it is fronted by "
                    f"{via} on {target} ({wanted}).",
                )
        elif record.proxied and not on_edge:
            finding(
                "warn",
                "proxied_without_ingress",
                f"Proxied hostname {name} points at {', '.join(landed)} "
                f"({address}), which runs no ingress service, and no endpoint "
                "declares which ingress fronts this hostname.",
            )
        elif workload_hosts and not (set(landed) & (workload_hosts | on_edge)):
            finding(
                "error",
                "points_elsewhere",
                f"{name} -> {address} lands on {', '.join(landed)}, but the "
                f"service publishing it runs on {', '.join(sorted(workload_hosts))}.",
            )

    # 4. Ingress and its upstream, as far as the catalog knows them.
    for service in expected_ingress:
        hops.append(
            {
                "step": "ingress",
                "service": service,
                "host": _runs_on(estate.services, service),
                "addresses": ingress_addresses,
            }
        )
    for endpoint, source in published:
        upstreams = _upstreams(endpoint.notes) if source != "declared" else []
        for upstream in upstreams:
            owners = estate.address_owner.get(upstream, [])
            hosts = sorted({entry["host"] for entry in owners})
            hops.append(
                {
                    "step": "upstream",
                    "dial": upstream,
                    "lands_on": owners,
                    "source": source,
                }
            )
            if hosts and workload_hosts and not set(hosts) & workload_hosts:
                finding(
                    "error",
                    "upstream_mismatch",
                    f"The ingress for {name} dials {upstream} ({', '.join(hosts)}), "
                    f"but {', '.join(expected_services)} runs on "
                    f"{', '.join(sorted(workload_hosts))}.",
                )
    for service in expected_services:
        hops.append(
            {
                "step": "service",
                "service": service,
                "runs_on": _runs_on(estate.services, service),
            }
        )
    if not published:
        finding(
            "info",
            "no_endpoint",
            f"No endpoint publishes {name}, so there is no expected ingress or "
            "service to check the record against. Declare one (`address: "
            f"{name}`, `fronted_by: <ingress>`) to make this check meaningful.",
        )

    levels = {item["level"] for item in findings}
    verdict = (
        "mismatch" if "error" in levels else "unverified" if "warn" in levels else "ok"
    )
    return _document(
        session,
        name,
        verdict,
        hops,
        findings,
        expected_services,
        expected_ingress,
        sorted(ingress_hosts),
        proxied,
    )


def _declared_vs_observed(estate: _Estate, name: str, finding: Any) -> None:
    declared = {
        (record.type, record.value)
        for record in estate.records
        if record.name == name and record.source == "declared"
    }
    observed = {
        (record.type, record.value)
        for record in estate.records
        if record.name == name and record.source != "declared"
    }
    if declared and observed and declared != observed:
        finding(
            "warn",
            "declared_observed_differ",
            f"Declared records for {name} ({_pairs(declared)}) differ from what "
            f"the DNS collector observed ({_pairs(observed)}).",
        )


def _pairs(items: set[tuple[str, str]]) -> str:
    return ", ".join(f"{kind} {value}" for kind, value in sorted(items))


def _document(
    session: Session,
    name: str,
    verdict: str,
    hops: list[dict[str, Any]],
    findings: list[dict[str, str]],
    services: list[str],
    ingress: list[str],
    ingress_hosts: list[str],
    proxied: bool,
) -> Document:
    rows = tuple(
        (
            hop["step"],
            str(
                hop.get("name")
                or hop.get("service")
                or hop.get("dial")
                or hop.get("value")
            ),
            _describe_hop(hop),
        )
        for hop in hops
    )
    blocks: list[Any] = [
        Para(f"Verdict: {verdict}."),
        Table(("step", "subject", "detail"), rows, empty_note="(no chain)"),
    ]
    blocks.extend(
        Finding(item["level"], item["code"], name, item["message"]) for item in findings
    )
    return Document(
        title=f"cadastre dns-chain {name}",
        sections=(Section(f"dns chain: {name}", tuple(blocks), note=LIMITATIONS),),
        provenance=session.provenance(),
        data={
            "hostname": name,
            "verdict": verdict,
            "proxied": proxied,
            "expected": {
                "services": services,
                "ingress": ingress,
                "ingress_hosts": ingress_hosts,
            },
            "chain": hops,
            "findings": findings,
            "limitations": LIMITATIONS,
        },
        exit_code=1 if verdict == "mismatch" else 0,
    )


def _describe_hop(hop: dict[str, Any]) -> str:
    step = hop["step"]
    if step == "dns":
        flag = " proxied" if hop["proxied"] else ""
        return f"{hop['type']} {hop['value']}{flag} ({hop['source']})"
    if step in {"address", "upstream"}:
        owners = hop.get("lands_on") or []
        if not owners:
            return "owner unknown"
        return "; ".join(f"{o['host']} via {o['via']}" for o in owners)
    if step == "ingress":
        return f"on {hop['host'] or '?'} {', '.join(hop['addresses'])}".strip()
    return f"runs on {hop.get('runs_on') or '?'}"
