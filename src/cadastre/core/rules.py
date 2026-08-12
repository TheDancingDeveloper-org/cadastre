"""The rule engine behind `cadastre check`.

DESIGN §3.3 is the format contract, and it is the whole value of this module:
an error states **what is wrong, why, and the fix**. An error of that shape
gets self-corrected in one turn. Ten read-only tools do not buy what one good
error message does.

Every rule here ships with a failing-artifact fixture in `tests/artifacts/`. A
rule without a negative case is untested.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cadastre.core import model
from cadastre.core.artifacts import (
    Artifact,
    ExecutionRequirement,
    PortBinding,
    ProposedService,
)
from cadastre.core.catalog import Catalog
from cadastre.core.observed import ObservedSource
from cadastre.core.placement import NON_TARGET_ROLES
from cadastre.render.document import Finding
from cadastre.render.inert import inert


@dataclass(frozen=True)
class RuleContext:
    catalog: Catalog
    artifact: Artifact
    observed: tuple[ObservedSource, ...] = ()

    def observed_hostnames(self) -> dict[str, str]:
        """Hostnames the ingress is actually serving, whatever `declared/` says.

        A collision with running configuration is a collision, and the ingress
        collector is the source that knows.
        """
        claims: dict[str, str] = {}
        for source in self.observed:
            for entity in source.entities.get("endpoint", []):
                assert isinstance(entity, model.Endpoint)
                if entity.address:
                    claims.setdefault(entity.address, f"{source.source}:{entity.id}")
            for entity in source.entities.get("domain", []):
                assert isinstance(entity, model.Domain)
                claims.setdefault(entity.name, f"{source.source}:{entity.id}")
        return claims

    def observed_bindings(self, host_id: str) -> list[tuple[PortBinding, str]]:
        """Fresh listeners observed on a host.

        An endpoint collector can report ``host`` even when its workload has no
        declared service yet.  The service join remains a compatibility fallback
        for older collectors, never a prerequisite for safety checking.
        """
        taken: list[tuple[PortBinding, str]] = []
        for source in self.observed:
            if not source.ok:
                continue
            for entity in source.entities.get("endpoint", []):
                if not isinstance(entity, model.Endpoint) or entity.port is None:
                    continue
                observed_host = entity.host
                if observed_host is None:
                    service = self.catalog.get("service", entity.service)
                    observed_host = (
                        service.runs_on if isinstance(service, model.Service) else None
                    )
                if observed_host == host_id:
                    taken.append(
                        (
                            PortBinding(
                                entity.port,
                                _transport(entity.protocol),
                                entity.bind_address,
                            ),
                            f"{source.source}:{entity.service or entity.id}",
                        )
                    )
        return taken

    def declared_bindings(self, host_id: str) -> list[tuple[PortBinding, str]]:
        services = {service.id for service in self.catalog.services_on(host_id)}
        return [
            (
                PortBinding(
                    endpoint.port,
                    _transport(endpoint.protocol),
                    endpoint.bind_address,
                ),
                endpoint.service or endpoint.id,
            )
            for endpoint in self.catalog.endpoints
            if endpoint.service in services and endpoint.port is not None
        ]

    def endpoint_sources_unavailable(self) -> bool:
        endpoint_sources = [
            source for source in self.observed if "endpoint.list" in source.capabilities
        ]
        return not endpoint_sources or any(not source.ok for source in endpoint_sources)


Rule = Callable[[RuleContext], Iterable[Finding]]

RULES: list[tuple[str, Rule]] = []


def rule(code: str) -> Callable[[Rule], Rule]:
    def register(func: Rule) -> Rule:
        RULES.append((code, func))
        return func

    return register


def _services(context: RuleContext) -> tuple[ProposedService, ...]:
    return context.artifact.services


def _transport(protocol: str) -> str:
    """Endpoint protocols such as HTTPS ride TCP; listener conflicts do not."""
    return protocol if protocol in {"tcp", "udp"} else "tcp"


def _addresses_overlap(first: str | None, second: str | None) -> bool:
    """A wildcard listener overlaps every address in its address family.

    Cadastre intentionally treats an omitted bind address as wildcard. This is
    the Compose default and avoids calling an unqualified published port free.
    """
    wildcards = {None, "", "0.0.0.0", "::", "[::]"}
    return first in wildcards or second in wildcards or first == second


def _bindings_conflict(first: PortBinding, second: PortBinding) -> bool:
    return (
        first.port == second.port
        and first.protocol == second.protocol
        and _addresses_overlap(first.bind_address, second.bind_address)
    )


def _binding_label(binding: PortBinding) -> str:
    address = binding.bind_address or "all interfaces"
    return f"{address}:{binding.port}/{binding.protocol}"


# ---------------------------------------------------------------------------


@rule("unknown-host")
def unknown_host(context: RuleContext) -> Iterable[Finding]:
    known = sorted(context.catalog.of("host"))
    for service in _services(context):
        if service.host and service.host not in context.catalog.of("host"):
            yield Finding(
                level="error",
                code="unknown-host",
                subject=f"services.{service.name}.host",
                message=f"`{service.host}` is not a host in the catalog.",
                why=(
                    "Placement, exposure, and port rules are all evaluated against "
                    "a declared host. An unknown one cannot be checked at all."
                ),
                fix=(
                    "Use one of: "
                    + ", ".join(known)
                    + ". If the host is real but missing, declare it in "
                    "declared/hosts/ first — do not infer it from a naming pattern."
                ),
            )


@rule("exposure-network-class")
def exposure_network_class(context: RuleContext) -> Iterable[Finding]:
    catalog = context.catalog
    for service in _services(context):
        if not service.expose or not service.host:
            continue
        tier = catalog.policy.tier(service.expose)
        if tier is None:
            yield Finding(
                level="error",
                code="unknown-exposure-tier",
                subject=f"services.{service.name}.expose",
                message=f"`{service.expose}` is not a declared exposure tier.",
                why=(
                    "Cadastre ships no opinion about which tiers exist; yours "
                    "are declared."
                ),
                fix=(
                    "Use one of: "
                    + ", ".join(t.name for t in catalog.policy.exposure)
                    + " (declared/policy/exposure.yaml)."
                ),
            )
            continue
        classes = catalog.host_network_classes(service.host)
        network_ids = {network.id for network in catalog.host_networks(service.host)}
        if tier.network and tier.network not in network_ids:
            better = [
                host.id
                for host in catalog.hosts
                if tier.network
                in {network.id for network in catalog.host_networks(host.id)}
            ]
            yield Finding(
                level="error",
                code="exposure-network-class",
                subject=f"services.{service.name}.expose",
                message=f'"{service.expose}" requires network `{tier.network}`.',
                why=(
                    f"Host `{service.host}` is reachable only from "
                    + ", ".join(sorted(network_ids) or ["no declared network"])
                    + "."
                ),
                fix=(
                    f"place on a host in a {tier.network_class}-class "
                    f"network reachable from `{tier.network}`"
                    + (f" ({', '.join(better)})." if better else ".")
                ),
            )
            continue
        if tier.network_class not in classes:
            networks = ", ".join(
                f"`{n.id}` (class={n.class_})"
                for n in catalog.host_networks(service.host)
            )
            better = [
                host.id
                for host in catalog.hosts
                if tier.network_class in catalog.host_network_classes(host.id)
            ]
            yield Finding(
                level="error",
                code="exposure-network-class",
                subject=f"services.{service.name}.expose",
                message=(
                    f'"{service.expose}" requires an exposure tier with '
                    f"class={tier.network_class}."
                ),
                why=(
                    f"Host `{service.host}` is reachable only from "
                    + (networks or "no declared network")
                    + "."
                ),
                fix=(
                    "set expose to a tier with class="
                    + ", ".join(sorted(classes) or ["(none)"])
                    + ", or place on a host in a "
                    + f"{tier.network_class}-class network"
                    + (f" ({', '.join(better)})" if better else "")
                    + "."
                ),
            )


@rule("exposure-requires-ingress")
def exposure_requires_ingress(context: RuleContext) -> Iterable[Finding]:
    catalog = context.catalog
    ingress_services = [s.id for s in catalog.services if "ingress" in s.tags]
    for service in _services(context):
        tier = catalog.policy.tier(service.expose) if service.expose else None
        if tier is None or not tier.requires_ingress or service.fronted_by:
            continue
        yield Finding(
            level="warn",
            code="exposure-requires-ingress",
            subject=f"services.{service.name}.expose",
            message=(
                f"Tier `{tier.name}` requires the service to sit behind an ingress."
            ),
            why="Nothing in this artifact says which ingress fronts it.",
            fix=(
                "add `x-cadastre: {fronted_by: "
                + (ingress_services[0] if ingress_services else "<ingress-service>")
                + "}`, and add the route to the ingress config in the same change."
            ),
        )


@rule("exposure-none-conflict")
def exposure_none_conflict(context: RuleContext) -> Iterable[Finding]:
    catalog = context.catalog
    for service in _services(context):
        tier = catalog.policy.tier(service.expose) if service.expose else None
        if tier is None or tier.name != "none":
            continue
        claims: list[str] = []
        if service.ports:
            claims.append("published ports")
        if service.hostnames:
            claims.append("hostnames")
        if service.fronted_by:
            claims.append("fronted_by")
        if claims:
            yield Finding(
                level="error",
                code="exposure-none-conflict",
                subject=f"services.{service.name}.expose",
                message="Exposure tier `none` cannot claim inbound access.",
                why="The artifact declares " + ", ".join(claims) + ".",
                fix=(
                    "Remove the inbound claims or choose an exposure tier that "
                    "permits them."
                ),
            )


@rule("fronted-by-validation")
def fronted_by_validation(context: RuleContext) -> Iterable[Finding]:
    catalog = context.catalog
    for service in _services(context):
        if not service.fronted_by:
            continue
        ingress = catalog.get("service", service.fronted_by)
        if not isinstance(ingress, model.Service):
            yield Finding(
                level="error",
                code="fronted-by-validation",
                subject=f"services.{service.name}.fronted_by",
                message=f"Ingress service `{service.fronted_by}` is not declared.",
                why="A non-empty frontend reference must resolve to a catalog service.",
                fix="Use a declared service tagged `ingress`.",
            )
            continue
        if "ingress" not in ingress.tags:
            yield Finding(
                level="error",
                code="fronted-by-validation",
                subject=f"services.{service.name}.fronted_by",
                message=f"Service `{service.fronted_by}` is not tagged `ingress`.",
                why=(
                    "Only a service with the ingress capability can front another "
                    "service."
                ),
                fix=(
                    f"Tag `{service.fronted_by}` as ingress or choose an ingress "
                    "service."
                ),
            )
        target_tier = catalog.policy.tier(service.expose) if service.expose else None
        if target_tier is not None and not target_tier.requires_ingress:
            yield Finding(
                level="error",
                code="fronted-by-validation",
                subject=f"services.{service.name}.fronted_by",
                message=f"Tier `{target_tier.name}` does not permit an ingress claim.",
                why=(
                    "The artifact declares fronting for a service whose exposure "
                    "tier does not require it."
                ),
                fix="Remove `fronted_by` or choose a tier that requires ingress.",
            )
        if (
            target_tier is not None
            and target_tier.requires_ingress
            and ingress.runs_on
            and target_tier.network
            and target_tier.network
            not in {network.id for network in catalog.host_networks(ingress.runs_on)}
        ):
            yield Finding(
                level="error",
                code="fronted-by-validation",
                subject=f"services.{service.name}.fronted_by",
                message=(
                    f"Frontend `{ingress.id}` cannot provide tier `{target_tier.name}`."
                ),
                why=(
                    f"Its host `{ingress.runs_on}` is not reachable from required "
                    f"network `{target_tier.network}`."
                ),
                fix=(
                    "Use an ingress on the required exposure network or choose a "
                    "tier compatible with its reachability."
                ),
            )
        if ingress.runs_on and service.host:
            ingress_networks = {
                network.id for network in catalog.host_networks(ingress.runs_on)
            }
            target_networks = {
                network.id for network in catalog.host_networks(service.host)
            }
            if not ingress_networks & target_networks:
                yield Finding(
                    level="error",
                    code="fronted-by-validation",
                    subject=f"services.{service.name}.fronted_by",
                    message=f"Frontend `{ingress.id}` cannot reach `{service.name}`.",
                    why=(
                        "The frontend and workload have no shared declared network: "
                        + ", ".join(sorted(ingress_networks | target_networks))
                    ),
                    fix=(
                        "Place both services on hosts with a viable shared network "
                        "path."
                    ),
                )


@rule("artifact-internal-collision")
def artifact_internal_collision(context: RuleContext) -> Iterable[Finding]:
    hostnames: dict[str, list[str]] = {}
    bindings: dict[str, list[tuple[PortBinding, str]]] = {}
    for service in _services(context):
        for hostname in service.hostnames:
            hostnames.setdefault(hostname, []).append(service.name)
        if service.host:
            for binding in service.bindings:
                bindings.setdefault(service.host, []).append((binding, service.name))
    for hostname, claimants in sorted(hostnames.items()):
        if len(claimants) > 1:
            yield Finding(
                level="error",
                code="artifact-internal-collision",
                subject=hostname,
                message=(
                    f"Hostname `{hostname}` is claimed by multiple proposed services."
                ),
                why=(
                    "Intra-artifact claims conflict before catalog state is considered."
                ),
                fix="Give each proposed service a unique hostname.",
            )
    for host, host_bindings in sorted(bindings.items()):
        for index, (binding, claimant) in enumerate(host_bindings):
            collisions = [
                other_claimant
                for other, other_claimant in host_bindings[index + 1 :]
                if _bindings_conflict(binding, other)
            ]
            if not collisions:
                continue
            yield Finding(
                level="error",
                code="artifact-internal-collision",
                subject=f"{host}:{_binding_label(binding)}",
                message=(
                    f"Listener {_binding_label(binding)} on `{host}` is published by "
                    "multiple proposed services."
                ),
                why=(
                    f"`{claimant}` conflicts with {', '.join(collisions)}; two "
                    "services cannot bind overlapping host listeners in one artifact."
                ),
                fix=(
                    "Choose distinct published ports or place one service on "
                    "another host."
                ),
            )


@rule("hostname-collision")
def hostname_collision(context: RuleContext) -> Iterable[Finding]:
    declared = context.catalog.hostnames()
    observed = context.observed_hostnames()
    for hostname, claimant in context.artifact.hostnames:
        existing = declared.get(hostname)
        if existing:
            yield Finding(
                level="error",
                code="hostname-collision",
                subject=hostname,
                message=f"`{hostname}` is already claimed in the catalog.",
                why="Claimed by "
                + ", ".join(existing)
                + f"; this artifact adds {claimant}.",
                fix=(
                    "choose another hostname, or if this is a deliberate move, remove "
                    "the existing claim in the same change so the two never overlap."
                ),
            )
        elif hostname in observed:
            yield Finding(
                level="error",
                code="hostname-collision",
                subject=hostname,
                message=f"`{hostname}` is already being served.",
                why=(
                    f"Observed at {observed[hostname]}, though nothing declares it. "
                    "That is itself worth reporting (`cadastre drift`)."
                ),
                fix="choose another hostname, or reconcile the undeclared route first.",
            )


@rule("port-collision")
def port_collision(context: RuleContext) -> Iterable[Finding]:
    checked_observed_hosts: set[str] = set()
    for service in _services(context):
        if not service.host or not service.bindings:
            continue
        taken = context.declared_bindings(service.host) + context.observed_bindings(
            service.host
        )
        checked_observed_hosts.add(service.host)
        for binding in service.bindings:
            conflicts = [
                claimant
                for existing, claimant in taken
                if _bindings_conflict(binding, existing)
                and claimant != service.name
                and not claimant.endswith(f":{service.name}")
            ]
            if conflicts:
                taken_labels = sorted(
                    {_binding_label(existing) for existing, _ in taken}
                )
                yield Finding(
                    level="error",
                    code="port-collision",
                    subject=f"services.{service.name}.ports",
                    message=(
                        f"Listener {_binding_label(binding)} is already bound on "
                        f"`{service.host}`."
                    ),
                    why="Bound by " + ", ".join(sorted(set(conflicts))) + ".",
                    fix=(
                        f"publish on a free port — taken on {service.host}: "
                        + ", ".join(taken_labels)
                        + " — or place the service on another host."
                    ),
                )
    if context.endpoint_sources_unavailable() and checked_observed_hosts:
        yield Finding(
            level="warn",
            code="port-collision-unchecked",
            subject="observed endpoint bindings",
            message="Current observed host bindings are stale or unavailable.",
            why=(
                "Port collision checking cannot establish that live bindings are "
                "absent."
            ),
            fix="Refresh an endpoint-capable collector before relying on this check.",
        )


@rule("secret-ref-format")
def secret_ref_format(context: RuleContext) -> Iterable[Finding]:
    pattern = context.catalog.policy.conventions.secret_ref
    if not pattern:
        return
    compiled = re.compile(pattern)
    for ref in context.artifact.secret_refs:
        if not compiled.match(ref):
            example = next(
                (s.ref for s in context.catalog.secrets if compiled.match(s.ref)), None
            )
            yield Finding(
                level="error",
                code="secret-ref-format",
                subject=ref,
                message="Secret reference does not match the required format.",
                why=f"The convention in force is `{pattern}`.",
                fix=(
                    "rewrite the reference to match"
                    + (f", e.g. `{example}`" if example else "")
                    + ". The value itself never appears in the artifact."
                ),
            )


@rule("secret-ref-unknown")
def secret_ref_unknown(context: RuleContext) -> Iterable[Finding]:
    known = context.catalog.secret_refs()
    observed_refs = {
        entity.ref
        for source in context.observed
        for entity in source.entities.get("secret", [])
        if isinstance(entity, model.Secret)
    }
    for ref in context.artifact.secret_refs:
        if ref in known or ref in observed_refs:
            continue
        yield Finding(
            level="error",
            code="secret-ref-unknown",
            subject=ref,
            message="No such secret reference.",
            why=(
                "Neither declared/secrets/ nor any collected secret store has a "
                "reference with this name. A reference that does not resolve "
                "fails at deploy time, not at review time."
            ),
            fix=(
                "declare the secret in declared/secrets/ and create it in the store, "
                "or use an existing reference: "
                + (", ".join(sorted(known)[:5]) or "(none declared)")
            ),
        )


@rule("service-name-convention")
def service_name_convention(context: RuleContext) -> Iterable[Finding]:
    pattern = context.catalog.policy.conventions.service_name
    if not pattern:
        return
    compiled = re.compile(pattern)
    for service in _services(context):
        if not compiled.match(service.name):
            yield Finding(
                level="error",
                code="service-name-convention",
                subject=f"services.{service.name}",
                message="Service name does not match the naming convention.",
                why=f"The convention in force is `{pattern}`.",
                fix="rename the service to match before committing.",
            )


@rule("service-name-collision")
def service_name_collision(context: RuleContext) -> Iterable[Finding]:
    for service in _services(context):
        existing = context.catalog.get("service", service.name)
        if existing is None or not isinstance(existing, model.Service):
            continue
        if service.host and existing.runs_on != service.host:
            yield Finding(
                level="error",
                code="service-name-collision",
                subject=f"services.{service.name}",
                message=(
                    f"A service called `{service.name}` is already declared on "
                    f"`{existing.runs_on}`."
                ),
                why="This artifact places the same id on a different host.",
                fix=(
                    "pick a different id, or if this is a move, update "
                    "declared/services/ in the same change rather than running two."
                ),
            )


@rule("pipeline-authority")
def pipeline_authority(context: RuleContext) -> Iterable[Finding]:
    """Dual-CI repositories are the norm here, and nothing in the repo says
    which pipeline actually deploys unless that authority is declared."""
    catalog = context.catalog
    for service in _services(context):
        pipelines = catalog.pipelines_for(service.name)
        if len(pipelines) < 2:
            continue
        if catalog.authoritative_pipeline(service.name):
            continue
        yield Finding(
            level="error",
            code="pipeline-authority",
            subject=f"services.{service.name}.deployed_by",
            message=(
                f"`{service.name}` is claimed by {len(pipelines)} pipelines and none "
                "is marked authoritative."
            ),
            why=(
                "Claimed by "
                + ", ".join(f"{p.id} ({p.system})" for p in pipelines)
                + ". Which one deploys is not inferable, and two that both deploy "
                "will race."
            ),
            fix=(
                "set `authoritative: true` on exactly one entry of "
                f"service `{service.name}`'s `deployed_by` list in declared/services/."
            ),
        )
    if context.artifact.kind == "pipeline" and context.artifact.repo:
        repo_pipelines = [
            p for p in catalog.pipelines if p.repo == context.artifact.repo
        ]
        systems = {p.system for p in repo_pipelines} | set(
            context.artifact.pipeline_systems
        )
        if len(systems) > 1:
            yield Finding(
                level="warn",
                code="pipeline-authority",
                subject=context.artifact.path,
                message=(
                    f"Repo `{context.artifact.repo}` carries pipelines for "
                    f"{len(systems)} CI systems."
                ),
                why="Systems: " + ", ".join(sorted(systems)) + ".",
                fix=(
                    "make sure exactly one is authoritative for deployment, and that "
                    "the other only builds and tests."
                ),
            )


# ---------------------------------------------------------------------------
# CI execution targets
#
# These rules exist because the questions could not be answered from plugin
# evidence: core cannot read `extra` or a vendor field and stay honest about
# what it knows. Each one names a policy question from the runner plan, and
# every one of them refuses to infer a fact it was not given.
# ---------------------------------------------------------------------------


def _execution_requirements(context: RuleContext) -> tuple[ExecutionRequirement, ...]:
    if context.artifact.kind != "pipeline":
        return ()
    return context.artifact.executions


def _system(context: RuleContext) -> str | None:
    systems = context.artifact.pipeline_systems
    return systems[0] if len(systems) == 1 else None


def _reachable_pools(context: RuleContext) -> list[model.CiPool]:
    """Pools this artifact's jobs could actually be scheduled onto."""
    catalog = context.catalog
    system = _system(context)
    wanted: set[str] = set()
    for requirement in _execution_requirements(context):
        if requirement.pool:
            wanted.add(requirement.pool)
        for executor in catalog.eligible_executors(
            system=system, labels=requirement.labels, pool=requirement.pool
        ):
            if executor.pool:
                wanted.add(executor.pool)
    return [pool for pool in catalog.ci_pools if pool.id in wanted]


