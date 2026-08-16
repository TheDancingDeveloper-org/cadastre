"""Read-only CLI projections for the opt-in Manifest module."""

from __future__ import annotations

from typing import Any

from cadastre.cli.session import Session
from cadastre.core.errors import UsageError
from cadastre.core.model import Repo
from cadastre.manifest.model import (
    ForgeItem,
    MarkdownFinding,
    RepoCheckout,
    WorkInitiative,
    WorkItem,
    WorkLink,
)
from cadastre.manifest.projection import (
    CheckoutEvidence,
    Divergence,
    ProjectRow,
    build_drift,
    build_projects,
)
from cadastre.manifest.ranking import RankedItem, rank
from cadastre.render.document import Bullets, Document, Fields, Section, Table


def _require(session: Session) -> None:
    if not session.modules.enabled("manifest"):
        raise UsageError(
            "Manifest is disabled; add modules.yaml with manifest.enabled: true"
        )


def _ranked(session: Session) -> tuple[RankedItem, ...]:
    return rank(
        [
            item
            for item in session.catalog.all("work_item")
            if isinstance(item, WorkItem)
        ],
        [
            item
            for item in session.catalog.all("work_initiative")
            if isinstance(item, WorkInitiative)
        ],
        now=session.now,
    )


def _row(result: RankedItem) -> tuple[str, ...]:
    item = result.item
    return (
        item.id,
        item.title,
        item.state,
        item.priority,
        str(result.score),
        result.confidence,
    )


def brief(session: Session) -> Document:
    _require(session)
    ranked = _ranked(session)
    open_items = [result for result in ranked if result.item.state == "open"]
    return Document(
        title="cadastre manifest brief",
        sections=(
            Section(
                "Manifest",
                (
                    Fields(
                        (
                            ("work items", str(len(ranked))),
                            ("open", str(len(open_items))),
                            ("confidence", "declared-only"),
                        )
                    ),
                ),
                note=(
                    "External forge and deployment evidence is not included "
                    "in this projection. Item lists come from "
                    "`manifest backlog` and `manifest next`, which are bounded."
                ),
            ),
        ),
        provenance=session.provenance(),
        # A brief is counts and confidence, not contents: its size must not
        # grow with the register, or the session preamble every agent is told
        # to call first is the call most likely to exceed a client's result
        # limit. Ranked items are served by `backlog`/`next`, which bound them.
        data={
            "counts": {"work_items": len(ranked), "open": len(open_items)},
            "confidence": "declared-only",
        },
    )


