"""The operational Cadastre storage contract.

The catalog database is the runtime source of truth.  YAML and JSON are bundle
formats; they are never used as an implicit live backend.  This module owns
SQLite mechanics only.  Policy and adapter decisions stay in the core and CLI
layers above it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre.core import model
from cadastre.core.catalog import Catalog
from cadastre.core.errors import Located, UsageError
from cadastre.core.loader import IssueCollector, parse_entity
from cadastre.core.migrations import CURRENT_SCHEMA
from cadastre.core.migrations import apply as apply_migrations
from cadastre.core.provenance import format_timestamp
from cadastre.core.serialize import entity_to_dict
from cadastre.modules.registry import EntityRegistry, active_registry, base_registry

CATALOG_DB = "catalog.sqlite3"
OBSERVED_DB = "observed.sqlite3"
FORMAT_VERSION = CURRENT_SCHEMA
MIN_SQLITE_VERSION = (3, 35, 0)


def _utc_now() -> str:
    return format_timestamp(datetime.now(tz=UTC))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _record_observed_connection(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    prefix: str = "main",
) -> None:
    source = str(payload.get("source", "unknown"))
    as_of = str(payload.get("as_of", ""))
    encoded = _json(payload)
    connection.execute(
        f"INSERT INTO {prefix}.sources(source,payload,as_of,ok,error) "
        "VALUES(?,?,?,?,?) "
        f"ON CONFLICT(source) DO UPDATE SET payload=excluded.payload, "
        f"as_of=excluded.as_of, ok=excluded.ok, error=excluded.error",
        (
            source,
            encoded,
            as_of,
            int(bool(payload.get("ok", True))),
            payload.get("error"),
        ),
    )
    for kind, values in sorted((payload.get("entities") or {}).items()):
        for value in sorted(values, key=lambda item: str(item.get("id", ""))):
            item_json = _json(value)
            item_digest = hashlib.sha256(item_json.encode()).hexdigest()
            connection.execute(
                f"INSERT OR IGNORE INTO {prefix}.observations("
                "source,as_of,kind,ident,payload,digest) VALUES(?,?,?,?,?,?)",
                (source, as_of, kind, str(value.get("id", "")), item_json, item_digest),
            )


def _increment_observed_revision(
    connection: sqlite3.Connection, *, prefix: str = "main"
) -> int:
    row = connection.execute(
        f"SELECT value FROM {prefix}.metadata WHERE key='revision'"
    ).fetchone()
    current = int(row[0]) if row else 0
    revision = current + 1
    connection.execute(
        f"INSERT OR REPLACE INTO {prefix}.metadata(key,value) VALUES('revision',?)",
        (str(revision),),
    )
    return revision


def _decode(value: str) -> Any:
    return json.loads(value)


def _record_required_modules(connection: sqlite3.Connection, catalog: Catalog) -> None:
    """Durably mark a database as holding Manifest-owned rows (F03).

    Only "manifest" is a supported module today (`modules/config.py`'s
    `SUPPORTED_MODULES`); this hardcodes its kind set rather than deriving
    module ownership generically, since the registry doesn't yet track a
    per-kind owning module. Widen this when a second module exists.
    """
    from cadastre.manifest.spec import ENTITY_SPECS as MANIFEST_ENTITY_SPECS

    manifest_kinds = frozenset(MANIFEST_ENTITY_SPECS)
    has_manifest_rows = any(
        catalog.of(kind) for kind in manifest_kinds if kind in catalog.registry.kinds
    )
    if not has_manifest_rows:
        return
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='required_modules'"
    ).fetchone()
    current = set(_decode(row[0])) if row else set()
    if "manifest" not in current:
        current.add("manifest")
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('required_modules',?)",
            (_json(sorted(current)),),
        )


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if not read_only:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _schema(connection: sqlite3.Connection, *, read_only: bool = False) -> None:
    if read_only:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='format_version'"
        ).fetchone()
        if row is None:
            raise UsageError("catalog database has no storage metadata")
        version = int(row[0])
        if version > FORMAT_VERSION:
            raise UsageError(
                f"catalog schema {version} is newer than this application supports "
                f"({FORMAT_VERSION})"
            )
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migration_lock (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          holder TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entities (
          kind TEXT NOT NULL,
          id TEXT NOT NULL,
          payload TEXT NOT NULL,
          PRIMARY KEY (kind, id)
        );
        CREATE INDEX IF NOT EXISTS entities_id ON entities(id);
        CREATE TABLE IF NOT EXISTS annotations (
          kind TEXT NOT NULL,
          id TEXT NOT NULL,
          payload TEXT NOT NULL,
          PRIMARY KEY (kind, id),
          FOREIGN KEY (kind, id) REFERENCES entities(kind, id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS policy (
          name TEXT PRIMARY KEY,
          payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          audit_id TEXT PRIMARY KEY,
          transaction_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          principal TEXT NOT NULL,
          reason TEXT NOT NULL,
          operation TEXT NOT NULL,
          changed TEXT NOT NULL,
          at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS audit_revision ON audit(revision);
        INSERT OR IGNORE INTO metadata(key, value) VALUES
          ('format_version', '1'), ('revision', '0');
        INSERT OR IGNORE INTO migrations(version, applied_at) VALUES (1, 'bootstrap');
        """
    )
    apply_migrations(connection)
    version = int(
        connection.execute(
            "SELECT value FROM metadata WHERE key='format_version'"
        ).fetchone()[0]
    )
    if version > FORMAT_VERSION:
        raise UsageError(
            f"catalog schema {version} is newer than this application supports "
            f"({FORMAT_VERSION})"
        )