def _reachable_executors(context: RuleContext) -> list[model.CiExecutor]:
    catalog = context.catalog
    system = _system(context)
    found: dict[str, model.CiExecutor] = {}
    for requirement in _execution_requirements(context):
        for executor in catalog.eligible_executors(
            system=system, labels=requirement.labels, pool=requirement.pool
        ):
            found[executor.id] = executor
    return [found[key] for key in sorted(found)]


def _stale_executor_sources(context: RuleContext) -> bool:
    sources = [s for s in context.observed if "ci.status" in s.capabilities]
    return bool(sources) and any(not source.ok for source in sources)


@rule("execution-indeterminate")
def execution_indeterminate(context: RuleContext) -> Iterable[Finding]:
    """A selector Cadastre cannot decide is reported, not resolved.

    Evaluating a CI expression needs the runtime context the CI system has.
    Guessing at it would turn "a human should look at this" into a confident
    answer that is sometimes wrong, which is worse than no answer.
    """
    for requirement in _execution_requirements(context):
        if requirement.kind != "indeterminate":
            continue
        yield Finding(
            level="warn",
            code="execution-indeterminate",
            subject=f"{context.artifact.path}:{requirement.job}",
            message=(
                f"Job `{requirement.job}` selects its executor with an expression, "
                "so its routing could not be checked."
            ),
            why=(
                "Selector: "
                + ", ".join(inert(item) for item in requirement.expressions)
                + ". Its value depends on runtime context Cadastre does not have."
            ),
            fix=(
                "review the routing by hand, or pin the selector to a literal label "
                "set if this job must always reach a particular pool."
            ),
        )


