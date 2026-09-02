"""`cadastre brief` — the whole estate, compressed.

Intended for session-preamble injection rather than on-demand calling. It is
deliberately dense: it replaces a hand-maintained infrastructure document, and
it has to cost materially less than the prose it replaces.

What is left out is as considered as what is in. Free-text `notes` do not
appear here — they are per-entity detail, and `lookup` is where detail lives.
"""

from __future__ import annotations

from typing import Any

from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.render.document import Bullets, Document, Fields, Section, Table


def _resources(host: model.Host) -> str:
    if host.resources is None:
        return ""
    parts = []
    if host.resources.cpu_cores:
        parts.append(f"{host.resources.cpu_cores}c")
    if host.resources.memory_gb:
        parts.append(f"{host.resources.memory_gb}G")
    if host.resources.disk_gb:
        parts.append(f"{host.resources.disk_gb}GB disk")
    if host.resources.gpu:
        parts.append("gpu")
    return " ".join(parts)


def _networks_section(session: Session) -> Section:
    rows = tuple((n.id, n.class_, ",".join(n.tags)) for n in session.catalog.networks)
    return Section(
        "Networks",
        (Table(("network", "class", "tags"), rows),),
        note="Exposure tiers are checked against these classes.",
    )


def _hosts_section(session: Session) -> Section:
    catalog = session.catalog
    rows = []
    for host in catalog.hosts:
        rows.append(
            (
                host.id,
                host.role,
                ",".join(host.reachable_from),
                _resources(host),
                str(len(catalog.services_on(host.id))),
                ",".join(host.tags),
            )
        )
    return Section(
        "Hosts",
        (
            Table(
                ("host", "role", "networks", "resources", "services", "tags"),
                tuple(rows),
            ),
        ),
    )


def _services_section(session: Session) -> Section:
    catalog = session.catalog
    rows = []
    for service in catalog.services:
        endpoints = catalog.endpoints_of(service.id)
        address = ", ".join(
            f"{e.address}:{e.port}" if e.port else e.address for e in endpoints
        )
        authoritative = catalog.authoritative_pipeline(service.id)
        pipelines = catalog.pipelines_for(service.id)
        if authoritative:
            deploys = authoritative
        elif pipelines:
            deploys = "AMBIGUOUS (" + ",".join(p.id for p in pipelines) + ")"
        else:
            deploys = ""
        rows.append(
            (
                service.id,
                service.runs_on,
                service.expose or "",
                address,
                deploys,
            )
        )
    return Section(
        "Services",
        (
            Table(
                ("service", "host", "expose", "endpoints", "deployed by"), tuple(rows)
            ),
        ),
        note=(
            "`AMBIGUOUS` means more than one pipeline claims the service and none "
            "is marked authoritative. Do not guess which one deploys."
        ),
    )


def _repos_section(session: Session) -> Section:
    rows = []
    for repo in session.catalog.repos:
        homes = ", ".join(f"{r.forge}({r.role})" for r in repo.remotes)
        mirror = ""
        if repo.mirror_from or repo.mirror_to:
            mirror = f"{repo.mirror_from or '?'} → {repo.mirror_to or '?'}"
        rows.append((repo.id, homes, mirror))
    return Section(
        "Repositories",
        (Table(("repo", "remotes", "mirror"), tuple(rows)),),
        note="Dual-homing is the norm here. Push to the origin remote, not the mirror.",
    )


def _observed_only_secrets_by_store(session: Session) -> dict[str, int]:
    """Per-store count of secrets a collector saw but no `secret_ref` declares.

    `brief` is the session-context entry point, so an agent reasonably reads
    its secrets list as complete. Listing only the declared `secret_refs` makes
    the ~87% of real secrets that are observed-only invisible, and that is how
    a session concludes "no such credential" for a secret already in the store
    (the FarmEggs incident, 2026-08-24 / GitHub #24). Counts, not names, keep
    the context cost bounded while telling the truth about scale.
    """
    declared_refs = {secret.ref for secret in session.catalog.secrets}
    declared_ids = {secret.id for secret in session.catalog.secrets}
    by_store: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for source in session.observed:
        for secret in source.entities.get("secret", []):
            ref = getattr(secret, "ref", "") or ""
            if ref in declared_refs or secret.id in declared_ids:
                continue
            key = (secret.id, ref)
            if key in seen:
                continue
            seen.add(key)
            store = getattr(secret, "store", "") or "(unknown store)"
            by_store[store] = by_store.get(store, 0) + 1
    return dict(sorted(by_store.items()))


