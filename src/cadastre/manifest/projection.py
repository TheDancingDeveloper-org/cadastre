"""Manifest work-item projection and cross-kind drift join (MANIFEST.md R06).

This is a pure function over the declared work model and collected work
evidence. It never merges declared and observed state, and it never treats
incomplete or out-of-coverage evidence as absence — see §5.1/§8.2 R06.

This first implementation does not yet persist divergences through the core
trust ledger (``cadastre.core.trust``); that requires the divergence-identity
decision from the "five findings" section of MANIFEST.md and is tracked as
follow-up work, not done here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cadastre.core.model import Repo
from cadastre.core.provenance import Provenance
from cadastre.manifest.model import (
    ForgeItem,
    MarkdownFinding,
    RepoCheckout,
    WorkItem,
    WorkLink,
)
from cadastre.manifest.ranking import RankedItem

CATEGORIES = (
    "link-target-missing",
    "unlinked-forge-item",
    "unlinked-markdown-finding",
    "reflected-field-differs",
    "completion-differs",
    "markdown-completion-differs",
    "evidence-incomplete",
)

_FORGE_REF = re.compile(r"\A(?P<forge>[^:]+):(?P<repo>[^#]+)#(?P<ref>\S+)\Z")


@dataclass(frozen=True)
class Divergence:
    category: str
    work_item: str | None
    forge: str | None
    repo: str
    ref: str | None
    field: str | None
    detail: str


@dataclass(frozen=True)
class CheckoutEvidence:
    checkout: RepoCheckout
    provenance: Provenance


@dataclass(frozen=True)
class ProjectRow:
    repo: str
    open: int
    done: int
    cancelled: int
    top: RankedItem | None
    open_with_open_blocker: int
    checkouts: tuple[CheckoutEvidence, ...]
    any_dirty: bool | None
    tracking_ref_mismatches: int | None
    primary_checkout: str | None
    freshness: str


@dataclass(frozen=True)
class ProjectsProjection:
    projects: tuple[ProjectRow, ...]
    unmatched_checkouts: tuple[CheckoutEvidence, ...]
    source_provenance: tuple[Provenance, ...]


def _freshness(evidence: tuple[CheckoutEvidence, ...]) -> str:
    if not evidence:
        return "unknown"
    provenance = tuple(item.provenance for item in evidence)
    if any(item.error for item in provenance):
        return "failed"
    if any(item.stale for item in provenance):
        return "stale"
    return "fresh"


def build_projects(
    *,
    repos: list[Repo],
    ranked: tuple[RankedItem, ...],
    checkout_evidence: list[CheckoutEvidence],
    source_provenance: tuple[Provenance, ...] = (),
) -> ProjectsProjection:
    """Build the deterministic, transport-neutral workspace register view."""
    declared_ids = {repo.id for repo in repos}
    all_items_by_id = {result.item.id: result.item for result in ranked}
    items_by_repo: dict[str, list[RankedItem]] = {repo: [] for repo in declared_ids}
    for result in ranked:
        if result.item.repo in items_by_repo:
            items_by_repo[result.item.repo].append(result)
    checkouts_by_repo: dict[str, list[CheckoutEvidence]] = {
        repo: [] for repo in declared_ids
    }
    unmatched: list[CheckoutEvidence] = []
    for checkout_item in checkout_evidence:
        if checkout_item.checkout.repo in checkouts_by_repo:
            checkouts_by_repo[checkout_item.checkout.repo].append(checkout_item)
        else:
            unmatched.append(checkout_item)

    rows: list[ProjectRow] = []
    for repo in sorted(declared_ids):
        selected = items_by_repo[repo]
        open_items = [result for result in selected if result.item.state == "open"]
        top = next((result for result in selected if result.eligible), None)
        blocked = sum(
            1
            for result in open_items
            if any(
                dependency.state == "open"
                for ident in result.item.blocked_by
                if (dependency := all_items_by_id.get(ident)) is not None
            )
        )
        evidence = tuple(
            sorted(
                checkouts_by_repo[repo],
                key=lambda item: (
                    item.checkout.id,
                    item.provenance.source,
                    item.provenance.as_of,
                ),
            )
        )
        established_tracking = [
            item.checkout.tracking_ref_matches
            for item in evidence
            if item.checkout.tracking_ref_matches is not None
        ]
        rows.append(
            ProjectRow(
                repo=repo,
                open=len(open_items),
                done=sum(result.item.state == "done" for result in selected),
                cancelled=sum(result.item.state == "cancelled" for result in selected),
                top=top,
                open_with_open_blocker=blocked,
                checkouts=evidence,
                any_dirty=(
                    any(item.checkout.dirty for item in evidence) if evidence else None
                ),
                tracking_ref_mismatches=(
                    sum(value is False for value in established_tracking)
                    if established_tracking
                    else None
                ),
                primary_checkout=evidence[0].checkout.id if evidence else None,
                freshness=_freshness(evidence),
            )
        )
    rows.sort(
        key=lambda row: (
            row.top is None,
            -(row.top.score if row.top is not None else 0),
            row.repo,
        )
    )
    unmatched.sort(
        key=lambda item: (
            item.checkout.repo,
            item.checkout.id,
            item.provenance.source,
        )
    )
    return ProjectsProjection(
        projects=tuple(rows),
        unmatched_checkouts=tuple(unmatched),
        source_provenance=tuple(
            sorted(source_provenance, key=lambda item: (item.source, item.plugin))
        ),
    )


def parse_forge_ref(value: str) -> tuple[str, str, str] | None:
    """Parse the ``FORGE:OWNER/REPO#NUMBER`` marker syntax from §8.2 R03."""
    match = _FORGE_REF.match(value)
    if not match:
        return None
    return match.group("forge"), match.group("repo"), match.group("ref")