def _policy_to_dict(policy: model.Policy) -> dict[str, Any]:
    return {
        "exposure": [
            {
                "name": item.name,
                "network_class": item.network_class,
                "network": item.network,
                "requires_ingress": item.requires_ingress,
                **({"description": item.description} if item.description else {}),
            }
            for item in policy.exposure
        ],
        "conventions": {
            key: value
            for key, value in {
                "host_name": policy.conventions.host_name,
                "service_name": policy.conventions.service_name,
                "secret_ref": policy.conventions.secret_ref,
                "endpoint_address": policy.conventions.endpoint_address,
            }.items()
            if value is not None
        },
        "grants": [
            {
                key: value
                for key, value in {
                    "id": item.id,
                    "principal": item.principal,
                    "role": item.role,
                    "targets": list(item.targets),
                    "actions": list(item.actions),
                    "deny": list(item.deny),
                    "ttl": item.ttl,
                }.items()
                if value is not None and value not in ((), [])
            }
            for item in policy.grants
        ],
        "known_undeclared": [
            {
                "source": item.source,
                "kind": item.kind,
                "reason": item.reason,
                "ids": list(item.ids),
            }
            for item in policy.known_undeclared
        ],
        "replication": [
            {
                "source": item.source,
                "target": item.target,
                "selectors": list(item.selectors),
                "mappings": dict(item.mappings),
            }
            for item in policy.replication
        ],
    }


def _policy_from_dict(raw: dict[str, Any]) -> model.Policy:
    exposure = tuple(
        model.ExposureTier(
            name=str(item["name"]),
            network_class=str(item["network_class"]),
            network=str(item["network"]) if item.get("network") else None,
            requires_ingress=bool(item.get("requires_ingress", False)),
            description=item.get("description"),
        )
        for item in raw.get("exposure", [])
    )
    c = raw.get("conventions", {})
    conventions = model.Conventions(
        host_name=c.get("host_name"),
        service_name=c.get("service_name"),
        secret_ref=c.get("secret_ref"),
        endpoint_address=c.get("endpoint_address"),
    )
    grants = tuple(
        model.Grant(
            id=item.get("id"),
            principal=str(item["principal"]),
            role=str(item["role"]),
            targets=tuple(item.get("targets", ())),
            actions=tuple(item.get("actions", ())),
            deny=tuple(item.get("deny", ())),
            ttl=item.get("ttl"),
        )
        for item in raw.get("grants", [])
    )
    known = tuple(
        model.KnownUndeclared(
            source=str(item["source"]),
            kind=str(item["kind"]),
            reason=str(item["reason"]),
            ids=tuple(item.get("ids", ())),
        )
        for item in raw.get("known_undeclared", [])
    )
    replication = tuple(
        model.ReplicationContract(
            source=str(item["source"]),
            target=str(item["target"]),
            selectors=tuple(item.get("selectors", ())),
            mappings={str(k): str(v) for k, v in (item.get("mappings") or {}).items()},
        )
        for item in raw.get("replication", [])
    )
    return model.Policy(
        exposure=exposure,
        conventions=conventions,
        grants=grants,
        known_undeclared=known,
        replication=replication,
    )


def _catalog_from_connection(
    root: Path,
    connection: sqlite3.Connection,
    registry: EntityRegistry | None = None,
) -> Catalog:
    registry = registry or base_registry()
    entities: dict[str, dict[str, model.Entity]] = {kind: {} for kind in registry.kinds}
    locations: dict[tuple[str, str], Located] = {}
    issues = IssueCollector()
    for row in connection.execute(
        "SELECT kind, id, payload FROM entities ORDER BY kind, id"
    ):
        kind = str(row["kind"])
        entity = parse_entity(
            registry.specs[kind],
            _decode(row["payload"]),
            Located(f"sqlite:{kind}:{row['id']}"),
            issues,
        )
        if entity is not None:
            entities[kind][entity.id] = entity
            locations[(kind, entity.id)] = Located(f"sqlite:{kind}:{entity.id}")
    annotations: dict[tuple[str, str], dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT kind, id, payload FROM annotations ORDER BY kind, id"
    ):
        annotations[(row["kind"], row["id"])] = _decode(row["payload"])
        base = entity_to_dict(entities[row["kind"]][row["id"]], registry=registry)
        base.update(annotations[(row["kind"], row["id"])].get("values", {}))
        parsed = parse_entity(
            registry.specs[row["kind"]],
            base,
            Located(f"sqlite:annotation:{row['kind']}:{row['id']}"),
            issues,
        )
        if parsed is not None:
            entities[row["kind"]][row["id"]] = parsed
    issues.raise_if_any()
    policy_row = connection.execute(
        "SELECT payload FROM policy WHERE name='catalog'"
    ).fetchone()
    policy = _policy_from_dict(_decode(policy_row[0])) if policy_row else model.Policy()
    return Catalog(
        root=root,
        entities=entities,
        policy=policy,
        locations=locations,
        annotations=annotations,
        declared_entities={kind: dict(items) for kind, items in entities.items()},
        registry=registry,
    )


