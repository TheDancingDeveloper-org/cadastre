"""Placement: intent in, candidates and exclusions out.

Two design constraints from DESIGN §7 are load-bearing:

* **Intent parsing is deliberately dumb.** Keyword and flag based, no model
  call. The model describes what it wants; the arithmetic is done here, by code
  with unit tests, because a model doing placement arithmetic produces a
  plausible answer rather than a correct one.
* **Exclusions carry equal weight to candidates.** "prox-01 excluded: no GPU"
  is what makes a wrong catalog visibly wrong instead of silently wrong. A
  filter that cannot say why it rejected a host is not finished.

If this ever needs a solver at this scale, the model is wrong, not the
algorithm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cadastre.core import model
from cadastre.core.catalog import Catalog

#: Roles that are never a deployment target. Stated once, cited in exclusions.
NON_TARGET_ROLES = {
    "workstation": "operator workstation, not a deployment target",
    "router": "network device; reached over SSH, runs no workloads",
    "appliance": "appliance; runs no workloads of ours",
    "hypervisor": "hypervisor; place on a guest, not on the host",
}

_SIZE_PATTERNS = (
    (
        re.compile(r"(\d+)\s*(?:g|gb|gib)\b(?:\s*(?:of\s*)?(?:ram|memory))?"),
        "memory_gb",
    ),
    (re.compile(r"(\d+)\s*(?:cores?|cpus?|vcpus?)\b"), "cpu_cores"),
    (re.compile(r"(\d+)\s*(?:gb|g)\s*(?:of\s*)?disk\b"), "disk_gb"),
)

_PORT = re.compile(r"\bport\s+(\d{1,5})\b")
_NAMED = re.compile(r"\b(?:named|called)\s+([a-z0-9][a-z0-9._-]*)")
_HOSTNAME = re.compile(
    r"\b(?:at|hostname|host name|on)\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)"
)
_GPU_WORDS = ("gpu", "cuda", "accelerator", "inference", "transcode")
_PUBLIC_WORDS = ("public", "internet", "internet-facing", "external", "publicly")
_NEGATED_CONSTRAINT = re.compile(
    r"\b(?:not|without|except|excluding|exclude|never)\b[^,.!?;]{0,32}"
    r"\b(?:public|internet|external|gpu|cuda|accelerator|overlay|lan|internal|host)\b"
)
_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*")


def _host_id_pattern(host_id: str) -> re.Pattern[str]:
    escaped = re.escape(host_id).replace(r"\-", "[- ]+")
    return re.compile(rf"\b{escaped}\b")


@dataclass(frozen=True)
class Requirements:
    """What the intent asked for, as far as keywords can tell."""

    intent: str
    expose: str | None = None
    tags: tuple[str, ...] = ()
    needs_gpu: bool = False
    cpu_cores: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    port: int | None = None
    target_host: str | None = None
    hostname: str | None = None
    name: str | None = None
    #: Words the parser did not recognise. Reported, so a requirement that was
    #: silently dropped is visible rather than assumed to have been applied.
    unrecognised: tuple[str, ...] = ()
    parse_conflicts: tuple[str, ...] = ()

    def summary(self) -> tuple[tuple[str, str], ...]:
        items: list[tuple[str, str]] = []
        if self.expose:
            items.append(("expose", self.expose))
        if self.tags:
            items.append(("tags", ", ".join(self.tags)))
        if self.needs_gpu:
            items.append(("gpu", "required"))
        for label, value in (
            ("cpu_cores", self.cpu_cores),
            ("memory_gb", self.memory_gb),
            ("disk_gb", self.disk_gb),
            ("port", self.port),
        ):
            if value is not None:
                items.append((label, str(value)))
        if self.hostname:
            items.append(("hostname", self.hostname))
        if self.target_host:
            items.append(("target_host", self.target_host))
        if self.name:
            items.append(("name", self.name))
        return tuple(items)


@dataclass(frozen=True)
class Exclusion:
    host: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"host": self.host, "reason": self.reason}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class Candidate:
    host: str
    because: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "because": list(self.because)}


@dataclass(frozen=True)
class Conflict:
    kind: str
    subject: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "detail": self.detail}


@dataclass(frozen=True)
class Placement:
    requirements: Requirements
    candidates: tuple[Candidate, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def parse_intent(intent: str, catalog: Catalog) -> Requirements:
    """Keywords only. Anything not recognised is reported, never guessed at."""
    text = intent.lower()
    # Host tags only. Placement filters hosts, and a network tag that happens
    # to read like a requirement ("internal") would otherwise become one.
    known_tags = {tag for host in catalog.hosts for tag in host.tags}
    tiers = {tier.name for tier in catalog.policy.exposure}

    expose: str | None = None
    mentioned_tiers = [
        tier for tier in sorted(tiers) if re.search(rf"\b{re.escape(tier)}\b", text)
    ]
    parse_conflicts: list[str] = []
    if len(mentioned_tiers) > 1:
        parse_conflicts.append(
            "mutually exclusive exposure tiers: " + ", ".join(mentioned_tiers)
        )
    if mentioned_tiers:
        expose = mentioned_tiers[0]
    if _NEGATED_CONSTRAINT.search(text):
        parse_conflicts.append(
            "negated or excluded constraint; restate the intent positively"
        )
    if re.search(r"\bpublic\b[^,.!?;]{0,32}\boverlay(?:-only)?\b", text):
        parse_conflicts.append("public and overlay-only exposure constraints conflict")
    if re.search(
        r"\b(?:except|excluding|exclude)\s+(?:host\s+)?[a-z0-9][a-z0-9 -]*", text
    ):
        parse_conflicts.append("excluded host syntax is not supported")
    if expose is None and any(
        re.search(rf"\b{re.escape(word)}\b", text) for word in _PUBLIC_WORDS
    ):
        public_tiers = [
            tier.name
            for tier in catalog.policy.exposure
            if tier.network_class == "public"
        ]
        if len(public_tiers) == 1:
            expose = public_tiers[0]

    # A word already spent naming the exposure tier is not also a tag
    # requirement: `internal` in "an internal service" is the tier.
    tags = tuple(
        sorted(
            tag
            for tag in known_tags
            if tag != expose and re.search(rf"\b{re.escape(tag)}\b", text)
        )
    )
    sizes: dict[str, int] = {}
    for pattern, key in _SIZE_PATTERNS:
        found = pattern.search(text)
        if found:
            sizes[key] = int(found.group(1))

    port_match = _PORT.search(text)
    name_match = _NAMED.search(text)
    host_match = _HOSTNAME.search(text)
    target_host_match = next(
        (
            host_id
            for host_id in sorted(
                catalog.of("host"), key=lambda value: (-len(value), value)
            )
            if _host_id_pattern(host_id).search(text)
        ),
        None,
    )

    consumed = set()
    for pattern in (_PORT, _NAMED, _HOSTNAME):
        for found in pattern.finditer(text):
            consumed.update(_WORD.findall(found.group(0)))
    if target_host_match:
        consumed.update(_WORD.findall(target_host_match.replace("-", " ")))
    consumed |= set(tags) | tiers | set(_GPU_WORDS) | set(_PUBLIC_WORDS)
    consumed |= {str(v) for v in sizes.values()}
    unrecognised = tuple(
        sorted(
            {
                word
                for word in _WORD.findall(text)
                if word not in consumed
                and word not in _STOPWORDS
                and not word.isdigit()
                and not any(word in v for v in sizes)
            }
        )
    )

    return Requirements(
        intent=intent,
        expose=expose,
        tags=tags,
        needs_gpu=any(word in text for word in _GPU_WORDS),
        cpu_cores=sizes.get("cpu_cores"),
        memory_gb=sizes.get("memory_gb"),
        disk_gb=sizes.get("disk_gb"),
        port=int(port_match.group(1)) if port_match else None,
        target_host=target_host_match,
        hostname=host_match.group(1) if host_match else None,
        name=name_match.group(1) if name_match else None,
        unrecognised=unrecognised,
        parse_conflicts=tuple(dict.fromkeys(parse_conflicts)),
    )


_STOPWORDS = {
    "a",
    "an",
    "the",
    "with",
    "and",
    "or",
    "for",
    "to",
    "on",
    "in",
    "of",
    "at",
    "new",
    "service",
    "deploy",
    "deployment",
    "run",
    "running",
    "needs",
    "need",
    "that",
    "this",
    "it",
    "is",
    "be",
    "should",
    "want",
    "wants",
    "add",
    "set",
    "up",
    "host",
    "hosts",
    "somewhere",
    "please",
    "app",
    "api",
    "container",
}


def _denied_tags(catalog: Catalog, principal: str = "agent") -> dict[str, str]:
    """Tags an existing grant explicitly denies, and the grant that denies them.

    Grants are Phase 4's to evaluate. Reading them here costs nothing and stops
    `context-for` proposing a host the Broker will later refuse — a refusal
    after the artifact is written is the expensive kind.
    """
    denied: dict[str, str] = {}
    for grant in catalog.policy.grants:
        if grant.principal != principal:
            continue
        for selector in grant.deny:
            if selector.startswith("tag:"):
                denied.setdefault(selector[4:], grant.id or grant.role)
    return denied


def _tier_for(catalog: Catalog, expose: str | None) -> model.ExposureTier | None:
    return catalog.policy.tier(expose) if expose else None


def evaluate(catalog: Catalog, requirements: Requirements) -> Placement:
    """Filter hosts. Every rejection carries the reason it was rejected."""
    tier = _tier_for(catalog, requirements.expose)
    denied = _denied_tags(catalog)
    candidates: list[Candidate] = []
    exclusions: list[Exclusion] = []
    notes: list[str] = []

    if requirements.parse_conflicts:
        notes.append(
            "Intent parsing conflict: " + "; ".join(requirements.parse_conflicts)
        )
        return Placement(
            requirements=requirements,
            notes=tuple(notes),
        )
    if requirements.expose and tier is None:
        notes.append(
            f"No exposure tier named {requirements.expose!r} is declared; the "
            "exposure constraint was not applied."
        )

    for host in catalog.hosts:
        reason = _reject(catalog, host, requirements, tier, denied)
        if reason is not None:
            exclusions.append(reason)
            continue
        candidates.append(
            Candidate(host.id, _because(catalog, host, requirements, tier))
        )

    conflicts = _conflicts(catalog, requirements, [c.host for c in candidates])

    if tier and tier.requires_ingress:
        ingress = [s.id for s in catalog.services if "ingress" in s.tags]
        notes.append(
            f"Tier {tier.name!r} requires an ingress. "
            + (
                f"Front it with: {', '.join(ingress)}."
                if ingress
                else "No service is tagged `ingress` — the catalog cannot say which."
            )
        )

    return Placement(
        requirements=requirements,
        candidates=tuple(candidates),
        exclusions=tuple(exclusions),
        conflicts=tuple(conflicts),
        notes=tuple(notes),
    )


def _reject(
    catalog: Catalog,
    host: model.Host,
    requirements: Requirements,
    tier: model.ExposureTier | None,
    denied: dict[str, str],
) -> Exclusion | None:
    if host.role in NON_TARGET_ROLES:
        return Exclusion(host.id, f"role `{host.role}`", NON_TARGET_ROLES[host.role])

    if requirements.target_host and host.id != requirements.target_host:
        return Exclusion(
            host.id,
            "not requested host",
            f"the intent explicitly selected `{requirements.target_host}`",
        )

    for tag, grant in sorted(denied.items()):
        if tag in host.tags and tag not in requirements.tags:
            return Exclusion(
                host.id,
                f"denied by grant `{grant}`",
                f"tagged `{tag}`, which that grant explicitly denies",
            )

    if tier is not None:
        classes = catalog.host_network_classes(host.id)
        networks = catalog.host_networks(host.id)
        if tier.network and tier.network not in {network.id for network in networks}:
            return Exclusion(
                host.id,
                f"no network of class `{tier.network_class}`",
                f"tier `{tier.name}` requires network `{tier.network}`; this host "
                "is not declared reachable from it (networks: "
                + ", ".join(f"{n.id} ({n.class_})" for n in networks)
                + ")",
            )
        if tier.network_class not in classes:
            network_list = ", ".join(
                f"{n.id} ({n.class_})" for n in catalog.host_networks(host.id)
            )
            return Exclusion(
                host.id,
                f"no network of class `{tier.network_class}`",
                f"tier `{tier.name}` requires one; this host is on "
                + (network_list or "no declared network"),
            )

    missing_tags = [t for t in requirements.tags if t not in host.tags]
    if missing_tags:
        return Exclusion(
            host.id,
            "missing required tag" + ("s" if len(missing_tags) > 1 else ""),
            ", ".join(f"`{t}`" for t in missing_tags)
            + f" (has: {', '.join(host.tags) or 'no tags'})",
        )

    resources = host.resources
    if requirements.needs_gpu and not (resources and resources.gpu):
        return Exclusion(host.id, "no GPU", "the intent asked for an accelerator")

    for label, wanted, actual in (
        (
            "cpu_cores",
            requirements.cpu_cores,
            resources.cpu_cores if resources else None,
        ),
        (
            "memory_gb",
            requirements.memory_gb,
            resources.memory_gb if resources else None,
        ),
        ("disk_gb", requirements.disk_gb, resources.disk_gb if resources else None),
    ):
        if wanted is None:
            continue
        if actual is None:
            return Exclusion(
                host.id,
                f"unknown {label}",
                f"the intent asked for {wanted}; the catalog does not say what this "
                "host has, and Cadastre will not guess",
            )
        if actual < wanted:
            return Exclusion(
                host.id, f"insufficient {label}", f"has {actual}, needs {wanted}"
            )
    return None


def _because(
    catalog: Catalog,
    host: model.Host,
    requirements: Requirements,
    tier: model.ExposureTier | None,
) -> tuple[str, ...]:
    reasons = []
    if requirements.target_host:
        reasons.append(f"explicitly selected as `{requirements.target_host}`")
    if tier is not None:
        matching = [
            n.id
            for n in catalog.host_networks(host.id)
            if (tier.network and n.id == tier.network)
            or (tier.network is None and n.class_ == tier.network_class)
        ]
        reasons.append(
            f"on {', '.join(matching)} (class {tier.network_class}) as tier "
            f"`{tier.name}` requires"
        )
    if requirements.tags:
        reasons.append("tagged " + ", ".join(f"`{t}`" for t in requirements.tags))
    if requirements.needs_gpu and host.resources and host.resources.gpu:
        reasons.append(f"has an accelerator ({host.resources.gpu})")
    running = catalog.services_on(host.id)
    reasons.append(f"currently runs {len(running)} service(s)")
    return tuple(reasons)


def _conflicts(
    catalog: Catalog, requirements: Requirements, candidate_hosts: list[str]
) -> list[Conflict]:
    conflicts: list[Conflict] = []
    if requirements.port is not None:
        for host_id in candidate_hosts:
            taken = catalog.ports_on_host(host_id)
            if requirements.port in taken:
                conflicts.append(
                    Conflict(
                        "port",
                        f"{host_id}:{requirements.port}",
                        "already bound by " + ", ".join(taken[requirements.port]),
                    )
                )
    if requirements.hostname:
        claims = catalog.hostnames().get(requirements.hostname)
        if claims:
            conflicts.append(
                Conflict(
                    "hostname",
                    requirements.hostname,
                    "already claimed by " + ", ".join(claims),
                )
            )
    if requirements.name and catalog.get("service", requirements.name):
        conflicts.append(
            Conflict(
                "service name",
                requirements.name,
                "a service with that id is already declared",
            )
        )
    return conflicts


def placement_for(catalog: Catalog, intent: str) -> Placement:
    return evaluate(catalog, parse_intent(intent, catalog))
