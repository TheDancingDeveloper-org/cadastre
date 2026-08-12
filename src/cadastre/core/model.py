"""The entity model.

The model is the product; everything downstream is shaped by it (DESIGN §2.3).

Two rules govern edits here:

* **No vendor nouns.** `network.class_`, never `tailnet`. Vendor specificity
  lives in plugins (DESIGN §2.4).
* **A field needs a question.** Adding one requires a real placement, drift, or
  check question that cannot be answered without it. Bias hard to refusing.

Field *declarations* live in `spec.py`, which is the single source of truth for
parsing and for the emitted JSON Schema. The dataclasses here mirror them, and
`tests/test_spec_parity.py` fails if the two drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Nested value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Resources:
    """What a host can offer. Only what placement actually filters on."""

    cpu_cores: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    gpu: str | None = None


@dataclass(frozen=True)
class Access:
    """A capability descriptor: that a path exists, and its shape.

    Never a credential (DESIGN §5.3). Safe in a public repository.
    """

    kind: str
    via: str
    role: str
    reachable_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class Remote:
    """One of a repo's remotes. A repository may have origin and mirror remotes."""

    forge: str
    url: str
    role: str = "origin"


@dataclass(frozen=True)
class Deployment:
    """A pipeline that deploys a service, and whether it is the authoritative one.

    A service may resolve to more than one pipeline. Which one actually deploys
    is not inferable, so it is declared.
    """

    pipeline: str
    authoritative: bool = False


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    """Common shape. Every entity is identified and taggable."""

    id: str
    tags: tuple[str, ...] = ()
    notes: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return KIND_BY_CLASS[type(self)]


@dataclass(frozen=True)
class Host(Entity):
    """Anything that can run something."""

    role: str = "server"
    hosted_in: str | None = None
    reachable_from: tuple[str, ...] = ()
    resources: Resources | None = None
    access: tuple[Access, ...] = ()


@dataclass(frozen=True)
class Network(Entity):
    """A reachability domain with a class."""

    class_: str = "private"


@dataclass(frozen=True)
class Service(Entity):
    """A deployed unit of work."""

    runs_on: str = ""
    repo: str | None = None
    expose: str | None = None
    deployed_by: tuple[Deployment, ...] = ()
    consumes_secret: tuple[str, ...] = ()


@dataclass(frozen=True)
class Endpoint(Entity):
    """An address a service is reachable at, within some network."""

    service: str = ""
    network: str = ""
    address: str = ""
    port: int | None = None
    protocol: str = "https"
    # The DNS/route address is not necessarily the address a process binds.
    # Collectors use these fields for a host-level listener observation,
    # including workloads which have not yet been declared as services.
    host: str | None = None
    bind_address: str | None = None
    fronted_by: str | None = None


@dataclass(frozen=True)
class Domain(Entity):
    """A DNS record."""

    zone: str = ""
    name: str = ""
    type: str = "A"
    value: str | None = None
    resolves_to: str | None = None


@dataclass(frozen=True)
class Secret(Entity):
    """A reference. Never a value (DESIGN §1.3)."""

    ref: str = ""
    store: str = ""
    last_rotated: str | None = None


@dataclass(frozen=True)
class Pipeline(Entity):
    """A CI definition that deploys some class of thing."""

    repo: str = ""
    system: str = ""
    file: str | None = None
    deploys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Repo(Entity):
    """A VCS repository, and everywhere it lives."""

    remotes: tuple[Remote, ...] = ()
    mirror_from: str | None = None
    mirror_to: str | None = None


@dataclass(frozen=True)
class CiExecutor(Entity):
    """One registration a CI system may schedule work onto.

    Vendor-neutral on purpose. GitHub calls this a self-hosted runner, other
    systems call it an agent or a worker; `github_runner` is not a core noun
    (DESIGN §2.4). Every field here exists because a check rule asks about it.

    `runs_on` is the one field that must never be inferred. A registration's
    name, OS, custom labels, IP, or successful job history do not establish
    which physical or virtual host it runs on — that is declared intent, to be
    compared against independent host-side evidence.
    """

    system: str = ""
    scope: str = ""
    pool: str | None = None
    status: str = "unknown"
    busy: bool = False
    ephemeral: bool = False
    os: str | None = None
    architecture: str | None = None
    version: str | None = None
    selectors: tuple[str, ...] = ()
    #: Toolchains this executor is declared to provide. Explicit intent, never
    #: read off the OS, a label, or the fact that a job once succeeded.
    capabilities: tuple[str, ...] = ()
    runs_on: str | None = None