@rule("execution-unsatisfiable")
def execution_unsatisfiable(context: RuleContext) -> Iterable[Finding]:
    """No declared executor can serve a static selector.

    Reported only when executors are declared at all: a catalog that has never
    declared one is not making a claim about routing, and a finding on every
    pipeline in that catalog would be noise.
    """
    catalog = context.catalog
    if not catalog.ci_executors:
        return
    hosted = set(catalog.policy.execution.hosted_selectors)
    system = _system(context)
    for requirement in _execution_requirements(context):
        if requirement.kind not in ("labels", "pool"):
            continue
        if hosted & set(requirement.labels):
            continue  # a vendor-hosted pool is not a declared executor
        if requirement.pool and catalog.get("ci_pool", requirement.pool) is None:
            yield Finding(
                level="error",
                code="execution-unknown-pool",
                subject=f"{context.artifact.path}:{requirement.job}",
                message=(
                    f"Job `{requirement.job}` selects pool "
                    f"`{requirement.pool}`, which is not declared."
                ),
                why=(
                    "Declared pools: "
                    + (", ".join(p.id for p in catalog.ci_pools) or "none")
                    + "."
                ),
                fix=(
                    "declare the pool in declared/ci-pools/, or select one that exists."
                ),
            )
            continue
        eligible = catalog.eligible_executors(
            system=system, labels=requirement.labels, pool=requirement.pool
        )
        if eligible:
            continue
        yield Finding(
            level="error",
            code="execution-unsatisfiable",
            subject=f"{context.artifact.path}:{requirement.job}",
            message=(
                f"No declared executor matches job `{requirement.job}`'s selector."
            ),
            why=(
                "It asks for "
                + (
                    ", ".join(inert(label) for label in requirement.labels)
                    or "no labels"
                )
                + (f" in pool `{requirement.pool}`" if requirement.pool else "")
                + ". Label matching is conjunctive: one executor must carry all of "
                "them."
            ),
            fix=(
                "add the labels to a declared executor, or change the selector to "
                "one an existing executor carries."
            ),
        )


