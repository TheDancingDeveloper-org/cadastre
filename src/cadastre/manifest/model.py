"""Catalog-owned Manifest entities.

These dataclasses deliberately contain no forge client or ranking behavior.
They are declarations of work and links, not commands to an upstream system.
"""

from __future__ import annotations

from dataclasses import dataclass

from cadastre.core.model import Entity


class ManifestEntity(Entity):
    """Entity base whose kind is independent of the base registry."""

    @property
    def kind(self) -> str:
        return _KINDS[type(self)]


@dataclass(frozen=True)
class WorkInitiative(ManifestEntity):
    title: str = ""
    weight: int = 0


@dataclass(frozen=True)
class WorkOrigin:
    """One reviewed source line represented by a migrated work item."""

    path: str
    line: int
    digest: str
    run: str


@dataclass(frozen=True)
class WorkItem(ManifestEntity):
    title: str = ""
    state: str = "open"
    priority: str = "p3"
    created_at: str = ""
    initiative: str | None = None
    order: int | None = None
    effort: int | None = None
    repo: str | None = None
    blocked_by: tuple[str, ...] = ()
    origin: tuple[WorkOrigin, ...] = ()


@dataclass(frozen=True)
class WorkLink(ManifestEntity):
    work_item: str = ""
    forge: str = ""
    repo: str = ""
    kind: str = "issue"
    ref: str = ""
    completion: str = "closed"
    required: bool = False
    reflect: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForgeItem(ManifestEntity):
    forge: str = ""
    repo: str = ""
    kind: str = "issue"
    ref: str = ""
    title: str = ""
    state: str = "open"
    created_at: str | None = None
    updated_at: str | None = None
    draft: bool = False
    head_revision: str | None = None
    merge_revision: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class MarkdownFinding(ManifestEntity):
    repo: str = ""
    path: str = ""
    line: int = 0
    text: str = ""
    checked: bool = False
    work_item: str | None = None
    forge_ref: str | None = None
    heading: str | None = None


@dataclass(frozen=True)
class RepoCheckout(ManifestEntity):
    repo: str = ""
    head_revision: str = ""
    branch: str | None = None
    dirty: bool = False
    upstream: str | None = None
    tracking_ref_matches: bool | None = None
    last_head_change: str | None = None
    worktree: str | None = None


@dataclass(frozen=True)
class RevisionCheck(ManifestEntity):
    system: str = ""
    repo: str = ""
    revision: str = ""
    check_id: str = ""
    state: str = "unknown"
    started_at: str | None = None
    completed_at: str | None = None
    url: str | None = None


_KINDS = {
    WorkInitiative: "work_initiative",
    WorkItem: "work_item",
    WorkLink: "work_link",
    ForgeItem: "forge_item",
    MarkdownFinding: "markdown_finding",
    RepoCheckout: "repo_checkout",
    RevisionCheck: "revision_check",
}
