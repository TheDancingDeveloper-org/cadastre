"""Field declarations — the single source of truth for the model's shape.

The loader parses from this table and the JSON Schema is emitted from it, so a
plugin validating against the published schema and the loader reading
`declared/` can never disagree about what a field is.

`tests/test_spec_parity.py` asserts the dataclasses in `model.py` carry exactly
these fields.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import Any

from cadastre.core import model

# Field types the loader and the schema emitter both understand.
SCALARS = ("str", "int", "bool")


@dataclass(frozen=True)
class FieldSpec:
    """One field.

    ``key`` is the YAML key; ``attr`` is the dataclass attribute, which differs
    only where the natural name collides with a Python keyword (`class`).
    """

    key: str
    type: str  # scalar, ref, object/list, mapping, or mapping[str]
    required: bool = False
    enum: tuple[str, ...] | None = None
    ref: str | None = None  # target kind, for ref / list[ref]
    fields: tuple[FieldSpec, ...] = ()  # for obj / list[obj]
    # `builtins.type`: the `type` field above shadows the builtin here.
    cls: builtins.type | None = None  # for obj / list[obj]
    ref_attr: str | None = None  # for list[obj]: which subfield is the ref
    description: str = ""
    attr_override: str | None = None

    @property
    def attr(self) -> str:
        return self.attr_override or self.key

    @property
    def is_list(self) -> bool:
        return self.type.startswith("list[")

    @property
    def item_type(self) -> str:
        return self.type[5:-1] if self.is_list else self.type

    def empty(self) -> Any:
        return () if self.is_list else None


@dataclass(frozen=True)
class EntitySpec:
    kind: str
    cls: builtins.type[model.Entity]
    description: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)

    def by_key(self, key: str) -> FieldSpec | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None


def _common() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec("id", "str", required=True, description="Stable identifier."),
        FieldSpec(
            "tags",
            "list[str]",
            description="Selectors. Grants and placement filter on these.",
        ),
        FieldSpec("notes", "str", description="Free text. Rendered as inert data."),
        FieldSpec("extra", "mapping", description="Namespaced plugin extensions."),
    )


RESOURCES = (
    FieldSpec("cpu_cores", "int"),
    FieldSpec("memory_gb", "int"),
    FieldSpec("disk_gb", "int"),
    FieldSpec("gpu", "str", description="Model string, or absent."),
)

ACCESS = (
    FieldSpec("kind", "str", required=True, enum=("shell", "api")),
    FieldSpec("via", "str", required=True, description="Broker backend id."),
    FieldSpec("role", "str", required=True, description="What the broker mints."),
    FieldSpec("reachable_from", "list[ref]", ref="network"),
)

REMOTE = (
    FieldSpec("forge", "str", required=True, description="Forge id, not a vendor."),
    FieldSpec("url", "str", required=True),
    FieldSpec("role", "str", enum=("origin", "mirror"), description="Default origin."),
)

DEPLOYMENT = (
    FieldSpec("pipeline", "ref", ref="pipeline", required=True),
    FieldSpec(
        "authoritative",
        "bool",
        description="Which pipeline actually deploys. Not inferable; declared.",
    ),
)


ENTITY_SPECS: dict[str, EntitySpec] = {
    "host": EntitySpec(
        "host",
        model.Host,
        "Anything that can run something.",
        _common()
        + (
            FieldSpec(
                "role",
                "str",
                required=True,
                enum=(
                    "workstation",
                    "server",
                    "hypervisor",
                    "container-host",
                    "appliance",
                    "router",
                    "edge",
                ),
            ),
            FieldSpec(
                "hosted_in",
                "ref",
                ref="host",
                description="The hypervisor or container host this runs on.",
            ),
            FieldSpec("reachable_from", "list[ref]", ref="network"),
            FieldSpec("resources", "obj", fields=RESOURCES, cls=model.Resources),
            FieldSpec("access", "list[obj]", fields=ACCESS, cls=model.Access),
        ),
    ),
    "network": EntitySpec(
        "network",
        model.Network,
        "A reachability domain with a class.",
        _common()
        + (
            FieldSpec(
                "class",
                "str",
                required=True,
                enum=("private", "public", "mixed"),
                attr_override="class_",
            ),
        ),
    ),
    "service": EntitySpec(
        "service",
        model.Service,
        "A deployed unit of work.",
        _common()
        + (
            FieldSpec("runs_on", "ref", ref="host", required=True),
            FieldSpec("repo", "ref", ref="repo"),
            FieldSpec(
                "expose",
                "str",
                description="An exposure tier name from declared/policy/exposure.yaml.",
            ),
            FieldSpec(
                "deployed_by",
                "list[obj]",
                fields=DEPLOYMENT,
                cls=model.Deployment,
                ref_attr="pipeline",
            ),
            FieldSpec("consumes_secret", "list[ref]", ref="secret"),
        ),
    ),
    "endpoint": EntitySpec(
        "endpoint",
        model.Endpoint,
        "An address a service is reachable at, within some network.",
        _common()
        + (
            # Not required: an ingress collector observes an address before it
            # can know whose it is. Declared endpoints should always name their
            # service; observed ones often cannot, and the join is a human's.
            FieldSpec("service", "ref", ref="service"),
            FieldSpec("network", "ref", ref="network", required=True),
            FieldSpec("address", "str", required=True),
            FieldSpec("port", "int"),
            FieldSpec(
                "protocol", "str", enum=("http", "https", "tcp", "udp", "ssh", "grpc")
            ),
            FieldSpec(
                "host",
                "ref",
                ref="host",
                description="Host on which this endpoint is observed bound.",
            ),
            FieldSpec(
                "bind_address",
                "str",
                description="Listener bind address, distinct from the route address.",
            ),
            FieldSpec(
                "fronted_by",
                "ref",
                ref="service",
                description="The ingress service in front of this endpoint.",
            ),
        ),
    ),
    "domain": EntitySpec(
        "domain",
        model.Domain,
        "A DNS record.",
        _common()
        + (
            FieldSpec("zone", "str", required=True),
            FieldSpec("name", "str", required=True),
            FieldSpec(
                "type",
                "str",
                required=True,
                enum=("A", "AAAA", "CNAME", "TXT", "MX", "SRV", "NS"),
            ),
            FieldSpec("value", "str"),
            FieldSpec("resolves_to", "ref", ref="endpoint"),
        ),
    ),
    "secret": EntitySpec(
        "secret",
        model.Secret,
        "A reference. Never a value.",
        _common()
        + (
            FieldSpec("ref", "str", required=True, description="Opaque path or name."),
            FieldSpec("store", "str", required=True, description="Secret store id."),
            FieldSpec("last_rotated", "str", description="RFC 3339 date or datetime."),
        ),
    ),
    "pipeline": EntitySpec(
        "pipeline",
        model.Pipeline,
        "A CI definition that deploys some class of thing.",
        _common()
        + (
            FieldSpec("repo", "ref", ref="repo", required=True),
            FieldSpec("system", "str", required=True, description="CI system id."),
            FieldSpec("file", "str", description="Path within the repo."),
            FieldSpec("deploys", "list[ref]", ref="service"),
        ),
    ),
    "repo": EntitySpec(
        "repo",
        model.Repo,
        "A VCS repository, and everywhere it lives.",
        _common()
        + (
            FieldSpec(
                "remotes",
                "list[obj]",
                fields=REMOTE,
                cls=model.Remote,
                required=True,
                description="Dual-homing is the norm, not an edge case.",
            ),
            FieldSpec("mirror_from", "str", description="Forge id mirrored from."),
            FieldSpec("mirror_to", "str", description="Forge id mirrored to."),
        ),
    ),
    "ci_executor": EntitySpec(
        "ci_executor",
        model.CiExecutor,
        "A registration a CI system may schedule work onto.",
        _common()
        + (
            FieldSpec("system", "str", required=True, description="CI system id."),
            FieldSpec(
                "scope",
                "str",
                description="The routing and authorization scope it registered in.",
            ),
            FieldSpec("pool", "ref", ref="ci_pool"),
            FieldSpec(
                "status",
                "str",
                enum=("online", "offline", "unknown"),
                description="As reported upstream. Unknown stays unknown.",
            ),
            FieldSpec("busy", "bool", description="A snapshot, never spare capacity."),
            FieldSpec("ephemeral", "bool"),
            FieldSpec("os", "str"),
            FieldSpec("architecture", "str"),
            FieldSpec("version", "str"),
            FieldSpec(
                "selectors",
                "list[str]",
                description="Routing labels a pipeline may select on.",
            ),
            FieldSpec(
                "capabilities",
                "list[str]",
                description="Declared toolchains. Never inferred from OS or labels.",
            ),
            FieldSpec(
                "runs_on",
                "ref",
                ref="host",
                description="Declared placement. Never inferred from registration.",
            ),
        ),
    ),
    "ci_pool": EntitySpec(
        "ci_pool",
        model.CiPool,
        "A routing and access boundary containing executors.",
        _common()
        + (
            FieldSpec("system", "str", required=True, description="CI system id."),
            FieldSpec("scope", "str"),
            FieldSpec(
                "visibility",
                "str",
                enum=("all", "private", "selected"),
                description="Which repositories may schedule onto it.",
            ),
            FieldSpec(
                "public_repositories",
                "bool",
                description="Whether publicly visible repositories may use it.",
            ),
            FieldSpec("repositories", "list[ref]", ref="repo"),
        ),
    ),
    "deployment_topology": EntitySpec(
        "deployment_topology",
        model.DeploymentTopology,
        "A repeatable path from repository to running workload.",
        _common()
        + (
            FieldSpec("repo", "ref", ref="repo", required=True),
            FieldSpec("path_pattern", "str", required=True),
            FieldSpec("pipeline", "ref", ref="pipeline", required=True),
            FieldSpec("produces", "str", required=True),
            FieldSpec("registry", "str", required=True),
            FieldSpec("target_kind", "str", required=True),
            FieldSpec("target", "str", required=True),
            FieldSpec("node", "ref", ref="host"),
            FieldSpec(
                "artifact", "str", required=True, enum=("compose", "image", "package")
            ),
            FieldSpec("exposure", "str"),
            FieldSpec("hostname_pattern", "str"),
            FieldSpec("secret_ref_format", "str"),
        ),
    ),
}


# Policy documents are not entities — they are configuration, one file each.

EXPOSURE_TIER = (
    FieldSpec("name", "str", required=True),
    FieldSpec(
        "network_class", "str", required=True, enum=("private", "public", "mixed")
    ),
    FieldSpec("network", "str", description="Exact required reachability domain."),
    FieldSpec("requires_ingress", "bool"),
    FieldSpec("description", "str"),
)

CONVENTIONS = (
    FieldSpec("host_name", "str", description="Regex."),
    FieldSpec("service_name", "str", description="Regex."),
    FieldSpec("secret_ref", "str", description="Regex."),
    FieldSpec("endpoint_address", "str", description="Regex."),
)

GRANT = (
    FieldSpec("id", "str"),
    FieldSpec("principal", "str", required=True),
    FieldSpec("role", "str", required=True),
    FieldSpec("targets", "list[str]", description="Selectors: tag:x, host id, or *."),
    FieldSpec("actions", "list[str]"),
    FieldSpec("deny", "list[str]", description="Explicit deny beats explicit allow."),
    FieldSpec("ttl", "str"),
)

KNOWN_UNDECLARED = (
    FieldSpec("source", "str", required=True),
    FieldSpec("kind", "str", required=True),
    FieldSpec("reason", "str", required=True),
    FieldSpec("ids", "list[str]"),
)

EXECUTION_POLICY = (
    FieldSpec(
        "hosted_selectors",
        "list[str]",
        description="Selectors that route to a vendor-hosted pool.",
    ),
    FieldSpec("require_self_hosted", "bool"),
    FieldSpec("allow_public_repositories", "bool"),
    FieldSpec("excluded_host_roles", "list[str]"),
)

REPLICATION_CONTRACT = (
    FieldSpec("source", "str", required=True, description="Source secret store id."),
    FieldSpec("target", "str", required=True, description="Target secret store id."),
    FieldSpec(
        "selectors",
        "list[str]",
        description="Reference selectors expected in both stores.",
    ),
    FieldSpec(
        "mappings",
        "mapping[str]",
        description="Explicit source reference to target reference mappings.",
    ),
)