@rule("execution-hosted-pool")
def execution_hosted_pool(context: RuleContext) -> Iterable[Finding]:
    """Vendor-hosted execution where policy requires self-hosted.

    Which selectors name a hosted pool is declared, not built in: `ubuntu-latest`
    means something to one CI system and nothing to another.
    """
    policy = context.catalog.policy.execution
    if not policy.require_self_hosted or not policy.hosted_selectors:
        return
    hosted = set(policy.hosted_selectors)
    for requirement in _execution_requirements(context):
        selected = sorted(hosted & set(requirement.labels))
        if not selected:
            continue
        yield Finding(
            level="error",
            code="execution-hosted-pool",
            subject=f"{context.artifact.path}:{requirement.job}",
            message=(
                f"Job `{requirement.job}` selects hosted execution "
                f"({', '.join(selected)}), which policy does not permit."
            ),
            why=(
                "declared/policy/execution.yaml sets `require_self_hosted: true` "
                f"and names {', '.join(sorted(hosted))} as hosted selectors."
            ),
            fix=(
                "select a declared executor's labels instead, or change the policy "
                "if hosted execution is in fact acceptable here."
            ),
        )


@rule("execution-capability")
def execution_capability(context: RuleContext) -> Iterable[Finding]:
    """A required toolchain no eligible executor declares.

    Availability must be explicit catalog intent. It is never inferred from the
    operating system, a custom label, or the fact that a job like this one
    succeeded before — a runner image without a Rust toolchain will fail a Rust
    build however many times its label says `build`.
    """
    catalog = context.catalog
    if not catalog.ci_executors:
        return
    system = _system(context)
    for requirement in _execution_requirements(context):
        if requirement.kind not in ("labels", "pool") or not requirement.capabilities:
            continue
        eligible = catalog.eligible_executors(
            system=system, labels=requirement.labels, pool=requirement.pool
        )
        if not eligible:
            continue  # already reported as unsatisfiable
        for capability in requirement.capabilities:
            if any(capability in executor.capabilities for executor in eligible):
                continue
            yield Finding(
                level="error",
                code="execution-capability",
                subject=f"{context.artifact.path}:{requirement.job}",
                message=(
                    f"Job `{requirement.job}` requires `{capability}`, which no "
                    "eligible executor declares."
                ),
                why=(
                    "Eligible: "
                    + ", ".join(executor.id for executor in eligible)
                    + ". A toolchain is not implied by the OS, by a label, or by a "
                    "previous successful run."
                ),
                fix=(
                    f"add `{capability}` to the executor's `capabilities` in "
                    "declared/ci-executors/ once it is actually installed, or drop "
                    "the requirement from `x-cadastre.requires`."
                ),
            )


