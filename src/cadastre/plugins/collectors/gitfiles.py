"""Reading a local Git checkout from its own on-disk files.

Shared by the collectors that observe a clone rather than an API. Git's plain
files are read directly instead of invoking the `git` executable, which is what
makes the no-hooks/no-network contract enforceable — a collector that shells
out to `git` inherits whatever aliases, hooks and remotes the checkout's
configuration names.

The price is `committed_at`: a commit that lives in a packfile cannot be read
this way without implementing delta resolution, so the answer is `None` and the
caller falls back to `last_head_change`, which is a file all the same and says
when the checkout last moved.
"""

from __future__ import annotations

import configparser
import re
import zlib
from datetime import UTC, datetime
from pathlib import Path

from cadastre.core.provenance import format_timestamp

_HEX = frozenset("0123456789abcdef")


def git_dir(root: Path) -> Path:
    """The `.git` directory for a checkout, following a worktree pointer."""
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            return (root / text.partition(":")[2].strip()).resolve()
    raise ValueError(f"not a supported Git checkout: {root}")


def common_dir(git: Path) -> Path:
    marker = git / "commondir"
    if not marker.exists():
        return git
    value = marker.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("empty Git commondir")
    return (git / value).resolve()


def _bases(git: Path) -> tuple[Path, ...]:
    return tuple(dict.fromkeys((git, common_dir(git))))


def read_ref(git: Path, ref: str) -> str | None:
    for base in _bases(git):
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


def head(git: Path) -> tuple[str, str | None]:
    """The resolved revision and, when HEAD is not detached, its branch."""
    value = (git / "HEAD").read_text(encoding="ascii").strip()
    if value.startswith("ref: "):
        ref = value[5:]
        revision = read_ref(git, ref)
        if revision is not None:
            return revision, ref.removeprefix("refs/heads/")
        raise ValueError(f"HEAD points to missing ref: {ref}")
    if len(value) >= 7 and all(char in _HEX for char in value):
        return value, None
    raise ValueError("invalid Git HEAD")


def config(git: Path) -> configparser.RawConfigParser:
    result = configparser.RawConfigParser()
    path = common_dir(git) / "config"
    if path.exists():
        result.read(path, encoding="utf-8")
    return result


_REFLOG_TIMESTAMP = re.compile(r" (?P<timestamp>[0-9]+) [+-][0-9]{4}(?:\t|$)")


def last_head_change(git: Path) -> str | None:
    """When HEAD last moved locally — in a mirror clone, the last `pull`."""
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


def committed_at(git: Path, revision: str) -> str | None:
    """A commit's committer date, or `None` if it is not a loose object.

    This is the age of the *data* a checkout holds, as opposed to the age of
    the run that read it.
    """
    if len(revision) != 40 or not all(char in _HEX for char in revision):
        return None
    for base in _bases(git):
        path = base / "objects" / revision[:2] / revision[2:]
        if not path.is_file():
            continue
        try:
            raw = zlib.decompress(path.read_bytes())
        except (OSError, zlib.error):
            return None
        header, _, body = raw.partition(b"\0")
        if not header.startswith(b"commit "):
            return None
        for line in body.splitlines():
            if not line:
                break
            if not line.startswith(b"committer "):
                continue
            fields = line.split()
            if len(fields) < 2:
                return None
            try:
                seconds = int(fields[-2])
            except ValueError:
                return None
            return format_timestamp(datetime.fromtimestamp(seconds, tz=UTC))
    return None
