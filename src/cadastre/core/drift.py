"""Declared versus observed.

Report, never reconcile — in either direction (DESIGN §1.3, §2.1). Which side
is wrong is a human decision, and an auto-reconciling catalog is a control
plane that needs write credentials to everything it describes.

Three shapes of divergence, plus one that is cheap and finds real problems:

* `undeclared` — observed, absent from `declared/`.
* `missing` — declared, absent from a source that reports that kind at all.
  The qualifier matters: a source that never returns hosts must not make every
  host look missing.
* `differs` — present on both sides, disagreeing on a field the collector
  actually reported.
* `secret-only-in` — a reference present in one secret store and not another.
  It is actionable only when a replication contract covers the relationship.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from cadastre.core import model
from cadastre.core.catalog import Catalog
from cadastre.core.observed import ObservedSource
from cadastre.core.serialize import entity_to_dict
from cadastre.plugins import EntityDeclaration, PluginRegistry, matches

#: Fields whose declared value is intent rather than observable fact.
#: `notes` is prose; `tags` are the operator's taxonomy, not the world's.
_INTENT_FIELDS = frozenset({"notes", "tags"})


@dataclass(frozen=True)
class Divergence:
    category: str
    kind: str
    id: str
    source: str
    field: str | None = None
    declared: str | None = None
    observed: str | None = None
    actionable: bool = True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "category": self.category,
            "kind": self.kind,
            "id": self.id,
            "source": self.source,
        }
        if self.field:
            out["field"] = self.field
        if self.declared is not None:
            out["declared"] = self.declared
        if self.observed is not None:
            out["observed"] = self.observed
        if not self.actionable:
            out["actionable"] = False
        return out

    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.category, self.kind, self.id, self.field or "")


def known_undeclared(
    catalog: Catalog, sources: list[ObservedSource]
) -> list[dict[str, str]]:
    """Return observed entities deliberately kept out of declarations."""
    rows: list[dict[str, str]] = []
    for source in sources:
        for kind in model.KINDS:
            declared = catalog.of(kind)
            for entity in source.entities.get(kind, []):
                for rule in catalog.policy.known_undeclared:
                    if rule.source != source.source or rule.kind != kind:
                        continue
                    if entity.id in declared:
                        continue
                    if rule.ids and entity.id not in rule.ids and "*" not in rule.ids:
                        continue
                    rows.append(
                        {
                            "category": "known-undeclared",
                            "kind": kind,
                            "id": entity.id,
                            "source": source.source,
                            "reason": rule.reason,
                        }
                    )
                    break
    return sorted(rows, key=lambda row: (row["source"], row["kind"], row["id"]))


def _render(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_render(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_render(v)}" for k, v in sorted(value.items()))
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def compare(catalog: Catalog, sources: list[ObservedSource]) -> list[Divergence]:
    """Every divergence between `declared/` and the evidence."""
    out: list[Divergence] = []
    registry = PluginRegistry.discover(catalog.root)
    exempt = {
        (row["source"], row["kind"], row["id"])
        for row in known_undeclared(catalog, sources)
    }
    for source in sources:
        for kind in model.KINDS:
            if kind not in source.entities:
                continue
            observed_entities = source.entities[kind]
            declared = catalog.of(kind)
            declaration = None
            plugin = registry.get(source.plugin)
            if plugin is not None:
                declaration = plugin.info.entity(kind)
            matched: dict[str, model.Entity] = {}
            unmatched: list[model.Entity] = []
            for observed_entity in observed_entities:
                ident = observed_entity.id
                if ident not in declared and declaration is not None:
                    ident = next(
                        (
                            declared_id
                            for declared_id, candidate in declared.items()
                            if matches(observed_entity, candidate, declaration)
                        ),
                        ident,
                    )
                if ident in declared:
                    matched[ident] = observed_entity
                else:
                    unmatched.append(observed_entity)

            for observed_entity in sorted(unmatched, key=lambda entity: entity.id):
                if (source.source, kind, observed_entity.id) in exempt:
                    continue
                out.append(
                    Divergence("undeclared", kind, observed_entity.id, source.source)
                )
            for ident in sorted(matched):
                out.extend(
                    _field_differences(
                        kind, ident, declared[ident], matched[ident], source
                    )
                )
            for ident in sorted(declared):
                if ident not in matched and _covers(
                    source, declared[ident], declaration
                ):
                    out.append(Divergence("missing", kind, ident, source.source))
    out.extend(_secret_store_diff(sources, catalog.policy.replication))
    return sorted(out, key=Divergence.sort_key)


def unobservable(
    catalog: Catalog, sources: list[ObservedSource]
) -> list[dict[str, str]]:
    """Declared entities that every collector of their kind excludes.

    The blind spot coverage creates. Narrowing is what stops one Infisical
    project reporting another's secrets `missing`, but it cuts both ways: an
    entity that falls outside EVERY source's scope is never compared by
    anything, so a genuinely absent one silently stops being reported. A
    mistyped `store`, a zone nobody collects, an org nobody scans — all read
    as "no drift" rather than "nobody looked".

    Only kinds some source actually reports are considered: a kind with no
    collector at all is an unwired estate, which the empty-evidence path
    already covers, not a scoping mistake.
    """
    registry = PluginRegistry.discover(catalog.root)
    out: list[dict[str, str]] = []
    for kind in model.KINDS:
        observing = [source for source in sources if kind in source.entities]
        if not observing:
            continue
        for ident, entity in sorted(catalog.of(kind).items()):
            seen_by = [
                source
                for source in observing
                if _covers(
                    source,
                    entity,
                    (
                        plugin.info.entity(kind)
                        if (plugin := registry.get(source.plugin)) is not None
                        else None
                    ),
                )
            ]
            if not seen_by:
                out.append(
                    {
                        "kind": kind,
                        "id": ident,
                        "sources": ", ".join(
                            sorted(source.source for source in observing)
                        ),
                    }
                )
    return out


def _covers(
    source: ObservedSource,
    entity: model.Entity,
    declaration: EntityDeclaration | None,
) -> bool:
    """Whether a source claims it can authoritatively see this entity.

    An observation can make a positive statement anywhere in the estate, but
    absence is only useful evidence inside the collector's declared scope.
    Configuration is deliberately allowed to *narrow* a plugin's general
    coverage for an organisation, project, region, or secret namespace.
    """
    scopes = [
        declaration.coverage if declaration is not None else {},
        source.coverage.get(entity.kind, {}),
    ]
    data = entity_to_dict(entity)
    return all(_scope_covers(scope, data) for scope in scopes if scope)


def _scope_covers(scope: dict[str, Any], data: dict[str, Any]) -> bool:
    """Apply one coverage boundary.

    Plugin and configured-source boundaries are evaluated independently and
    conjunctively. A configured source can therefore narrow a plugin contract,
    but cannot replace or broaden it.
    """
    if not scope:
        return True
    ids = scope.get("ids")
    if not isinstance(ids, list) and ids is not None:
        return False
    if ids is not None and data.get("id") not in ids:
        return False
    where = scope.get("where") or {}
    if not isinstance(where, dict):
        return False
    return all(
        _field_matches(data.get(field), expected) for field, expected in where.items()
    )


def _field_matches(value: Any, expected: Any) -> bool:
    """Small coverage predicate: scalar equality or any-overlap for lists."""
    expected_values = expected if isinstance(expected, list) else [expected]
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return any(item in expected_values for item in values)


def _field_differences(
    kind: str,
    ident: str,
    declared: model.Entity,
    observed: model.Entity,
    source: ObservedSource,
) -> list[Divergence]:
    declared_data = entity_to_dict(declared)
    observed_data = entity_to_dict(observed)
    out = []
    for field, observed_value in sorted(observed_data.items()):
        if field in _INTENT_FIELDS or field == "id":
            continue
        declared_value = declared_data.get(field)
        if declared_value == observed_value:
            continue
        # Drift compares assertions, not silences — in both directions.
        #
        # An undeclared optional field is *not asserted*, not asserted-empty:
        # reporting it turns every field the catalog chose not to declare into
        # a finding. The same holds of the collector: a source that does not
        # report a field has not observed it to be absent. A GitOps repo names
        # a service without knowing its host, and "declared node-b, observed
        # nothing" is a statement about the collector, not about the estate.
        #
        # The model cannot distinguish "absent" from "explicitly empty" — both
        # are None or () — so silence wins on both sides. To assert a value,
        # declare it; to contradict one, observe it.
        if not declared_value or not observed_value:
            continue
        out.append(
            Divergence(
                "differs",
                kind,
                ident,
                source.source,
                field=field,
                declared=_render(declared_value),
                observed=_render(observed_value),
            )
        )
    return out


def _secret_key(ref: str) -> str:
    """The comparable part of a reference.

    Stores disagree about spelling, not about identity: Infisical holds
    `infisical://cicd/prod/GIT_AUTH_TOKEN` and the CI store holds
    `git_auth_token`, and those are one secret in two places. Comparing raw
    refs makes every name appear in exactly one store, which reports total
    divergence between stores that are actually in sync — 37 rows saying
    nothing.

    So the diff is on the terminal segment, case-folded, with the two word
    separators treated as one. What is *displayed* stays the full ref, because
    the reader needs to know which store to go and look in.
    """
    return ref.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def _secret_store_diff(
    sources: list[ObservedSource],
    contracts: tuple[model.ReplicationContract, ...] = (),
) -> list[Divergence]:
    """Names present in one store and not another. Names only — never values."""
    by_store: dict[str, set[str]] = {}
    origin: dict[str, str] = {}
    for source in sources:
        for entity in source.entities.get("secret", []):
            assert isinstance(entity, model.Secret)
            by_store.setdefault(entity.store, set()).add(entity.ref)
            origin.setdefault(entity.store, source.source)
        # A store that reports bare names rather than modelled secrets.
        for store, names in (source.extra.get("secret_names") or {}).items():
            by_store.setdefault(str(store), set()).update(str(n) for n in names)
            origin.setdefault(str(store), source.source)

    keys = {
        store: {_secret_key(ref) for ref in refs} for store, refs in by_store.items()
    }

    out = []
    stores = sorted(by_store)
    pairs = (
        [(contract.source, contract.target, contract) for contract in contracts]
        if contracts
        else [
            (stores[i], stores[j], None)
            for i in range(len(stores))
            for j in range(i + 1, len(stores))
        ]
    )
    for left, right, contract in pairs:
        if left not in keys or right not in keys:
            continue
        if contract is not None and contract.mappings:
            for expected_left, expected_right in sorted(contract.mappings.items()):
                actual_left = _matching_secret(by_store[left], expected_left)
                actual_right = _matching_secret(by_store[right], expected_right)
                if actual_left is not None and actual_right is None:
                    out.append(
                        Divergence(
                            "secret-only-in",
                            "secret",
                            actual_left,
                            origin.get(left, left),
                            field=left,
                            declared=left,
                            observed=f"absent from {right}",
                        )
                    )
                elif actual_right is not None and actual_left is None:
                    out.append(
                        Divergence(
                            "secret-only-in",
                            "secret",
                            actual_right,
                            origin.get(right, right),
                            field=right,
                            declared=right,
                            observed=f"absent from {left}",
                        )
                    )
            continue
        if contract is not None and contract.selectors:
            left_refs = _selected_secrets(by_store[left], contract.selectors)
            right_refs = _selected_secrets(by_store[right], contract.selectors)
        else:
            left_refs, right_refs = by_store[left], by_store[right]
        left_keys = {_secret_key(r) for r in left_refs}
        right_keys = {_secret_key(r) for r in right_refs}
        missing = left_keys - right_keys
        for ref in sorted(r for r in left_refs if _secret_key(r) in missing):
            out.append(
                Divergence(
                    "secret-only-in",
                    "secret",
                    ref,
                    origin.get(left, left),
                    field=left,
                    declared=left,
                    observed=f"absent from {right}",
                    actionable=contract is not None,
                )
            )
        missing = right_keys - left_keys
        for ref in sorted(r for r in right_refs if _secret_key(r) in missing):
            out.append(
                Divergence(
                    "secret-only-in",
                    "secret",
                    ref,
                    origin.get(right, right),
                    field=right,
                    declared=right,
                    observed=f"absent from {left}",
                    actionable=contract is not None,
                )
            )
    return out


def _selected_secrets(refs: set[str], selectors: tuple[str, ...]) -> set[str]:
    return {
        ref
        for ref in refs
        if any(
            fnmatch(ref, selector) or fnmatch(_secret_key(ref), selector)
            for selector in selectors
        )
    }


def _matching_secret(refs: set[str], expected: str) -> str | None:
    return next(
        (
            ref
            for ref in sorted(refs)
            if ref == expected or _secret_key(ref) == _secret_key(expected)
        ),
        None,
    )