def _secrets_section(session: Session) -> Section:
    catalog = session.catalog
    by_store: dict[str, list[str]] = {}
    for secret in catalog.secrets:
        by_store.setdefault(secret.store, []).append(secret.ref)
    items = [
        f"{store}: {len(refs)} refs — " + ", ".join(sorted(refs))
        for store, refs in sorted(by_store.items())
    ]
    observed_only = _observed_only_secrets_by_store(session)
    blocks: list[object] = [Bullets(tuple(items))]
    if observed_only:
        total = sum(observed_only.values())
        blocks.append(
            Fields(
                (
                    (
                        "declared / observed-only",
                        f"{len(catalog.secrets)} declared / {total} observed-only",
                    ),
                )
            )
        )
        blocks.append(
            Bullets(
                tuple(
                    f"{store}: {count} observed-only"
                    for store, count in observed_only.items()
                )
            )
        )
        note = (
            "The bullet list is the declared `secret_refs` — a curated subset. "
            f"Collectors have also observed {total} secrets that no declaration "
            "names (counts above; no values, ever). `cadastre lookup <name>` "
            "finds an observed-only secret; `cadastre drift --kind secret` lists "
            "them. Do not conclude a credential is absent from the declared list "
            "alone."
        )
    else:
        note = "References and existence only. No value ever transits this layer."
    return Section("Secret references", tuple(blocks), note=note)


def _conventions_section(session: Session) -> Section:
    conventions = session.catalog.policy.conventions
    items = tuple(
        (label, value)
        for label, value in (
            ("host name", conventions.host_name),
            ("service name", conventions.service_name),
            ("secret ref", conventions.secret_ref),
            ("endpoint address", conventions.endpoint_address),
        )
        if value
    )
    tiers = tuple(
        f"{tier.name} → network class {tier.network_class}"
        + (", must be fronted by ingress" if tier.requires_ingress else "")
        for tier in session.catalog.policy.exposure
    )
    return Section(
        "Conventions in force",
        (Fields(items), Bullets(tiers)),
        note="Regexes are enforced by `cadastre check`. Match them before committing.",
    )


def _data(session: Session) -> dict[str, Any]:
    catalog = session.catalog
    return {
        "counts": catalog.counts(),
        "networks": [
            {"id": n.id, "class": n.class_, "tags": list(n.tags)}
            for n in catalog.networks
        ],
        "hosts": [
            {
                "id": h.id,
                "role": h.role,
                "reachable_from": list(h.reachable_from),
                "tags": list(h.tags),
                "services": [s.id for s in catalog.services_on(h.id)],
            }
            for h in catalog.hosts
        ],
        "services": [
            {
                "id": s.id,
                "runs_on": s.runs_on,
                "expose": s.expose,
                "endpoints": [e.id for e in catalog.endpoints_of(s.id)],
                "authoritative_pipeline": catalog.authoritative_pipeline(s.id),
                "pipelines": [p.id for p in catalog.pipelines_for(s.id)],
            }
            for s in catalog.services
        ],
        "repos": [
            {
                "id": r.id,
                "remotes": [
                    {"forge": m.forge, "role": m.role, "url": m.url} for m in r.remotes
                ],
                "mirror_from": r.mirror_from,
                "mirror_to": r.mirror_to,
            }
            for r in catalog.repos
        ],
        "secret_refs": [
            {"id": s.id, "ref": s.ref, "store": s.store} for s in catalog.secrets
        ],
        "secrets": {
            "declared": len(catalog.secrets),
            "observed_only_total": sum(
                _observed_only_secrets_by_store(session).values()
            ),
            "observed_only_by_store": _observed_only_secrets_by_store(session),
        },
        "policy": {
            "exposure": [
                {
                    "name": t.name,
                    "network_class": t.network_class,
                    "requires_ingress": t.requires_ingress,
                }
                for t in catalog.policy.exposure
            ],
            **(
                {
                    "replication": [
                        {
                            "source": c.source,
                            "target": c.target,
                            "selectors": list(c.selectors),
                            "mappings": dict(c.mappings),
                        }
                        for c in catalog.policy.replication
                    ]
                }
                if catalog.policy.replication
                else {}
            ),
            "conventions": {
                "host_name": catalog.policy.conventions.host_name,
                "service_name": catalog.policy.conventions.service_name,
                "secret_ref": catalog.policy.conventions.secret_ref,
                "endpoint_address": catalog.policy.conventions.endpoint_address,
            },
        },
    }


def brief(session: Session) -> Document:
    counts = session.catalog.counts()
    summary = ", ".join(f"{count} {kind}s" for kind, count in counts.items() if count)
    return Document(
        title="cadastre brief",
        sections=(
            Section("Estate", (Fields((("summary", summary),)),)),
            _networks_section(session),
            _hosts_section(session),
            _services_section(session),
            _repos_section(session),
            _secrets_section(session),
            _conventions_section(session),
        ),
        provenance=session.provenance(),
        data=_data(session),
    )
