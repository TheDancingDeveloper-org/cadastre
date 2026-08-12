"""Provenance and staleness.

Every query return type carries per-source `as_of`; `Response` refuses to exist
without it, so no plugin data path can bypass provenance.

An agent handed a two-week-old port map acts on it without hesitating, so
staleness is not decoration — it is the one piece of metadata that changes what
the reader should do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar

from cadastre.core.errors import CadastreError

#: D4 — 24h default, overridable per capability in declared/plugins.yaml.
DEFAULT_TTL_SECONDS = 86_400

#: Capabilities whose data goes wrong fastest. A hardware inventory tolerates
#: days; a port map does not (DESIGN §2.5).
DEFAULT_TTL_BY_CAPABILITY: dict[str, int] = {
    "inventory.list": 86_400,
    "network.list": 86_400,
    "network.members": 3_600,
    "dns.zones": 86_400,
    "dns.records": 3_600,
    "secret.list": 86_400,
    "secret.stat": 86_400,
    "vcs.repos": 86_400,
    "ci.pipelines": 86_400,
    "ci.status": 900,
    "endpoint.list": 3_600,
}


def parse_timestamp(value: str) -> datetime:
    """RFC 3339. Accepts a trailing Z, which `fromisoformat` predates."""
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CadastreError(f"not an RFC 3339 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Provenance:
    """Where one piece of the answer came from, and whether to trust its age."""

    source: str
    plugin: str
    as_of: str
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    stale: bool = False
    #: Set when the source could not be refreshed. A failing plugin degrades
    #: to a stale source; it never fails the command (DESIGN §2.2).
    error: str | None = None

    def age(self, now: datetime) -> timedelta:
        return now - parse_timestamp(self.as_of)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "plugin": self.plugin,
            "as_of": self.as_of,
            "ttl_seconds": self.ttl_seconds,
            "stale": self.stale,
        }
        if self.error:
            out["error"] = self.error
        return out


def ttl_for(capability: str, overrides: dict[str, int] | None = None) -> int:
    """Freshness threshold for a capability, operator overrides winning."""
    if overrides:
        if capability in overrides:
            return overrides[capability]
        prefix = capability.split(".", 1)[0]
        if prefix in overrides:
            return overrides[prefix]
        if "default" in overrides:
            return overrides["default"]
    return DEFAULT_TTL_BY_CAPABILITY.get(capability, DEFAULT_TTL_SECONDS)


def evaluate(
    provenance: Provenance, now: datetime, *, ttl_seconds: int | None = None
) -> Provenance:
    """Re-decide `stale` against a clock. A source already marked stale by its
    collector stays stale — a failure to refresh is not cured by being recent."""
    ttl = ttl_seconds if ttl_seconds is not None else provenance.ttl_seconds
    aged = provenance.age(now) > timedelta(seconds=ttl)
    return Provenance(
        source=provenance.source,
        plugin=provenance.plugin,
        as_of=provenance.as_of,
        ttl_seconds=ttl,
        stale=provenance.stale or aged,
        error=provenance.error,
    )


def declared_provenance(as_of: str) -> Provenance:
    """`declared/` is authoritative and never stale — it is not evidence about
    the world, it is the statement of intent the world is compared against
    (DESIGN §2.1). Its `as_of` is the catalog's own last change."""
    return Provenance(
        source="declared",
        plugin="static",
        as_of=as_of,
        ttl_seconds=0,
        stale=False,
    )


T = TypeVar("T")


@dataclass(frozen=True)
class Response(Generic[T]):
    """A result and where it came from.

    `provenance` has no default and an empty tuple is rejected: a code path
    returning data without per-source `as_of` is a bug, so it is made
    unrepresentable rather than discouraged.
    """

    result: T
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        if not self.provenance:
            raise CadastreError(
                "a Response must carry provenance for at least one source"
            )

    @property
    def stale_sources(self) -> tuple[Provenance, ...]:
        return tuple(p for p in self.provenance if p.stale)

    def to_dict(self, result: Any = None) -> dict[str, Any]:
        return {
            "result": self.result if result is None else result,
            "provenance": [p.to_dict() for p in self.sorted_provenance()],
        }

    def sorted_provenance(self) -> tuple[Provenance, ...]:
        return tuple(sorted(self.provenance, key=lambda p: (p.source, p.plugin)))


@dataclass
class ProvenanceSet:
    """Accumulates sources while a command computes, deduplicating by source."""

    items: dict[str, Provenance] = field(default_factory=dict)

    def add(self, provenance: Provenance) -> None:
        existing = self.items.get(provenance.source)
        # Keep the worst news: a stale reading of a source is the one that
        # should reach the reader.
        if existing is None or (provenance.stale and not existing.stale):
            self.items[provenance.source] = provenance

    def extend(self, items: object) -> None:
        for item in items:  # type: ignore[attr-defined]
            self.add(item)

    def frozen(self) -> tuple[Provenance, ...]:
        return tuple(sorted(self.items.values(), key=lambda p: (p.source, p.plugin)))
