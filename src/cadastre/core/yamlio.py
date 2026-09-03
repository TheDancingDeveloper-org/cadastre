"""YAML in, YAML out.

In: a SafeLoader that keeps line numbers, so a bad field can name its line.
Out: one canonical form. Determinism is a stated property (DESIGN §7), and the
round-trip test in M1 depends on there being exactly one way to write a file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from cadastre.core.errors import CatalogError, CatalogIssue, Located


class LinedDict(dict[str, Any]):
    """A mapping that remembers where it and its keys came from."""

    line: int = 0
    key_lines: dict[str, int]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key_lines = {}

    def line_of(self, key: str) -> int:
        return self.key_lines.get(key, self.line)


class _LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _LineLoader, node: yaml.MappingNode) -> LinedDict:
    loader.flatten_mapping(node)
    mapping = LinedDict(loader.construct_pairs(node, deep=True))
    mapping.line = node.start_mark.line + 1
    for key_node, _ in node.value:
        if isinstance(key_node.value, str):
            mapping.key_lines[key_node.value] = key_node.start_mark.line + 1
    return mapping


_LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _construct_untagged(loader: _LineLoader, node: yaml.Node) -> Any:
    """Construct a node as if it carried no explicit tag.

    Used to see through a Compose merge tag to the value beneath it, resolving
    scalars to their implicit type (int, bool, null) rather than leaving them
    as the raw string a tagged construction would return.
    """
    if isinstance(node, yaml.ScalarNode):
        implicit = loader.resolve(yaml.ScalarNode, node.value, (True, False))
        return loader.construct_object(
            yaml.ScalarNode(
                implicit, node.value, node.start_mark, node.end_mark, node.style
            ),
            deep=True,
        )
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return _construct_mapping(loader, node)
    return None


def _construct_reset(loader: _LineLoader, node: yaml.Node) -> None:
    """`!reset` marks a key for removal when a Compose override is merged; as a
    standalone value it carries no content, so it resolves to null."""
    return None


# Compose override files (`!reset`, `!override`) are the reason `cadastre check`
# could not validate an overlay: the base compose parsed clean, but the tags in
# the overlay stopped at the loader. These tags only appear in Compose, never in
# a declared catalog file, so teaching the shared loader to see through them
# makes overlays checkable without loosening anything the catalog relies on.
_LineLoader.add_constructor("!reset", _construct_reset)
_LineLoader.add_constructor("!override", _construct_untagged)


def load_yaml(path: Path, *, rel: str | None = None) -> Any:
    """Parse one YAML file, keeping line numbers. An empty file is ``None``."""
    display = rel or str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(
            [CatalogIssue(Located(display), "<file>", f"cannot read: {exc.strerror}")]
        ) from exc
    try:
        return yaml.load(text, Loader=_LineLoader)
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else None
        raise CatalogError(
            [
                CatalogIssue(
                    Located(display, line),
                    "<syntax>",
                    (exc.problem or "invalid YAML").strip(),
                    expected="well-formed YAML",
                )
            ]
        ) from exc
    except yaml.YAMLError as exc:
        raise CatalogError(
            [CatalogIssue(Located(display), "<syntax>", str(exc))]
        ) from exc


class _CanonicalDumper(yaml.SafeDumper):
    """Block style, insertion order, no aliases, no line folding."""

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        # Sequences indent under their key: the form humans write by hand.
        super().increase_indent(flow, False)


def dump_yaml(data: Any) -> str:
    """The canonical serialisation. Byte-stable for equal inputs."""
    return yaml.dump(
        data,
        Dumper=_CanonicalDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
