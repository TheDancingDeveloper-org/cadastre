"""Durable trust state derived from observed evidence.

The ledger is generated cache state. It records that a disagreement was seen,
when it was first seen, and whether it has flapped; it never chooses declared
or observed as truth. Explicit choices live in the catalog database and are
read by this module when presenting state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from cadastre.core.catalog import Catalog
from cadastre.core.drift import Divergence, compare
from cadastre.core.observed import ObservedSource
from cadastre.core.provenance import format_timestamp, parse_timestamp

LEDGER_VERSION = 1
LEDGER_PATH = Path("observed/.cadastre/trust.json")
RESOLUTIONS_PATH = Path("declared/.cadastre/resolutions.yaml")
ACKNOWLEDGEMENTS_PATH = Path("declared/policy/acknowledged.yaml")


@dataclass(frozen=True)
class TrustRecord:
    kind: str
    id: str
    field: str | None
    source: str
    state: str
    first_seen: str
    last_seen: str
    declared: str | None = None
    observed: str | None = None
    observations: tuple[str, ...] = ()
    flapping: bool = False
    resolution: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.id, self.field or "", self.source)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "source": self.source,
            "state": self.state,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }
        if self.field:
            result["field"] = self.field
        if self.declared is not None:
            result["declared"] = self.declared
        if self.observed is not None:
            result["observed"] = self.observed
        if self.observations:
            result["observations"] = list(self.observations)
        if self.flapping:
            result["flapping"] = True
        if self.resolution:
            result["resolution"] = self.resolution
        return result


def _divergence_key(divergence: Divergence) -> tuple[str, str, str, str]:
    return (
        divergence.kind,
        divergence.id,
        divergence.field or "",
        divergence.source,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_records(root: Path) -> tuple[TrustRecord, ...]:
    raw = _read_json(root / LEDGER_PATH)
    if raw.get("records"):
        records = []
        for item in raw["records"]:
            if isinstance(item, dict):
                try:
                    records.append(_record(item))
                except (KeyError, TypeError, ValueError):
                    continue
        return tuple(sorted(records, key=lambda record: record.key))
    from cadastre.core.observed_db import load_trust_records

    cached = load_trust_records(root)
    if cached:
        return tuple(sorted((_record(item) for item in cached), key=lambda r: r.key))
    records = []
    for item in raw.get("records", []):
        if not isinstance(item, dict):
            continue
        try:
            records.append(_record(item))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(sorted(records, key=lambda record: record.key))


def _record(item: dict[str, Any]) -> TrustRecord:
    return TrustRecord(
        kind=str(item["kind"]),
        id=str(item["id"]),
        field=str(item["field"]) if item.get("field") else None,
        source=str(item["source"]),
        state=str(item["state"]),
        first_seen=str(item["first_seen"]),
        last_seen=str(item["last_seen"]),
        declared=item.get("declared"),
        observed=item.get("observed"),
        observations=tuple(str(v) for v in item.get("observations", [])),
        flapping=bool(item.get("flapping", False)),
        resolution=item.get("resolution"),
    )


def _signature(divergence: Divergence) -> str:
    return json.dumps(
        {
            "declared": divergence.declared,
            "observed": divergence.observed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def update_records(
    root: Path,
    catalog: Catalog,
    source: ObservedSource,
    now: datetime,
) -> tuple[TrustRecord, ...]:
    """Record a collection without resolving an existing contest."""
    records = {record.key: record for record in load_records(root)}
    if not source.ok:
        return tuple(sorted(records.values(), key=lambda record: record.key))
    seen = compare(catalog, [source])
    timestamp = format_timestamp(now)
    for divergence in seen:
        key = _divergence_key(divergence)
        signature = _signature(divergence)
        previous = records.get(key)
        if previous is None:
            records[key] = TrustRecord(
                kind=divergence.kind,
                id=divergence.id,
                field=divergence.field,
                source=divergence.source,
                state="contested",
                first_seen=timestamp,
                last_seen=timestamp,
                declared=divergence.declared,
                observed=divergence.observed,
                observations=(signature,),
            )
            continue
        observations = (*previous.observations, signature)[-20:]
        distinct = len(set(observations))
        records[key] = replace(
            previous,
            state="contested",
            last_seen=timestamp,
            declared=divergence.declared,
            observed=divergence.observed,
            observations=observations,
            flapping=previous.flapping or distinct > 1,
            resolution=(
                previous.resolution
                if previous.observations and previous.observations[-1] == signature
                else None
            ),
        )
    return tuple(sorted(records.values(), key=lambda record: record.key))


def write_records(root: Path, records: tuple[TrustRecord, ...]) -> Path:
    from cadastre.core.observed_db import write_records as cache_records

    return cache_records(root, tuple(record.to_dict() for record in records))


def _yaml_records(root: Path, path: Path, key: str) -> list[dict[str, Any]]:
    if (root / "catalog.sqlite3").exists():
        from cadastre.core.storage import CatalogStore

        name = "resolutions" if key == "resolutions" else "acknowledgements"
        with CatalogStore.open(root, read_only=True) as store:
            row = store.connection.execute(
                "SELECT value FROM metadata WHERE key=?", (name,)
            ).fetchone()
            if row:
                raw = json.loads(row[0])
                return [dict(item) for item in raw if isinstance(item, dict)]
        return []
    from cadastre.core.yamlio import load_yaml

    full = root / path
    if not full.exists():
        return []
    raw = load_yaml(full, rel=str(path)) or {}
    items = raw.get(key) if isinstance(raw, dict) else []
    return (
        [dict(item) for item in items if isinstance(item, dict)]
        if isinstance(items, list)
        else []
    )


def resolutions(root: Path) -> list[dict[str, Any]]:
    return _yaml_records(root, RESOLUTIONS_PATH, "resolutions")


def acknowledgements(root: Path) -> list[dict[str, Any]]:
    return _yaml_records(root, ACKNOWLEDGEMENTS_PATH, "acknowledged")


def active_acknowledgements(root: Path, now: datetime) -> tuple[dict[str, Any], ...]:
    active = []
    for item in acknowledgements(root):
        until = item.get("until")
        if not isinstance(until, str):
            continue
        try:
            if parse_timestamp(until) > now:
                active.append(item)
        except Exception:
            continue
    return tuple(active)


def presented_records(root: Path, now: datetime) -> tuple[TrustRecord, ...]:
    """Apply explicit resolutions for presentation, never during collection."""
    records = {record.key: record for record in load_records(root)}
    for item in resolutions(root):
        key = (
            str(item.get("kind", "")),
            str(item.get("id", "")),
            str(item.get("field", "")),
            str(item.get("source", "")),
        )
        record = records.get(key)
        if record is None:
            continue
        action = item.get("action")
        if action == "accept-observed":
            records[key] = replace(record, state="agreed", resolution="accept-observed")
        elif action == "leave-contested":
            records[key] = replace(
                record, state="contested", resolution="leave-contested"
            )
    return tuple(sorted(records.values(), key=lambda record: record.key))


def unverified_sources(
    root: Path, configured: tuple[str, ...], observed: tuple[str, ...]
) -> tuple[str, ...]:
    seen = set(observed)
    return tuple(sorted(source for source in configured if source not in seen))


def contest_policy(root: Path, kind: str, field: str | None) -> str:
    if not field:
        return "warn"
    from cadastre.plugins import PluginRegistry

    for plugin in PluginRegistry.discover(root).plugins:
        declaration = plugin.info.entity(kind)
        if declaration is not None and declaration.authority == "source":
            return declaration.on_contest.get(field, "warn")
    return "warn"


def record_for(
    root: Path, kind: str, ident: str, field: str | None, source: str
) -> TrustRecord | None:
    key = (kind, ident, field or "", source)
    return next((record for record in load_records(root) if record.key == key), None)
