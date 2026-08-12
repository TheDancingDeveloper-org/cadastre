"""SQLite cache for observed evidence.

The JSON files in ``observed/`` are the interchange format.  This module adds
the rebuildable cache described by DESIGN §2.11: current source payloads are
queryable from SQLite and every distinct collection is retained as history.
The database never becomes the source of truth and can be deleted safely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cadastre.core.errors import Located
from cadastre.core.observed import ObservedSource, parse_source, source_to_dict
from cadastre.modules.registry import EntityRegistry

DB_PATH = Path("observed/cadastre.db")


def database_path(root: Path) -> Path:
    # Runtime observed evidence lives beside the catalog database.  The old
    # observed/cadastre.db location remains only for YAML fixture compatibility.
    return (
        root / "observed.sqlite3"
        if (root / "catalog.sqlite3").exists()
        else root / DB_PATH
    )


def _connect(root: Path) -> sqlite3.Connection:
    path = database_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            source TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            as_of TEXT NOT NULL,
            ok INTEGER NOT NULL,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS observations (
            source TEXT NOT NULL,
            as_of TEXT NOT NULL,
            kind TEXT NOT NULL,
            ident TEXT NOT NULL,
            payload TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY (source, as_of, kind, ident, digest)
        );
        CREATE INDEX IF NOT EXISTS observations_entity
            ON observations (source, kind, ident, as_of);
        """
    )


def _payload(source: ObservedSource) -> str:
    return json.dumps(source_to_dict(source), sort_keys=True, separators=(",", ":"))


