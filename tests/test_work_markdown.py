from pathlib import Path

import pytest

from cadastre.plugins.collectors.work_markdown import scan_file, transform


def test_markdown_scanner_extracts_tasks_and_ignores_fences(tmp_path: Path) -> None:
    path = tmp_path / "PLAN.md"
    path.write_text(
        "# Roadmap\n"
        "- [ ] First <!-- cadastre-work item=w1 forge=github:org/repo#1 -->\n"
        "```\n- [ ] Not work\n```\n- [x] Done\n",
        encoding="utf-8",
    )

    findings = scan_file(path, repo="repo", max_file_bytes=10000)

    assert [item["work_item"] for item in findings[:1]] == ["w1"]
    assert len(findings) == 2
    assert findings[0]["heading"] == "Roadmap"
    assert "Not work" not in str(findings)


def test_transform_rejects_root_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes root"):
        transform({"repo": "repo", "root": str(tmp_path), "files": ["../x.md"]})


def test_transform_rejects_budget_overrun(tmp_path: Path) -> None:
    path = tmp_path / "PLAN.md"
    path.write_text("- [ ] work\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_total_bytes"):
        transform(
            {
                "repo": "repo",
                "root": str(tmp_path),
                "files": [path.name],
                "max_total_bytes": 1,
            }
        )


def test_transform_rejects_max_files_overrun(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("- [ ] a\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("- [ ] b\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_files"):
        transform(
            {
                "repo": "repo",
                "root": str(tmp_path),
                "files": ["a.md", "b.md"],
                "max_files": 1,
            }
        )


def test_scan_file_rejects_file_over_max_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "PLAN.md"
    path.write_text("- [ ] work\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_file_bytes"):
        scan_file(path, repo="repo", max_file_bytes=1)


def test_transform_rejects_configured_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("- [ ] work\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="symlink"):
        transform({"repo": "repo", "root": str(tmp_path), "files": [link.name]})


def test_scan_file_rejects_invalid_encoding(tmp_path: Path) -> None:
    path = tmp_path / "PLAN.md"
    path.write_bytes(b"- [ ] \xff\xfe not utf-8\n")

    with pytest.raises(UnicodeDecodeError):
        scan_file(path, repo="repo", max_file_bytes=10000)


def test_retitle_without_a_marker_changes_identity_but_marker_keeps_it_stable(
    tmp_path: Path,
) -> None:
    marked = tmp_path / "marked.md"
    marked.write_text(
        "- [ ] Old title <!-- cadastre-work item=w1 -->\n", encoding="utf-8"
    )
    findings_before = scan_file(marked, repo="repo", max_file_bytes=10000)
    marked.write_text(
        "- [ ] New title <!-- cadastre-work item=w1 -->\n", encoding="utf-8"
    )
    findings_after = scan_file(marked, repo="repo", max_file_bytes=10000)
    assert findings_before[0]["id"] == findings_after[0]["id"] == "w1"

    unmarked = tmp_path / "unmarked.md"
    unmarked.write_text("- [ ] Old title\n", encoding="utf-8")
    before = scan_file(unmarked, repo="repo", max_file_bytes=10000)
    unmarked.write_text("- [ ] New title\n", encoding="utf-8")
    after = scan_file(unmarked, repo="repo", max_file_bytes=10000)
    assert before[0]["id"] != after[0]["id"]


def test_duplicate_text_produces_distinct_findings(tmp_path: Path) -> None:
    path = tmp_path / "PLAN.md"
    path.write_text("- [ ] Same text\n- [ ] Same text\n", encoding="utf-8")

    findings = scan_file(path, repo="repo", max_file_bytes=10000)

    assert len(findings) == 2
    assert findings[0]["id"] != findings[1]["id"]
    assert findings[0]["line"] == 1
    assert findings[1]["line"] == 2
