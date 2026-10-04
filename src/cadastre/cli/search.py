"""`cadastre search` and `credential-for` — find things without knowing their id.

`lookup` addresses one entity by id. Agents mostly do not have an id: they
have "the komodo api key" or "whatever deploys to komodo". Every one of 27
logged `lookup` failures was a caller passing a phrase where an id was
required, and the fallback was a manual login to the secret store — exactly
the bypass this catalog exists to make unnecessary.

Both commands rank declared *and* observed entities by token overlap and
return candidates, never a single confident pick: a fuzzy match can be wrong,
so the caller sees the ranking, the project, and where each candidate came
from. Secret hits carry the facts a consumer needs to fetch the secret through
its own credential path — store, project id, environment, path, server, and a
ready agent-auth manifest line — and never a value (DESIGN §1.3).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import UnknownKindError, UsageError
from cadastre.core.observed import ObservedSource
from cadastre.core.serialize import entity_to_dict
from cadastre.render.document import Document, Para, Section, Table
from cadastre.render.inert import inert

DEFAULT_LIMIT = 10
MAX_LIMIT = 50

#: The agent-auth manifest's flag vocabulary, quoted so a caller can finish
#: the line without looking it up.
MANIFEST_FLAGS = ("optional", "ondemand", "writable")


def normalize(text: str) -> str:
    folded = "".join(ch if ch.isalnum() else "-" for ch in text.lower())
    return "-".join(part for part in folded.split("-") if part)


def tokens(text: str) -> list[str]:
    return [part for part in normalize(text).split("-") if part]


# --------------------------------------------------------------------------
# Walking declared and observed together
# --------------------------------------------------------------------------


@dataclass
class Hit:
    """One entity — or, for a secret, one reference seen by several sources."""

    kind: str
    id: str
    entity: model.Entity
    declared: bool
    sources: list[ObservedSource] = field(default_factory=list)
    observed: list[model.Entity] = field(default_factory=list)
    declared_entity: model.Entity | None = None


def _haystack(entity: model.Entity) -> list[str]:
    values = [entity.id]
    for attr in ("ref", "name", "address"):
        value = getattr(entity, attr, None)
        if isinstance(value, str) and value:
            values.append(value)
            if attr == "ref":
                values.append(value.rstrip("/").split("/")[-1])
    return values


def all_hits(session: Session, *, kind: str | None = None) -> list[Hit]:
    """Declared and observed entities, with secrets merged by reference.

    A declared secret and the collector's observation of the same reference
    are one thing to a caller looking for a credential; listing them twice
    reads as two candidates and splits the evidence.
    """
    hits: dict[tuple[str, str], Hit] = {}
    kinds = [k for k in session.registry.kinds if kind is None or k == kind]
    for entity_kind in kinds:
        for entity in session.catalog.all(entity_kind):
            key = (entity_kind, _merge_key(entity_kind, entity))
            hits[key] = Hit(
                entity_kind, entity.id, entity, True, declared_entity=entity
            )
    for source in session.observed:
        for entity_kind in kinds:
            for entity in source.entities.get(entity_kind, []):
                key = (entity_kind, _merge_key(entity_kind, entity))
                hit = hits.get(key)
                if hit is None:
                    hit = Hit(entity_kind, entity.id, entity, False)
                    hits[key] = hit
                hit.sources.append(source)
                hit.observed.append(entity)
    return list(hits.values())


def _merge_key(kind: str, entity: model.Entity) -> str:
    ref = getattr(entity, "ref", None)
    if kind == "secret" and isinstance(ref, str) and ref:
        return "ref:" + ref
    return "id:" + entity.id


def _hit_strings(hit: Hit) -> list[str]:
    values: list[str] = []
    for entity in [hit.entity, *hit.observed]:
        values.extend(_haystack(entity))
    return values


# --------------------------------------------------------------------------
# Secret location: what a consumer needs to fetch it
# --------------------------------------------------------------------------


def _store_block(hit: Hit) -> dict[str, Any]:
    for entity in hit.observed:
        block = entity.extra.get("x-secret-store")
        if isinstance(block, dict):
            return block
    return {}


def _source_config(session: Session, store: str) -> dict[str, Any]:
    """The configured source for a store, when this process can see it."""
    for source in session.plugins.sources:
        if str(source.config.get("store") or "") == store:
            return source.config
    return {}


def _ref_parts(ref: str) -> dict[str, str]:
    """`scheme://project/env/[path/]KEY` -> its parts, when it has that shape."""
    if "://" not in ref:
        return {}
    _, rest = ref.split("://", 1)
    parts = [part for part in rest.split("/") if part]
    if len(parts) < 3:
        return {"key": parts[-1]} if parts else {}
    return {
        "project": parts[0],
        "environment": parts[1],
        "path": "/" + "/".join(parts[2:-1]),
        "key": parts[-1],
    }


def secret_location(session: Session, hit: Hit) -> dict[str, Any]:
    """Store, project, environment, path, key, server and a manifest line.

    Read from the collector's `x-secret-store` block first (it saw the store),
    then from the source's own configuration, then from the reference's shape.
    Each fact says nothing about the value.
    """
    entity = hit.declared_entity or hit.entity
    ref = str(getattr(entity, "ref", "") or "")
    store = str(getattr(entity, "store", "") or "")
    block = _store_block(hit)
    config = _source_config(session, store)
    parts = _ref_parts(ref)
    key = block.get("key") or parts.get("key") or entity.id
    project_id = block.get("project_id") or config.get("workspace_id")
    location: dict[str, Any] = {
        "ref": ref or None,
        "store": store or None,
        "key": key,
        "project_id": project_id,
        "project_slug": block.get("project_slug")
        or config.get("project_slug")
        or parts.get("project"),
        "environment": block.get("environment")
        or config.get("environment")
        or parts.get("environment"),
        "path": block.get("path") or config.get("path") or parts.get("path") or "/",
        "server": block.get("server") or config.get("endpoint"),
    }
    if project_id:
        location["manifest_line"] = f"{key} {project_id} {key}"
        location["manifest_note"] = (
            "Agent-auth manifest format `VAR PROJECT_ID SECRET_NAME [flags]`; "
            "VAR defaults to the secret name — rename it to what the consumer "
            f"reads. Flags: comma-separated from {', '.join(MANIFEST_FLAGS)}. "
            "The line carries no environment or path: this secret is in "
            f"environment {location['environment']!r}, path {location['path']!r}."
        )
    else:
        location["manifest_line"] = None
        location["manifest_note"] = (
            "Project id unknown: no collector reported this store's project "
            "and no source configuration for it is visible here."
            if store
            else "Not a store-held secret."
        )
    return location


def _hit_record(session: Session, hit: Hit, score: float, match: str) -> dict[str, Any]:
    entity = hit.declared_entity or hit.entity
    record: dict[str, Any] = {
        "kind": hit.kind,
        "id": hit.id,
        "declared": hit.declared,
        "observed_by": sorted({source.source for source in hit.sources}),
        "score": round(score, 3),
        "match": match,
        "entity": _public_entity(session, entity),
    }
    if hit.kind == "secret":
        record["secret"] = secret_location(session, hit)
    return record


def _public_entity(session: Session, entity: model.Entity) -> dict[str, Any]:
    data = entity_to_dict(entity, registry=session.registry)
    if "notes" in data:
        data["notes"] = inert(data["notes"])
    return data


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score(query_tokens: list[str], strings: list[str]) -> tuple[float, int]:
    """(fraction of query tokens matched, haystack size). Whole-token matches
    count double a substring match, so `api` prefers `..._API_KEY` over
    `rapid`."""
    hay_tokens: set[str] = set()
    joined = []
    for value in strings:
        hay_tokens.update(tokens(value))
        joined.append(normalize(value))
    text = " ".join(joined)
    total = 0
    for token in query_tokens:
        if token in hay_tokens:
            total += 2
        elif token in text:
            total += 1
    return total / (2 * len(query_tokens)), len(hay_tokens)


def _matched_all(query_tokens: list[str], strings: list[str]) -> bool:
    text = " ".join(normalize(value) for value in strings)
    return all(token in text for token in query_tokens)


def _limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise UsageError("limit must be a positive integer")
    return min(limit, MAX_LIMIT)


def search(
    session: Session,
    query: str,
    *,
    kind: str | None = None,
    limit: int | None = None,
) -> Document:
    """Free-text search over entity ids, names, addresses and secret refs."""
    if kind is not None and kind not in session.registry.kinds:
        raise UnknownKindError(
            f"unknown entity kind {kind!r}; expected one of: "
            + ", ".join(sorted(session.registry.kinds))
        )
    wanted = [token for token in tokens(query or "") if len(token) >= 2]
    if not wanted:
        raise UsageError(
            "search needs a query of at least one word of two or more "
            "characters, e.g. 'komodo api key'"
        )
    cap = _limit(limit)
    ranked: list[tuple[float, int, Hit, str]] = []
    for hit in all_hits(session, kind=kind):
        strings = _hit_strings(hit)
        fraction, size = score(wanted, strings)
        if fraction <= 0:
            continue
        match = "all" if _matched_all(wanted, strings) else "partial"
        ranked.append((fraction, size, hit, match))
    full = [item for item in ranked if item[3] == "all"]
    # Partial matches are a fallback, not padding: when every query word
    # matched somewhere, a candidate that matched half of them is noise.
    pool = full or [item for item in ranked if item[0] >= 0.5]
    pool.sort(key=lambda item: (-item[0], item[1], not item[2].declared, item[2].id))
    shown = pool[:cap]
    records = [_hit_record(session, hit, frac, match) for frac, _, hit, match in shown]
    rows = tuple(
        (
            record["kind"],
            record["id"],
            "yes" if record["declared"] else "no",
            ", ".join(record["observed_by"]) or "-",
            record["match"],
            (record.get("secret") or {}).get("manifest_line") or "",
        )
        for record in records
    )
    note = (
        "Ranked candidates, not an identification: confirm the project and "
        "environment before using one. Secret values are never returned."
    )
    blurb = (
        f"{len(pool)} candidate(s) for {query!r}"
        + (f"; showing {len(shown)}." if len(pool) > len(shown) else ".")
        if pool
        else f"Nothing declared or observed matches {query!r}."
    )
    return Document(
        title=f"cadastre search {query}",
        sections=(
            Section(
                f"search: {query}",
                (
                    Para(blurb),
                    Table(
                        ("kind", "id", "declared", "observed by", "match", "manifest"),
                        rows,
                        empty_note="(no candidates)",
                    ),
                ),
                note=note,
            ),
        ),
        provenance=session.provenance(),
        data={
            "query": query,
            "kind": kind,
            "resolution": "search",
            "total": len(pool),
            "results": records,
        },
    )


# --------------------------------------------------------------------------
# credential_for(service, action)
# --------------------------------------------------------------------------

#: What an action usually needs, as name fragments. A ranking hint, never a
#: filter: an estate that calls its deploy credential `..._WEBHOOK` still finds
#: it, one place lower.
ACTION_HINTS: dict[str, tuple[str, ...]] = {
    "deploy": ("api", "key", "secret", "token", "deploy"),
    "api": ("api", "key", "secret", "token"),
    "read": ("read", "token", "api", "key"),
    "write": ("write", "token", "api", "key"),
    "push": ("push", "token", "pat", "registry"),
    "git": ("git", "token", "pat", "ssh"),
    "ssh": ("ssh", "key", "private"),
    "login": ("password", "admin", "user", "login"),
    "admin": ("admin", "password", "root"),
    "webhook": ("webhook", "url"),
    "notify": ("webhook", "url", "token"),
    "tunnel": ("tunnel", "token"),
    "database": ("password", "user", "url", "dsn", "database", "db"),
    "db": ("password", "user", "url", "dsn", "db"),
}

_RETIRED = ("revoked", "retired", "deprecated", "old", "legacy")


def _action_tokens(action: str | None) -> list[str]:
    if not action:
        return []
    out: list[str] = []
    for token in tokens(action):
        out.append(token)
        out.extend(ACTION_HINTS.get(token, ()))
    return list(dict.fromkeys(out))


def _service_hits(session: Session, service: str) -> list[Hit]:
    want = normalize(service)
    return [
        hit
        for hit in all_hits(session, kind="service")
        if normalize(hit.id) == want
        or any(
            normalize(name) == want
            for entity in hit.observed
            for name in _member_names(entity)
        )
    ]


def _member_names(entity: model.Entity) -> Iterator[str]:
    for block in entity.extra.values():
        if not isinstance(block, dict):
            continue
        for value in block.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        yield item["name"]


def credential_for(
    session: Session, service: str, action: str | None = None
) -> Document:
    """Which secret(s) a caller needs to do `action` against `service`."""
    service_tokens = [token for token in tokens(service or "") if len(token) >= 2]
    if not service_tokens:
        raise UsageError(
            "credential_for needs `service` (a service name such as 'komodo'); "
            "`action` is optional, e.g. 'deploy'"
        )
    hints = _action_tokens(action)
    services = _service_hits(session, service)
    consumed: set[str] = set()
    for hit in services:
        if hit.declared_entity is not None:
            consumed.update(getattr(hit.declared_entity, "consumes_secret", ()) or ())
    ranked: list[tuple[float, Hit, list[str]]] = []
    for hit in all_hits(session, kind="secret"):
        strings = _hit_strings(hit)
        why: list[str] = []
        total = 0.0
        if hit.id in consumed:
            total += 2.0
            why.append("declared consumes_secret of the service")
        if _matched_all(service_tokens, strings):
            fraction, _ = score(service_tokens, strings)
            total += fraction
            why.append("name matches the service")
        if total == 0:
            continue
        if hints:
            hay = set()
            for value in strings:
                hay.update(tokens(value))
            matched = [token for token in hints if token in hay]
            if matched:
                total += 0.25 * len(matched)
                why.append("name suggests " + "/".join(matched))
        flagged = {*tokens(" ".join(strings)), *(hit.entity.tags or ())}
        if flagged & set(_RETIRED):
            total -= 1.5
            why.append("named or tagged as retired")
        ranked.append((total, hit, why))
    ranked.sort(key=lambda item: (-item[0], not item[1].declared, item[1].id))
    shown = ranked[:DEFAULT_LIMIT]
    candidates = []
    for rank, hit, why in shown:
        record = _hit_record(session, hit, rank, "ranked")
        record["why"] = why
        declared = hit.declared_entity
        if declared is not None and declared.notes:
            record["scope_notes"] = inert(declared.notes)
        candidates.append(record)
    endpoints = [
        _public_entity(session, endpoint)
        for endpoint in session.catalog.endpoints
        if any(endpoint.service == hit.id for hit in services)
    ]
    rows = tuple(
        (
            record["id"],
            record["secret"]["store"] or "-",
            record["secret"]["project_id"] or "?",
            record["secret"]["manifest_line"] or "",
            "; ".join(record["why"]),
        )
        for record in candidates
    )
    blurb = (
        f"{len(ranked)} secret(s) relate to {service!r}"
        + (f" for {action!r}" if action else "")
        + ". Credentials often come in pairs (key + secret); take every "
        "candidate the consumer reads."
        if ranked
        else f"No secret name or declaration relates to {service!r}. Try "
        "`lookup(query=...)` with other words, or declare `consumes_secret` "
        "on the service."
    )
    return Document(
        title=f"cadastre credential-for {service}" + (f" {action}" if action else ""),
        sections=(
            Section(
                f"credentials for {service}" + (f" ({action})" if action else ""),
                (
                    Para(blurb),
                    Table(
                        ("secret", "store", "project", "manifest", "why"),
                        rows,
                        empty_note="(no candidates)",
                    ),
                ),
                note=(
                    "Ranked by name and declared consumption, not by grant: "
                    "Cadastre does not hold or check the permission itself. "
                    "Values are never returned."
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "service": service,
            "action": action,
            "service_found": [hit.id for hit in services],
            "service_endpoints": endpoints,
            "total": len(ranked),
            "candidates": candidates,
        },
    )