@rule("execution-availability")
def execution_availability(context: RuleContext) -> Iterable[Finding]:
    """Every eligible executor is offline, or its evidence is stale.

    A warning, not an error. Eligibility is a fact about the catalog;
    availability is a fact about a moment, and a moment is not a deployment
    outcome. Reported so nobody reads a green check as "this will schedule".
    """
    catalog = context.catalog
    if not catalog.ci_executors:
        return
    stale = _stale_executor_sources(context)
    system = _system(context)
    for requirement in _execution_requirements(context):
        if requirement.kind not in ("labels", "pool"):
            continue
        eligible = catalog.eligible_executors(
            system=system, labels=requirement.labels, pool=requirement.pool
        )
        if not eligible:
            continue
        if any(executor.status == "online" for executor in eligible) and not stale:
            continue
        states = ", ".join(f"{e.id} ({e.status})" for e in eligible)
        yield Finding(
            level="warn",
            code="execution-availability",
            subject=f"{context.artifact.path}:{requirement.job}",
            message=(
                f"No eligible executor for job `{requirement.job}` is known to be "
                "online."
            ),
            why=(
                f"Eligible: {states}."
                + (
                    " The runner evidence is stale, so this is what was last seen "
                    "rather than what is true now."
                    if stale
                    else ""
                )
            ),
            fix=(
                "check the executors before relying on this pipeline. This is an "
                "availability risk, not a guaranteed failure."
            ),
        )