def record_source(root: Path, source: ObservedSource) -> Path:
    """Atomically update current evidence and append its observation history."""
    payload = _payload(source)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if (root / "catalog.sqlite3").exists():
        from cadastre.core.storage import record_observed

        record_observed(root, source_to_dict(source))
        return database_path(root)
    history_path = (
        root
        / "observed"
        / ".cadastre"
        / "history"
        / source.source.replace("/", "_")
        / f"{source.as_of.replace(':', '-')}-{digest}.json"
    )
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(source_to_dict(source), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    with _connect(root) as connection:
        _schema(connection)
        connection.execute(
            """INSERT INTO sources(source, payload, as_of, ok, error)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET payload=excluded.payload,
                 as_of=excluded.as_of, ok=excluded.ok, error=excluded.error""",
            (source.source, payload, source.as_of, int(source.ok), source.error),
        )
        for kind, entities in sorted(source.entities.items()):
            for entity in sorted(entities, key=lambda item: item.id):
                entity_payload = json.dumps(
                    source_to_dict(
                        ObservedSource(
                            source=source.source,
                            plugin=source.plugin,
                            as_of=source.as_of,
                            capabilities=source.capabilities,
                            entities={kind: [entity]},
                            ok=source.ok,
                            error=source.error,
                            coverage=source.coverage,
                            extensions=source.extensions,
                            registry=source.registry,
                        )
                    )["entities"][kind][0],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                digest = hashlib.sha256(entity_payload.encode()).hexdigest()
                connection.execute(
                    """INSERT OR IGNORE INTO observations
                       (source, as_of, kind, ident, payload, digest)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source.source,
                        source.as_of,
                        kind,
                        entity.id,
                        entity_payload,
                        digest,
                    ),
                )
    return database_path(root)


def sync_snapshots(root: Path) -> Path:
    """Build or refresh the cache from all JSON snapshots in ``observed/``."""
    from cadastre.core.observed import load_snapshot_files
    from cadastre.modules.config import load_modules
    from cadastre.modules.registry import active_registry

    registry = active_registry(load_modules(root))
    snapshots = load_snapshot_files(root, registry=registry)
    history_dir = root / "observed" / ".cadastre" / "history"
    history: list[ObservedSource] = []
    if history_dir.is_dir():
        for path in sorted(history_dir.glob("*/*.json")):
            history.append(
                parse_source(
                    json.loads(path.read_text(encoding="utf-8")),
                    Located(str(path.relative_to(root))),
                    registry=registry,
                )
            )
    for source in [*history, *snapshots]:
        record_source(root, source)
    path = database_path(root)
    if not path.exists():
        with _connect(root) as connection:
            _schema(connection)
    return path


def _sources_from_rows(
    rows: Iterator[sqlite3.Row], *, registry: EntityRegistry | None = None
) -> list[ObservedSource]:
    return [
        parse_source(
            json.loads(row["payload"]),
            Located(f"observed/{row['source']}.json"),
            registry=registry,
        )
        for row in rows
    ]


def load_sources(root: Path) -> list[ObservedSource]:
    """Load current source snapshots from the cache, ordered by source id."""
    from cadastre.modules.config import load_modules
    from cadastre.modules.registry import active_registry

    registry = active_registry(load_modules(root))
    if (root / "catalog.sqlite3").exists():
        from cadastre.core.storage import observed_payloads

        return [
            parse_source(
                payload,
                Located(f"sqlite:observed:{payload.get('source', '')}"),
                registry=registry,
            )
            for payload in observed_payloads(root)
        ]
    path = database_path(root)
    if not path.exists():
        return []
    with _connect(root) as connection:
        _schema(connection)
        rows = connection.execute("SELECT payload, source FROM sources ORDER BY source")
        return _sources_from_rows(rows, registry=registry)


def history(root: Path, *, source: str | None = None) -> tuple[dict[str, Any], ...]:
    """Return deterministic observation history rows for diagnostics and tests."""
    if (root / "catalog.sqlite3").exists():
        from cadastre.core.storage import observed_history

        runtime_rows = observed_history(root)
        return tuple(
            row for row in runtime_rows if source is None or row["source"] == source
        )
    path = database_path(root)
    if not path.exists():
        return ()
    with _connect(root) as connection:
        _schema(connection)
        if source is None:
            legacy_rows = connection.execute(
                "SELECT source, as_of, kind, ident, payload, digest "
                "FROM observations ORDER BY source, as_of, kind, ident, digest"
            )
        else:
            legacy_rows = connection.execute(
                "SELECT source, as_of, kind, ident, payload, digest "
                "FROM observations WHERE source=? "
                "ORDER BY source, as_of, kind, ident, digest",
                (source,),
            )
        return tuple(dict(row) for row in legacy_rows)


def write_records(root: Path, records: tuple[dict[str, Any], ...]) -> Path:
    """Store generated trust records in the observed database.

    Legacy fixture roots retain the JSON ledger for compatibility; an
    initialized runtime never creates a second persistent backend.
    """
    if not (root / "catalog.sqlite3").exists():
        ledger = root / "observed" / ".cadastre" / "trust.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps({"v": 1, "records": list(records)}, indent=2) + "\n",
            encoding="utf-8",
        )
    if (root / "catalog.sqlite3").exists():
        from cadastre.core.storage import write_trust_records

        write_trust_records(root, records)
        return root / "observed.sqlite3"
    path = database_path(root)
    with _connect(root) as connection:
        _schema(connection)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS trust_records (
                 record_key TEXT PRIMARY KEY, payload TEXT NOT NULL
               )"""
        )
        connection.execute("DELETE FROM trust_records")
        for record in records:
            key = json.dumps(
                [
                    record.get("kind"),
                    record.get("id"),
                    record.get("field", ""),
                    record.get("source"),
                ],
                separators=(",", ":"),
            )
            connection.execute(
                "INSERT INTO trust_records(record_key, payload) VALUES (?, ?)",
                (key, json.dumps(record, sort_keys=True, separators=(",", ":"))),
            )
    return path


def load_trust_records(root: Path) -> tuple[dict[str, Any], ...]:
    if (root / "catalog.sqlite3").exists():
        from cadastre.core.storage import read_trust_records

        return read_trust_records(root)
    path = database_path(root)
    if not path.exists():
        return ()
    with _connect(root) as connection:
        _schema(connection)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS trust_records (
                 record_key TEXT PRIMARY KEY, payload TEXT NOT NULL
               )"""
        )
        rows = connection.execute(
            "SELECT payload FROM trust_records ORDER BY record_key"
        )
        return tuple(json.loads(row[0]) for row in rows)
