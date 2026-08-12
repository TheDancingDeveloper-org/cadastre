"""The loaded catalog, and traversal over it.

A plain in-memory value: no database, no daemon, no server (DESIGN §7). The CLI
is a process that starts, answers, and exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

from cadastre.core import model
from cadastre.core.errors import Located
from cadastre.modules.registry import EntityRegistry, base_registry

E = TypeVar("E", bound=model.Entity)


@dataclass(frozen=True)
class Neighbor:
    """One end of a relation, with the relation that connects it."""

    relation: str
    direction: str  # "out" — this entity points at it; "in" — it points here
    kind: str
    id: str


@dataclass(frozen=True)
class Catalog:
    """Everything `declared/` says, plus where each statement was made."""

    root: Path
    entities: dict[str, dict[str, model.Entity]]
    policy: model.Policy = field(default_factory=model.Policy)
    locations: dict[tuple[str, str], Located] = field(default_factory=dict)
    #: Catalog-owned annotations remain keyed separately from reflected entity
    #: data, so a missing target can be reported as an orphan.
    annotations: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: The entity tree before annotations are overlaid. Writes use this to
    #: avoid serialising the same annotation into both stores.
    declared_entities: dict[str, dict[str, model.Entity]] | None = None
    registry: EntityRegistry = field(default_factory=base_registry)

    # -- accessors ---------------------------------------------------------

    def of(self, kind: str) -> dict[str, model.Entity]:
        return self.entities.get(kind, {})

    def all(self, kind: str) -> list[model.Entity]:
        """Every entity of a kind, ordered by id. Ordering is part of the
        determinism contract, so it belongs here rather than at each call."""
        return [self.entities[kind][i] for i in sorted(self.entities.get(kind, {}))]

    def base_all(self, kind: str) -> list[model.Entity]:
        source = self.declared_entities or self.entities
        return [source[kind][i] for i in sorted(source.get(kind, {}))]

    def base_get(self, kind: str, ident: str) -> model.Entity | None:
        source = self.declared_entities or self.entities
        return source.get(kind, {}).get(ident)

    def get(self, kind: str, ident: str) -> model.Entity | None:
        return self.entities.get(kind, {}).get(ident)

    def typed(self, kind: str, cls: type[E]) -> list[E]:
        return cast(list[E], self.all(kind))

    @property
    def hosts(self) -> list[model.Host]:
        return self.typed("host", model.Host)

    @property
    def networks(self) -> list[model.Network]:
        return self.typed("network", model.Network)

    @property
    def services(self) -> list[model.Service]:
        return self.typed("service", model.Service)

    @property
    def endpoints(self) -> list[model.Endpoint]:
        return self.typed("endpoint", model.Endpoint)

    @property
    def domains(self) -> list[model.Domain]:
        return self.typed("domain", model.Domain)

    @property
    def secrets(self) -> list[model.Secret]:
        return self.typed("secret", model.Secret)

    @property
    def pipelines(self) -> list[model.Pipeline]:
        return self.typed("pipeline", model.Pipeline)

    @property
    def repos(self) -> list[model.Repo]:
        return self.typed("repo", model.Repo)

    @property
    def ci_executors(self) -> list[model.CiExecutor]:
        return self.typed("ci_executor", model.CiExecutor)

    @property
    def ci_pools(self) -> list[model.CiPool]:
        return self.typed("ci_pool", model.CiPool)

    @property
    def deployment_topologies(self) -> list[model.DeploymentTopology]:
        return self.typed("deployment_topology", model.DeploymentTopology)

    def find(self, ident: str) -> list[tuple[str, model.Entity]]:
        """Every entity with this id, across kinds. Ids are unique per kind, so
        a bare id can be ambiguous; `lookup` reports that rather than guessing."""
        return [
            (kind, self.entities[kind][ident])
            for kind in self.registry.kinds
            if ident in self.entities.get(kind, {})
        ]

    def location(self, kind: str, ident: str) -> Located | None:
        return self.locations.get((kind, ident))

    # -- relations ---------------------------------------------------------

    def neighbors(self, kind: str, ident: str) -> list[Neighbor]:
        """Both directions of every relation touching this entity."""
        out: list[Neighbor] = []
        entity = self.get(kind, ident)
        if entity is None:
            return out
        for relation, from_kind, attr, to_kind in self.registry.relations:
            if from_kind == kind:
                for target in self._targets(entity, attr):
                    out.append(Neighbor(relation, "out", to_kind, target))
            if to_kind == kind:
                for other in self.all(from_kind):
                    if ident in self._targets(other, attr):
                        out.append(Neighbor(relation, "in", from_kind, other.id))
        return sorted(out, key=lambda n: (n.relation, n.direction, n.kind, n.id))

    @staticmethod
    def _targets(entity: model.Entity, attr: str) -> list[str]:
        value: Any = getattr(entity, attr, None)
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, model.Deployment):
                out.append(item.pipeline)
        return out

    # -- derived views used by more than one command -----------------------

    def host_networks(self, host_id: str) -> list[model.Network]:
        host = self.get("host", host_id)
        if not isinstance(host, model.Host):
            return []
        found = [self.get("network", n) for n in host.reachable_from]
        return [n for n in found if isinstance(n, model.Network)]

    def host_network_classes(self, host_id: str) -> set[str]:
        return {n.class_ for n in self.host_networks(host_id)}

    def services_on(self, host_id: str) -> list[model.Service]:
        return [s for s in self.services if s.runs_on == host_id]

    def endpoints_of(self, service_id: str) -> list[model.Endpoint]:
        return [e for e in self.endpoints if e.service == service_id]

    def ports_on_host(self, host_id: str) -> dict[int, list[str]]:
        """port -> service ids bound to it on this host. The basis of the port
        collision rule in `check` and the conflict list in `context-for`."""
        taken: dict[int, list[str]] = {}
        on_host = {s.id for s in self.services_on(host_id)}
        for endpoint in self.endpoints:
            if endpoint.service in on_host and endpoint.port is not None:
                taken.setdefault(endpoint.port, []).append(endpoint.service)
        return {port: sorted(set(v)) for port, v in sorted(taken.items())}

    def hostnames(self) -> dict[str, list[str]]:
        """hostname -> what claims it, across endpoints and DNS records."""
        claims: dict[str, list[str]] = {}
        for endpoint in self.endpoints:
            if endpoint.address:
                claims.setdefault(endpoint.address, []).append(
                    f"endpoint {endpoint.id}"
                )
        for domain in self.domains:
            claims.setdefault(domain.name, []).append(f"domain {domain.id}")
        return {name: sorted(v) for name, v in sorted(claims.items())}

    def secret_refs(self) -> dict[str, model.Secret]:
        return {s.ref: s for s in self.secrets}

    def pipelines_for(self, service_id: str) -> list[model.Pipeline]:
        service = self.get("service", service_id)
        declared = (
            {d.pipeline for d in service.deployed_by}
            if isinstance(service, model.Service)
            else set()
        )
        by_deploys = {p.id for p in self.pipelines if service_id in p.deploys}
        wanted = declared | by_deploys
        return [p for p in self.pipelines if p.id in wanted]

    def authoritative_pipeline(self, service_id: str) -> str | None:
        service = self.get("service", service_id)
        if not isinstance(service, model.Service):
            return None
        authoritative = [d.pipeline for d in service.deployed_by if d.authoritative]
        if len(authoritative) == 1:
            return authoritative[0]
        if not authoritative and len(service.deployed_by) == 1:
            return service.deployed_by[0].pipeline
        return None

    def eligible_executors(
        self,
        *,
        system: str | None = None,
        labels: tuple[str, ...] = (),
        pool: str | None = None,
    ) -> list[model.CiExecutor]:
        """Executors a job with this selector could be scheduled onto.

        Label matching is conjunctive, as every CI system that has labels
        defines it: a job asking for `[self-hosted, linux, gpu]` needs one
        executor carrying all three, not three executors carrying one each.

        This answers eligibility, never availability. An eligible executor may
        be offline, busy, or stale; those are separate findings, because "no
        executor could ever run this" and "none is up right now" call for
        different responses.
        """
        wanted = set(labels)
        found = []
        for executor in self.ci_executors:
            if system and executor.system != system:
                continue
            if pool is not None and executor.pool != pool:
                continue
            if not wanted <= set(executor.selectors):
                continue
            found.append(executor)
        return found

    def counts(self) -> dict[str, int]:
        return {kind: len(self.entities.get(kind, {})) for kind in self.registry.kinds}