def _forge_key(forge: str, repo: str, kind: str, ref: str) -> tuple[str, str, str, str]:
    return (forge, repo, kind, ref)


def build_drift(
    *,
    links: list[WorkLink],
    items: list[WorkItem],
    forge_items: list[ForgeItem],
    findings: list[MarkdownFinding],
    forge_coverage: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[Divergence, ...]:
    """Join declared work links against collected forge and Markdown evidence.

    ``forge_coverage`` names the ``(forge, repo)`` pairs with fresh, complete
    ``work.items`` evidence. A link target absent from an uncovered pair is
    ``evidence-incomplete``, never ``link-target-missing`` — coverage, not
    mere absence from the payload, is what licenses a missing-target claim.
    """
    items_by_id = {item.id: item for item in items}
    forge_by_key = {
        _forge_key(fi.forge, fi.repo, fi.kind, fi.ref): fi for fi in forge_items
    }
    claimed_forge_keys: set[tuple[str, str, str, str]] = set()
    claimed_forge_refs: set[tuple[str, str, str]] = set()
    result: list[Divergence] = []

    for link in links:
        key = _forge_key(link.forge, link.repo, link.kind, link.ref)
        claimed_forge_keys.add(key)
        claimed_forge_refs.add((link.forge, link.repo, link.ref))
        target = forge_by_key.get(key)
        covered = (link.forge, link.repo) in forge_coverage

        if target is None:
            if not covered:
                result.append(
                    Divergence(
                        "evidence-incomplete",
                        link.work_item,
                        link.forge,
                        link.repo,
                        link.ref,
                        None,
                        f"no fresh, complete forge coverage for "
                        f"{link.forge}:{link.repo}",
                    )
                )
            elif link.required:
                result.append(
                    Divergence(
                        "link-target-missing",
                        link.work_item,
                        link.forge,
                        link.repo,
                        link.ref,
                        None,
                        f"required link target {link.kind}#{link.ref} absent from "
                        f"fresh, complete forge coverage",
                    )
                )
            continue

        item = items_by_id.get(link.work_item)
        expected_done = link.completion
        if "completion" in link.reflect and target.state != expected_done:
            result.append(
                Divergence(
                    "completion-differs",
                    link.work_item,
                    link.forge,
                    link.repo,
                    link.ref,
                    "completion",
                    f"declared completion {expected_done!r} != forge state "
                    f"{target.state!r}",
                )
            )
        for field in link.reflect:
            if field == "completion":
                continue
            declared = getattr(item, field, None) if item is not None else None
            observed = getattr(target, field, None)
            if declared is not None and declared != observed:
                result.append(
                    Divergence(
                        "reflected-field-differs",
                        link.work_item,
                        link.forge,
                        link.repo,
                        link.ref,
                        field,
                        f"declared {field} {declared!r} != forge {field} {observed!r}",
                    )
                )

    for fi in forge_items:
        key = _forge_key(fi.forge, fi.repo, fi.kind, fi.ref)
        if key not in claimed_forge_keys:
            result.append(
                Divergence(
                    "unlinked-forge-item",
                    None,
                    fi.forge,
                    fi.repo,
                    fi.ref,
                    None,
                    f"{fi.forge}:{fi.repo}:{fi.kind}#{fi.ref} has no work_link",
                )
            )

    for finding in findings:
        if finding.work_item is not None or finding.forge_ref is not None:
            parsed = (
                parse_forge_ref(finding.forge_ref)
                if finding.forge_ref is not None
                else None
            )
            if finding.forge_ref is not None and parsed is None:
                result.append(
                    Divergence(
                        "evidence-incomplete",
                        finding.work_item,
                        None,
                        finding.repo,
                        None,
                        None,
                        f"unparseable forge reference on {finding.path}:{finding.line}",
                    )
                )
                continue
            if parsed is not None:
                forge, repo, ref = parsed
                if (forge, repo, ref) not in claimed_forge_refs:
                    continue
                matching = [
                    link
                    for link in links
                    if (link.forge, link.repo, link.ref) == (forge, repo, ref)
                ]
                for link in matching:
                    target = forge_by_key.get(
                        _forge_key(link.forge, link.repo, link.kind, link.ref)
                    )
                    if target is None:
                        continue
                    forge_done = target.state == link.completion
                    if finding.checked != forge_done:
                        result.append(
                            Divergence(
                                "markdown-completion-differs",
                                link.work_item,
                                link.forge,
                                link.repo,
                                link.ref,
                                "checked",
                                f"Markdown checkbox at {finding.path}:{finding.line} "
                                f"is {finding.checked} but forge completion is "
                                f"{forge_done}",
                            )
                        )
            continue
        result.append(
            Divergence(
                "unlinked-markdown-finding",
                None,
                None,
                finding.repo,
                None,
                None,
                f"{finding.path}:{finding.line} has no work marker",
            )
        )

    return tuple(result)