@dataclass(frozen=True)
class WriteResult:
    database_revision: int
    transaction_id: str
    audit_id: str
    principal: str
    reason: str
    operation: str
    changed: tuple[tuple[str, str], ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f"{kind}:{ident}" for kind, ident in self.changed)


class CatalogStore:
    """Narrow core-owned SQLite catalog backend."""

    def __init__(
        self, data_dir: Path, connection: sqlite3.Connection, *, read_only: bool = False
    ):
        self.data_dir = data_dir
        self.connection = connection
        self.read_only = read_only

    @classmethod
    def open(
        cls, data_dir: Path, *, create: bool = False, read_only: bool = False
    ) -> CatalogStore:
        if read_only:
            if not data_dir.is_dir():
                raise UsageError(f"no initialized data directory at {data_dir}")
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / CATALOG_DB
        if not path.exists() and not create:
            raise UsageError(
                f"no initialized SQLite catalog at {path}; run `cadastre init "
                f"--data-dir {data_dir}`"
            )
        connection = _connect(path, read_only=read_only)
        if not read_only:
            # SQLite serializes this short schema transaction. A second
            # process waits on busy_timeout instead of migrating concurrently.
            connection.execute("BEGIN IMMEDIATE")
        try:
            _schema(connection, read_only=read_only)
            if not read_only:
                connection.commit()
        except Exception:
            if not read_only:
                connection.rollback()
            connection.close()
            raise
        return cls(data_dir, connection, read_only=read_only)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> CatalogStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def revision(self) -> int:
        return int(
            self.connection.execute(
                "SELECT value FROM metadata WHERE key='revision'"
            ).fetchone()[0]
        )

    @property
    def declared_as_of(self) -> str:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='declared_as_of'"
        ).fetchone()
        return str(row[0]) if row else "1970-01-01T00:00:00Z"

    @property
    def required_modules(self) -> frozenset[str]:
        """Modules whose entity kinds this database holds rows for.

        Set durably by `_record_required_modules` the first time a
        module-owned row is written (MANIFEST.md F03) and never cleared
        automatically, so a later delete of every such row cannot make the
        marker silently disappear and let a build without the module treat
        the database as though it never held that data.
        """
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='required_modules'"
        ).fetchone()
        return frozenset(_decode(row[0])) if row else frozenset()

    def read_catalog(self, *, registry: EntityRegistry | None = None) -> Catalog:
        return _catalog_from_connection(self.data_dir, self.connection, registry)

    def apply_catalog_transaction(
        self,
        catalog: Catalog,
        *,
        principal: str,
        reason: str,
        operation: str,
        changed: tuple[tuple[str, str], ...],
        expected_revision: int | None = None,
        observed: tuple[dict[str, Any], ...] = (),
        observed_history: tuple[dict[str, Any], ...] = (),
        metadata: dict[str, list[dict[str, Any]]] | None = None,
    ) -> WriteResult:
        if self.read_only:
            raise UsageError("catalog is opened read-only")
        from cadastre.core.rules import check_catalog

        errors = [
            finding for finding in check_catalog(catalog) if finding.level == "error"
        ]
        if errors:
            raise UsageError(
                "catalog check failed: "
                + "; ".join(f"{item.code}: {item.message}" for item in errors)
            )
        transaction_id, audit_id = str(uuid.uuid4()), str(uuid.uuid4())
        at = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if observed or observed_history or metadata:
                observed_path = self.data_dir / OBSERVED_DB
                if not observed_path.exists():
                    with _connect(observed_path) as observed_connection:
                        _observed_schema(observed_connection)
                self.connection.execute(
                    "ATTACH DATABASE ? AS observed", (str(observed_path),)
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS observed.sources("
                    "source TEXT PRIMARY KEY,payload TEXT NOT NULL,as_of TEXT NOT NULL,"
                    "ok INTEGER NOT NULL,error TEXT)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS observed.observations("
                    "source TEXT NOT NULL,as_of TEXT NOT NULL,kind TEXT NOT NULL,"
                    "ident TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,"
                    "PRIMARY KEY(source,as_of,kind,ident,digest))"
                )
            current = self.revision
            if expected_revision is not None and current != expected_revision:
                raise UsageError(
                    f"catalog revision conflict: expected {expected_revision}, "
                    f"current {current}"
                )
            self.connection.execute("DELETE FROM annotations")
            self.connection.execute("DELETE FROM entities")
            for kind in catalog.registry.kinds:
                for entity in catalog.all(kind):
                    self.connection.execute(
                        "INSERT INTO entities(kind,id,payload) VALUES(?,?,?)",
                        (
                            kind,
                            entity.id,
                            _json(entity_to_dict(entity, registry=catalog.registry)),
                        ),
                    )
            _record_required_modules(self.connection, catalog)
            for key, value in sorted(catalog.annotations.items()):
                self.connection.execute(
                    "INSERT INTO annotations(kind,id,payload) VALUES(?,?,?)",
                    (key[0], key[1], _json(value)),
                )
            self.connection.execute(
                "INSERT OR REPLACE INTO policy(name,payload) VALUES('catalog',?)",
                (_json(_policy_to_dict(catalog.policy)),),
            )
            revision = current + 1
            self.connection.execute(
                "UPDATE metadata SET value=? WHERE key='revision'", (str(revision),)
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('declared_as_of',?)",
                (at,),
            )
            self.connection.execute(
                "INSERT INTO audit(audit_id,transaction_id,revision,principal,reason,"
                "operation,changed,at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    transaction_id,
                    revision,
                    principal,
                    reason,
                    operation,
                    _json(list(changed)),
                    at,
                ),
            )
            for payload in observed:
                _record_observed_connection(self.connection, payload, prefix="observed")
            for item in observed_history:
                self.connection.execute(
                    "INSERT OR IGNORE INTO observed.observations("
                    "source,as_of,kind,ident,payload,digest) VALUES(?,?,?,?,?,?)",
                    tuple(
                        item[key]
                        for key in (
                            "source",
                            "as_of",
                            "kind",
                            "ident",
                            "payload",
                            "digest",
                        )
                    ),
                )
            for name, values in (metadata or {}).items():
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                    (name, _json(values)),
                )
            if observed or observed_history:
                _increment_observed_revision(self.connection, prefix="observed")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            if observed or observed_history or metadata:
                with suppress(sqlite3.OperationalError):
                    self.connection.execute("DETACH DATABASE observed")
        return WriteResult(
            revision, transaction_id, audit_id, principal, reason, operation, changed
        )

    def audit(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit ORDER BY revision, audit_id"
            )
        )

    def read_observed(self) -> tuple[dict[str, Any], ...]:
        with open_observed(self.data_dir) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT source,payload,as_of,ok,error FROM sources ORDER BY source"
                )
            )


