"""`cadastre collect` — run the configured collectors, write `observed/`.

Runs on the collector host, from cron / CI / a systemd timer. Cadastre does not
daemonize (DESIGN §2.2), so this is a process that starts, collects, and exits.

The failure behaviour is the point: when a plugin cannot be reached, the
previous evidence is **kept and marked stale**, not deleted. Deleting it would
render as "nothing there", which reads as a fact rather than as an absence.
"""

from __future__ import annotations

import json
from typing import Any

from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import Located
from cadastre.core.observed import ObservedSource, parse_source, write_source
from cadastre.core.observed_db import record_source, sync_snapshots
from cadastre.core.provenance import format_timestamp
from cadastre.core.trust import update_records, write_records
from cadastre.plugins import PluginRegistry, runner
from cadastre.plugins.config import SourceConfig
from cadastre.plugins.contract import parse_plugin_info
from cadastre.render.document import Bullets, Document, Section, Table

#: Methods a source implements but does not declare are not called. Collection
#: is explicit: a plugin gaining a capability does not silently widen what
#: Cadastre asks it for.
DEFAULT_METHODS = ("inventory.list", "network.list", "endpoint.list")


def _merge(
    into: dict[str, list[model.Entity]], new: dict[str, list[model.Entity]]
) -> None:
    for kind, entities in new.items():
        seen = {e.id for e in into.get(kind, [])}
        into.setdefault(kind, []).extend(e for e in entities if e.id not in seen)


