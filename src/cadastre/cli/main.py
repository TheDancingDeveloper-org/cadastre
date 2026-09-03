"""The CLI. Every surface — MCP, CI, humans — consumes these commands.

Exit codes are part of the contract:

* ``0`` the command answered.
* ``1`` the command answered and found something the caller asked to be told
  about with a non-zero exit (`check` failures, `drift --exit-code`,
  `collect --exit-code`).
* ``2`` the invocation or the catalog was wrong. Never a traceback.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre import __version__
from cadastre.core import model
from cadastre.core.errors import CadastreError, CatalogError, UsageError
from cadastre.core.provenance import parse_timestamp
from cadastre.render import json_out, text
from cadastre.render.document import Document

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

CATALOG_ENV = "CADASTRE_CATALOG"
DATA_DIR_ENV = "CADASTRE_DATA_DIR"


def build_parser(*, include_manifest: bool = False) -> argparse.ArgumentParser:
    from cadastre.cli.question import QUESTION_IDS

    parser = argparse.ArgumentParser(
        prog="cadastre",
        description=(
            "A map of an estate, and the policy for choosing within it. "
            "Cadastre runs nothing and changes nothing."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"cadastre {__version__}"
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help=f"Catalog root (default: ${CATALOG_ENV}, else the current directory).",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="Initialized SQLite data directory."
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument(
        "--now",
        default=None,
        # Test seam: fixes the clock that staleness is judged against.
        help=argparse.SUPPRESS,
    )
    # The same flags after the subcommand, because `cadastre brief --json` is what
    # anyone types first. SUPPRESS keeps the subparser's default from clobbering
    # a value already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Machine-readable output.",
    )
    common.add_argument("--now", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def sub_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        result = subparsers.add_parser(name, help=help_text, parents=[common])
        result.add_argument(
            "--data-dir",
            type=Path,
            default=argparse.SUPPRESS,
            help="Initialized SQLite data directory.",
        )
        return result

    sub_parser("brief", "The whole estate, compressed. Once per session.")

    if include_manifest:
        # Built through `sub_parser` like every other command, so it accepts
        # `--data-dir` after the subcommand too. Constructed directly, it was
        # the one command where `cadastre manifest brief --data-dir D` was a
        # usage error while `cadastre brief --data-dir D` worked.
        manifest = sub_parser("manifest", "Read the opt-in Manifest work register.")
        manifest.add_argument(
            "operation",
            choices=("brief", "projects", "backlog", "next", "why", "drift", "repo"),
        )
        manifest.add_argument("id", nargs="?")
        manifest.add_argument("--state", default=None)
        manifest.add_argument("--initiative", default=None)
        manifest.add_argument("--repo", default=None)
        manifest.add_argument("--limit", type=int, default=10)

    lookup = sub_parser("lookup", "Drill down on one entity.")
    lookup.add_argument("id")
    lookup.add_argument("--kind", default=None, help="Disambiguate a shared id.")

    context = sub_parser(
        "context-for", "The truth relevant to one decision, pre-joined."
    )
    context.add_argument("intent", help='e.g. "deploy a public web service with a gpu"')

    question = sub_parser(
        "question", "Answer one explicit operational migration question."
    )
    question.add_argument("id", choices=QUESTION_IDS)
    question.add_argument("--subject", default=None)
    question.add_argument("--value", default=None)

    check = sub_parser("check", "Consult the map about a proposed artifact.")
    check.add_argument("artifact", type=Path)
    check.add_argument(
        "--kind",
        default=None,
        choices=("compose", "ingress", "pipeline", "grants"),
        help="Artifact type. Inferred from the filename when omitted.",
    )
    check.add_argument(
        "--warnings-as-errors",
        action="store_true",
        help="Exit non-zero on warnings too. For the CI gate.",
    )

    drift = sub_parser("drift", "Where declared and observed disagree.")
    drift.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 when drift is found. Off by default so CI does not break.",
    )
    drift.add_argument(
        "--category",
        choices=("undeclared", "missing", "differs", "secret-only-in"),
        help="Only this divergence category.",
    )
    drift.add_argument("--kind", help="Only this entity kind (e.g. secret, repo).")
    drift.add_argument("--source", help="Only divergences attributed to this source.")
    drift.add_argument("--entity-id", help="Only divergences for this entity id.")
    drift.add_argument(
        "--limit",
        type=int,
        help="Page size (1-1000). A filtered/paged call returns a next cursor.",
    )
    drift.add_argument("--cursor", help="Continue a paged result from its next cursor.")
    drift.add_argument(
        "--summary-only",
        action="store_true",
        help="Just the counts matrix — small enough for any MCP result cap.",
    )

    observations = sub_parser(
        "observations", "Retained plugin evidence that has no entity form."
    )
    observations.add_argument("--source", default=None, help="Limit to one source id.")
    observations.add_argument(
        "--method", default=None, help="Limit to sources that collected this method."
    )
    observations.add_argument("--key", default=None, help="Limit to one evidence key.")
    observations.add_argument("--limit", type=int, default=None)
    observations.add_argument(
        "--summary-only",
        action="store_true",
        help="List what was retained without returning any payload.",
    )

    collect = sub_parser("collect", "Run collectors, write observed/.")
    collect.add_argument(
        "--source", action="append", default=None, help="Limit to these source ids."
    )
    collect.add_argument(
        "--dry-run", action="store_true", help="Report what would be written."
    )
    collect.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 when any source fails. For a scheduler that watches this.",
    )

    fmt = sub_parser("fmt", "Rewrite declared/ in canonical form.")
    fmt.add_argument("--check", action="store_true", help="Report, do not rewrite.")

    add = sub_parser("add", "Add a catalog-owned entity through the write gate.")
    add_kind_choices = model.KINDS
    if include_manifest:
        from cadastre.manifest.spec import ENTITY_SPECS as MANIFEST_ENTITY_SPECS

        add_kind_choices = model.KINDS + tuple(MANIFEST_ENTITY_SPECS)
    add.add_argument("kind", choices=add_kind_choices)
    add.add_argument("record", type=Path)
    _write_options(add)

    update = sub_parser("update", "Update intended fields through the write gate.")
    update.add_argument("target")
    update.add_argument("record", type=Path)
    _write_options(update)

    delete = sub_parser(
        "delete", "Delete a catalog-owned entity through the write gate."
    )
    delete.add_argument("target")
    _write_options(delete)

    annotate = sub_parser(
        "annotate", "Annotate an existing entity through the write gate."
    )
    annotate.add_argument("target")
    annotate.add_argument("values", nargs="+", metavar="KEY=VALUE")
    _write_options(annotate)

    sub_parser("stale", "Report stale, unverified, and contested state.")
    accept = sub_parser("accept", "Accept observed state for a contest.")
    accept.add_argument("target")
    accept.add_argument("--source", required=True)
    accept.add_argument("--field", default=None)
    _write_options(accept)
    leave = sub_parser("leave-contested", "Record that a contest remains unresolved.")
    leave.add_argument("target")
    leave.add_argument("--source", required=True)
    leave.add_argument("--field", default=None)
    _write_options(leave)
    acknowledge = sub_parser("acknowledge", "Defer a contest until a stated date.")
    acknowledge.add_argument("target")
    acknowledge.add_argument("--source", required=True)
    acknowledge.add_argument("--until", required=True)
    acknowledge.add_argument("--reason", required=True)
    acknowledge.add_argument("--principal", default="agent")

    schema = sub_parser("schema", "Print the JSON Schema for the entity model.")
    schema.add_argument(
        "--openapi", action="store_true", help="Print the HTTP OpenAPI 3.1 document."
    )
    serve = sub_parser("serve", "Run the optional HTTP API adapter.")
    serve.add_argument("--bind", default="127.0.0.1:8000")
    serve.add_argument("--allow-write", action="store_true")
    serve.add_argument("--allow-non-loopback", action="store_true")
    serve.add_argument(
        "--require-auth",
        action="store_true",
        help="Require a bearer token for all HTTP routes.",
    )
    serve.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Read a protected JSON file containing explicitly scoped tokens.",
    )
    serve.add_argument("--tls-cert", type=Path, default=None)
    serve.add_argument("--tls-key", type=Path, default=None)
    serve.add_argument("--tls-ca", type=Path, default=None)
    serve.add_argument("--require-client-cert", action="store_true")
    serve.add_argument("--allowed-host", action="append", default=[])
    serve.add_argument("--allowed-origin", action="append", default=[])
    serve.add_argument("--audit-path", type=Path, default=None)
    serve.add_argument("--proxy-network", action="append", default=[])
    serve.add_argument("--proxy-secret-file", type=Path, default=None)
    serve.add_argument("--proxy-scope", action="append", default=[])
    serve.add_argument("--mtls-principal", action="append", default=[])
    serve.add_argument("--audience", default="cadastre")
    serve.add_argument(
        "--profile",
        choices=(
            "loopback-development",
            "direct-https",
            "development-plaintext",
            "trusted-proxy",
            "mtls",
        ),
        default="loopback-development",
    )
    sub_parser("mcp", "Run the MCP adapter over stdio.")
    mcp_http = sub_parser("mcp-http", "Run MCP over standard Streamable HTTP.")
    mcp_http.add_argument("--bind", default="127.0.0.1:8001")
    mcp_http.add_argument("--allow-write", action="store_true")
    mcp_http.add_argument("--token-file", type=Path, default=None)
    mcp_http.add_argument("--allow-non-loopback", action="store_true")
    mcp_http.add_argument("--tls-cert", type=Path, default=None)
    mcp_http.add_argument("--tls-key", type=Path, default=None)
    mcp_http.add_argument("--tls-ca", type=Path, default=None)
    mcp_http.add_argument("--require-client-cert", action="store_true")
    mcp_http.add_argument("--allowed-host", action="append", default=[])
    mcp_http.add_argument("--allowed-origin", action="append", default=[])
    mcp_http.add_argument("--audit-path", type=Path, default=None)
    mcp_http.add_argument("--proxy-network", action="append", default=[])
    mcp_http.add_argument("--proxy-secret-file", type=Path, default=None)
    mcp_http.add_argument("--proxy-scope", action="append", default=[])
    mcp_http.add_argument("--mtls-principal", action="append", default=[])
    mcp_http.add_argument("--audience", default="cadastre")
    security = sub_parser(
        "security-check", "Check the configured network security profile."
    )
    security.add_argument("--bind", default="127.0.0.1:8000")
    security.add_argument("--profile", default="loopback-development")
    security.add_argument("--require-auth", action="store_true")
    security.add_argument("--scope", action="append", default=[])
    security.add_argument("--tls-cert", type=Path, default=None)
    security.add_argument("--tls-key", type=Path, default=None)
    security.add_argument("--tls-ca", type=Path, default=None)
    security.add_argument("--proxy-network", action="append", default=[])
    security.add_argument("--proxy-secret-file", type=Path, default=None)
    sub_parser("sources", "List configured plugins and their handshake.")
    sub_parser("plugins", "List registered plugins and their active state.")
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument("--data-dir", type=Path, required=True, dest="runtime_data_dir")
    init_parser = subparsers.add_parser(
        "init", help="Initialize an empty SQLite catalog.", parents=[common, data]
    )
    init_parser.add_argument(
        "--empty", action="store_true", help="Explicitly initialize without a bundle."
    )
    init_parser.add_argument("--from-bundle", type=Path, default=None)
    for name, help_text in (
        ("status", "Show SQLite catalog status."),
        ("integrity-check", "Check both SQLite databases."),
        ("migrate", "Run approved forward migrations."),
    ):
        subparsers.add_parser(name, help=help_text, parents=[common, data])
    backup = subparsers.add_parser(
        "backup",
        help="Create a transaction-consistent SQLite backup.",
        parents=[common, data],
    )
    backup.add_argument("--output", type=Path, required=True)
    restore = subparsers.add_parser(
        "restore", help="Restore a verified SQLite backup.", parents=[common, data]
    )
    restore.add_argument("--input", type=Path, required=True)
    export = subparsers.add_parser(
        "export",
        help="Export a deterministic interchange bundle.",
        parents=[common, data],
    )
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--without-observed", action="store_true")
    imp = subparsers.add_parser(
        "import",
        help="Import a bundle through one transaction.",
        parents=[common, data],
    )
    imp.add_argument("--input", type=Path, required=True)
    imp.add_argument(
        "--mode", choices=("replace", "merge", "dry-run"), default="replace"
    )
    load = subparsers.add_parser(
        "load",
        help="Reload a declared/ tree into an initialized catalog.",
        parents=[common, data],
    )
    load.add_argument(
        "--from",
        dest="from_catalog",
        type=Path,
        required=True,
        help="Catalog root containing declared/.",
    )
    load.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    _write_options(load)
    return parser


def _write_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--principal",
        default="agent",
        help="Identity recorded in the edit stamp.",
    )
    parser.add_argument(
        "--reason",
        default="catalog edit",
        help="Reason recorded in the edit stamp and commit.",
    )


def resolve_root(explicit: Path | None) -> Path:
    import os

    if explicit is not None:
        return explicit
    from_env = os.environ.get(CATALOG_ENV)
    data_env = os.environ.get(DATA_DIR_ENV)
    return Path(from_env) if from_env else (Path(data_env) if data_env else Path.cwd())


def _rollback_init(data_dir: Path, pre_existing: set[Path], storage: Any) -> None:
    """Undo a partial `init`, touching only what this run created.

    Anything that existed beforehand is left alone: a failed `--from-bundle`
    against a populated directory must not delete the operator's catalog.
    """
    for name in (storage.CATALOG_DB, storage.OBSERVED_DB):
        path = data_dir / name
        if path in pre_existing:
            continue
        for candidate in (path, *path.parent.glob(f"{path.name}-*")):
            with contextlib.suppress(OSError):
                candidate.unlink()
    if data_dir not in pre_existing:
        with contextlib.suppress(OSError):
            data_dir.rmdir()


def _now(raw: str | None) -> datetime:
    return parse_timestamp(raw) if raw else datetime.now(tz=UTC)


def dispatch(args: argparse.Namespace) -> Document:
    from cadastre.adapters.http import openapi_schema
    from cadastre.cli import brief, context_for, lookup
    from cadastre.cli import check as check_cmd
    from cadastre.cli import collect as collect_cmd
    from cadastre.cli import drift as drift_cmd
    from cadastre.cli import fmt as fmt_cmd
    from cadastre.cli import manifest as manifest_cmd
    from cadastre.cli import plugins as plugins_cmd
    from cadastre.cli import question as question_cmd
    from cadastre.cli import sources as sources_cmd
    from cadastre.cli import trust as trust_cmd
    from cadastre.cli import write as write_cmd
    from cadastre.cli.session import Session
    from cadastre.core.schema import render_schema

    root = getattr(args, "data_dir", None) or resolve_root(args.catalog)

    if args.command in {
        "init",
        "status",
        "integrity-check",
        "migrate",
        "backup",
        "restore",
        "export",
        "import",
        "load",
    }:
        from cadastre.core import storage

        if args.command == "init":
            # `init` is all-or-nothing. A half-initialized data directory is
            # worse than none: `initialize()` leaves a valid but EMPTY catalog,
            # every later command accepts it, and `drift` then renders the
            # whole estate as `undeclared` — a confident, completely wrong
            # report from the tool whose value is being trustworthy.
            pre_existing = {
                path
                for path in (
                    args.runtime_data_dir,
                    args.runtime_data_dir / storage.CATALOG_DB,
                    args.runtime_data_dir / storage.OBSERVED_DB,
                )
                if path.exists()
            }
            try:
                data = storage.initialize(args.runtime_data_dir)
                if args.from_bundle is not None:
                    data.update(
                        storage.import_bundle(
                            args.runtime_data_dir, args.from_bundle, mode="replace"
                        )
                    )
            except BaseException:
                _rollback_init(args.runtime_data_dir, pre_existing, storage)
                raise
        elif args.command == "status":
            from cadastre.modules.config import load_modules
            from cadastre.modules.registry import active_registry

            status_registry = active_registry(load_modules(args.runtime_data_dir))
            with storage.CatalogStore.open(
                args.runtime_data_dir, read_only=True
            ) as store:
                status_catalog = store.read_catalog(registry=status_registry)
                data = {
                    "data_dir": str(args.runtime_data_dir),
                    "application_version": __version__,
                    "schema_version": storage.FORMAT_VERSION,
                    "revision": store.revision,
                    "declared_as_of": store.declared_as_of,
                    "audit_records": len(store.audit()),
                    "empty": not any(
                        status_catalog.all(kind) for kind in status_registry.kinds
                    ),
                }
        elif args.command == "integrity-check":
            data = storage.integrity(args.runtime_data_dir)
        elif args.command == "migrate":
            storage.initialize(args.runtime_data_dir)
            with storage.CatalogStore.open(args.runtime_data_dir, create=True) as store:
                data = {
                    "data_dir": str(args.runtime_data_dir),
                    "application_version": __version__,
                    "schema_version": storage.FORMAT_VERSION,
                    "revision": store.revision,
                    "migrations": [1],
                }
        elif args.command == "backup":
            data = storage.backup(args.runtime_data_dir, args.output)
        elif args.command == "restore":
            data = storage.restore(args.runtime_data_dir, args.input)
        elif args.command == "export":
            data = storage.export_bundle(
                args.runtime_data_dir,
                args.output,
                include_observed=not args.without_observed,
            )
        elif args.command == "load":
            data = storage.load_declared(
                args.from_catalog,
                args.runtime_data_dir,
                principal=args.principal,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        else:
            data = storage.import_bundle(
                args.runtime_data_dir, args.input, mode=args.mode
            )
        return Document(title=f"cadastre {args.command}", data=data)

    if args.command == "schema":
        from cadastre.modules.config import load_modules
        from cadastre.modules.registry import active_registry

        registry = active_registry(load_modules(root))
        if args.openapi:
            return Document(
                title="cadastre schema --openapi", data={"openapi": openapi_schema()}
            )
        return Document(
            title="cadastre schema",
            data={"schema": render_schema(registry=registry)},
            sections=(),
        )
    if args.command == "fmt":
        return fmt_cmd.fmt(root, check_only=args.check)
    if args.command == "serve":
        from cadastre.cli.serve import serve as serve_cmd

        return serve_cmd(
            root,
            bind=args.bind,
            allow_write=args.allow_write,
            allow_non_loopback=args.allow_non_loopback,
            require_auth=args.require_auth,
            token_file=args.token_file,
            tls_certfile=args.tls_cert,
            tls_keyfile=args.tls_key,
            tls_ca_file=args.tls_ca,
            require_client_cert=args.require_client_cert,
            allowed_hosts=tuple(args.allowed_host),
            allowed_origins=tuple(args.allowed_origin),
            audit_path=args.audit_path,
            profile=args.profile,
            proxy_networks=tuple(args.proxy_network),
            proxy_secret_file=args.proxy_secret_file,
            proxy_scopes=args.proxy_scope,
            mtls_scopes=args.mtls_principal,
            audience=args.audience,
        )
    if args.command == "mcp-http":
        from cadastre.cli.streamable import serve as streamable_serve

        return streamable_serve(
            root,
            bind=args.bind,
            allow_write=args.allow_write,
            token_file=args.token_file,
            allow_non_loopback=args.allow_non_loopback,
            tls_certfile=args.tls_cert,
            tls_keyfile=args.tls_key,
            tls_ca_file=args.tls_ca,
            require_client_cert=args.require_client_cert,
            allowed_hosts=tuple(args.allowed_host),
            allowed_origins=tuple(args.allowed_origin),
            audit_path=args.audit_path,
            proxy_networks=tuple(args.proxy_network),
            proxy_secret_file=args.proxy_secret_file,
            proxy_scopes=args.proxy_scope,
            mtls_scopes=args.mtls_principal,
            audience=args.audience,
        )
    if args.command == "security-check":
        from cadastre.cli.security import security_check

        return security_check(
            bind=args.bind,
            profile=args.profile,
            require_auth=args.require_auth,
            scopes=tuple(args.scope),
            certfile=args.tls_cert,
            keyfile=args.tls_key,
            ca_file=args.tls_ca,
            proxy_networks=tuple(args.proxy_network),
            proxy_secret_file=args.proxy_secret_file,
        )
    if args.command == "mcp":
        from cadastre.mcp.server import main as mcp_main

        mcp_main()
        return Document(title="cadastre mcp", data={"stopped": True})

    session = Session.open(root, now=_now(args.now))

    if args.command == "brief":
        return brief.brief(session)
    if args.command == "manifest":
        if args.operation == "brief":
            return manifest_cmd.brief(session)
        if args.operation == "projects":
            return manifest_cmd.projects(session)
        if args.operation == "backlog":
            return manifest_cmd.backlog(
                session,
                state=args.state,
                initiative=args.initiative,
                repo=args.repo,
                limit=args.limit,
            )
        if args.operation == "next":
            return manifest_cmd.next_(session, limit=args.limit)
        if args.operation == "drift":
            return manifest_cmd.drift(session, repo=args.repo)
        if args.operation == "repo":
            if not args.id and not args.repo:
                raise UsageError("manifest repo requires a repository name")
            return manifest_cmd.repo(session, args.id or args.repo)
        if not args.id:
            raise UsageError("manifest why requires a work item id")
        return manifest_cmd.why(session, args.id)
    if args.command == "lookup":
        return lookup.lookup(session, args.id, kind=args.kind)
    if args.command == "context-for":
        return context_for.context_for(session, args.intent)
    if args.command == "question":
        return question_cmd.question(
            session, args.id, subject=args.subject, value=args.value
        )
    if args.command == "check":
        return check_cmd.check(
            session,
            args.artifact,
            kind=args.kind,
            warnings_as_errors=args.warnings_as_errors,
        )
    if args.command == "drift":
        return drift_cmd.drift(
            session,
            exit_code=args.exit_code,
            category=args.category,
            kind=args.kind,
            source=args.source,
            entity_id=args.entity_id,
            limit=args.limit,
            cursor=args.cursor,
            summary_only=args.summary_only,
        )
    if args.command == "collect":
        return collect_cmd.collect(
            session,
            sources=args.source,
            dry_run=args.dry_run,
            exit_code=args.exit_code,
        )
    if args.command == "add":
        return write_cmd.run(
            session,
            "add",
            args.kind,
            record=args.record,
            principal=args.principal,
            reason=args.reason,
        )
    if args.command == "update":
        kind, ident = write_cmd.parse_target(args.target, registry=session.registry)
        return write_cmd.run(
            session,
            "update",
            kind,
            ident,
            record=args.record,
            principal=args.principal,
            reason=args.reason,
        )
    if args.command == "delete":
        kind, ident = write_cmd.parse_target(args.target, registry=session.registry)
        return write_cmd.run(
            session,
            "delete",
            kind,
            ident,
            principal=args.principal,
            reason=args.reason,
        )
    if args.command == "annotate":
        kind, ident = write_cmd.parse_target(args.target, registry=session.registry)
        return write_cmd.run(
            session,
            "annotate",
            kind,
            ident,
            values=write_cmd.annotate_values(args.values),
            principal=args.principal,
            reason=args.reason,
        )
    if args.command == "stale":
        return trust_cmd.stale(session)
    if args.command in {"accept", "leave-contested"}:
        return trust_cmd.resolve(
            session,
            "accept-observed" if args.command == "accept" else "leave-contested",
            args.target,
            source=args.source,
            field=args.field,
            principal=args.principal,
            reason=args.reason,
        )
    if args.command == "acknowledge":
        return trust_cmd.acknowledge(
            session,
            args.target,
            source=args.source,
            until=args.until,
            reason=args.reason,
            principal=args.principal,
        )
    if args.command == "observations":
        from cadastre.cli import observations as observations_cmd

        return observations_cmd.observations(
            session,
            source=args.source,
            method=args.method,
            key=args.key,
            limit=args.limit,
            summary_only=args.summary_only,
        )
    if args.command == "sources":
        return sources_cmd.sources(session)
    if args.command == "plugins":
        return plugins_cmd.plugins(session)
    raise UsageError(f"unknown command {args.command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--catalog", type=Path, default=None)
    probe.add_argument("--data-dir", type=Path, default=None)
    probe_args, _ = probe.parse_known_args(raw_argv)
    probe_root = resolve_root(probe_args.catalog or probe_args.data_dir)
    from cadastre.modules.config import load_modules

    try:
        include_manifest = load_modules(probe_root).enabled("manifest")
    except CatalogError as exc:
        print(
            "The module configuration could not be used as written:\n",
            file=sys.stderr,
        )
        print(exc.render(), file=sys.stderr)
        return EXIT_ERROR
    parser = build_parser(include_manifest=include_manifest)
    args = parser.parse_args(raw_argv)
    if args.command == "mcp":
        from cadastre.mcp.server import main as mcp_main

        if args.catalog is not None:
            os.environ[CATALOG_ENV] = str(resolve_root(args.catalog))
        return mcp_main()
    try:
        document = dispatch(args)
    except CatalogError as exc:
        print("The catalog could not be used as written:\n", file=sys.stderr)
        print(exc.render(), file=sys.stderr)
        return EXIT_ERROR
    except UsageError as exc:
        print(f"cadastre: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except CadastreError as exc:
        print(f"cadastre: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.command == "schema" and not args.json:
        if args.openapi:
            import json

            sys.stdout.write(json.dumps(document.data["openapi"], indent=2) + "\n")
        else:
            sys.stdout.write(document.data["schema"])
        return EXIT_OK
    sys.stdout.write(json_out.render(document) if args.json else text.render(document))
    return document.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
