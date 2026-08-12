"""Deterministic ranking for declared Manifest work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cadastre.manifest.model import WorkInitiative, WorkItem

PRIORITY = {"p0": 1000, "p1": 500, "p2": 250, "p3": 100, "p4": 0}


@dataclass(frozen=True)
class Contribution:
    name: str
    raw: int
    weight: int
    value: int


@dataclass(frozen=True)
class RankedItem:
    item: WorkItem
    score: int
    eligible: bool
    confidence: str
    contributions: tuple[Contribution, ...]
    tie_break: tuple[int, str]


def _created_at(item: WorkItem) -> datetime:
    return datetime.fromisoformat(item.created_at.replace("Z", "+00:00")).astimezone(
        UTC
    )


def _contribution(name: str, raw: int, weight: int = 1) -> Contribution:
    return Contribution(name, raw, weight, raw * weight)


def rank(
    items: list[WorkItem],
    initiatives: list[WorkInitiative],
    *,
    now: datetime,
) -> tuple[RankedItem, ...]:
    """Rank work using only declared inputs available in the register.

    Forge and deployment contributions are deliberately absent until fresh
    source evidence exists; those unknown signals are not treated as zero
    failures. The confidence communicates that distinction.
    """
    initiative_weights = {item.id: item.weight for item in initiatives}
    by_id = {item.id: item for item in items}
    dependants: dict[str, int] = {}
    for item in items:
        for blocker in item.blocked_by:
            if by_id.get(item.id, item).state == "open":
                dependants[blocker] = dependants.get(blocker, 0) + 1
    ranked: list[RankedItem] = []
    for item in items:
        open_blockers = sum(
            1 for ident in item.blocked_by if by_id.get(ident, item).state == "open"
        )
        eligible = item.state == "open" and open_blockers == 0
        age_days = max(0, (now.astimezone(UTC) - _created_at(item)).days)
        age_days = min(age_days, 90)
        open_dependants = min(dependants.get(item.id, 0), 10)
        contributions = (
            _contribution("priority", PRIORITY[item.priority]),
            _contribution(
                "initiative", initiative_weights.get(item.initiative or "", 0)
            ),
            _contribution("age", age_days),
            _contribution("blocking", open_dependants, 50),
            _contribution("external_evidence", 0),
        )
        score = sum(part.value for part in contributions) if eligible else 0
        tie_order = item.order if item.order is not None else 2**31 - 1
        ranked.append(
            RankedItem(
                item=item,
                score=score,
                eligible=eligible,
                confidence="declared-only",
                contributions=contributions,
                tie_break=(tie_order, item.id),
            )
        )
    return tuple(
        sorted(
            ranked,
            key=lambda result: (
                not result.eligible,
                -result.score,
                result.tie_break,
            ),
        )
    )
