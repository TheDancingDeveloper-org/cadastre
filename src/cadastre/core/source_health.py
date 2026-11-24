"""Freshness health of observed sources.

A source past its TTL is `stale`; that is a trust label on answers. A source
that stays stale for more than twice its TTL, or whose collector keeps
failing, is an alert: nobody is looking at the estate through it any more.
This module decides that, deterministically, from evidence and a clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from cadastre.core.observed import ObservedSource
from cadastre.core.provenance import parse_timestamp

#: A source older than this multiple of its TTL is overdue.
OVERDUE_FACTOR = 2


@dataclass(frozen=True)
class SourceHealth:
    source: str
    plugin: str
    as_of: str
    ttl_seconds: int
    age_seconds: float
    error: str | None

    @property
    def stale(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    @property
    def overdue(self) -> bool:
        """Stale beyond OVERDUE_FACTOR x TTL."""
        return self.age_seconds > self.ttl_seconds * OVERDUE_FACTOR

    @property
    def erroring(self) -> bool:
        """The collector reported an error and the evidence is past its TTL.

        A single failed run on fresh evidence is not yet persistent.
        """
        return self.error is not None and self.stale

    @property
    def alerting(self) -> bool:
        return self.overdue or self.erroring

    def reason(self) -> str:
        parts = [
            f"last collected {self.as_of} "
            f"({int(self.age_seconds)}s ago, ttl {self.ttl_seconds}s)"
        ]
        if self.overdue:
            parts.append(f"stale for more than {OVERDUE_FACTOR}x ttl")
        if self.error:
            parts.append(f"collector error: {self.error}")
        return "; ".join(parts)


def assess(
    sources: Iterable[ObservedSource],
    now: datetime,
    *,
    ttl_overrides: dict[str, int] | None = None,
) -> list[SourceHealth]:
    out: list[SourceHealth] = []
    for source in sources:
        provenance = source.provenance(ttl_overrides=ttl_overrides)
        age = (now - parse_timestamp(source.as_of)).total_seconds()
        out.append(
            SourceHealth(
                source=source.source,
                plugin=source.plugin,
                as_of=source.as_of,
                ttl_seconds=provenance.ttl_seconds,
                age_seconds=max(age, 0.0),
                error=source.error,
            )
        )
    return sorted(out, key=lambda h: (h.source, h.plugin))


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(health: Iterable[SourceHealth]) -> str:
    """Prometheus text exposition format."""
    rows = list(health)
    lines: list[str] = []

    def family(
        name: str, help_text: str, values: list[tuple[SourceHealth, float]]
    ) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for item, value in values:
            labels = f'source="{_label(item.source)}",plugin="{_label(item.plugin)}"'
            lines.append(f"{name}{{{labels}}} {value:g}")

    family(
        "cadastre_source_age_seconds",
        "Seconds since the source was last collected.",
        [(h, h.age_seconds) for h in rows],
    )
    family(
        "cadastre_source_ttl_seconds",
        "Freshness threshold of the source in seconds.",
        [(h, h.ttl_seconds) for h in rows],
    )
    family(
        "cadastre_source_stale",
        "1 when the source is past its TTL.",
        [(h, float(h.stale)) for h in rows],
    )
    family(
        "cadastre_source_overdue",
        f"1 when the source is stale for more than {OVERDUE_FACTOR}x its TTL"
        " or its collector keeps failing.",
        [(h, float(h.alerting)) for h in rows],
    )
    family(
        "cadastre_source_error",
        "1 when the last collection of the source reported an error.",
        [(h, float(h.error is not None)) for h in rows],
    )
    return "\n".join(lines) + "\n"
