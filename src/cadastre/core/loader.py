"""Load `declared/` into a Catalog, with precise, located errors.

Every problem found is collected rather than raised on the spot: one run
reports every bad field in the tree, not the first.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from cadastre.core import model, spec
from cadastre.core.catalog import Catalog
from cadastre.core.errors import CatalogError, CatalogIssue, Located
from cadastre.core.spec import EntitySpec, FieldSpec
from cadastre.core.yamlio import LinedDict, load_yaml
from cadastre.modules.registry import EntityRegistry, base_registry

_TYPE_NAMES = {"str": "a string", "int": "an integer", "bool": "true or false"}


class IssueCollector:
    """Accumulates located issues so one load reports every problem."""

    def __init__(self) -> None:
        self.issues: list[CatalogIssue] = []

    def add(
        self,
        where: Located,
        field: str,
        message: str,
        expected: str | None = None,
    ) -> None:
        self.issues.append(CatalogIssue(where, field, message, expected))

    def raise_if_any(self) -> None:
        if self.issues:
            raise CatalogError(sorted(self.issues, key=_issue_sort_key))


def _issue_sort_key(issue: CatalogIssue) -> tuple[str, int, str]:
    return (issue.where.path, issue.where.line or 0, issue.field)


def _describe(fs: FieldSpec) -> str:
    if fs.enum:
        return "one of: " + ", ".join(fs.enum)
    if fs.type == "ref":
        return f"the id of a {fs.ref}"
    if fs.type == "list[ref]":
        return f"a list of {fs.ref} ids"
    if fs.type == "list[str]":
        return "a list of strings"
    if fs.type == "obj":
        return "a mapping with keys: " + ", ".join(f.key for f in fs.fields)
    if fs.type == "list[obj]":
        return "a list of mappings with keys: " + ", ".join(f.key for f in fs.fields)
    if fs.type == "mapping[str]":
        return "a mapping of string keys to string values"
    return _TYPE_NAMES.get(fs.type, fs.type)


def _line(raw: Any, key: str, fallback: int | None) -> int | None:
    if isinstance(raw, LinedDict):
        return raw.line_of(key)
    return fallback


def _parse_scalar(
    fs: FieldSpec,
    value: Any,
    where: Located,
    path: str,
    issues: IssueCollector,
) -> Any:
    ok = {
        "str": lambda v: isinstance(v, str),
        # bool is an int in Python; a port of `true` is a mistake, not a 1.
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "bool": lambda v: isinstance(v, bool),
    }[fs.type]
    if not ok(value):
        issues.add(where, path, f"got {_typename(value)}", _describe(fs))
        return None
    if fs.enum and value not in fs.enum:
        issues.add(where, path, f"unknown value {value!r}", _describe(fs))
        return None
    return value


def _typename(value: Any) -> str:
    return {
        bool: "true/false",
        int: "an integer",
        str: "a string",
        list: "a list",
        dict: "a mapping",
        type(None): "nothing",
    }.get(type(value), type(value).__name__)


def _parse_object(
    fields: tuple[FieldSpec, ...],
    cls: type,
    raw: Any,
    where: Located,
    path: str,
    line: int | None,
    issues: IssueCollector,
) -> Any:
    at = Located(where.path, line)
    if not isinstance(raw, dict):
        issues.add(at, path, f"got {_typename(raw)}", "a mapping")
        return None
    known = {f.key for f in fields}
    for key in raw:
        if key not in known:
            issues.add(
                Located(where.path, _line(raw, key, line)),
                f"{path}.{key}",
                "unknown field",
                "one of: " + ", ".join(sorted(known)),
            )
    kwargs: dict[str, Any] = {}
    for fs in fields:
        sub = Located(where.path, _line(raw, fs.key, line))
        if fs.key not in raw or raw[fs.key] is None:
            if fs.required:
                issues.add(
                    at, f"{path}.{fs.key}", "missing required field", _describe(fs)
                )
            kwargs[fs.attr] = fs.empty()
            continue
        kwargs[fs.attr] = _parse_field(
            fs, raw[fs.key], sub, f"{path}.{fs.key}", sub.line, issues
        )
    try:
        result = cls(**{k: v for k, v in kwargs.items() if v is not None})
    except TypeError as exc:  # pragma: no cover - spec/dataclass drift
        issues.add(at, path, f"cannot construct {cls.__name__}: {exc}")
        return None
    # Work origins license removal of a source file, so accepting a malformed
    # record would be materially worse than omitting optional metadata. Keep
    # this validation here, while the nested LinedDict is still available, so
    # an error names origin[N].field and the field's actual YAML line.
    from cadastre.manifest.model import WorkOrigin

    if isinstance(result, WorkOrigin):
        source_path = PurePosixPath(result.path)
        if (
            not result.path
            or result.path == "."
            or source_path.is_absolute()
            or ".." in source_path.parts
        ):
            issues.add(
                Located(where.path, _line(raw, "path", line)),
                f"{path}.path",
                "must be a non-empty relative path that does not escape the workspace",
                "for example project/TODO.md",
            )
        if result.line < 1:
            issues.add(
                Located(where.path, _line(raw, "line", line)),
                f"{path}.line",
                "must be positive",
                "an integer greater than zero",
            )
        if re.fullmatch(r"[0-9a-f]{64}", result.digest) is None:
            issues.add(
                Located(where.path, _line(raw, "digest", line)),
                f"{path}.digest",
                "must be a lowercase SHA-256 digest",
                "64 lowercase hexadecimal characters",
            )
        if not result.run:
            issues.add(
                Located(where.path, _line(raw, "run", line)),
                f"{path}.run",
                "must not be empty",
                "a non-empty extraction run identifier",
            )
    return result


def _parse_field(
    fs: FieldSpec,
    value: Any,
    where: Located,
    path: str,
    line: int | None,
    issues: IssueCollector,
) -> Any:
    if fs.type in {"mapping", "mapping[str]"}:
        if not isinstance(value, dict):
            issues.add(where, path, f"got {_typename(value)}", "a mapping")
            return None
        if fs.type == "mapping[str]" and not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            issues.add(where, path, "contains a non-string key or value", _describe(fs))
            return None
        return dict(value)
    if fs.type in spec.SCALARS:
        return _parse_scalar(fs, value, where, path, issues)
    if fs.type == "ref":
        return _parse_scalar(FieldSpec(fs.key, "str"), value, where, path, issues)
    if fs.type == "obj":
        assert fs.cls is not None
        return _parse_object(fs.fields, fs.cls, value, where, path, line, issues)
    if fs.is_list:
        if not isinstance(value, list):
            issues.add(where, path, f"got {_typename(value)}", _describe(fs))
            return ()
        out = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            item_line = item.line if isinstance(item, LinedDict) else line
            if fs.item_type == "obj":
                assert fs.cls is not None
                parsed = _parse_object(
                    fs.fields, fs.cls, item, where, item_path, item_line, issues
                )
            else:
                parsed = _parse_scalar(
                    FieldSpec(fs.key, "str"), item, where, item_path, issues
                )
            if parsed is not None:
                out.append(parsed)
        return tuple(out)
    raise AssertionError(f"unhandled field type {fs.type}")  # pragma: no cover


def parse_entity(
    entity_spec: EntitySpec,
    raw: Any,
    where: Located,
    issues: IssueCollector,
    *,
    strict: bool = True,
    extensions: set[str] | None = None,
) -> model.Entity | None:
    """Parse one entity mapping. Returns None if it could not be constructed.

    `strict=False` is for observed evidence, where partial knowledge is the
    normal case rather than an error. A GitOps repo names a service before it
    can say which host runs it, exactly as an ingress collector sees an address
    before it can say whose it is — the model already made `endpoint.service`
    optional for that second reason. Requiring `service.runs_on` of a collector
    forces the choice between inventing a host and discarding the whole source.

    `id` stays required either way: evidence about an entity nobody can name is
    not evidence. Unknown fields and wrong types stay errors too, so a plugin
    still cannot invent a field the model does not have.
    """
    line = raw.line if isinstance(raw, LinedDict) else where.line
    at = Located(where.path, line)
    if not isinstance(raw, dict):
        issues.add(at, entity_spec.kind, f"got {_typename(raw)}", "a mapping")
        return None
    # ``extra`` is an internal holder for declared x-* plugin fields. It is not
    # itself a catalog/observation field and is deliberately absent from schema.
    known = {f.key for f in entity_spec.fields if f.key != "extra"}
    ident = raw.get("id") if isinstance(raw.get("id"), str) else "<no id>"
    label = f"{entity_spec.kind}[{ident}]"
    for key in raw:
        if key not in known and not (extensions and key in extensions):
            issues.add(
                Located(where.path, _line(raw, key, line)),
                f"{label}.{key}",
                "unknown field",
                "one of: " + ", ".join(sorted(known)),
            )
    kwargs: dict[str, Any] = {}
    if extensions:
        kwargs["extra"] = {key: raw[key] for key in extensions if key in raw}
    for fs in entity_spec.fields:
        if fs.key == "extra":
            continue
        sub = Located(where.path, _line(raw, fs.key, line))
        if fs.key not in raw or raw[fs.key] is None:
            if fs.required and (strict or fs.key == "id"):
                issues.add(
                    at, f"{label}.{fs.key}", "missing required field", _describe(fs)
                )
                if fs.key == "id":
                    return None
            kwargs[fs.attr] = fs.empty()
            continue
        kwargs[fs.attr] = _parse_field(
            fs, raw[fs.key], sub, f"{label}.{fs.key}", sub.line, issues
        )
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    try:
        return entity_spec.cls(**kwargs)
    except TypeError as exc:  # pragma: no cover - spec/dataclass drift
        issues.add(at, label, f"cannot construct {entity_spec.kind}: {exc}")
        return None


def declared_as_of(root: Path) -> str:
    """Return database provenance for a runtime catalog.

    A file-tree value has no trustworthy runtime edit metadata; its timestamp
    is only an explicit fixture fallback.
    """
    from cadastre.core.provenance import format_timestamp

    # A runtime SQLite catalog records its own metadata.  Legacy file trees are
    # accepted only as explicit interchange/test fixtures.
    database = root / "catalog.sqlite3"
    if database.exists():
        import sqlite3

        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='declared_as_of'"
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
        except sqlite3.Error:
            pass
    declared = root / "declared"
    times = [p.stat().st_mtime for p in declared.rglob("*") if p.is_file()]
    return format_timestamp(
        datetime.fromtimestamp(max(times) if times else 0.0, tz=UTC)
    )


def _entity_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file()
    )


def _parse_policy(root: Path, issues: IssueCollector) -> model.Policy:
    policy_dir = root / "policy"
    exposure: list[model.ExposureTier] = []
    conventions = model.Conventions()
    grants: list[model.Grant] = []
    known_undeclared: list[model.KnownUndeclared] = []
    replication: list[model.ReplicationContract] = []

    exposure_file = policy_dir / "exposure.yaml"
    if exposure_file.exists():
        rel = str(exposure_file.relative_to(root.parent))
        where = Located(rel)
        raw = load_yaml(exposure_file, rel=rel)
        items = raw.get("tiers") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            issues.add(where, "exposure", "expected tiers", "a list under `tiers:`")
        else:
            for index, item in enumerate(items):
                tier = _parse_object(
                    spec.EXPOSURE_TIER,
                    model.ExposureTier,
                    item,
                    where,
                    f"tiers[{index}]",
                    item.line if isinstance(item, LinedDict) else None,
                    issues,
                )
                if tier is not None:
                    exposure.append(tier)

    conventions_file = policy_dir / "conventions.yaml"
    if conventions_file.exists():
        rel = str(conventions_file.relative_to(root.parent))
        raw = load_yaml(conventions_file, rel=rel) or {}
        parsed = _parse_object(
            spec.CONVENTIONS,
            model.Conventions,
            raw,
            Located(rel),
            "conventions",
            raw.line if isinstance(raw, LinedDict) else None,
            issues,
        )
        if parsed is not None:
            conventions = parsed

    grants_file = policy_dir / "grants.yaml"
    if grants_file.exists():
        rel = str(grants_file.relative_to(root.parent))
        where = Located(rel)
        raw = load_yaml(grants_file, rel=rel)
        items = raw.get("grants") if isinstance(raw, dict) else raw
        if items is None:
            items = []
        if not isinstance(items, list):
            issues.add(where, "grants", "expected grants", "a list under `grants:`")
        else:
            for index, item in enumerate(items):
                grant = _parse_object(
                    spec.GRANT,
                    model.Grant,
                    item,
                    where,
                    f"grants[{index}]",
                    item.line if isinstance(item, LinedDict) else None,
                    issues,
                )
                if grant is not None:
                    grants.append(grant)

    known_file = policy_dir / "undeclared.yaml"
    if known_file.exists():
        rel = str(known_file.relative_to(root.parent))
        where = Located(rel)
        raw = load_yaml(known_file, rel=rel)
        items = raw.get("known_undeclared") if isinstance(raw, dict) else raw
        if items is None:
            items = []
        if not isinstance(items, list):
            issues.add(
                where,
                "known_undeclared",
                "expected entries",
                "a list under `known_undeclared:`",
            )
        else:
            for index, item in enumerate(items):
                entry = _parse_object(
                    spec.KNOWN_UNDECLARED,
                    model.KnownUndeclared,
                    item,
                    where,
                    f"known_undeclared[{index}]",
                    item.line if isinstance(item, LinedDict) else None,
                    issues,
                )
                if entry is not None:
                    if entry.kind not in model.KINDS:
                        issues.add(
                            where,
                            f"known_undeclared[{index}].kind",
                            f"unknown entity kind {entry.kind!r}",
                            ", ".join(model.KINDS),
                        )
                    known_undeclared.append(entry)

    replication_file = policy_dir / "replication.yaml"
    if replication_file.exists():
        rel = str(replication_file.relative_to(root.parent))
        where = Located(rel)
        raw = load_yaml(replication_file, rel=rel)
        items = raw.get("replication") if isinstance(raw, dict) else raw
        if items is None:
            items = []
        if not isinstance(items, list):
            issues.add(
                where,
                "replication",
                "expected contracts",
                "a list under `replication:`",
            )
        else:
            for index, item in enumerate(items):
                contract = _parse_object(
                    spec.REPLICATION_CONTRACT,
                    model.ReplicationContract,
                    item,
                    where,
                    f"replication[{index}]",
                    item.line if isinstance(item, LinedDict) else None,
                    issues,
                )
                if contract is not None:
                    if contract.source == contract.target:
                        issues.add(
                            where,
                            f"replication[{index}]",
                            "source and target must differ",
                            "distinct stores",
                        )
                    replication.append(contract)

    execution = model.ExecutionPolicy()
    execution_file = policy_dir / "execution.yaml"
    if execution_file.exists():
        rel = str(execution_file.relative_to(root.parent))
        raw = load_yaml(execution_file, rel=rel) or {}
        parsed_execution = _parse_object(
            spec.EXECUTION_POLICY,
            model.ExecutionPolicy,
            raw,
            Located(rel),
            "execution",
            raw.line if isinstance(raw, LinedDict) else None,
            issues,
        )
        if parsed_execution is not None:
            execution = parsed_execution

    return model.Policy(
        exposure=tuple(exposure),
        conventions=conventions,
        grants=tuple(grants),
        known_undeclared=tuple(known_undeclared),
        replication=tuple(replication),
        execution=execution,
    )


def _check_references(
    entities: dict[str, dict[str, model.Entity]],
    locations: dict[tuple[str, str], Located],
    issues: IssueCollector,
    registry: EntityRegistry | None = None,
) -> None:
    """Every ref must name an entity that exists. An unresolvable ref is the
    most common way a hand-edited catalog goes quietly wrong."""
    registry = registry or base_registry()
    for kind, entity_spec in registry.specs.items():
        for ident, entity in entities[kind].items():
            where = locations.get((kind, ident), Located(f"{kind}/{ident}"))
            for fs in entity_spec.fields:
                value = getattr(entity, fs.attr, None)
                if not value:
                    continue
                # A topology is a durable deployment claim, not a snapshot of
                # the current estate. Keep loading it when a repo, pipeline,
                # or node has been retired so `cadastre drift` can explain the
                # broken path instead of hiding it behind a load error.
                if kind == "deployment_topology" and fs.key in {
                    "repo",
                    "pipeline",
                    "node",
                }:
                    continue
                targets: list[tuple[str, str]] = []
                if fs.type == "ref":
                    targets = [(fs.ref or "", str(value))]
                elif fs.type == "list[ref]":
                    targets = [(fs.ref or "", str(v)) for v in value]
                elif fs.type == "list[obj]" and fs.ref_attr:
                    sub = next(f for f in fs.fields if f.key == fs.ref_attr)
                    targets = [
                        (sub.ref or "", str(getattr(v, sub.attr))) for v in value
                    ]
                elif fs.type == "obj" or fs.type == "list[obj]":
                    for sub in fs.fields:
                        if sub.type != "list[ref]":
                            continue
                        holders = value if fs.is_list else [value]
                        for holder in holders:
                            targets += [
                                (sub.ref or "", str(v))
                                for v in getattr(holder, sub.attr, ())
                            ]
                for target_kind, target_id in targets:
                    if target_id not in entities.get(target_kind, {}):
                        issues.add(
                            where,
                            f"{kind}[{ident}].{fs.key}",
                            f"no such {target_kind}: {target_id!r}",
                            f"one of: {_sample(entities.get(target_kind, {}))}",
                        )


def _check_manifest_invariants(
    entities: dict[str, dict[str, model.Entity]],
    locations: dict[tuple[str, str], Located],
    issues: IssueCollector,
) -> None:
    """Validate the cross-record rules owned by the Manifest register."""
    from cadastre.manifest.model import WorkItem, WorkLink

    items = entities.get("work_item", {})
    links = entities.get("work_link", {})
    seen_targets: dict[tuple[str, str, str, str], str] = {}
    for ident, link in links.items():
        if not isinstance(link, WorkLink):
            continue
        target = (link.forge, link.repo, link.kind, link.ref)
        where = locations.get(("work_link", ident), Located(f"work-link/{ident}"))
        previous = seen_targets.get(target)
        if previous is not None:
            issues.add(
                where,
                f"work_link[{ident}]",
                f"duplicate target, first linked by {previous!r}",
                "one link per forge/repository/kind/reference",
            )
        else:
            seen_targets[target] = ident
        expected = "merged" if link.kind == "pull_request" else "closed"
        if link.completion != expected:
            issues.add(
                where,
                f"work_link[{ident}].completion",
                f"incompatible with kind {link.kind!r}",
                expected,
            )
        invalid_reflect = set(link.reflect) - {"title", "completion"}
        if invalid_reflect:
            issues.add(
                where,
                f"work_link[{ident}].reflect",
                "contains unsupported reflected fields: "
                + ", ".join(sorted(invalid_reflect)),
                "a subset of: title, completion",
            )

    for ident, item in items.items():
        if not isinstance(item, WorkItem):
            continue
        where = locations.get(("work_item", ident), Located(f"work-item/{ident}"))
        try:
            timestamp = item.created_at.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            issues.add(
                where,
                f"work_item[{ident}].created_at",
                "not an RFC 3339 timestamp with an offset",
                "for example 2026-08-10T00:00:00Z",
            )
        if item.effort is not None and item.effort < 0:
            issues.add(
                where,
                f"work_item[{ident}].effort",
                "must not be negative",
                "a non-negative integer",
            )
        if ident in item.blocked_by:
            issues.add(
                where,
                f"work_item[{ident}].blocked_by",
                "cannot block itself",
                "other work item ids",
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ident: str, path: tuple[str, ...]) -> None:
        if ident in visiting:
            cycle = " -> ".join((*path, ident))
            where = locations.get(("work_item", ident), Located(f"work-item/{ident}"))
            issues.add(
                where,
                f"work_item[{ident}].blocked_by",
                f"dependency cycle: {cycle}",
                "an acyclic dependency graph",
            )
            return
        if ident in visited or ident not in items:
            return
        item = items[ident]
        if not isinstance(item, WorkItem):
            return
        visiting.add(ident)
        for dependency in item.blocked_by:
            visit(dependency, (*path, ident))
        visiting.remove(ident)
        visited.add(ident)

    for ident in sorted(items):
        visit(ident, ())


def _parse_annotations(
    declared: Path,
    entities: dict[str, dict[str, model.Entity]],
    locations: dict[tuple[str, str], Located],
    issues: IssueCollector,
    registry: EntityRegistry | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read catalog-owned annotations and overlay them on declared entities."""
    registry = registry or base_registry()
    path = declared / ".cadastre" / "annotations.yaml"
    if not path.exists():
        return {}
    rel = str(path.relative_to(declared.parent))
    raw = load_yaml(path, rel=rel) or {}
    items = raw.get("annotations") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        issues.add(Located(rel), "annotations", "expected a list", "a list of mappings")
        return {}

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(items):
        label = f"annotations[{index}]"
        where = Located(rel, item.line if isinstance(item, LinedDict) else None)
        if not isinstance(item, dict):
            issues.add(where, label, "not a mapping", "a mapping")
            continue
        kind, ident, values = item.get("kind"), item.get("id"), item.get("values")
        if kind not in registry.kinds:
            issues.add(
                where,
                f"{label}.kind",
                "unknown entity kind",
                ", ".join(registry.kinds),
            )
            continue
        if not isinstance(ident, str) or not ident:
            issues.add(where, f"{label}.id", "missing required field", "a string")
            continue
        if not isinstance(values, dict):
            issues.add(where, f"{label}.values", "not a mapping", "a mapping")
            continue
        key = (kind, ident)
        if key in result:
            issues.add(
                where,
                label,
                "duplicate annotation target",
                "one mapping per entity",
            )
            continue
        if ident not in entities[kind]:
            issues.add(
                where,
                label,
                f"orphaned annotation: no such {kind}:{ident}",
                "an entity declared in declared/",
            )
            continue
        unknown = set(values) - {"tags", "notes"}
        for field_name in sorted(unknown):
            issues.add(
                where,
                f"{label}.values.{field_name}",
                "not an annotatable field",
                "tags or notes",
            )
        clean = {
            key: value for key, value in values.items() if key in {"tags", "notes"}
        }
        base = _entity_data(entities[kind][ident], registry=registry)
        base.update(clean)
        parsed = parse_entity(registry.specs[kind], base, where, issues)
        if parsed is not None:
            entities[kind][ident] = parsed
            result[key] = {
                "kind": kind,
                "id": ident,
                "values": deepcopy(clean),
                **{
                    name: item[name]
                    for name in ("principal", "at")
                    if isinstance(item.get(name), str)
                },
            }
    return result


