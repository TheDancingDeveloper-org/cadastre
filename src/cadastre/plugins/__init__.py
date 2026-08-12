"""The plugin contract and discovery surface.

Plugins contribute observations and declarations; the core owns storage and
policy.  An executable plugin and an in-process plugin are represented by the
same :class:`PluginInfo` value after discovery.
"""

from cadastre.plugins.contract import (
    FIELD_CLASSES,
    EntityDeclaration,
    PluginInfo,
    default_entity_declaration,
    identity_key,
    matches,
    parse_plugin_info,
    validate_plugin_info,
)
from cadastre.plugins.registry import PluginRegistry, RegisteredPlugin

__all__ = [
    "FIELD_CLASSES",
    "EntityDeclaration",
    "PluginInfo",
    "PluginRegistry",
    "RegisteredPlugin",
    "default_entity_declaration",
    "identity_key",
    "matches",
    "parse_plugin_info",
    "validate_plugin_info",
]