def _public_access_findings(
    catalog: Catalog, pools: list[model.CiPool]
) -> list[Finding]:
    """A pool that public repositories may schedule onto.

    A persistent executor reachable from a public repository can be persistently
    compromised by untrusted workflow code, and an organisation-scoped pool
    widens the blast radius to every repository that shares it. Cadastre cannot
    secure the machine; it can refuse to let this be invisible.
    """
    findings: list[Finding] = []
    if catalog.policy.execution.allow_public_repositories:
        return findings
    for pool in pools:
        if not pool.public_repositories:
            continue
        persistent = [
            executor
            for executor in catalog.ci_executors
            if executor.pool == pool.id and not executor.ephemeral
        ]
        findings.append(
            Finding(
                level="error" if persistent else "warn",
                code="execution-public-access",
                subject=f"ci_pool.{pool.id}",
                message=f"Pool `{pool.id}` permits public repositories.",
                why=(
                    (
                        "Persistent executors in it: "
                        + ", ".join(executor.id for executor in persistent)
                        + ". Untrusted workflow code from a public repository "
                        "runs on a machine that survives the job."
                    )
                    if persistent
                    else (
                        "It has no declared persistent executor, so the blast "
                        "radius is smaller — ephemeral registration is still not "
                        "proof of a clean underlying host."
                    )
                ),
                fix=(
                    "set `public_repositories: false` on the pool, or set "
                    "`allow_public_repositories: true` in "
                    "declared/policy/execution.yaml if this is a deliberate, "
                    "reviewed decision."
                ),
            )
        )
    return findings


