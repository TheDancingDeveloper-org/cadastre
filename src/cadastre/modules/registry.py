"""Immutable active entity registry.

The base tables remain available for compatibility and are never expanded by
an installed optional dependency. Callers that opt into a module pass this
registry explicitly through their model-loading path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from cadastre.core import model
from cadastre.core.model import Entity
from cadastre.core.spec import EntitySpec
from cadastre.modules.config import ModulesFile


@dataclass(frozen=True)
class EntityRegistry:
    classes: Mapping[str, type[Entity]]
    specs: Mapping[str, EntitySpec]
    dirs: Mapping[str, str]
    relations: tuple[tuple[str, str, str, str], ...]
    modules: ModulesFile

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self.classes)

    def kind_for_class(self, cls: type[Entity]) -> str:
        for kind, entity_cls in self.classes.items():
            if entity_cls is cls:
                return kind
        raise KeyError(cls)


def base_registry() -> EntityRegistry:
    from cadastre.core.spec import ENTITY_SPECS

    return EntityRegistry(
        classes=MappingProxyType(dict(model.ENTITY_CLASSES)),
        specs=MappingProxyType(dict(ENTITY_SPECS)),
        dirs=MappingProxyType(dict(model.KIND_DIRS)),
        relations=model.RELATIONS,
        modules=ModulesFile(),
    )


def active_registry(modules: ModulesFile | None = None) -> EntityRegistry:
    """Return base kinds plus explicitly enabled module contributions."""
    modules = modules or ModulesFile()
    base = base_registry()
    if not modules.enabled("manifest"):
        return EntityRegistry(
            base.classes, base.specs, base.dirs, base.relations, modules
        )

    from cadastre.manifest import model as manifest_model
    from cadastre.manifest.spec import ENTITY_SPECS, RELATIONS

    classes = dict(base.classes)
    specs = dict(base.specs)
    dirs = dict(base.dirs)
    for kind, cls in (
        ("work_initiative", manifest_model.WorkInitiative),
        ("work_item", manifest_model.WorkItem),
        ("work_link", manifest_model.WorkLink),
        ("forge_item", manifest_model.ForgeItem),
        ("markdown_finding", manifest_model.MarkdownFinding),
        ("repo_checkout", manifest_model.RepoCheckout),
        ("revision_check", manifest_model.RevisionCheck),
    ):
        if kind in classes or kind in dirs or kind in specs:
            raise ValueError(f"Manifest kind collides with base registry: {kind}")
        classes[kind] = cls
        specs[kind] = ENTITY_SPECS[kind]
        dirs[kind] = kind.replace("_", "-") + "s"
    relations = base.relations + RELATIONS
    if len(relations) != len(set(relations)):
        raise ValueError("duplicate relation in active registry")
    return EntityRegistry(
        MappingProxyType(classes),
        MappingProxyType(specs),
        MappingProxyType(dirs),
        relations,
        modules,
    )