def _parse_reported_coverage(
    raw: Any, where: str, registry: Any = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate the `coverage` block a collector reported for itself.

    Rejected rather than trusted blindly: coverage SHRINKS what a source is
    allowed to claim absence about, so a malformed one that silently parsed as
    empty would restore exactly the over-claiming it exists to prevent. Bad
    input is dropped with a warning naming the source and method.
    """
    if raw is None:
        return {}, []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {}, [f"{where}: coverage is not an object; ignored"]
    specs = getattr(registry, "specs", None)
    scopes: dict[str, dict[str, Any]] = {}
    for kind, scope in raw.items():
        label = f"{where}: coverage.{kind}"
        if specs is not None and kind not in specs:
            warnings.append(f"{label}: unknown kind; ignored")
            continue
        if not isinstance(scope, dict):
            warnings.append(f"{label}: not an object; ignored")
            continue
        if any(key not in {"ids", "where"} for key in scope):
            warnings.append(f"{label}: only 'ids' and 'where' are allowed; ignored")
            continue
        ids = scope.get("ids")
        if ids is not None and (
            not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
        ):
            warnings.append(f"{label}.ids: not a list of strings; ignored")
            continue
        clause = scope.get("where")
        if clause is not None and not isinstance(clause, dict):
            warnings.append(f"{label}.where: not an object; ignored")
            continue
        if specs is not None and isinstance(clause, dict):
            known = {field.key for field in specs[kind].fields}
            unknown = sorted(str(k) for k in clause if k not in known)
            if unknown:
                warnings.append(
                    f"{label}.where: unknown fields: {', '.join(unknown)}; ignored"
                )
                continue
        scopes[str(kind)] = dict(scope)
    return scopes, warnings


def collect_source(
    session: Session, config: SourceConfig
) -> tuple[ObservedSource, list[str]]:
    """Collect one source. Returns the new evidence and any warnings."""
    previous = session.observed_source(config.id)
    methods = config.methods or DEFAULT_METHODS
    entities: dict[str, list[model.Entity]] = {}
    extra: dict[str, Any] = {}
    warnings: list[str] = []
    failures: list[str] = []
    as_of: str | None = None
    capabilities: list[str] = []
    extensions: dict[str, set[str]] = {}
    reported_coverage: dict[str, dict[str, Any]] = {}
    plugin_info = runner.info(config)
    observed_plugin = config.plugin
    if plugin_info.ok and plugin_info.reply is not None:
        try:
            info = parse_plugin_info(plugin_info.reply.result)
            # The collector's signed-by-protocol identity, rather than the
            # operator's local source label, is what drift needs to select the
            # correct identity function.  Only adopt registered declarations:
            # an arbitrary executable must not be able to impersonate one.
            if PluginRegistry.discover(session.root).get(info.name) is not None:
                observed_plugin = info.name
            elif config.plugin != config.id:
                warnings.append(
                    f"{config.id}: plugin.info named unregistered plugin "
                    f"{info.name!r}; "
                    f"using configured plugin {config.plugin!r}"
                )
            else:
                warnings.append(
                    f"{config.id}: plugin {info.name!r} is not registered; "
                    "drift can only use literal ids until its declaration is installed"
                )
            registered = PluginRegistry.discover(session.root).get(info.name)
            extensions = {
                declaration.kind: set(declaration.attributes.get("properties", {}))
                for declaration in (registered.info.entities if registered else ())
                if isinstance(declaration.attributes.get("properties", {}), dict)
            }
        except ValueError as exc:
            warnings.append(f"{config.id}: invalid plugin.info: {exc}")
    else:
        warnings.append(f"{config.id}: plugin.info unavailable; extensions rejected")

    for method in methods:
        outcome = runner.call(config, method)
        if not outcome.ok or outcome.reply is None:
            failures.append(f"{method}: {outcome.message}")
            continue
        reply = outcome.reply
        capabilities.append(method)
        warnings.extend(f"{method}: {w}" for w in reply.warnings)
        payload = reply.result
        raw_entities = payload.get("entities") or {}
        if isinstance(raw_entities, dict):
            parsed = parse_source(
                {"entities": raw_entities},
                Located(f"{config.id}:{method}"),
                extensions=extensions,
                registry=session.registry,
            )
            _merge(entities, parsed.entities)
        if isinstance(payload.get("extra"), dict):
            extra.update(payload["extra"])
        # A source's own account of what it can see. `plugin.info` cannot
        # carry this: its declaration is per-PLUGIN, while three sources may
        # share one plugin with different projects, zones or orgs. The method
        # reply is the only per-source channel that sees `config`.
        scopes, coverage_warnings = _parse_reported_coverage(
            payload.get("coverage"), f"{config.id}:{method}", session.registry
        )
        warnings.extend(coverage_warnings)
        reported_coverage.update(scopes)
        if reply.as_of and (as_of is None or reply.as_of > as_of):
            as_of = reply.as_of

    if failures and not capabilities:
        # Nothing came back at all. Keep what we had, marked stale.
        return (
            ObservedSource(
                source=config.id,
                plugin=previous.plugin if previous else observed_plugin,
                as_of=previous.as_of if previous else format_timestamp(session.now),
                capabilities=previous.capabilities if previous else (),
                entities=previous.entities if previous else {},
                ok=False,
                error="; ".join(failures),
                extra=previous.extra if previous else {},
                coverage=previous.coverage if previous else config.coverage,
                extensions=previous.extensions if previous else {},
                registry=session.registry,
            ),
            warnings,
        )

    if capabilities and not any(entities.values()):
        plugin = PluginRegistry.discover(session.root).get(config.plugin)
        unexpected_empty = bool(
            plugin
            and plugin.info.entities
            and any(
                not declaration.empty_expected for declaration in plugin.info.entities
            )
        )
        if unexpected_empty:
            warnings.append(
                f"{config.id}: returned zero entities although its plugin declares "
                "that an empty result is unexpected; previous evidence was not used "
                "to infer absence"
            )
            # An unexpected empty result is not evidence that the estate is
            # empty. Preserve the last known set and mark this source stale so
            # neither drift nor a query can silently treat a broken response
            # as absence.
            return (
                ObservedSource(
                    source=config.id,
                    plugin=previous.plugin if previous else observed_plugin,
                    as_of=previous.as_of if previous else format_timestamp(session.now),
                    capabilities=(
                        previous.capabilities if previous else tuple(capabilities)
                    ),
                    entities=previous.entities if previous else {},
                    ok=False,
                    error="unexpected empty result",
                    extra=previous.extra if previous else extra,
                    coverage=previous.coverage if previous else config.coverage,
                    extensions=previous.extensions if previous else {},
                    registry=session.registry,
                ),
                warnings,
            )

    return (
        ObservedSource(
            source=config.id,
            plugin=observed_plugin,
            as_of=as_of or format_timestamp(session.now),
            capabilities=tuple(capabilities),
            entities=entities,
            ok=not failures,
            error="; ".join(failures) if failures else None,
            extra=extra,
            # The collector's own account of its scope is the default; an
            # explicit `coverage:` in plugins.yaml overrides it per kind, so an
            # operator can always narrow further than the plugin claims.
            coverage={**reported_coverage, **config.coverage},
            extensions={
                kind: tuple(sorted(names)) for kind, names in extensions.items()
            },
            registry=session.registry,
        ),
        warnings,
    )


def collect(
    session: Session,
    *,
    sources: list[str] | None = None,
    dry_run: bool = False,
) -> Document:
    wanted = set(sources) if sources else None
    # A catalog may have been initialized from checked-in snapshots before its
    # first collection.  Seed the cache once, without treating that as a new
    # observation.  This keeps the JSON files as a lossless interchange format
    # while making all subsequent queries use SQLite.
    if not dry_run:
        sync_snapshots(session.root)
    rows = []
    warnings: list[str] = []
    written: list[str] = []
    data: list[dict[str, Any]] = []

    for config in session.plugins.sources:
        if wanted and config.id not in wanted:
            continue
        if not config.enabled:
            rows.append((config.id, "skipped", "disabled in plugins.yaml", ""))
            continue
        observed, source_warnings = collect_source(session, config)
        warnings.extend(source_warnings)
        counts = ", ".join(
            f"{len(v)} {k}" for k, v in sorted(observed.entities.items()) if v
        )
        state = "ok" if observed.ok else "STALE"
        rows.append((config.id, state, observed.error or "", counts))
        data.append(
            {
                "id": config.id,
                "ok": observed.ok,
                "as_of": observed.as_of,
                "error": observed.error,
                "counts": {k: len(v) for k, v in sorted(observed.entities.items())},
            }
        )
        if not dry_run:
            if (session.root / "catalog.sqlite3").exists():
                path = record_source(session.root, observed)
            else:
                path = write_source(session.root, observed)
                # The legacy path is an explicit test/interchange fixture. Its
                # rebuildable cache still needs the same current-source update.
                record_source(session.root, observed)
            written.append(str(path.relative_to(session.root)))
            records = update_records(
                session.root, session.catalog, observed, session.now
            )
            if records:
                write_records(session.root, records)

    sections = [
        Section(
            "Sources",
            (
                Table(
                    ("source", "state", "error", "collected"),
                    tuple(rows),
                    empty_note="(no sources configured)",
                ),
            ),
            note=(
                "A STALE source kept its previous evidence. It did not lose it, and "
                "it is not empty — every answer that uses it will say so."
            ),
        )
    ]
    if warnings:
        sections.append(Section("Plugin warnings", (Bullets(tuple(warnings)),)))
    if written:
        sections.append(Section("Written", (Bullets(tuple(sorted(written))),)))
    elif dry_run:
        sections.append(
            Section("Written", (), note="Dry run: observed/ was not touched.")
        )

    current = {source.source: source for source in session.observed}
    if (session.root / "catalog.sqlite3").exists() and not dry_run:
        # Runtime collection writes SQLite, so the returned path is a database,
        # not a JSON snapshot that can be parsed here.
        from cadastre.core.storage import observed_payloads

        for payload in observed_payloads(session.root):
            source = parse_source(
                payload, Located("observed.sqlite3"), registry=session.registry
            )
            current[source.source] = source
    else:
        for written_path in written:
            source = parse_source(
                json.loads((session.root / written_path).read_text(encoding="utf-8")),
                Located(written_path),
                registry=session.registry,
            )
            current[source.source] = source
    # The session was opened before collection.  Provenance must describe the
    # evidence that remains after this run, or a repaired stale source would
    # still print the old stale banner.
    from cadastre.core.provenance import ProvenanceSet, declared_provenance

    provenance = ProvenanceSet()
    provenance.add(declared_provenance(session.declared_as_of))
    for source in current.values():
        provenance.add(source.provenance(ttl_overrides=session.plugins.freshness))

    return Document(
        title="cadastre collect",
        sections=tuple(sections),
        provenance=provenance.frozen(),
        data={"sources": data, "written": sorted(written), "warnings": warnings},
    )
