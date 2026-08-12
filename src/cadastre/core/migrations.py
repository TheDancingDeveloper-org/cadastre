"""Forward-only SQLite format migration registry."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from cadastre.core.errors import UsageError

CURRENT_SCHEMA = 1
Migration = Callable[[sqlite3.Connection], None]


def _bootstrap(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migrations (
          version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migration_lock (
          id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL
        );
        """
    )


MIGRATIONS: tuple[tuple[int, Migration], ...] = ((1, _bootstrap),)


def apply(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Apply missing migrations in order, refusing newer formats."""
    _bootstrap(connection)
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='format_version'"
    ).fetchone()
    version = int(row[0]) if row else 0
    if version > CURRENT_SCHEMA:
        raise UsageError(
            f"catalog schema {version} is newer than this application supports "
            f"({CURRENT_SCHEMA})"
        )
    applied: list[int] = []
    for number, migration in MIGRATIONS:
        if number <= version:
            continue
        migration(connection)
        connection.execute(
            "INSERT OR IGNORE INTO migrations(version, applied_at) "
            "VALUES(?, 'migration')",
            (number,),
        )
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('format_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(number),),
        )
        applied.append(number)
        version = number
    connection.commit()
    return tuple(applied)