class RuntimeStore:
    """Coherent read façade over the two runtime databases.

    Services use this boundary to validate that a data directory is a complete
    initialized runtime store.  Bundle fixtures intentionally do not pass
    through it.
    """

    def __init__(
        self,
        data_dir: Path,
        catalog: CatalogStore,
        observed: sqlite3.Connection,
    ):
        self.data_dir = data_dir
        self.catalog = catalog
        self.observed = observed

    @classmethod
    def open(cls, data_dir: Path, *, read_only: bool = True) -> RuntimeStore:
        if (
            not (data_dir / CATALOG_DB).exists()
            or not (data_dir / OBSERVED_DB).exists()
        ):
            raise UsageError(
                "runtime store is incomplete; both catalog.sqlite3 and "
                "observed.sqlite3 are required"
            )
        catalog = CatalogStore.open(data_dir, read_only=read_only)
        try:
            observed = _connect(data_dir / OBSERVED_DB, read_only=read_only)
            _observed_schema(observed, read_only=read_only)
        except Exception:
            catalog.close()
            raise
        return cls(data_dir, catalog, observed)

    @property
    def revision(self) -> int:
        return self.catalog.revision

    @property
    def observed_revision(self) -> int:
        row = self.observed.execute(
            "SELECT value FROM metadata WHERE key='revision'"
        ).fetchone()
        return int(row[0]) if row else 0

    def revisions(self) -> dict[str, int]:
        return {CATALOG_DB: self.revision, OBSERVED_DB: self.observed_revision}

    def close(self) -> None:
        self.observed.close()
        self.catalog.close()

    def __enter__(self) -> RuntimeStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def initialize(data_dir: Path) -> dict[str, Any]:
    from cadastre.modules.config import load_modules

    data_dir.mkdir(parents=True, exist_ok=True)
    registry = active_registry(load_modules(data_dir))
    with CatalogStore.open(data_dir, create=True) as store:
        store.connection.execute(
            "INSERT OR IGNORE INTO policy(name,payload) VALUES('catalog',?)",
            (_json(_policy_to_dict(model.Policy())),),
        )
        store.connection.commit()
        observed = data_dir / OBSERVED_DB
        if not observed.exists():
            with _connect(observed) as connection:
                _observed_schema(connection)
        catalog = store.read_catalog(registry=registry)
        return {
            "data_dir": str(data_dir),
            "catalog": CATALOG_DB,
            "observed": OBSERVED_DB,
            "schema_version": FORMAT_VERSION,
            "revision": store.revision,
            "empty": not any(catalog.all(kind) for kind in registry.kinds),
        }


