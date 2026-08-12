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
