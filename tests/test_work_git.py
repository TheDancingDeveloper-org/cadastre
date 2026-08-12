from pathlib import Path
from unittest.mock import patch

import pytest

from cadastre.plugins.collectors.work_git import inspect_checkout, transform


def make_checkout(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    revision = "a" * 40
    (git / "refs/heads/main").write_text(revision + "\n", encoding="ascii")
    (git / "config").write_text(
        '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return root, revision


def test_inspect_checkout_reads_head_branch_and_upstream_without_git(
    tmp_path: Path,
) -> None:
    root, revision = make_checkout(tmp_path)

    result = inspect_checkout(ident="checkout", repo="repo", path=root)

    assert result["head_revision"] == revision
    assert result["branch"] == "main"
    assert result["upstream"] == "origin/main"
    assert "tracking_ref_matches" not in result
    assert result["dirty"] is True  # the fixture file is untracked


def test_transform_requires_explicit_bounded_checkouts() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        transform({})


def test_detached_head_has_no_branch_or_upstream(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    git = root / ".git"
    git.mkdir(parents=True)
    revision = "b" * 40
    (git / "HEAD").write_text(revision + "\n", encoding="ascii")

    result = inspect_checkout(ident="checkout", repo="repo", path=root)

    assert result["head_revision"] == revision
    assert result["branch"] is None
    assert result["upstream"] is None
    assert "tracking_ref_matches" not in result


def test_linked_worktree_resolves_gitdir_file(tmp_path: Path) -> None:
    _main_root, revision = make_checkout(tmp_path / "main")
    linked_gitdir = tmp_path / "main" / ".git" / "worktrees" / "feature"
    linked_gitdir.mkdir(parents=True)
    (linked_gitdir / "HEAD").write_text(revision + "\n", encoding="ascii")

    worktree_root = tmp_path / "feature"
    worktree_root.mkdir()
    (worktree_root / ".git").write_text(f"gitdir: {linked_gitdir}\n", encoding="utf-8")

    result = inspect_checkout(ident="feature", repo="repo", path=worktree_root)

    assert result["head_revision"] == revision
    assert result["branch"] is None


def test_clean_checkout_matching_index_mtime_is_not_dirty(tmp_path: Path) -> None:
    import struct

    root = tmp_path / "repo"
    git = root / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (git / "refs/heads/main").write_text("a" * 40 + "\n", encoding="ascii")

    tracked = root / "README.md"
    tracked.write_text("hello\n", encoding="utf-8")
    stat = tracked.stat()
    mtime = int(stat.st_mtime)
    path_bytes = b"README.md"
    # 62-byte fixed index entry header (ctime, mtime, dev, ino, mode, uid,
    # gid, size, sha1) per the on-disk format `_tracked_paths` parses.
    entry = bytearray(62 + len(path_bytes))
    struct.pack_into(">I", entry, 8, mtime)
    struct.pack_into(">I", entry, 24, 0o100644)
    entry[62 : 62 + len(path_bytes)] = path_bytes
    padded = bytes(entry) + b"\0" * (8 - ((62 + len(path_bytes) + 1) % 8) % 8)
    index_body = struct.pack(">4sII", b"DIRC", 2, 1) + padded
    (git / "index").write_bytes(index_body)

    result = inspect_checkout(ident="checkout", repo="repo", path=root)

    assert result["dirty"] is False


@pytest.mark.parametrize(
    ("tracking_revision", "expected"),
    [("a", True), ("b", False)],
)
def test_tracking_ref_is_compared_without_walking_history(
    tmp_path: Path, tracking_revision: str, expected: bool
) -> None:
    root, revision = make_checkout(tmp_path)
    tracking = root / ".git/refs/remotes/origin/main"
    tracking.parent.mkdir(parents=True)
    tracking.write_text(tracking_revision * 40 + "\n", encoding="ascii")

    result = inspect_checkout(ident="checkout", repo="repo", path=root)

    assert result["tracking_ref_matches"] is expected
    assert result["head_revision"] == revision


def test_latest_valid_head_reflog_timestamp_is_checkout_activity(
    tmp_path: Path,
) -> None:
    root, _revision = make_checkout(tmp_path)
    log = root / ".git/logs/HEAD"
    log.parent.mkdir(parents=True)
    log.write_text(
        f"{'0' * 40} {'a' * 40} Test <test@example.invalid> 1720000000 +0000\tclone\n"
        "malformed later-looking line\n"
        f"{'a' * 40} {'b' * 40} Test <test@example.invalid> 1720003600 "
        "+0000\tcheckout\n",
        encoding="utf-8",
    )

    result = inspect_checkout(ident="checkout", repo="repo", path=root)

    assert result["last_head_change"] == "2024-07-03T10:46:40Z"


def test_absent_or_invalid_reflog_is_unknown(tmp_path: Path) -> None:
    root, _revision = make_checkout(tmp_path)
    assert "last_head_change" not in inspect_checkout(
        ident="checkout", repo="repo", path=root
    )
    log = root / ".git/logs/HEAD"
    log.parent.mkdir(parents=True)
    log.write_text("not a reflog entry\n", encoding="utf-8")
    assert "last_head_change" not in inspect_checkout(
        ident="checkout", repo="repo", path=root
    )


def test_transform_never_invokes_a_subprocess(tmp_path: Path) -> None:
    root, _revision = make_checkout(tmp_path)
    with patch("subprocess.run", side_effect=AssertionError("Git reader shelled out")):
        result = transform(
            {"checkouts": [{"id": "checkout", "repo": "repo", "path": str(root)}]}
        )
    assert result["entities"]["repo_checkout"][0]["id"] == "checkout"