def startup_check(data_dir: Path) -> dict[str, Any]:
    """Validate runtime storage before a listener is exposed."""
    from cadastre.core.lifecycle import degraded, ready

    with RuntimeStore.open(data_dir, read_only=True) as runtime:
        result = integrity(data_dir)
        result["revisions"] = runtime.revisions()
        source_rows = runtime.observed.execute(
            "SELECT ok FROM sources ORDER BY source"
        ).fetchall()
        result["observed_freshness"] = {
            "sources": len(source_rows),
            "failed_sources": sum(not bool(row[0]) for row in source_rows),
        }
    usage = shutil.disk_usage(data_dir)
    if usage.free <= 0:
        raise UsageError("data directory has no free space")
    import os

    if not data_dir.is_dir() or not os.access(data_dir, os.W_OK):
        raise UsageError("data directory is not writable")
    result["free_bytes"] = usage.free
    result["application_version"] = __import__("cadastre").__version__
    result.pop("data_dir", None)
    health = (
        degraded(result)
        if result["observed_freshness"]["failed_sources"]
        else ready(data_dir, result)
    )
    return {**result, "lifecycle": health.to_dict()}


def _observed_schema(
    connection: sqlite3.Connection, *, read_only: bool = False
) -> None:
    if read_only:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='format_version'"
        ).fetchone()
        if row is None:
            raise UsageError("observed database has no storage metadata")
        version = int(row[0])
        if version > FORMAT_VERSION:
            raise UsageError(
                f"observed schema {version} is newer than this application supports "
                f"({FORMAT_VERSION})"
            )
        return
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sources(
      source TEXT PRIMARY KEY, payload TEXT NOT NULL, as_of TEXT NOT NULL,
      ok INTEGER NOT NULL, error TEXT
    );
    CREATE TABLE IF NOT EXISTS observations(
      source TEXT NOT NULL, as_of TEXT NOT NULL, kind TEXT NOT NULL,
      ident TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
      PRIMARY KEY(source,as_of,kind,ident,digest)
    );
    CREATE INDEX IF NOT EXISTS observations_entity
      ON observations(source,kind,ident,as_of);
    CREATE TABLE IF NOT EXISTS trust_records(
      record_key TEXT PRIMARY KEY, payload TEXT NOT NULL
    );
    INSERT OR IGNORE INTO metadata(key,value) VALUES
      ('format_version','1'), ('revision','0');
    """)


def open_observed(data_dir: Path, *, create: bool = True) -> sqlite3.Connection:
    path = data_dir / OBSERVED_DB
    if not path.exists() and not create:
        raise UsageError(f"no observed database at {path}")
    connection = _connect(path)
    _observed_schema(connection)
    connection.commit()
    return connection


def record_observed(data_dir: Path, payload: dict[str, Any]) -> None:
    """Atomically update a source and append entity-level observation history."""
    source = str(payload.get("source", "unknown"))
    as_of = str(payload.get("as_of", ""))
    encoded = _json(payload)
    with open_observed(data_dir) as connection:
        connection.execute(
            "INSERT INTO sources(source,payload,as_of,ok,error) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET payload=excluded.payload, "
            "as_of=excluded.as_of, ok=excluded.ok, error=excluded.error",
            (
                source,
                encoded,
                as_of,
                int(bool(payload.get("ok", True))),
                payload.get("error"),
            ),
        )
        for kind, values in sorted((payload.get("entities") or {}).items()):
            for value in sorted(values, key=lambda item: str(item.get("id", ""))):
                item_json = _json(value)
                item_digest = hashlib.sha256(item_json.encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO observations(source,as_of,kind,ident,"
                    "payload,digest) VALUES(?,?,?,?,?,?)",
                    (
                        source,
                        as_of,
                        kind,
                        str(value.get("id", "")),
                        item_json,
                        item_digest,
                    ),
                )
        _increment_observed_revision(connection)
        connection.commit()


def observed_payloads(data_dir: Path) -> tuple[dict[str, Any], ...]:
    with open_observed(data_dir) as connection:
        return tuple(
            _decode(row["payload"])
            for row in connection.execute("SELECT payload FROM sources ORDER BY source")
        )


def refuse_if_module_disabled(
    store: CatalogStore, registry: EntityRegistry, action: str
) -> None:
    """Refuse a mutating action when the database holds rows a disabled
    module owns (MANIFEST.md F03). Base read queries stay available; this
    guard sits only on the write/export/import paths, not on `read_catalog`
    itself."""
    missing = sorted(
        module
        for module in store.required_modules
        if not registry.modules.enabled(module)
    )
    if missing:
        raise UsageError(
            f"REFUSED {action}\n  This database holds rows owned by "
            f"{', '.join(missing)}, which {'is' if len(missing) == 1 else 'are'} "
            "not enabled here.\n  Enable it in modules.yaml before writing, "
            f"exporting, or importing: modules:\n    {missing[0]}:\n      "
            "enabled: true"
        )


def load_declared(
    root: Path,
    data_dir: Path,
    *,
    principal: str = "import",
    reason: str = "declared/ reload",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reload a `declared/` file tree into an already-initialized catalog.

    The missing half of the round trip. `export` produces a bundle and
    `import` consumes one, but nothing put a corrected YAML tree back into a
    live catalog — and the fields most likely to be wrong on a first run are
    exactly the ones the write gate refuses, because a source owns them. An
    operator who mistyped a secret's `store` had no in-band fix at all: the
    only route was export, hand-edit the JSON, recompute the manifest SHA-256
    by hand, then import.

    ANNOTATIONS SURVIVE. The underlying transaction replaces the catalog
    wholesale, and annotations are catalog-owned edits made through the write
    gate that generally have no representation in `declared/` — reloading
    would otherwise quietly delete every one of them. Any annotation the
    incoming tree does not itself define is carried across; where both define
    the same target, the tree wins, since that is the explicit statement.
    """
    import dataclasses

    from cadastre.core.loader import load_catalog
    from cadastre.modules.config import load_modules

    registry = active_registry(load_modules(root))
    catalog = load_catalog(root, registry=registry)
    with CatalogStore.open(data_dir, read_only=dry_run) as store:
        current = store.read_catalog(registry=registry)
        preserved = {
            key: value
            for key, value in current.annotations.items()
            if key not in catalog.annotations
        }
        merged = dict(preserved)
        merged.update(catalog.annotations)
        catalog = dataclasses.replace(catalog, annotations=merged)

        before = {
            (kind, entity.id): entity_to_dict(entity, registry=registry)
            for kind in registry.kinds
            for entity in current.all(kind)
        }
        after = {
            (kind, entity.id): entity_to_dict(entity, registry=registry)
            for kind in registry.kinds
            for entity in catalog.all(kind)
        }
        changed = tuple(
            sorted(
                key
                for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            )
        )
        summary: dict[str, Any] = {
            "data_dir": str(data_dir),
            "catalog": str(root),
            "added": sorted(f"{k}:{i}" for k, i in set(after) - set(before)),
            "removed": sorted(f"{k}:{i}" for k, i in set(before) - set(after)),
            "updated": sorted(
                f"{k}:{i}"
                for k, i in (set(before) & set(after))
                if before[(k, i)] != after[(k, i)]
            ),
            "annotations_preserved": sorted(f"{k}:{i}" for k, i in preserved),
            "dry_run": dry_run,
        }
        if dry_run:
            summary["revision"] = store.revision
            return summary
        # Declared only. `import_legacy` also ingests `<root>/observed/*.json`
        # because it seeds a fresh database from a whole file-tree catalog;
        # doing that here would merge a checkout's stale evidence files into a
        # live observed database that collectors own, resurrecting sources
        # that were retired and inflating every `undeclared` count. Evidence
        # comes from `cadastre collect`, never from a reload of intent.
        result = store.apply_catalog_transaction(
            catalog,
            principal=principal,
            reason=reason,
            operation="import",
            changed=changed,
        )
    summary["revision"] = result.database_revision
    summary["transaction_id"] = result.transaction_id
    summary["audit_id"] = result.audit_id
    return summary