@rule("execution-public-access")
def execution_public_access(context: RuleContext) -> Iterable[Finding]:
    """Public-repository access, for the pools this artifact could reach.

    Scoped to the artifact rather than reported over the whole catalog on every
    run: a warning that appears on every unrelated `check` is one people learn
    to scroll past. `check_catalog` carries the estate-wide version, which is
    what the write gate needs.
    """
    catalog = context.catalog
    reachable = _reachable_pools(context)
    yield from _public_access_findings(catalog, reachable)


def _placement_findings(
    catalog: Catalog, executors: list[model.CiExecutor]
) -> list[Finding]:
    """A declared executor placed nowhere, or somewhere it may not be.

    `runs_on` is intent that someone stated. It is checked against the same
    host exclusions placement uses, because a registration that claims to live
    on a workstation or a router is either wrong or a policy problem.
    """
    findings: list[Finding] = []
    excluded = dict(NON_TARGET_ROLES)
    for role in catalog.policy.execution.excluded_host_roles:
        excluded.setdefault(role, "excluded by declared/policy/execution.yaml")
    for executor in executors:
        if executor.runs_on is None:
            continue
        host = catalog.get("host", executor.runs_on)
        if host is None:
            findings.append(
                Finding(
                    level="error",
                    code="execution-placement",
                    subject=f"ci_executor.{executor.id}.runs_on",
                    message=(
                        f"Executor `{executor.id}` is placed on host "
                        f"`{executor.runs_on}`, which is not declared."
                    ),
                    why="Placement is declared intent, so an unknown host is a typo or "
                    "a host nobody declared.",
                    fix=f"declare host `{executor.runs_on}`, or correct the placement.",
                )
            )
            continue
        assert isinstance(host, model.Host)
        if host.role in excluded:
            findings.append(
                Finding(
                    level="error",
                    code="execution-placement",
                    subject=f"ci_executor.{executor.id}.runs_on",
                    message=(
                        f"Executor `{executor.id}` is placed on `{host.id}`, "
                        f"a {host.role}."
                    ),
                    why=f"{excluded[host.role]}.",
                    fix=(
                        "place the executor on a host that runs workloads, or correct "
                        "the host's role if it really is a deployment target."
                    ),
                )
            )
    return findings


