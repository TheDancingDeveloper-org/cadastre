"""Discovery for in-process plugins (M10)."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from cadastre.plugins.contract import (
    PluginInfo,
    declaration_for,
    parse_plugin_info,
    validate_plugin_info,
)


@dataclass(frozen=True)
class RegisteredPlugin:
    name: str
    info: PluginInfo
    origin: str
    active: bool = False
    configuration: str | None = None


class PluginRegistry:
    """Find installed entry points and single-file plugins in ``plugins/``."""

    def __init__(self, plugins: tuple[RegisteredPlugin, ...] = ()) -> None:
        self.plugins = tuple(sorted(plugins, key=lambda item: item.name))

    @classmethod
    def discover(cls, root: Path | None = None) -> PluginRegistry:
        found: dict[str, RegisteredPlugin] = {
            plugin.name: plugin for plugin in _builtin_plugins()
        }
        if root is not None and _manifest_enabled(root):
            for plugin in _manifest_builtin_plugins():
                found[plugin.name] = plugin
        entry_points = importlib.metadata.entry_points()
        selected = entry_points.select(group="cadastre.plugins")
        for entry in selected:
            try:
                loaded = entry.load()
                info = _module_info(loaded)
                validate_plugin_info(info)
            except (ImportError, AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid plugin {entry.name!r}: {exc}") from exc
            found[info.name] = RegisteredPlugin(info.name, info, str(entry))

        if root is not None:
            directory = root / "plugins"
            for path in sorted(directory.glob("*.py")) if directory.is_dir() else ():
                info = _file_info(path)
                found[info.name] = RegisteredPlugin(info.name, info, str(path))
        return cls(tuple(found.values()))

    def get(self, name: str) -> RegisteredPlugin | None:
        return next((plugin for plugin in self.plugins if plugin.name == name), None)


def _builtin_plugins() -> tuple[RegisteredPlugin, ...]:
    """The in-tree plugins remain visible without an installed wheel.

    Entry points add third-party in-process plugins; these declarations make
    the core's own plugins registered by default in a source checkout too.
    """
    sources = (
        (
            "static",
            ("Inventory", "Network", "Endpoint", "SecretRef", "VCS", "CI"),
            "catalog",
            (
                "host",
                "service",
                "network",
                "endpoint",
                "secret",
                "repo",
                "pipeline",
                "ci_executor",
                "ci_pool",
            ),
        ),
        ("exec", (), "source", ()),
        ("ingress-caddy", ("Endpoint",), "source", ("endpoint",)),
        ("forge-forgejo", ("VCS", "SecretRef"), "source", ("repo", "secret")),
        (
            "forge-github",
            ("VCS", "CI"),
            "source",
            ("repo", "pipeline", "ci_executor", "ci_pool"),
        ),
        ("ci-woodpecker", ("CI", "SecretRef"), "source", ("pipeline", "secret")),
        ("secrets-infisical", ("SecretRef",), "source", ("secret",)),
        ("orchestrator-gitops", ("Inventory",), "source", ("service",)),
        ("dns-cloudflare", ("DNS",), "source", ("domain",)),
        ("vpn-tailscale", ("Network",), "source", ("host", "network")),
        ("hypervisor-proxmox", ("Inventory",), "source", ("host",)),
        ("registry-crates", (), "source", ()),
    )
    result: list[RegisteredPlugin] = []
    for name, capabilities, authority, kinds in sources:
        declarations = tuple(
            declaration_for(kind, authority=authority, plugin=name) for kind in kinds
        )
        info = PluginInfo(name, "1", capabilities, declarations)
        result.append(RegisteredPlugin(name, info, "cadastre (in-tree)"))
    return tuple(result)


def _manifest_enabled(root: Path) -> bool:
    from cadastre.modules.config import load_modules

    try:
        return load_modules(root).enabled("manifest")
    except Exception:
        # An invalid modules.yaml is reported elsewhere on the same path this
        # discovery runs; plugin discovery itself must not raise from it.
        return False


def _manifest_builtin_plugins() -> tuple[RegisteredPlugin, ...]:
    """The Manifest module's in-tree collectors (MANIFEST.md §5.2 R02).

    Registered only while the module is active — see `discover` — so a
    disabled catalog's `plugins` output, schema, and write-gate declarations
    are unchanged from base Cadastre.
    """
    sources = (
        ("work-git", ("repo_checkout",)),
        ("work-github", ("forge_item",)),
        ("work-markdown", ("markdown_finding",)),
    )
    result: list[RegisteredPlugin] = []
    for name, kinds in sources:
        declarations = tuple(
            declaration_for(kind, authority="source", plugin=name) for kind in kinds
        )
        info = PluginInfo(name, "1", ("Work",), declarations)
        result.append(RegisteredPlugin(name, info, "cadastre (in-tree, manifest)"))
    return tuple(result)


def _module_info(loaded: Any) -> PluginInfo:
    if isinstance(loaded, ModuleType):
        loaded = getattr(loaded, "PLUGIN", loaded)
    value = getattr(loaded, "plugin_info", getattr(loaded, "info", loaded))
    if callable(value):
        value = value()
    if isinstance(value, PluginInfo):
        return value
    if isinstance(value, dict):
        return parse_plugin_info(value)
    raise TypeError("plugin must expose PLUGIN, info, or plugin_info")


def _file_info(path: Path) -> PluginInfo:
    module_name = f"cadastre_local_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load plugin file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return _module_info(module)
    finally:
        sys.modules.pop(module_name, None)