def import_legacy(root: Path, data_dir: Path) -> dict[str, Any]:
    """Explicitly import a file-tree catalog into an initialized database."""
    from cadastre.core.loader import load_catalog
    from cadastre.modules.config import load_modules

    registry = active_registry(load_modules(root))
    catalog = load_catalog(root, registry=registry)
    with CatalogStore.open(data_dir, create=True) as store:
        result = store.apply_catalog_transaction(
            catalog,
            principal="import",
            reason="explicit legacy catalog import",
            operation="import",
            changed=tuple(
                (kind, entity.id)
                for kind in registry.kinds
                for entity in catalog.all(kind)
            ),
            observed=tuple(
                json.loads(path.read_text(encoding="utf-8"))
                for path in (
                    sorted((root / "observed").glob("*.json"))
                    if (root / "observed").is_dir()
                    else ()
                )
            ),
        )
    return {
        "revision": result.database_revision,
        "transaction_id": result.transaction_id,
        "audit_id": result.audit_id,
    }


def _metadata_list(store: CatalogStore, name: str) -> list[dict[str, Any]]:
    row = store.connection.execute(
        "SELECT value FROM metadata WHERE key=?", (name,)
    ).fetchone()
    if row is None:
        return []
    raw = _decode(row[0])
    return (
        [dict(item) for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else []
    )


def _observed_history(data_dir: Path) -> tuple[dict[str, Any], ...]:
    with open_observed(data_dir) as connection:
        return tuple(
            dict(row)
            for row in connection.execute(
                "SELECT source,as_of,kind,ident,payload,digest "
                "FROM observations ORDER BY source,as_of,kind,ident,digest"
            )
        )


def observed_history(data_dir: Path) -> tuple[dict[str, Any], ...]:
    """Read canonical observed entity history from the runtime database."""
    return _observed_history(data_dir)


def write_trust_records(data_dir: Path, records: tuple[dict[str, Any], ...]) -> None:
    with open_observed(data_dir) as connection:
        connection.execute("DELETE FROM trust_records")
        for record in records:
            key = _json(
                [
                    record.get("kind"),
                    record.get("id"),
                    record.get("field", ""),
                    record.get("source"),
                ]
            )
            connection.execute(
                "INSERT OR REPLACE INTO trust_records(record_key,payload) VALUES(?,?)",
                (key, _json(record)),
            )
        connection.commit()


def read_trust_records(data_dir: Path) -> tuple[dict[str, Any], ...]:
    with open_observed(data_dir) as connection:
        return tuple(
            _decode(row[0])
            for row in connection.execute(
                "SELECT payload FROM trust_records ORDER BY record_key"
            )
        )


def export_bundle(
    data_dir: Path, destination: Path, *, include_observed: bool = True
) -> dict[str, Any]:
    """Write a deterministic, checksummed interchange bundle."""
    from cadastre.modules.config import load_modules

    registry = active_registry(load_modules(data_dir))
    with CatalogStore.open(data_dir, read_only=True) as store:
        refuse_if_module_disabled(store, registry, "export")
        catalog = store.read_catalog(registry=registry)
        payload: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "source_database_revision": store.revision,
            "exported_at": store.declared_as_of,
            "declared": {
                kind: [
                    entity_to_dict(entity, registry=registry)
                    for entity in catalog.all(kind)
                ]
                for kind in registry.kinds
                if catalog.all(kind)
            },
            "policy": _policy_to_dict(catalog.policy),
            "annotations": [value for _, value in sorted(catalog.annotations.items())],
            "resolutions": _metadata_list(store, "resolutions"),
            "acknowledgements": _metadata_list(store, "acknowledgements"),
        }
        if include_observed:
            payload["observed_current"] = list(observed_payloads(data_dir))
            payload["observed_history"] = list(_observed_history(data_dir))
    destination.mkdir(parents=True, exist_ok=False)
    checksums: dict[str, str] = {}
    for name, value in sorted(payload.items()):
        content = json.dumps(value, indent=2, sort_keys=True) + "\n"
        path = destination / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        checksums[path.name] = hashlib.sha256(content.encode()).hexdigest()
    manifest = {
        "format_version": FORMAT_VERSION,
        "source_database_revision": payload["source_database_revision"],
        "exported_at": payload["exported_at"],
        "checksums": checksums,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def import_bundle(
    data_dir: Path, source: Path, *, mode: str = "replace"
) -> dict[str, Any]:
    from cadastre.modules.config import load_modules

    registry = active_registry(load_modules(data_dir))
    if mode not in {"replace", "merge", "dry-run"}:
        raise UsageError("import mode must be replace, merge, or dry-run")
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        # Name what a bundle IS. The obvious guess is a `declared/` tree — the
        # thing an operator actually has — and "manifest.json is required"
        # gives them no way to learn otherwise.
        raise UsageError(
            f"no bundle at {source}: expected a directory produced by "
            "`cadastre export` (it contains manifest.json, declared.json and "
            "observed_current.json). A `declared/` YAML tree is not a bundle."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise UsageError("unknown required bundle format version")
    for name, digest in manifest.get("checksums", {}).items():
        path = source / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise UsageError(f"bundle checksum validation failed for {name}")
    declared_file = source / "declared.json"
    declared = (
        json.loads(declared_file.read_text(encoding="utf-8"))
        if declared_file.exists()
        else {}
    )
    if not isinstance(declared, dict):
        raise UsageError("bundle declared payload must be an object")

    def contains_secret(value: Any, key: str = "") -> bool:
        lowered = key.lower()
        if any(
            word in lowered
            for word in (
                "password",
                "token",
                "credential",
                "private_key",
                "secret_value",
            )
        ):
            return True
        if isinstance(value, dict):
            return any(
                contains_secret(child, str(child_key))
                for child_key, child in value.items()
            )
        if isinstance(value, list):
            return any(contains_secret(child, key) for child in value)
        return False

    payload_files = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(source.glob("*.json"))
        if path.name != "manifest.json"
    }
    if any(contains_secret(value) for value in payload_files.values()):
        raise UsageError("bundle contains secret values")
    issues = IssueCollector()
    entities: dict[str, dict[str, model.Entity]] = {kind: {} for kind in registry.kinds}
    for kind, values in sorted(declared.items()):
        if kind not in registry.kinds or not isinstance(values, list):
            raise UsageError(f"invalid declared bundle section: {kind}")
        for value in values:
            entity = parse_entity(
                registry.specs[kind],
                value,
                Located(f"bundle:declared:{kind}"),
                issues,
            )
            if entity is not None:
                if entity.id in entities[kind]:
                    raise UsageError(
                        f"duplicate identity in bundle: {kind}:{entity.id}"
                    )
                entities[kind][entity.id] = entity
    issues.raise_if_any()
    annotations = (
        json.loads((source / "annotations.json").read_text())
        if (source / "annotations.json").exists()
        else []
    )
    for item in annotations:
        item_kind = item.get("kind") if isinstance(item, dict) else None
        item_id = item.get("id") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item_kind not in registry.kinds
            or not isinstance(item_id, str)
            or item_id not in entities[item_kind]
        ):
            raise UsageError("bundle contains an invalid or orphaned annotation")
        values = item.get("values", {})
        if not isinstance(values, dict) or set(values) - {"tags", "notes"}:
            raise UsageError("bundle contains an invalid annotation")
    observed_payload = payload_files.get("observed_current", [])
    if not isinstance(observed_payload, list):
        raise UsageError("bundle observed_current payload must be a list")
    for item in observed_payload:
        if not isinstance(item, dict):
            raise UsageError("bundle observed_current contains a non-object")
        from cadastre.core.observed import parse_source

        parse_source(item, Located("bundle:observed_current"))
    observed_history = payload_files.get("observed_history", [])
    if not isinstance(observed_history, list):
        raise UsageError("bundle observed_history payload must be a list")
    required_history = {"source", "as_of", "kind", "ident", "payload", "digest"}
    for item in observed_history:
        if not isinstance(item, dict) or not required_history.issubset(item):
            raise UsageError("bundle observed_history row is incomplete")
    catalog = Catalog(
        root=data_dir,
        entities=entities,
        policy=_policy_from_dict(
            json.loads((source / "policy.json").read_text())
            if (source / "policy.json").exists()
            else {}
        ),
        annotations={(item["kind"], item["id"]): item for item in annotations},
        declared_entities=entities,
        registry=registry,
    )
    from cadastre.core.rules import check_catalog

    errors = [finding for finding in check_catalog(catalog) if finding.level == "error"]
    if errors:
        raise UsageError(
            "bundle catalog check failed: "
            + "; ".join(f"{item.code}: {item.message}" for item in errors)
        )
    if mode == "dry-run":
        return {
            "mode": mode,
            "entities": sum(len(values) for values in entities.values()),
            "changed": False,
        }
    with CatalogStore.open(data_dir, create=True) as store:
        refuse_if_module_disabled(store, registry, "import")
        if mode == "merge":
            current = store.read_catalog(registry=registry)
            merged = {kind: dict(current.of(kind)) for kind in registry.kinds}
            for kind, values in entities.items():
                merged[kind].update(values)
            catalog = Catalog(
                root=data_dir,
                entities=merged,
                policy=catalog.policy,
                annotations=catalog.annotations,
                declared_entities=merged,
                registry=registry,
            )
        result = store.apply_catalog_transaction(
            catalog,
            principal="import",
            reason=f"bundle import ({mode})",
            operation="import",
            changed=tuple(
                (kind, entity.id)
                for kind in registry.kinds
                for entity in catalog.all(kind)
            ),
            observed=tuple(observed_payload),
            observed_history=tuple(observed_history),
            metadata={
                name: payload_files.get(name, [])
                for name in ("resolutions", "acknowledgements")
            },
        )
    return {
        "mode": mode,
        "revision": result.database_revision,
        "transaction_id": result.transaction_id,
        "audit_id": result.audit_id,
        "changed": True,
    }


