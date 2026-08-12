from datetime import UTC, datetime
from pathlib import Path

from cadastre.cli.manifest import backlog, drift, next_, projects, repo, why
from cadastre.cli.session import Session


def session_with_items(tmp_path: Path) -> Session:
    declared = tmp_path / "declared" / "work-items"
    declared.mkdir(parents=True)
    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (declared / "a.yaml").write_text(
        "id: a\ntitle: High\nstate: open\npriority: p1\n"
        "created_at: '2026-08-01T00:00:00Z'\n",
        encoding="utf-8",
    )
    (declared / "b.yaml").write_text(
        "id: b\ntitle: Blocked\nstate: open\npriority: p0\n"
        "created_at: '2026-08-01T00:00:00Z'\nblocked_by: [a]\n",
        encoding="utf-8",
    )
    return Session.open_fixture(tmp_path, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_backlog_excludes_blocked_items_from_eligible_ranking(tmp_path: Path) -> None:
    document = backlog(session_with_items(tmp_path))

    assert document.data["items"][0]["id"] == "a"
    assert document.data["items"][0]["eligible"] is True
    assert document.data["items"][1]["eligible"] is False
    assert document.data["confidence"] == "declared-only"


def test_why_exposes_recomputable_contributions(tmp_path: Path) -> None:
    document = why(session_with_items(tmp_path), "a")
    contributions = document.data["contributions"]

    assert sum(item["value"] for item in contributions) == document.data["score"]


def test_next_only_lists_eligible_unblocked_open_items(tmp_path: Path) -> None:
    document = next_(session_with_items(tmp_path))

    assert [item["id"] for item in document.data["items"]] == ["a"]
    assert all(item["eligible"] for item in document.data["items"])


def _session_with_link_and_evidence(tmp_path: Path) -> Session:
    from cadastre.core.observed import ObservedSource, write_source

    declared = tmp_path / "declared" / "work-items"
    declared.mkdir(parents=True)
    (tmp_path / "declared" / "work-links").mkdir()
    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (declared / "a.yaml").write_text(
        "id: a\ntitle: Ship\nstate: open\npriority: p1\n"
        "created_at: '2026-08-01T00:00:00Z'\n",
        encoding="utf-8",
    )
    (tmp_path / "declared" / "work-links" / "l1.yaml").write_text(
        "id: l1\nwork_item: a\nforge: github\nrepo: org/repo\nkind: issue\n"
        "ref: '1'\ncompletion: closed\nrequired: true\nreflect: [completion]\n",
        encoding="utf-8",
    )
    from cadastre.manifest.model import ForgeItem, MarkdownFinding
    from cadastre.modules.config import load_modules
    from cadastre.modules.registry import active_registry

    source = ObservedSource(
        source="work-github",
        plugin="work-github",
        as_of="2026-08-10T00:00:00Z",
        capabilities=("Work",),
        registry=active_registry(load_modules(tmp_path)),
        entities={
            "forge_item": [
                ForgeItem(
                    id="github:org/repo:issue:1",
                    forge="github",
                    repo="org/repo",
                    kind="issue",
                    ref="1",
                    title="Ship",
                    state="open",
                )
            ],
            "markdown_finding": [
                MarkdownFinding(
                    id="m1",
                    repo="org/repo",
                    path="TODO.md",
                    line=1,
                    text="unmarked",
                    checked=False,
                )
            ],
        },
    )
    write_source(tmp_path, source)
    return Session.open_fixture(tmp_path, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_drift_reports_completion_and_unlinked_markdown_categories(
    tmp_path: Path,
) -> None:
    document = drift(_session_with_link_and_evidence(tmp_path))
    categories = {item["category"] for item in document.data["divergences"]}

    assert "completion-differs" in categories
    assert "unlinked-markdown-finding" in categories


def test_drift_can_be_filtered_by_repo(tmp_path: Path) -> None:
    document = drift(_session_with_link_and_evidence(tmp_path), repo="other/repo")

    assert document.data["divergences"] == []


def test_repo_view_joins_work_checkouts_and_drift_for_one_repository(
    tmp_path: Path,
) -> None:
    document = repo(_session_with_link_and_evidence(tmp_path), "org/repo")

    assert document.data["repo"] == "org/repo"
    assert any(
        item["category"] == "completion-differs"
        for item in document.data["divergences"]
    )


def test_lookup_accepts_manifest_kinds_when_the_module_is_enabled(
    tmp_path: Path,
) -> None:
    from cadastre.cli.lookup import lookup

    session = session_with_items(tmp_path)
    document = lookup(session, "a", kind="work_item")

    assert document.data["kind"] == "work_item"
    assert document.data["entity"]["id"] == "a"


def test_open_direct_dependants_contribute_to_blocking_score(
    tmp_path: Path,
) -> None:
    document = why(session_with_items(tmp_path), "a")
    contributions = {item["name"]: item for item in document.data["contributions"]}

    assert contributions["blocking"]["raw"] == 1
    assert contributions["blocking"]["weight"] == 50
    assert contributions["blocking"]["value"] == 50


def _session_with_projects(tmp_path: Path, *, reverse: bool = False) -> Session:
    from cadastre.core.observed import ObservedSource, write_source
    from cadastre.manifest.model import RepoCheckout
    from cadastre.modules.config import load_modules
    from cadastre.modules.registry import active_registry

    (tmp_path / "declared/repos").mkdir(parents=True)
    (tmp_path / "declared/work-items").mkdir()
    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (tmp_path / "declared/repos/repos.yaml").write_text(
        "- id: alpha\n  remotes: []\n- id: beta\n  remotes: []\n"
        "- id: empty\n  remotes: []\n",
        encoding="utf-8",
    )
    (tmp_path / "declared/work-items/items.yaml").write_text(
        "- id: a\n  title: Alpha top\n  state: open\n  priority: p1\n"
        "  created_at: '2026-08-01T00:00:00Z'\n  repo: alpha\n"
        "- id: b\n  title: Beta blocked\n  state: open\n  priority: p0\n"
        "  created_at: '2026-08-01T00:00:00Z'\n  repo: beta\n  blocked_by: [a]\n"
        "- id: done\n  title: Done\n  state: done\n  priority: p3\n"
        "  created_at: '2026-08-01T00:00:00Z'\n  repo: beta\n",
        encoding="utf-8",
    )
    registry = active_registry(load_modules(tmp_path))
    checkouts = [
        RepoCheckout(
            id="z-secondary",
            repo="alpha",
            head_revision="b" * 40,
            dirty=True,
            tracking_ref_matches=False,
        ),
        RepoCheckout(
            id="a-primary",
            repo="alpha",
            head_revision="a" * 40,
            dirty=False,
            tracking_ref_matches=True,
            last_head_change="2026-08-09T00:00:00Z",
        ),
        RepoCheckout(
            id="typo",
            repo="betaa",
            head_revision="c" * 40,
            dirty=False,
        ),
    ]
    if reverse:
        checkouts.reverse()
    write_source(
        tmp_path,
        ObservedSource(
            source="git-workspace",
            plugin="work-git",
            as_of="2026-08-01T00:00:00Z",
            capabilities=("work.repo-state",),
            entities={"repo_checkout": checkouts},  # type: ignore[dict-item]
            registry=registry,
        ),
    )
    return Session.open_fixture(tmp_path, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_projects_includes_empty_repos_multiple_checkouts_and_mismatches(
    tmp_path: Path,
) -> None:
    document = projects(_session_with_projects(tmp_path))

    assert [row["repo"] for row in document.data["projects"]] == [
        "alpha",
        "beta",
        "empty",
    ]
    alpha = document.data["projects"][0]
    assert alpha["top"]["id"] == "a"
    assert [item["id"] for item in alpha["checkouts"]] == [
        "a-primary",
        "z-secondary",
    ]
    assert alpha["checkout_summary"] == {
        "any_dirty": True,
        "tracking_ref_mismatches": 1,
        "primary_checkout": "a-primary",
        "freshness": "stale",
    }
    assert document.data["projects"][1]["open_with_open_blocker"] == 1
    assert document.data["projects"][2]["counts"] == {
        "open": 0,
        "done": 0,
        "cancelled": 0,
    }
    assert document.data["unmatched_checkouts"][0]["repo"] == "betaa"


def test_projects_is_byte_stable_when_collector_payload_is_reordered(
    tmp_path: Path,
) -> None:
    from cadastre.render.json_out import render

    first = render(projects(_session_with_projects(tmp_path / "one")))
    second = render(projects(_session_with_projects(tmp_path / "two", reverse=True)))
    # Paths are intentionally absent from these checkout records, so payload
    # order is the only variable and the rendered answer must remain identical.
    assert first == second
