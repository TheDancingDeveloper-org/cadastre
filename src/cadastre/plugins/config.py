"""Plugin source configuration from the catalog/bundle plugin section.

Safe in a public repository by construction: it names the *environment
variable* a credential arrives in, never the credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cadastre.core.errors import CatalogError, CatalogIssue, Located
from cadastre.core.yamlio import LinedDict, load_yaml

CONFIG_FILENAME = "plugins.yaml"


@dataclass(frozen=True)
class SourceConfig:
    """One configured plugin invocation."""

    id: str
    command: tuple[str, ...]
    plugin: str = ""
    methods: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30
    enabled: bool = True
    #: Environment variables to pass through from the collector host's
    #: environment. Named here, valued nowhere in the catalog.
    env: tuple[str, ...] = ()
    #: Per-kind coverage for this configured source.  It narrows (never
    #: broadens) the registered plugin declaration for missing-drift checks.
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Mint the credential as a GitHub App installation token instead of
    #: reading a static one: see `AppAuth`.
    app_auth: AppAuth | None = None


@dataclass(frozen=True)
class AppAuth:
    """A GitHub App identity the runner turns into an installation token.

    Like `token_env`, it names where things are, never what they are: the app
    id (and optionally the installation id) arrive by environment variable,
    and the private key is a file path. Without `installation_id_env` the
    installation is discovered for `owner` — by default the source's
    `config.org`, else `config.user`. The minted token is handed to the
    plugin under the source's `config.token_env`, so the plugin reads it
    exactly as it would a static token and never sees the key.
    """

    app_id_env: str
    private_key_file: str
    installation_id_env: str | None = None
    owner: str | None = None


APP_AUTH_REQUIRED = ("app_id_env", "private_key_file")
APP_AUTH_OPTIONAL = ("installation_id_env", "owner")
APP_AUTH_KEYS = APP_AUTH_REQUIRED + APP_AUTH_OPTIONAL


@dataclass(frozen=True)
class PluginsFile:
    sources: tuple[SourceConfig, ...] = ()
    freshness: dict[str, int] = field(default_factory=dict)


def load_plugins(root: Path) -> PluginsFile:
    """Read `<root>/declared/plugins.yaml`. Absent is fine — a catalog with
    no collectors is a valid catalog, it just has nothing observed."""
    from cadastre.modules.config import load_modules
    from cadastre.modules.registry import active_registry

    registry_specs = active_registry(load_modules(root)).specs
    # Runtime collector configuration is mounted beside the databases. The
    # declared/ path is retained only for file-tree interchange fixtures.
    path = root / CONFIG_FILENAME
    if not path.exists():
        path = root / "declared" / CONFIG_FILENAME
    if not path.exists():
        return PluginsFile()
    rel = str(path.relative_to(root))
    where = Located(rel)
    raw = load_yaml(path, rel=rel) or {}
    issues: list[CatalogIssue] = []
    if not isinstance(raw, dict):
        raise CatalogError(
            [CatalogIssue(where, "<plugins>", "not a mapping", "a mapping")]
        )

    freshness: dict[str, int] = {}
    for key, value in (raw.get("freshness") or {}).items():
        if isinstance(value, int) and not isinstance(value, bool):
            freshness[str(key)] = value
        else:
            issues.append(
                CatalogIssue(
                    where, f"freshness.{key}", "not an integer", "seconds, e.g. 3600"
                )
            )

    sources: list[SourceConfig] = []
    raw_sources = raw.get("sources") or []
    if not isinstance(raw_sources, list):
        issues.append(
            CatalogIssue(where, "sources", "not a list", "a list of source mappings")
        )
        raw_sources = []
    seen: set[str] = set()
    for index, item in enumerate(raw_sources):
        line = item.line if isinstance(item, LinedDict) else None
        at = Located(rel, line)
        label = f"sources[{index}]"
        if not isinstance(item, dict):
            issues.append(CatalogIssue(at, label, "not a mapping", "a mapping"))
            continue
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            issues.append(
                CatalogIssue(at, f"{label}.id", "missing required field", "a string")
            )
            continue
        label = f"sources[{ident}]"
        if ident in seen:
            issues.append(
                CatalogIssue(at, f"{label}.id", "duplicate source id", "a unique id")
            )
            continue
        seen.add(ident)
        command = item.get("command")
        if isinstance(command, str):
            issues.append(
                CatalogIssue(
                    at,
                    f"{label}.command",
                    "got a string",
                    "a list of argv words — Cadastre never runs a shell",
                )
            )
            continue
        if not isinstance(command, list) or not command:
            issues.append(
                CatalogIssue(
                    at, f"{label}.command", "missing required field", "a list of argv"
                )
            )
            continue
        methods = item.get("methods") or []
        if not isinstance(methods, list):
            issues.append(
                CatalogIssue(at, f"{label}.methods", "not a list", "a list of methods")
            )
            methods = []
        coverage = item.get("coverage") or {}
        if not isinstance(coverage, dict):
            issues.append(
                CatalogIssue(
                    at,
                    f"{label}.coverage",
                    "not an object",
                    "kind -> coverage mapping",
                )
            )
            coverage = {}
        invalid_coverage = [
            str(kind) for kind, value in coverage.items() if not isinstance(value, dict)
        ]
        if invalid_coverage:
            issues.append(
                CatalogIssue(
                    at,
                    f"{label}.coverage",
                    "invalid kind coverage",
                    "each kind maps to an object",
                )
            )
            coverage = {
                kind: value
                for kind, value in coverage.items()
                if isinstance(value, dict)
            }
        for kind, scope in coverage.items():
            if kind not in registry_specs:
                issues.append(
                    CatalogIssue(
                        at,
                        f"{label}.coverage.{kind}",
                        "unknown entity kind",
                        "one of: " + ", ".join(sorted(registry_specs)),
                    )
                )
                continue
            unknown = sorted(str(key) for key in scope if key not in {"ids", "where"})
            ids = scope.get("ids")
            where_clause = scope.get("where")
            if unknown:
                issues.append(
                    CatalogIssue(
                        at,
                        f"{label}.coverage.{kind}",
                        "unknown keys: " + ", ".join(unknown),
                        "ids and/or where",
                    )
                )
            if ids is not None and (
                not isinstance(ids, list)
                or not all(isinstance(item, str) for item in ids)
            ):
                issues.append(
                    CatalogIssue(
                        at,
                        f"{label}.coverage.{kind}.ids",
                        "not a list of strings",
                        "exact entity ids",
                    )
                )
            if where_clause is not None and not isinstance(where_clause, dict):
                issues.append(
                    CatalogIssue(
                        at,
                        f"{label}.coverage.{kind}.where",
                        "not an object",
                        "entity field constraints",
                    )
                )
            elif isinstance(where_clause, dict):
                known_fields = {field.key for field in registry_specs[kind].fields}
                unknown_fields = sorted(
                    str(key) for key in where_clause if key not in known_fields
                )
                if unknown_fields:
                    issues.append(
                        CatalogIssue(
                            at,
                            f"{label}.coverage.{kind}.where",
                            "unknown fields: " + ", ".join(unknown_fields),
                            "declared entity fields",
                        )
                    )
        app_auth: AppAuth | None = None
        raw_app_auth = item.get("app_auth")
        if raw_app_auth is not None:
            config_section = item.get("config") or {}
            expected = (
                ", ".join(APP_AUTH_REQUIRED)
                + " (optionally "
                + ", ".join(APP_AUTH_OPTIONAL)
                + ") as non-empty strings"
            )
            if not isinstance(raw_app_auth, dict) or (
                any(str(k) not in APP_AUTH_KEYS for k in raw_app_auth)
                or not all(k in raw_app_auth for k in APP_AUTH_REQUIRED)
                or not all(isinstance(v, str) and v for v in raw_app_auth.values())
            ):
                issues.append(
                    CatalogIssue(at, f"{label}.app_auth", "invalid app_auth", expected)
                )
            elif not (
                isinstance(config_section, dict)
                and isinstance(config_section.get("token_env"), str)
                and config_section["token_env"]
            ):
                issues.append(
                    CatalogIssue(
                        at,
                        f"{label}.config.token_env",
                        "app_auth without token_env",
                        "the variable the minted token is handed over in",
                    )
                )
            else:
                app_auth = AppAuth(**{str(k): str(v) for k, v in raw_app_auth.items()})
        sources.append(
            SourceConfig(
                id=ident,
                command=tuple(str(word) for word in command),
                plugin=str(item.get("plugin") or ident),
                methods=tuple(str(m) for m in methods),
                config=dict(item.get("config") or {}),
                params=dict(item.get("params") or {}),
                timeout_seconds=int(item.get("timeout_seconds", 30)),
                enabled=bool(item.get("enabled", True)),
                env=tuple(str(name) for name in (item.get("env") or ())),
                coverage={str(kind): dict(value) for kind, value in coverage.items()},
                app_auth=app_auth,
            )
        )
    if issues:
        raise CatalogError(issues)
    return PluginsFile(
        sources=tuple(sorted(sources, key=lambda s: s.id)), freshness=freshness
    )