def integrity(data_dir: Path) -> dict[str, Any]:
    results = {}
    for name in (CATALOG_DB, OBSERVED_DB):
        path = data_dir / name
        if not path.exists():
            raise UsageError(f"missing database: {path}")
        with _connect(path, read_only=True) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if result != "ok":
                raise UsageError(f"SQLite integrity check failed for {name}: {result}")
            results[name] = {"integrity": result, "bytes": path.stat().st_size}
    return {
        "data_dir": str(data_dir),
        "schema_version": FORMAT_VERSION,
        "databases": results,
    }


def backup(data_dir: Path, destination: Path) -> dict[str, Any]:
    integrity(data_dir)
    destination.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    revisions: dict[str, int] = {}
    lock_path = data_dir / ".cadastre-storage.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            for name in (CATALOG_DB, OBSERVED_DB):
                source = _connect(data_dir / name, read_only=True)
                target_path = destination / name
                target = sqlite3.connect(target_path)
                try:
                    source.backup(target)
                    target.commit()
                    if name == CATALOG_DB:
                        revisions[name] = int(
                            source.execute(
                                "SELECT value FROM metadata WHERE key='revision'"
                            ).fetchone()[0]
                        )
                    else:
                        revisions[name] = int(
                            source.execute(
                                "SELECT value FROM metadata WHERE key='revision'"
                            ).fetchone()[0]
                        )
                finally:
                    target.close()
                    source.close()
                files[name] = hashlib.sha256(target_path.read_bytes()).hexdigest()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    manifest = {
        "format_version": FORMAT_VERSION,
        "schema_version": FORMAT_VERSION,
        "application_version": __import__("cadastre").__version__,
        "backup_at": _utc_now(),
        "files": files,
        "revisions": revisions,
    }
    manifest_path = destination / ".manifest.tmp"
    with manifest_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        handle.flush()
        import os

        os.fsync(handle.fileno())
    manifest_path.replace(destination / "manifest.json")
    return manifest


def restore(data_dir: Path, source: Path) -> dict[str, Any]:
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise UsageError("backup manifest.json is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {CATALOG_DB, OBSERVED_DB}
    files = manifest.get("files", {})
    if set(files) != expected:
        raise UsageError("backup manifest must contain both runtime databases")
    for name, digest in files.items():
        path = source / name
        if (
            name not in (CATALOG_DB, OBSERVED_DB)
            or not path.exists()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise UsageError(f"backup checksum validation failed for {name}")
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in (CATALOG_DB, OBSERVED_DB):
        temp = data_dir / f".{name}.restore"
        shutil.copy2(source / name, temp)
        temp.replace(data_dir / name)
    result = integrity(data_dir)
    result["restored_from"] = str(source)
    return result
