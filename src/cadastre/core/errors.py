"""Located errors.

A parse or validation failure names the file, the line, the field, and the
shape that was expected. A malformed field produces an error
naming the file, line, and expected shape."
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Located:
    """Where a problem is."""

    path: str
    line: int | None = None

    def __str__(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass(frozen=True)
class CatalogIssue:
    """One located problem with the catalog."""

    where: Located
    field: str
    message: str
    expected: str | None = None

    def render(self) -> str:
        head = f"{self.where}: {self.field}: {self.message}"
        return f"{head}\n  expected: {self.expected}" if self.expected else head


class CadastreError(Exception):
    """Base for every error Cadastre raises deliberately."""


@dataclass
class CatalogError(CadastreError):
    """One or more located catalog problems."""

    issues: list[CatalogIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.render())

    def render(self) -> str:
        return "\n".join(issue.render() for issue in self.issues)

    def __str__(self) -> str:
        return self.render()


class PluginError(CadastreError):
    """A plugin misbehaved in a way that is not a protocol-level error."""


class UsageError(CadastreError):
    """The invocation was wrong. Exits 2, never a traceback."""


class UnknownKindError(UsageError):
    """A requested entity kind is not part of the catalog model."""


class MissingEntityError(UsageError):
    """A requested entity identifier has no matching catalog entity."""


class AmbiguousEntityError(UsageError):
    """An identifier names more than one entity kind."""
