"""Schema declarations for the catalog-owned Manifest register."""

from __future__ import annotations

from cadastre.core.spec import EntitySpec, FieldSpec
from cadastre.manifest.model import (
    ForgeItem,
    MarkdownFinding,
    RepoCheckout,
    RevisionCheck,
    WorkInitiative,
    WorkItem,
    WorkLink,
    WorkOrigin,
)


def _common() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec("id", "str", required=True, description="Stable identifier."),
        FieldSpec("tags", "list[str]"),
        FieldSpec("notes", "str"),
        FieldSpec("extra", "mapping"),
    )


ORIGIN = (
    FieldSpec("path", "str", required=True),
    FieldSpec("line", "int", required=True),
    FieldSpec("digest", "str", required=True),
    FieldSpec("run", "str", required=True),
)


ENTITY_SPECS: dict[str, EntitySpec] = {
    "work_initiative": EntitySpec(
        "work_initiative",
        WorkInitiative,
        "A catalog-owned group of related work.",
        _common()
        + (
            FieldSpec("title", "str", required=True),
            FieldSpec("weight", "int", required=True),
        ),
    ),
    "work_item": EntitySpec(
        "work_item",
        WorkItem,
        "A catalog-owned unit of work.",
        _common()
        + (
            FieldSpec("title", "str", required=True),
            FieldSpec(
                "state", "str", required=True, enum=("open", "done", "cancelled")
            ),
            FieldSpec(
                "priority", "str", required=True, enum=("p0", "p1", "p2", "p3", "p4")
            ),
            FieldSpec("created_at", "str", required=True),
            FieldSpec("initiative", "ref", ref="work_initiative"),
            FieldSpec("order", "int"),
            FieldSpec("effort", "int"),
            FieldSpec("repo", "ref", ref="repo"),
            FieldSpec("blocked_by", "list[ref]", ref="work_item"),
            FieldSpec("origin", "list[obj]", fields=ORIGIN, cls=WorkOrigin),
        ),
    ),
    "work_link": EntitySpec(
        "work_link",
        WorkLink,
        "A declared link from work to a forge item.",
        _common()
        + (
            FieldSpec("work_item", "ref", ref="work_item", required=True),
            FieldSpec("forge", "str", required=True),
            FieldSpec("repo", "str", required=True),
            FieldSpec("kind", "str", required=True, enum=("issue", "pull_request")),
            FieldSpec("ref", "str", required=True),
            FieldSpec("completion", "str", required=True, enum=("closed", "merged")),
            FieldSpec("required", "bool"),
            FieldSpec("reflect", "list[str]"),
        ),
    ),
}

ENTITY_SPECS.update(
    {
        "forge_item": EntitySpec(
            "forge_item",
            ForgeItem,
            "A forge issue or pull request.",
            _common()
            + (
                FieldSpec("forge", "str", required=True),
                FieldSpec("repo", "str", required=True),
                FieldSpec("kind", "str", required=True, enum=("issue", "pull_request")),
                FieldSpec("ref", "str", required=True),
                FieldSpec("title", "str", required=True),
                FieldSpec("state", "str", required=True),
                FieldSpec("created_at", "str"),
                FieldSpec("updated_at", "str"),
                FieldSpec("draft", "bool"),
                FieldSpec("head_revision", "str"),
                FieldSpec("merge_revision", "str"),
                FieldSpec("url", "str"),
            ),
        ),
        "markdown_finding": EntitySpec(
            "markdown_finding",
            MarkdownFinding,
            "A task-list finding in Markdown.",
            _common()
            + (
                FieldSpec("repo", "str", required=True),
                FieldSpec("path", "str", required=True),
                FieldSpec("line", "int", required=True),
                FieldSpec("text", "str", required=True),
                FieldSpec("checked", "bool"),
                FieldSpec("work_item", "ref", ref="work_item"),
                FieldSpec("forge_ref", "str"),
                FieldSpec("heading", "str"),
            ),
        ),
        "repo_checkout": EntitySpec(
            "repo_checkout",
            RepoCheckout,
            "A local checkout observation.",
            _common()
            + (
                FieldSpec("repo", "str", required=True),
                FieldSpec("head_revision", "str", required=True),
                FieldSpec("branch", "str"),
                FieldSpec("dirty", "bool"),
                FieldSpec("upstream", "str"),
                FieldSpec("tracking_ref_matches", "bool"),
                FieldSpec("last_head_change", "str"),
                FieldSpec("worktree", "str"),
            ),
        ),
        "revision_check": EntitySpec(
            "revision_check",
            RevisionCheck,
            "A selected revision check result.",
            _common()
            + (
                FieldSpec("system", "str", required=True),
                FieldSpec("repo", "str", required=True),
                FieldSpec("revision", "str", required=True),
                FieldSpec("check_id", "str", required=True),
                FieldSpec("state", "str", required=True),
                FieldSpec("started_at", "str"),
                FieldSpec("completed_at", "str"),
                FieldSpec("url", "str"),
            ),
        ),
    }
)

RELATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("initiative", "work_item", "initiative", "work_initiative"),
    ("blocked_by", "work_item", "blocked_by", "work_item"),
    ("work_item", "work_link", "work_item", "work_item"),
)
