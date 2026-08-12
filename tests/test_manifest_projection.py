from cadastre.manifest.model import ForgeItem, MarkdownFinding, WorkItem, WorkLink
from cadastre.manifest.projection import build_drift, parse_forge_ref


def _link(**kwargs: object) -> WorkLink:
    defaults: dict[str, object] = {
        "id": "l1",
        "work_item": "w1",
        "forge": "github",
        "repo": "org/repo",
        "kind": "issue",
        "ref": "1",
        "completion": "closed",
        "required": True,
        "reflect": (),
    }
    defaults.update(kwargs)
    return WorkLink(**defaults)  # type: ignore[arg-type]


def _item(**kwargs: object) -> WorkItem:
    defaults: dict[str, object] = {
        "id": "w1",
        "title": "Ship it",
        "state": "open",
        "priority": "p1",
        "created_at": "2026-08-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return WorkItem(**defaults)  # type: ignore[arg-type]


def _forge(**kwargs: object) -> ForgeItem:
    defaults: dict[str, object] = {
        "id": "github:org/repo:issue:1",
        "forge": "github",
        "repo": "org/repo",
        "kind": "issue",
        "ref": "1",
        "title": "Ship it",
        "state": "open",
    }
    defaults.update(kwargs)
    return ForgeItem(**defaults)  # type: ignore[arg-type]


def test_parse_forge_ref_extracts_forge_repo_ref() -> None:
    assert parse_forge_ref("github:org/repo#7") == ("github", "org/repo", "7")
    assert parse_forge_ref("not-a-ref") is None


def test_required_link_missing_from_fresh_coverage_is_link_target_missing() -> None:
    drift = build_drift(
        links=[_link()],
        items=[_item()],
        forge_items=[],
        findings=[],
        forge_coverage=frozenset({("github", "org/repo")}),
    )

    assert [d.category for d in drift] == ["link-target-missing"]


def test_link_target_absent_without_coverage_is_evidence_incomplete() -> None:
    drift = build_drift(links=[_link()], items=[_item()], forge_items=[], findings=[])

    assert [d.category for d in drift] == ["evidence-incomplete"]


def test_unlinked_forge_item_and_declared_only_work_item_no_drift() -> None:
    drift = build_drift(
        links=[],
        items=[_item()],
        forge_items=[_forge()],
        findings=[],
        forge_coverage=frozenset({("github", "org/repo")}),
    )

    assert [d.category for d in drift] == ["unlinked-forge-item"]


def test_unlinked_markdown_finding_without_a_marker() -> None:
    finding = MarkdownFinding(
        id="m1", repo="org/repo", path="TODO.md", line=3, text="do it", checked=False
    )

    drift = build_drift(links=[], items=[], forge_items=[], findings=[finding])

    assert [d.category for d in drift] == ["unlinked-markdown-finding"]


def test_reflected_title_and_completion_differ() -> None:
    link = _link(reflect=("title", "completion"))
    forge = _forge(title="Different title", state="open")

    drift = build_drift(
        links=[link],
        items=[_item(title="Ship it")],
        forge_items=[forge],
        findings=[],
        forge_coverage=frozenset({("github", "org/repo")}),
    )

    categories = {d.category for d in drift}
    assert "reflected-field-differs" in categories
    assert "completion-differs" in categories


def test_markdown_completion_differs_when_checkbox_disagrees_with_forge() -> None:
    link = _link()
    forge = _forge(state="closed")
    finding = MarkdownFinding(
        id="m1",
        repo="org/repo",
        path="TODO.md",
        line=5,
        text="ship it",
        checked=False,
        forge_ref="github:org/repo#1",
    )

    drift = build_drift(
        links=[link],
        items=[_item()],
        forge_items=[forge],
        findings=[finding],
        forge_coverage=frozenset({("github", "org/repo")}),
    )

    assert [d.category for d in drift] == ["markdown-completion-differs"]


def test_matching_evidence_produces_no_drift() -> None:
    link = _link(reflect=("title", "completion"), completion="closed")
    forge = _forge(state="closed")
    finding = MarkdownFinding(
        id="m1",
        repo="org/repo",
        path="TODO.md",
        line=5,
        text="ship it",
        checked=True,
        forge_ref="github:org/repo#1",
    )

    drift = build_drift(
        links=[link],
        items=[_item()],
        forge_items=[forge],
        findings=[finding],
        forge_coverage=frozenset({("github", "org/repo")}),
    )

    assert drift == ()