def _entity_data(
    entity: model.Entity, *, registry: EntityRegistry | None = None
) -> dict[str, Any]:
    from cadastre.core.serialize import entity_to_dict

    return entity_to_dict(entity, registry=registry)


def _sample(pool: dict[str, model.Entity]) -> str:
    ids = sorted(pool)
    if not ids:
        return "(none declared)"
    shown = ", ".join(ids[:8])
    return shown + (", …" if len(ids) > 8 else "")


def load_catalog(root: Path, *, registry: EntityRegistry | None = None) -> Catalog:
    """Load `<root>/declared/`. Raises CatalogError listing every problem."""
    registry = registry or base_registry()
    declared = root / "declared"
    issues = IssueCollector()
    entities: dict[str, dict[str, model.Entity]] = {k: {} for k in registry.kinds}
    locations: dict[tuple[str, str], Located] = {}

    if not declared.is_dir():
        issues.add(
            Located(str(declared)),
            "<catalog>",
            "no declared/ directory",
            "a catalog root containing declared/",
        )
        issues.raise_if_any()

    for kind, dirname in registry.dirs.items():
        entity_spec = registry.specs[kind]
        for path in _entity_files(declared / dirname):
            rel = str(path.relative_to(root))
            raw = load_yaml(path, rel=rel)
            if raw is None:
                continue
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                where = Located(rel, item.line if isinstance(item, LinedDict) else None)
                entity = parse_entity(entity_spec, item, where, issues)
                if entity is None:
                    continue
                if entity.id in entities[kind]:
                    first = locations[(kind, entity.id)]
                    issues.add(
                        where,
                        f"{kind}[{entity.id}].id",
                        f"duplicate id, first declared at {first}",
                        "an id unique within its kind",
                    )
                    continue
                entities[kind][entity.id] = entity
                locations[(kind, entity.id)] = where

    base_entities = {kind: dict(items) for kind, items in entities.items()}
    annotations = _parse_annotations(declared, entities, locations, issues, registry)
    policy = _parse_policy(declared, issues)
    _check_references(entities, locations, issues, registry)
    if "work_item" in registry.kinds:
        _check_manifest_invariants(entities, locations, issues)
    _check_policy_references(entities, locations, policy, issues)
    issues.raise_if_any()
    return Catalog(
        root=root,
        entities=entities,
        policy=policy,
        locations=locations,
        annotations=annotations,
        declared_entities=base_entities,
        registry=registry,
    )


def _check_policy_references(
    entities: dict[str, dict[str, model.Entity]],
    locations: dict[tuple[str, str], Located],
    policy: model.Policy,
    issues: IssueCollector,
) -> None:
    """A service's exposure tier must be one the operator declared. Cadastre ships
    no opinion about which tiers exist (DESIGN §2.4) — only that they are named."""
    if not policy.exposure:
        return
    names = tuple(tier.name for tier in policy.exposure)
    for ident, service in entities["service"].items():
        assert isinstance(service, model.Service)
        if service.expose and service.expose not in names:
            issues.add(
                locations.get(("service", ident), Located(f"service/{ident}")),
                f"service[{ident}].expose",
                f"unknown exposure tier {service.expose!r}",
                "one of: " + ", ".join(names),
            )