@dataclass(frozen=True)
class CiPool(Entity):
    """A routing and access boundary that executors belong to.

    It exists separately from the executor because access policy belongs to the
    boundary: whether public repositories may schedule onto it is a property of
    the pool, and a pool with no executors is still a policy object worth
    seeing.
    """

    system: str = ""
    scope: str = ""
    visibility: str = "private"
    public_repositories: bool = False
    repositories: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeploymentTopology(Entity):
    """A catalog-owned repeatable path from repository to workload."""

    repo: str = ""
    path_pattern: str = ""
    pipeline: str = ""
    produces: str = ""
    registry: str = ""
    target_kind: str = ""
    target: str = ""
    node: str | None = None
    artifact: str = "compose"
    exposure: str | None = None
    hostname_pattern: str | None = None
    secret_ref_format: str | None = None


# --------------------------------------------------------------------------
# Policy — user-defined, Cadastre ships no opinion about the tiers (DESIGN §2.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExposureTier:
    name: str
    network_class: str
    network: str | None = None
    requires_ingress: bool = False
    description: str | None = None


@dataclass(frozen=True)
class Conventions:
    """Naming and format rules. Regexes, applied by `check` and reported by
    `context-for` so the agent gets them before it writes rather than after."""

    host_name: str | None = None
    service_name: str | None = None
    secret_ref: str | None = None
    endpoint_address: str | None = None


@dataclass(frozen=True)
class Grant:
    """A pre-authorised scope, approved by explicit catalog policy (DESIGN §5.5).

    Cadastre reads these so that `check` can warn on a grant that is wildcard
    in both target and action.
    """

    principal: str
    role: str
    targets: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    ttl: str | None = None
    id: str | None = None


@dataclass(frozen=True)
class KnownUndeclared:
    """A deliberate review-queue exemption for observed entities."""

    source: str
    kind: str
    reason: str
    ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplicationContract:
    """Policy describing which secret stores are expected to share names."""

    source: str
    target: str
    selectors: tuple[str, ...] = ()
    mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionPolicy:
    """What the estate has decided about where CI work may run.

    Cadastre ships no opinion here either. Which selectors name a vendor-hosted
    pool is an estate fact — `ubuntu-latest` means something to GitHub and
    nothing to Woodpecker — so it is declared rather than built in.
    """

    #: Selectors that route to a vendor-hosted pool rather than to a declared
    #: executor. Named explicitly: core cannot tell a hosted label from a custom
    #: one by looking at it.
    hosted_selectors: tuple[str, ...] = ()
    require_self_hosted: bool = False
    allow_public_repositories: bool = False
    #: Host roles an executor may not be placed on. Empty means the placement
    #: exclusions apply unchanged.
    excluded_host_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    exposure: tuple[ExposureTier, ...] = ()
    conventions: Conventions = field(default_factory=Conventions)
    grants: tuple[Grant, ...] = ()
    known_undeclared: tuple[KnownUndeclared, ...] = ()
    replication: tuple[ReplicationContract, ...] = ()
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)

    def tier(self, name: str) -> ExposureTier | None:
        for tier in self.exposure:
            if tier.name == name:
                return tier
        return None


# --------------------------------------------------------------------------
# Kind registry
# --------------------------------------------------------------------------

ENTITY_CLASSES: dict[str, type[Entity]] = {
    "host": Host,
    "network": Network,
    "service": Service,
    "endpoint": Endpoint,
    "domain": Domain,
    "secret": Secret,
    "pipeline": Pipeline,
    "repo": Repo,
    "ci_executor": CiExecutor,
    "ci_pool": CiPool,
    "deployment_topology": DeploymentTopology,
}

KIND_BY_CLASS: dict[type, str] = {cls: kind for kind, cls in ENTITY_CLASSES.items()}

#: Directory under ``declared/`` (and file stem under ``observed/``) per kind.
KIND_DIRS: dict[str, str] = {
    "host": "hosts",
    "network": "networks",
    "service": "services",
    "endpoint": "endpoints",
    "domain": "domains",
    "secret": "secrets",
    "pipeline": "pipelines",
    "repo": "repos",
    "ci_executor": "ci-executors",
    "ci_pool": "ci-pools",
    "deployment_topology": "topologies",
}

KINDS: tuple[str, ...] = tuple(ENTITY_CLASSES)

#: Relations, as (name, from-kind, field, to-kind). DESIGN §2.3 lists these by
#: name; here they are the actual fields that carry them, so relation traversal
#: and reference validation read from one table.
RELATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("runs_on", "service", "runs_on", "host"),
    ("hosted_in", "host", "hosted_in", "host"),
    ("reachable_from", "host", "reachable_from", "network"),
    ("reachable_from", "endpoint", "network", "network"),
    ("fronted_by", "endpoint", "fronted_by", "service"),
    ("resolves_to", "domain", "resolves_to", "endpoint"),
    ("consumes_secret", "service", "consumes_secret", "secret"),
    ("deployed_by", "service", "deployed_by", "pipeline"),
    # Declared intent, never inferred from a registration's own metadata.
    ("runs_on", "ci_executor", "runs_on", "host"),
    ("pool", "ci_executor", "pool", "ci_pool"),
    ("repositories", "ci_pool", "repositories", "repo"),
)
