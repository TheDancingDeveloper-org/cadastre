"""Read-only local Git checkout collector.

This reader deliberately inspects Git's on-disk files instead of invoking the
Git executable. That makes the no-hooks/no-network contract enforceable.
"""

from __future__ import annotations

import configparser
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "work-git"
VERSION = "1"
CAPABILITIES = ("Work",)


def _git_dir(root: Path) -> Path:
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            return (root / text.partition(":")[2].strip()).resolve()
    raise ValueError(f"not a supported Git checkout: {root}")


def _common_dir(git: Path) -> Path:
    marker = git / "commondir"
    if not marker.exists():
        return git
    value = marker.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("empty Git commondir")
    return (git / value).resolve()


def _read_ref(git: Path, ref: str) -> str | None:
    common = _common_dir(git)
    for base in dict.fromkeys((git, common)):
        ref_path = base / ref
        if ref_path.exists():
            return ref_path.read_text(encoding="ascii").strip()
        packed = base / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="ascii").splitlines():
                if line and not line.startswith(("#", "^")) and " " in line:
                    revision, packed_ref = line.split(" ", 1)
                    if packed_ref == ref:
                        return revision
    return None


def _head(git: Path) -> tuple[str, str | None]:
    value = (git / "HEAD").read_text(encoding="ascii").strip()
    if value.startswith("ref: "):
        ref = value[5:]
        revision = _read_ref(git, ref)
        if revision is not None:
            return revision, ref.removeprefix("refs/heads/")
        raise ValueError(f"HEAD points to missing ref: {ref}")
    if len(value) >= 7 and all(char in "0123456789abcdef" for char in value):
        return value, None
    raise ValueError("invalid Git HEAD")


def _config(git: Path) -> configparser.RawConfigParser:
    result = configparser.RawConfigParser()
    path = _common_dir(git) / "config"
    if path.exists():
        result.read(path, encoding="utf-8")
    return result


_REFLOG_TIMESTAMP = re.compile(r" (?P<timestamp>[0-9]+) [+-][0-9]{4}(?:\t|$)")


def _last_head_change(git: Path) -> str | None:
    path = git / "logs" / "HEAD"
    if not path.exists():
        return None
    timestamps: list[int] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _REFLOG_TIMESTAMP.search(line)
        if match is not None:
            timestamps.append(int(match.group("timestamp")))
    if not timestamps:
        return None
    return format_timestamp(datetime.fromtimestamp(max(timestamps), tz=UTC))


def _tracking_revision(
    git: Path, config: configparser.RawConfigParser, branch: str | None
) -> tuple[str | None, str | None]:
    if branch is None or not config.has_section(f'branch "{branch}"'):
        return None, None
    remote = config.get(f'branch "{branch}"', "remote", fallback="")
    merge = config.get(f'branch "{branch}"', "merge", fallback="")
    if not remote or not merge:
        return None, None
    short = merge.removeprefix("refs/heads/")
    upstream = short if remote == "." else f"{remote}/{short}"
    ref = merge if remote == "." else f"refs/remotes/{remote}/{short}"
    return upstream, _read_ref(git, ref)


def _tracked_paths(git: Path) -> dict[str, tuple[int, int, int]]:
    index = git / "index"
    if not index.exists():
        return {}
    raw = index.read_bytes()
    if len(raw) < 12 or raw[:4] != b"DIRC":
        raise ValueError("unsupported Git index")
    count = int.from_bytes(raw[8:12], "big")
    offset = 12
    result: dict[str, tuple[int, int, int]] = {}
    for _ in range(count):
        if offset + 62 > len(raw):
            raise ValueError("truncated Git index")
        ctime = int.from_bytes(raw[offset : offset + 4], "big")
        mtime = int.from_bytes(raw[offset + 8 : offset + 12], "big")
        mode = int.from_bytes(raw[offset + 24 : offset + 28], "big")
        cursor = offset + 62
        end = raw.index(b"\0", cursor)
        path = raw[cursor:end].decode("utf-8")
        result[path] = (ctime, mtime, mode)
        entry_size = ((end - offset + 1 + 8) // 8) * 8
        offset += entry_size
    return result


def _dirty(root: Path, git: Path) -> bool:
    tracked = _tracked_paths(git)
    for relative, (_ctime, mtime, _mode) in tracked.items():
        path = root / relative
        if not path.exists() or path.stat().st_mtime_ns // 1_000_000_000 != mtime:
            return True
    for path in root.rglob("*"):
        if (
            path.is_file()
            and git not in path.parents
            and ".git" not in path.parts
            and path.relative_to(root).as_posix() not in tracked
        ):
            return True
    return False


def inspect_checkout(*, ident: str, repo: str, path: Path) -> dict[str, Any]:
    root = path.resolve()
    git = _git_dir(root)
    revision, branch = _head(git)
    config = _config(git)
    upstream, tracking_revision = _tracking_revision(git, config, branch)
    result = {
        "id": ident,
        "repo": repo,
        "head_revision": revision,
        "branch": branch,
        "dirty": _dirty(root, git),
        "upstream": upstream,
        "worktree": str(root),
    }
    if tracking_revision is not None:
        result["tracking_ref_matches"] = tracking_revision == revision
    last_head_change = _last_head_change(git)
    if last_head_change is not None:
        result["last_head_change"] = last_head_change
    return result


def transform(config: dict[str, Any]) -> dict[str, Any]:
    checkouts = config.get("checkouts")
    if not isinstance(checkouts, list) or not checkouts:
        raise ValueError("config.checkouts must be a non-empty list")
    records = []
    for item in checkouts:
        if not isinstance(item, dict):
            raise ValueError("each checkout must be a mapping")
        ident = item.get("id")
        repo = item.get("repo")
        path = item.get("path")
        if not all(isinstance(value, str) and value for value in (ident, repo, path)):
            raise ValueError("each checkout requires string id, repo, and path")
        assert isinstance(ident, str)
        assert isinstance(repo, str)
        assert isinstance(path, str)
        records.append(inspect_checkout(ident=ident, repo=repo, path=Path(path)))
    return {"entities": {"repo_checkout": sorted(records, key=lambda item: item["id"])}}


def _collect(request: Request) -> Reply:
    return ok(transform(request.config), format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={"work.repo-state": _collect},
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