def backlog(
    session: Session,
    *,
    state: str | None = None,
    initiative: str | None = None,
    repo: str | None = None,
    limit: int = 10,
) -> Document:
    _require(session)
    if limit < 1 or limit > 100:
        raise UsageError("manifest backlog limit must be between 1 and 100")
    ranked = _ranked(session)
    selected = [
        result
        for result in ranked
        if (state is None or result.item.state == state)
        and (initiative is None or result.item.initiative == initiative)
        and (repo is None or result.item.repo == repo)
    ][:limit]
    rows = tuple(_row(result) for result in selected)
    return Document(
        title="cadastre manifest backlog",
        sections=(
            Section(
                "Backlog",
                (
                    Table(
                        ("id", "title", "state", "priority", "score", "confidence"),
                        rows,
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "items": [_item_data(result) for result in selected],
            "limit": limit,
            "confidence": "declared-only",
        },
    )


def _observed_entities(session: Session, kind: str) -> list[Any]:
    result: list[Any] = []
    for source in session.observed:
        if source.ok:
            result.extend(source.entities.get(kind, []))
    return result


def _forge_coverage(session: Session) -> frozenset[tuple[str, str]]:
    """Repos with at least one fresh, complete ``forge_item`` observation.

    Conservative by construction: a repo with genuinely zero open issues and
    no fresh evidence would be undercounted here, producing
    ``evidence-incomplete`` instead of a real ``link-target-missing`` — never
    the other way around. See MANIFEST.md §5.1/§8.2 R06.
    """
    covered = set()
    for item in _observed_entities(session, "forge_item"):
        if isinstance(item, ForgeItem):
            covered.add((item.forge, item.repo))
    return frozenset(covered)


def _drift(session: Session) -> tuple[Divergence, ...]:
    links = [
        item for item in session.catalog.all("work_link") if isinstance(item, WorkLink)
    ]
    items = [
        item for item in session.catalog.all("work_item") if isinstance(item, WorkItem)
    ]
    forge_items = [
        item
        for item in _observed_entities(session, "forge_item")
        if isinstance(item, ForgeItem)
    ]
    findings = [
        item
        for item in _observed_entities(session, "markdown_finding")
        if isinstance(item, MarkdownFinding)
    ]
    return build_drift(
        links=links,
        items=items,
        forge_items=forge_items,
        findings=findings,
        forge_coverage=_forge_coverage(session),
    )


def _divergence_row(divergence: Divergence) -> tuple[str, ...]:
    return (
        divergence.category,
        divergence.work_item or "",
        divergence.repo,
        divergence.ref or "",
        divergence.field or "",
        divergence.detail,
    )


def drift(session: Session, *, repo: str | None = None) -> Document:
    _require(session)
    selected = [item for item in _drift(session) if repo is None or item.repo == repo]
    rows = tuple(_divergence_row(item) for item in selected)
    return Document(
        title="cadastre manifest drift",
        sections=(
            Section(
                "Divergences",
                (
                    Table(
                        ("category", "work_item", "repo", "ref", "field", "detail"),
                        rows,
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "divergences": [
                {
                    "category": item.category,
                    "work_item": item.work_item,
                    "forge": item.forge,
                    "repo": item.repo,
                    "ref": item.ref,
                    "field": item.field,
                    "detail": item.detail,
                }
                for item in selected
            ]
        },
    )


def repo(session: Session, repo: str) -> Document:
    _require(session)
    ranked = [result for result in _ranked(session) if result.item.repo == repo]
    checkouts = [
        item
        for item in _observed_entities(session, "repo_checkout")
        if item.repo == repo
    ]
    selected_drift = [item for item in _drift(session) if item.repo == repo]
    return Document(
        title=f"cadastre manifest repo {repo}",
        sections=(
            Section(
                "Work",
                (
                    Table(
                        ("id", "title", "state", "priority", "score", "confidence"),
                        tuple(_row(result) for result in ranked),
                    ),
                ),
            ),
            Section(
                "Divergences",
                (
                    Table(
                        ("category", "work_item", "repo", "ref", "field", "detail"),
                        tuple(_divergence_row(item) for item in selected_drift),
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "repo": repo,
            "items": [_item_data(result) for result in ranked],
            "checkouts": [
                {
                    "id": item.id,
                    "head_revision": item.head_revision,
                    "branch": item.branch,
                    "dirty": item.dirty,
                }
                for item in checkouts
            ],
            "divergences": [
                {
                    "category": item.category,
                    "work_item": item.work_item,
                    "ref": item.ref,
                    "field": item.field,
                    "detail": item.detail,
                }
                for item in selected_drift
            ],
        },
    )


def _checkout_data(evidence: CheckoutEvidence) -> dict[str, Any]:
    item = evidence.checkout
    return {
        "id": item.id,
        "repo": item.repo,
        "head_revision": item.head_revision,
        "branch": item.branch,
        "dirty": item.dirty,
        "upstream": item.upstream,
        "tracking_ref_matches": item.tracking_ref_matches,
        "last_head_change": item.last_head_change,
        "worktree": item.worktree,
        "provenance": evidence.provenance.to_dict(),
    }


def _project_data(row: ProjectRow) -> dict[str, Any]:
    return {
        "repo": row.repo,
        "counts": {
            "open": row.open,
            "done": row.done,
            "cancelled": row.cancelled,
        },
        "top": _item_data(row.top) if row.top is not None else None,
        "open_with_open_blocker": row.open_with_open_blocker,
        "checkout_summary": {
            "any_dirty": row.any_dirty,
            "tracking_ref_mismatches": row.tracking_ref_mismatches,
            "primary_checkout": row.primary_checkout,
            "freshness": row.freshness,
        },
        "checkouts": [_checkout_data(item) for item in row.checkouts],
        "confidence": "declared-only",
    }


def projects(session: Session) -> Document:
    _require(session)
    ranked = _ranked(session)
    source_provenance = tuple(
        source.provenance(ttl_overrides=session.plugins.freshness)
        for source in session.observed
        if "repo_checkout" in source.entities
        or source.plugin == "work-git"
        or "work.repo-state" in source.capabilities
    )
    evidence = [
        CheckoutEvidence(
            item, source.provenance(ttl_overrides=session.plugins.freshness)
        )
        for source in session.observed
        for item in source.entities.get("repo_checkout", [])
        if isinstance(item, RepoCheckout)
    ]
    projection = build_projects(
        repos=[item for item in session.catalog.all("repo") if isinstance(item, Repo)],
        ranked=ranked,
        checkout_evidence=evidence,
        source_provenance=source_provenance,
    )
    source_warnings = tuple(
        (
            item.source,
            "failed" if item.error else "stale",
            item.as_of,
            item.error or "evidence exceeded its freshness threshold",
        )
        for item in projection.source_provenance
        if item.stale or item.error
    )
    unmatched_rows = tuple(
        (
            item.checkout.repo,
            item.checkout.id,
            item.provenance.source,
            "failed"
            if item.provenance.error
            else ("stale" if item.provenance.stale else "fresh"),
        )
        for item in projection.unmatched_checkouts
    )
    project_rows = tuple(
        (
            row.repo,
            str(row.open),
            str(row.done),
            str(row.cancelled),
            row.top.item.id if row.top else "",
            str(row.top.score) if row.top else "",
            str(row.open_with_open_blocker),
            "unknown" if row.any_dirty is None else str(row.any_dirty).lower(),
            (
                "unknown"
                if row.tracking_ref_mismatches is None
                else str(row.tracking_ref_mismatches)
            ),
            row.primary_checkout or "unknown",
            row.freshness,
        )
        for row in projection.projects
    )
    return Document(
        title="cadastre manifest projects",
        # Evidence problems precede ranked content by contract (MANIFEST R08).
        sections=(
            Section(
                "Stale or failed checkout sources",
                (Table(("source", "state", "as_of", "detail"), source_warnings),),
            ),
            Section(
                "Unmatched checkouts",
                (
                    Table(
                        ("repo value", "checkout", "source", "freshness"),
                        unmatched_rows,
                    ),
                ),
                note="These checkout repo values match no declared repository id.",
            ),
            Section(
                "Projects",
                (
                    Table(
                        (
                            "repo",
                            "open",
                            "done",
                            "cancelled",
                            "top",
                            "score",
                            "blocked",
                            "any dirty",
                            "tracking mismatches",
                            "primary checkout",
                            "checkout freshness",
                        ),
                        project_rows,
                    ),
                ),
                note=(
                    "Ranking confidence is declared-only. Checkout freshness is "
                    "reported separately; tracking mismatch means only that local "
                    "revisions differ, not that work is ahead or unpushed."
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "projects": [_project_data(row) for row in projection.projects],
            "unmatched_checkouts": [
                _checkout_data(item) for item in projection.unmatched_checkouts
            ],
            "checkout_sources": [
                item.to_dict() for item in projection.source_provenance
            ],
            "confidence": "declared-only",
        },
    )


def next_(session: Session, *, limit: int = 10) -> Document:
    _require(session)
    if limit < 1 or limit > 100:
        raise UsageError("manifest next limit must be between 1 and 100")
    selected = [result for result in _ranked(session) if result.eligible][:limit]
    rows = tuple(_row(result) for result in selected)
    return Document(
        title="cadastre manifest next",
        sections=(
            Section(
                "Next",
                (
                    Table(
                        ("id", "title", "state", "priority", "score", "confidence"),
                        rows,
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={
            "items": [_item_data(result) for result in selected],
            "limit": limit,
            "confidence": "declared-only",
        },
    )


def why(session: Session, ident: str) -> Document:
    _require(session)
    result = next(
        (result for result in _ranked(session) if result.item.id == ident), None
    )
    if result is None:
        raise UsageError(f"unknown work item {ident!r}")
    contributions = [
        (part.name, f"{part.raw} x {part.weight} = {part.value}")
        for part in result.contributions
    ]
    data = _item_data(result)
    data["contributions"] = [
        {
            "name": part.name,
            "raw": part.raw,
            "weight": part.weight,
            "value": part.value,
        }
        for part in result.contributions
    ]
    return Document(
        title=f"cadastre manifest why {ident}",
        sections=(
            Section(
                "Score explanation",
                (
                    Fields(
                        (
                            ("item", ident),
                            ("eligible", str(result.eligible).lower()),
                            ("confidence", result.confidence),
                            ("score", str(result.score)),
                            (
                                "tie-break",
                                f"{result.tie_break[0]}, {result.tie_break[1]}",
                            ),
                        )
                    ),
                    Table(("contribution", "arithmetic"), tuple(contributions)),
                    Bullets(
                        ("Recompute score by summing the contribution values above.",)
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data=data,
    )


def _item_data(result: RankedItem) -> dict[str, Any]:
    item = result.item
    return {
        "id": item.id,
        "title": item.title,
        "state": item.state,
        "priority": item.priority,
        "score": result.score,
        "eligible": result.eligible,
        "confidence": result.confidence,
        "blocked_by": list(item.blocked_by),
    }
