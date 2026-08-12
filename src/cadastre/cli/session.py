"""One loaded view of a catalog root, shared by every command.

Commands take a `Session` and return a `Document`. Neither reads a clock nor
touches the filesystem, which is what makes every command testable against a
fixture directory and a fixed `now`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cadastre.core.catalog import Catalog
from cadastre.core.loader import declared_as_of, load_catalog
from cadastre.core.observed import ObservedSource, load_observed
from cadastre.core.provenance import Provenance, ProvenanceSet, declared_provenance
from cadastre.modules.config import ModulesFile, load_modules
from cadastre.modules.registry import EntityRegistry, active_registry
from cadastre.plugins.config import PluginsFile, load_plugins


@dataclass(frozen=True)
class Session:
    root: Path
    catalog: Catalog
    observed: tuple[ObservedSource, ...]
    plugins: PluginsFile
    now: datetime
    declared_as_of: str
    modules: ModulesFile
    registry: EntityRegistry

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        now: datetime | None = None,
        as_of: str | None = None,
        runtime: bool = False,
    ) -> Session:
        """Load a catalog root.

        `now` fixes the clock staleness is judged against, and `as_of` fixes the
        catalog's own age. Both exist so golden-file tests can assert byte
        identity: everything else in the output is a function of content alone,
        and these two are functions of the filesystem.
        """
        moment = now or datetime.now(tz=UTC)
        modules = load_modules(root)
        registry = active_registry(modules)
        # A live installation is SQLite-backed.  The explicit catalog argument
        # remains the interchange/test fixture path and is never silently
        # converted into a runtime store.
        if (root / "catalog.sqlite3").exists():
            from cadastre.core.storage import CatalogStore

            with CatalogStore.open(root, read_only=True) as store:
                catalog = store.read_catalog(registry=registry)
                declared_stamp = store.declared_as_of
            plugins = load_plugins(root)
            return cls(
                root=root,
                catalog=catalog,
                # Use the same freshness evaluator for SQLite-backed and
                # file-backed catalogs.  Reconstructing sources here used to
                # preserve collector status but ignored their age, allowing
                # HTTP and MCP answers to disagree with the CLI after cache
                # initialization.
                #
                # `registry` is not optional here. Omitting it parsed observed
                # evidence against the BASE registry, so a module-owned kind a
                # collector had just written back was rejected as an unknown
                # kind — failing every command that opens a session, base ones
                # included, on the live SQLite path this branch exists to serve.
                observed=tuple(
                    load_observed(
                        root,
                        now=moment,
                        ttl_overrides=plugins.freshness,
                        registry=registry,
                    )
                ),
                plugins=plugins,
                now=moment,
                declared_as_of=as_of or declared_stamp,
                modules=modules,
                registry=registry,
            )
        if runtime:
            from cadastre.core.errors import UsageError

            raise UsageError(
                "runtime sessions require an initialized SQLite data directory; "
                "import a bundle with `cadastre init --from-bundle` first"
            )
        plugins = load_plugins(root)
        return cls(
            root=root,
            catalog=load_catalog(root, registry=registry),
            observed=tuple(
                load_observed(
                    root,
                    now=moment,
                    ttl_overrides=plugins.freshness,
                    registry=registry,
                )
            ),
            plugins=plugins,
            now=moment,
            declared_as_of=as_of or declared_as_of(root),
            modules=modules,
            registry=registry,
        )

    @classmethod
    def open_fixture(
        cls,
        root: Path,
        *,
        now: datetime | None = None,
        as_of: str | None = None,
    ) -> Session:
        return cls.open(root, now=now, as_of=as_of, runtime=False)

    def provenance(self, *sources: str) -> tuple[Provenance, ...]:
        """Provenance for an answer. `declared` is always included — every
        answer rests on it — plus each observed source that was consulted, or
        all of them when the caller names none."""
        collected = ProvenanceSet()
        collected.add(declared_provenance(self.declared_as_of))
        wanted = set(sources)
        for source in self.observed:
            if not wanted or source.source in wanted:
                collected.add(source.provenance(ttl_overrides=self.plugins.freshness))
        return collected.frozen()

    def observed_source(self, name: str) -> ObservedSource | None:
        for source in self.observed:
            if source.source == name:
                return source
        return None
