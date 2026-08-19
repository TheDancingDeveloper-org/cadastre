"""M10 plugin declarations and identity contract.

The wire protocol carries JSON; this module turns the handshake into typed,
validated data before any catalog or drift code consumes it.  No plugin code is
executed while a declaration is being validated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from cadastre.core import model
from cadastre.core.serialize import entity_to_dict
from cadastre.core.spec import ENTITY_SPECS

FIELD_CLASSES = frozenset(("reflected", "intended", "annotated"))
CONTEST_POLICIES = frozenset(("exclude", "warn", "ignore"))
AUTHORITIES = frozenset(("source", "catalog"))


@dataclass(frozen=True)
class EntityDeclaration:
    """What one plugin owns about one core entity kind."""

    kind: str
    authority: str
    reflected: tuple[str, ...] = ()
    intended: tuple[str, ...] = ()
    annotated: tuple[str, ...] = ()
    identity: tuple[str, ...] = ("id",)
    attributes: dict[str, Any] = field(default_factory=dict)
    on_contest: dict[str, str] = field(default_factory=dict)
    empty_expected: bool = True
    #: Optional constraint on the declared records this source can authoritatively
    #: cover.  Absent means the source covers every record of this kind.
    #:
    #: ``{"ids": ["a"], "where": {"tags": ["project-a"]}}`` is the
    #: deliberately small, serialisable form.  Both constraints apply when
    #: supplied.  Source configuration may narrow this further per source.
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def field_classes(self) -> dict[str, str]:
        return {
            **{name: "reflected" for name in self.reflected},
            **{name: "intended" for name in self.intended},
            **{name: "annotated" for name in self.annotated},
        }

    def identity_key(self, record: Any) -> tuple[str, ...]:
        return identity_key(record, self.identity)


@dataclass(frozen=True)
class PluginInfo:
    """Validated result of ``plugin.info``."""

    name: str
    version: str
    capabilities: tuple[str, ...]
    entities: tuple[EntityDeclaration, ...] = ()

    def entity(self, kind: str) -> EntityDeclaration | None:
        return next((item for item in self.entities if item.kind == kind), None)


#: Kinds whose upstream id convention never matches the catalog's declared
#: id, so `("id",)` correlates nothing. Each
#: override names the reflected field(s) collectors and the catalog agree
#: on independent of naming: a secret's `ref` (the store path) rather than a
#: catalog-chosen slug, a pipeline's `(repo, system)` rather than a
#: collector-namespaced name. `repo` is deliberately absent — its stable
#: correlating field is `remotes[].url`, a set-overlap match `identity` (a
#: flat AND of scalar fields) cannot express. See `MATCH_OVERRIDES` below.
IDENTITY_OVERRIDES: dict[str, tuple[str, ...]] = {
    "secret": ("ref",),
    "pipeline": ("repo", "system"),
}

_SCP_LIKE = re.compile(r"^(?:[^@\s]+@)?([^:/\s]+):(.+)$")


def normalize_remote_url(url: str) -> str:
    """Normalize a repo remote URL to `host/org/name`, forge and scheme
    agnostic (§2d). Tolerant of ssh scp-like syntax (`git@host:org/name`),
    `ssh://` and `https://` forms, embedded userinfo, and a `.git` suffix.
    """
    raw = url.strip()
    if "://" in raw:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        path = parts.path
    else:
        match = _SCP_LIKE.match(raw)
        host, path = (match.group(1), match.group(2)) if match else ("", raw)
    host = host.lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}" if host else path


def _repo_matches(collected: Any, declared: Any) -> bool:
    """Any-overlap between normalized `remotes[].url` sets (§2d).

    A canonical-remote scalar breaks the moment a repo legitimately carries
    two remotes (an active forge migration, a mirror). Set overlap on
    normalized URLs correlates the same logical repo across forges without
    picking a winner. Falls back to `id` equality when either side declares
    no remotes at all.
    """
    collected_urls = _remote_url_set(collected)
    declared_urls = _remote_url_set(declared)
    if not collected_urls or not declared_urls:
        return identity_key(collected, ("id",)) == identity_key(declared, ("id",))
    return not collected_urls.isdisjoint(declared_urls)


def _remote_url_set(record: Any) -> set[str]:
    remotes = _record_data(record).get("remotes") or []
    return {
        normalize_remote_url(remote["url"])
        for remote in remotes
        if isinstance(remote, dict) and remote.get("url")
    }


#: Kinds whose correlation cannot be expressed as a flat AND of scalar
#: identity fields. Consulted before the generic `identity_key` comparison.
MATCH_OVERRIDES: dict[str, Callable[[Any, Any], bool]] = {
    "repo": _repo_matches,
}


def default_entity_declaration(
    kind: str,
    *,
    authority: str = "source",
    empty_expected: bool = True,
) -> EntityDeclaration:
    """Build the conservative declaration used by simple read-only plugins.

    The core-owned fields ``tags`` and ``notes`` are annotations.  All other
    fields are reflected by a source plugin.  Integrations with a more precise
    ownership model provide an explicit declaration in their handshake.
    """
    specs = ENTITY_SPECS
    if kind not in specs:
        # A Manifest-owned kind (MANIFEST.md §5.1). This module has no build
        # or activation flag of its own — work-git/work-github/work-markdown
        # are ordinary subprocess plugins whose own `plugin.info` handshake
        # must describe themselves correctly whenever they're invoked,
        # independent of whether the *catalog* they're pointed at happens to
        # have modules.yaml's manifest.enabled set.
        from cadastre.manifest.spec import ENTITY_SPECS as MANIFEST_ENTITY_SPECS

        specs = MANIFEST_ENTITY_SPECS
    if kind not in specs:
        raise ValueError(f"unknown entity kind {kind!r}")
    fields = tuple(fs.key for fs in specs[kind].fields)
    annotated = tuple(name for name in fields if name in ("tags", "notes"))
    owned = tuple(name for name in fields if name not in annotated)
    reflected = owned if authority == "source" else ()
    intended_fields = owned if authority == "catalog" else ()
    return EntityDeclaration(
        kind=kind,
        authority=authority,
        reflected=reflected,
        intended=intended_fields,
        annotated=annotated,
        identity=IDENTITY_OVERRIDES.get(kind, ("id",)),
        empty_expected=empty_expected,
        on_contest={name: "exclude" for name in reflected},
        coverage={},
        attributes={
            "type": "object",
            "additionalProperties": True,
        },
    )


#: Fields on a CI execution target that a collector must never claim.
#:
#: A registration's own metadata cannot establish which host it runs on, and a
#: label is not a toolchain — both are catalog intent, to be compared against
#: independent evidence rather than reflected from upstream. Declaring them as
#: intended is what stops a collector overwriting them and what makes drift
#: read a disagreement here as "the estate is not as declared" rather than as
#: a stale field.
CATALOG_OWNED_FIELDS: dict[str, tuple[str, ...]] = {
    "ci_executor": ("runs_on", "capabilities"),
}


#: Built-in plugin/kind pairs for which a successful *empty* result is not
#: credible evidence of an empty estate.
#:
#: `empty_expected` answers a narrow question: if this source returns zero
#: records and reports success, is "there are none" a reading worth believing?
#: For a secret store, or an organisation with no repositories, yes. For a
#: hypervisor inventory, no — a Proxmox node always reports at least itself as
#: a `node` resource, so zero hosts is evidence about the credential and never
#: about the estate.
#:
#: The failure is not hypothetical. A Proxmox API token with privilege
#: separation and no ACL is answered `200 {"data": []}` rather than `403`: you
#: may ask, you just cannot see. Declared as expected, that produced a green
#: collection followed by every declared host reported `missing` (#52).
#:
#: Deliberately not extended to `ingress-caddy` or `vpn-tailscale`, which #52
#: raises as candidates. A Caddy serving no routes is an ordinary fresh
#: install, and Tailscale answers an under-scoped key with `403` rather than an
#: empty list — neither converts a permissions fault into a confident zero the
#: way Proxmox does. This marks inventories that *cannot truthfully be empty*,
#: not every inventory we would be surprised to find empty; the difference
#: matters, because the entry costs a real empty source a false staleness
#: warning it can never clear.
EMPTINESS_NOT_CREDIBLE: dict[str, tuple[str, ...]] = {
    "hypervisor-proxmox": ("host",),
}


#: Per-(plugin, kind) JSON-Schema fragment for the `x-<namespace>.*` attribute
#: block a plugin's evidence may carry (§2.8/§4.2), keyed by plugin so two
#: collectors covering the same kind can each ship their own vendor-shaped
#: facts without colliding.
#:
#: `orchestrator-gitops` emits one `service` per stack (its altitude matches
#: the catalog's own — §2e); the compose-file service/container inventory
#: that used to be one entity per compose service now lives here instead.
ATTRIBUTE_SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    ("orchestrator-gitops", "service"): {
        "type": "object",
        "properties": {
            "x-orchestrator": {
                "type": "object",
                "properties": {
                    "compose_services": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "runs_on": {"type": "string"},
                                "expose": {"type": "string"},
                                "repo": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    # Why the stack has no `runs_on`. A GitOps repo does not
                    # know its deployment target, and an empty `runs_on` is
                    # read as agreement with whatever was declared, so the gap
                    # is stated here rather than left to silence.
                    "host_attribution": {"type": "string", "enum": ["unknown"]},
                    "host_attribution_reason": {"type": "string"},
                },
                "required": ["compose_services"],
                "additionalProperties": False,
            },
        },
        "additionalProperties": True,
    },
}


def declaration_for(
    kind: str, *, authority: str = "source", plugin: str | None = None
) -> EntityDeclaration:
    """The declaration a built-in collector makes for one kind.

    The conservative default, narrowed where the catalog owns a field outright
    and where this particular plugin cannot honestly report nothing. Shared by
    the plugin's own handshake and the in-tree registry so the two cannot
    disagree about who owns what — which is why the plugin name is a parameter
    rather than something each caller decides for itself.
    """
    base = default_entity_declaration(
        kind,
        authority=authority,
        empty_expected=kind not in EMPTINESS_NOT_CREDIBLE.get(plugin or "", ()),
    )
    attributes = ATTRIBUTE_SCHEMAS.get((plugin or "", kind))
    if attributes is not None:
        base = replace(base, attributes=attributes)
    catalog_owned = CATALOG_OWNED_FIELDS.get(kind)
    if not catalog_owned or authority != "source":
        return base
    reflected = tuple(name for name in base.reflected if name not in catalog_owned)
    return EntityDeclaration(
        kind=base.kind,
        authority=base.authority,
        reflected=reflected,
        intended=catalog_owned,
        annotated=base.annotated,
        identity=base.identity,
        attributes=base.attributes,
        on_contest={name: "exclude" for name in reflected},
        empty_expected=base.empty_expected,
        coverage=base.coverage,
    )


def _as_string_tuple(value: Any, label: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{label} must be a list of strings")
        return ()
    return tuple(value)


def _declaration(raw: Any, index: int) -> EntityDeclaration | None:
    label = f"entities[{index}]"
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in ENTITY_SPECS:
        errors.append(f"{label}.kind must be one of: {', '.join(sorted(ENTITY_SPECS))}")
        kind = str(kind or "")
    authority = raw.get("authority")
    if authority not in AUTHORITIES:
        errors.append(f"{label}.authority must be source or catalog")
        authority = str(authority or "")
    reflected = _as_string_tuple(raw.get("reflected", []), f"{label}.reflected", errors)
    intended = _as_string_tuple(raw.get("intended", []), f"{label}.intended", errors)
    annotated = _as_string_tuple(raw.get("annotated", []), f"{label}.annotated", errors)
    identity = _as_string_tuple(
        raw.get("identity", ["id"]), f"{label}.identity", errors
    )
    classes = (set(reflected), set(intended), set(annotated))
    if sum(len(items) for items in classes) != len(set().union(*classes)):
        errors.append(f"{label} field classes must not overlap")
    if kind in ENTITY_SPECS:
        known = {fs.key for fs in ENTITY_SPECS[kind].fields}
        for field_name in set().union(*classes, set(identity)):
            if field_name not in known:
                errors.append(f"{label} names unknown field {field_name!r}")
    if not identity:
        errors.append(f"{label}.identity must contain at least one field")
    attributes = raw.get("attributes", {})
    if not isinstance(attributes, dict) or attributes.get("type") not in (
        None,
        "object",
    ):
        errors.append(f"{label}.attributes must be an object JSON-Schema fragment")
        attributes = {}
    else:
        properties = attributes.get("properties", {})
        if isinstance(properties, dict):
            unnamespaced = [
                key
                for key in properties
                if isinstance(key, str) and not key.startswith("x-")
            ]
            if unnamespaced:
                errors.append(
                    f"{label}.attributes properties must use x-<plugin> names; "
                    f"found {', '.join(sorted(unnamespaced))}"
                )
    on_contest = raw.get("on_contest", {})
    if not isinstance(on_contest, dict) or not all(
        isinstance(key, str) and value in CONTEST_POLICIES
        for key, value in on_contest.items()
    ):
        errors.append(f"{label}.on_contest values must be exclude, warn, or ignore")
        on_contest = {}
    else:
        for field_name in on_contest:
            if field_name not in set().union(*classes):
                errors.append(
                    f"{label}.on_contest names undeclared field {field_name!r}"
                )
    empty_expected = raw.get("empty_expected", True)
    if not isinstance(empty_expected, bool):
        errors.append(f"{label}.empty_expected must be a boolean")
        empty_expected = True
    coverage = raw.get("coverage", {})
    if not isinstance(coverage, dict):
        errors.append(f"{label}.coverage must be an object")
        coverage = {}
    else:
        unknown = sorted(str(key) for key in coverage if key not in {"ids", "where"})
        if unknown:
            errors.append(f"{label}.coverage names unknown key {', '.join(unknown)}")
        ids = coverage.get("ids")
        where = coverage.get("where")
        if ids is not None and (
            not isinstance(ids, list) or not all(isinstance(item, str) for item in ids)
        ):
            errors.append(f"{label}.coverage.ids must be a list of strings")
        if where is not None and not isinstance(where, dict):
            errors.append(f"{label}.coverage.where must be an object")
        elif isinstance(where, dict) and kind in ENTITY_SPECS:
            known = {fs.key for fs in ENTITY_SPECS[kind].fields}
            unknown = sorted(str(key) for key in where if key not in known)
            if unknown:
                errors.append(
                    f"{label}.coverage.where names unknown field {', '.join(unknown)}"
                )
    if errors:
        raise ValueError("; ".join(errors))
    return EntityDeclaration(
        kind=kind,
        authority=authority,
        reflected=reflected,
        intended=intended,
        annotated=annotated,
        identity=identity,
        attributes=dict(attributes),
        on_contest={str(key): str(value) for key, value in on_contest.items()},
        empty_expected=empty_expected,
        coverage=dict(coverage),
    )


def parse_plugin_info(raw: dict[str, Any]) -> PluginInfo:
    """Parse and validate the result object from ``plugin.info``."""
    if not isinstance(raw, dict):
        raise ValueError("plugin.info result must be an object")
    name = raw.get("name")
    version = raw.get("version")
    capabilities = raw.get("capabilities")
    entities = raw.get("entities")
    errors: list[str] = []
    if not isinstance(name, str) or not name:
        errors.append("name must be a non-empty string")
        name = ""
    if not isinstance(version, str) or not version:
        errors.append("version must be a non-empty string")
        version = ""
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        errors.append("capabilities must be a list of strings")
        capabilities = []
    if not isinstance(entities, list):
        errors.append("entities must be a list")
        entities = []
    declarations: list[EntityDeclaration] = []
    for index, item in enumerate(entities):
        try:
            declaration = _declaration(item, index)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if declaration is not None:
            declarations.append(declaration)
    if len({item.kind for item in declarations}) != len(declarations):
        errors.append("entities must contain each kind at most once")
    if errors:
        raise ValueError("; ".join(errors))
    return PluginInfo(str(name), str(version), tuple(capabilities), tuple(declarations))


def validate_plugin_info(info: PluginInfo) -> None:
    """Validate an in-process plugin's already-constructed metadata."""
    parse_plugin_info(
        {
            "name": info.name,
            "version": info.version,
            "capabilities": list(info.capabilities),
            "entities": [
                {
                    "kind": item.kind,
                    "authority": item.authority,
                    "reflected": list(item.reflected),
                    "intended": list(item.intended),
                    "annotated": list(item.annotated),
                    "identity": list(item.identity),
                    "attributes": item.attributes,
                    "on_contest": item.on_contest,
                    "empty_expected": item.empty_expected,
                    "coverage": item.coverage,
                }
                for item in info.entities
            ],
        }
    )


def _record_data(record: Any) -> dict[str, Any]:
    if isinstance(record, model.Entity):
        return entity_to_dict(record)
    if isinstance(record, dict):
        return record
    raise TypeError("identity records must be entities or mappings")


def identity_key(record: Any, fields: tuple[str, ...] = ("id",)) -> tuple[str, ...]:
    """Return a deterministic identity key, preserving missing values."""
    data = _record_data(record)
    return tuple(
        json.dumps(data.get(name), sort_keys=True, separators=(",", ":"))
        for name in fields
    )


def matches(collected: Any, declared: Any, declaration: EntityDeclaration) -> bool:
    """Whether two records refer to the same upstream entity."""
    override = MATCH_OVERRIDES.get(declaration.kind)
    if override is not None:
        return override(collected, declared)
    return declaration.identity_key(collected) == declaration.identity_key(declared)
