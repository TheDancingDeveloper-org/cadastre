"""Fail-closed activation for optional Cadastre modules.

Module activation is configuration, not package discovery.  In particular, an
installed extra must never make a module appear on a running server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cadastre.core.errors import CatalogError, CatalogIssue, Located
from cadastre.core.yamlio import LinedDict, load_yaml

CONFIG_FILENAME = "modules.yaml"
SUPPORTED_MODULES = frozenset({"manifest"})


@dataclass(frozen=True)
class ModuleConfig:
    name: str
    enabled: bool = False
    version: str = "1"


@dataclass(frozen=True)
class ModulesFile:
    modules: tuple[ModuleConfig, ...] = ()

    def enabled(self, name: str) -> bool:
        return any(item.name == name and item.enabled for item in self.modules)

    def by_name(self, name: str) -> ModuleConfig | None:
        return next((item for item in self.modules if item.name == name), None)


def _located(path: Path, root: Path, value: Any) -> Located:
    line = value.line if isinstance(value, LinedDict) else None
    return Located(str(path.relative_to(root)), line)


def load_modules(root: Path) -> ModulesFile:
    """Load ``modules.yaml`` beside runtime data, or its bundle counterpart.

    An absent file means all modules are disabled. Unknown module names,
    malformed entries, and non-boolean ``enabled`` values are errors with
    source locations rather than silently ignored configuration.
    """
    path = root / CONFIG_FILENAME
    if not path.exists():
        path = root / "declared" / CONFIG_FILENAME
    if not path.exists():
        return ModulesFile()

    rel = str(path.relative_to(root))
    raw = load_yaml(path, rel=rel) or {}
    where = Located(rel, raw.line if isinstance(raw, LinedDict) else None)
    issues: list[CatalogIssue] = []
    if not isinstance(raw, dict):
        raise CatalogError(
            [CatalogIssue(where, "<modules>", "not a mapping", "a mapping")]
        )
    entries = raw.get("modules", {})
    if not isinstance(entries, dict):
        raise CatalogError(
            [
                CatalogIssue(
                    where,
                    "modules",
                    "not a mapping",
                    "a mapping of module names",
                )
            ]
        )

    result: list[ModuleConfig] = []
    for name, value in entries.items():
        field = f"modules.{name}"
        entry_where = _located(path, root, value)
        if name not in SUPPORTED_MODULES:
            issues.append(
                CatalogIssue(entry_where, field, "unknown module", "manifest")
            )
            continue
        if not isinstance(value, dict):
            issues.append(
                CatalogIssue(entry_where, field, "not a mapping", "enabled: true|false")
            )
            continue
        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            issues.append(
                CatalogIssue(
                    entry_where,
                    f"{field}.enabled",
                    "not a boolean",
                    "true or false",
                )
            )
            continue
        version = value.get("version", "1")
        if not isinstance(version, str) or not version:
            issues.append(
                CatalogIssue(
                    entry_where,
                    f"{field}.version",
                    "not a non-empty string",
                    "a module version",
                )
            )
            continue
        result.append(ModuleConfig(name=name, enabled=enabled, version=version))
    if issues:
        raise CatalogError(issues)
    return ModulesFile(tuple(result))