@rule("execution-placement")
def execution_placement(context: RuleContext) -> Iterable[Finding]:
    """Placement, for the executors this artifact could actually reach."""
    yield from _placement_findings(context.catalog, _reachable_executors(context))


@rule("grant-wildcard")
def grant_wildcard(context: RuleContext) -> Iterable[Finding]:
    """Cadastre does not defend against an operator writing an over-broad grant.
    `targets: ["*"]` with `actions: ["shell.exec"]` is a working configuration
    and a bad one — so it is warned about, loudly (DESIGN §6).

    Only grants files are examined, not the catalog's own grants on every run.
    A warning that appears on every unrelated `check` is a warning people learn
    to scroll past; the CI gate checks `declared/policy/grants.yaml` directly.
    """
    if context.artifact.kind != "grants":
        return
    grants = context.artifact.grants
    for grant in grants:
        wildcard_target = any(t in ("*", "tag:*") for t in grant.targets)
        wildcard_action = any(
            a in ("*", "**") or a.endswith(".*") for a in grant.actions
        )
        if wildcard_target and wildcard_action:
            yield Finding(
                level="warn",
                code="grant-wildcard",
                subject=f"grants.{grant.id or grant.role}",
                message="This grant is wildcard in both target and action.",
                why=(
                    f"principal `{grant.principal}` may perform "
                    f"{', '.join(grant.actions)} on {', '.join(grant.targets)}. "
                    "Non-interactive operation is safe exactly to the degree the "
                    "boundary was drawn deliberately."
                ),
                fix=(
                    "narrow either side — tag selectors for targets, named actions "
                    "instead of a wildcard — and keep the deny list explicit."
                ),
            )
        if grant.principal == "*":
            yield Finding(
                level="error",
                code="grant-wildcard-principal",
                subject=f"grants.{grant.id or grant.role}",
                message="A grant has a wildcard principal.",
                why=(
                    "Evaluation is default-deny with no wildcard principal "
                    "(DESIGN §5.5)."
                ),
                fix="name the principal explicitly.",
            )


# ---------------------------------------------------------------------------


def run(context: RuleContext) -> list[Finding]:
    """Every rule, in registration order. Findings are stable across runs."""
    findings: list[Finding] = []
    for _code, func in RULES:
        findings.extend(func(context))
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(findings, key=lambda f: (order.get(f.level, 3), f.code, f.subject))


def check_catalog(catalog: Catalog) -> tuple[Finding, ...]:
    """Run catalog-level policy checks used by the write transaction.

    Artifact checks need a proposed artifact and therefore cannot be applied to
    every catalog edit.  These checks are the invariant portion of the same
    gate: topology references, broad grants, and other catalog-owned policy
    must be safe before bytes are committed.
    """
    from cadastre.core.topology import drift as topology_drift

    findings = list(topology_drift(catalog))
    # The estate-wide half of the execution rules. `check` scopes these to the
    # artifact so unrelated runs stay quiet; the write gate has no artifact and
    # wants the invariant.
    findings.extend(_public_access_findings(catalog, catalog.ci_pools))
    findings.extend(_placement_findings(catalog, catalog.ci_executors))
    for grant in catalog.policy.grants:
        wildcard_target = any(target in {"*", "tag:*"} for target in grant.targets)
        wildcard_action = any(
            action in {"*", "**"} or action.endswith(".*") for action in grant.actions
        )
        if wildcard_target and wildcard_action:
            findings.append(
                Finding(
                    "warn",
                    "grant-wildcard",
                    f"grants.{grant.id or grant.role}",
                    "This grant is wildcard in both target and action.",
                    why=(
                        "A broad grant makes non-interactive operation difficult "
                        "to contain."
                    ),
                    fix="Narrow the target or action before committing.",
                )
            )
        if grant.principal == "*":
            findings.append(
                Finding(
                    "error",
                    "grant-wildcard-principal",
                    f"grants.{grant.id or grant.role}",
                    "A grant has a wildcard principal.",
                    why="Broker evaluation is default-deny with no wildcard principal.",
                    fix="Name the principal explicitly.",
                )
            )
    return tuple(
        sorted(
            findings,
            key=lambda finding: (finding.level, finding.code, finding.subject),
        )
    )
