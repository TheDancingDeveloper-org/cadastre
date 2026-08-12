"""Deterministic synthetic workspace built from Git's on-disk formats."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

REV_A = "a" * 40
REV_B = "b" * 40
FIXED_MTIME = 1_720_000_000
FIXED_REFLOG = 1_720_003_600


@dataclass(frozen=True)
class Workspace:
    root: Path
    repos: dict[str, Path]
    markdown: tuple[Path, ...]


class WorkspaceBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def nine_repositories(self) -> Workspace:
        variants = (
            "clean",
            "dirty",
            "untracked",
            "detached",
            "packed",
            "no-upstream",
            "in-sync",
            "diverged",
            "linked",
        )
        repos = {name: self._repo(name) for name in variants[:-1]}
        self._linked(repos["clean"], self.root / "linked")
        repos["linked"] = self.root / "linked"

        self._index(repos["clean"])
        self._index(repos["dirty"], tracked_mtime=FIXED_MTIME - 1)
        self._index(repos["untracked"])
        (repos["untracked"] / "NEW.txt").write_text("new\n", encoding="utf-8")
        (repos["detached"] / ".git/HEAD").write_text(REV_A + "\n", encoding="ascii")
        packed = repos["packed"] / ".git"
        (packed / "refs/heads/main").unlink()
        (packed / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted\n{REV_A} refs/heads/main\n",
            encoding="ascii",
        )
        self._index(repos["packed"])
        (repos["no-upstream"] / ".git/config").write_text("[core]\n", encoding="utf-8")
        self._index(repos["no-upstream"])
        self._tracking(repos["in-sync"], REV_A)
        self._index(repos["in-sync"])
        self._tracking(repos["diverged"], REV_B)
        self._index(repos["diverged"])
        self._index(repos["linked"], git=repos["clean"] / ".git/worktrees/linked")
        for path in sorted(self.root.rglob("*")):
            if path.exists():
                os.utime(path, (FIXED_MTIME, FIXED_MTIME), follow_symlinks=False)
        markdown = self._markdown()
        return Workspace(self.root, repos, markdown)

    def _repo(self, name: str) -> Path:
        root = self.root / name
        git = root / ".git"
        (git / "refs/heads").mkdir(parents=True)
        (git / "logs").mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (git / "refs/heads/main").write_text(REV_A + "\n", encoding="ascii")
        (git / "config").write_text(
            '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
            encoding="utf-8",
        )
        (git / "logs/HEAD").write_text(
            f"{'0' * 40} {REV_A} Test <test@example.invalid> "
            f"{FIXED_REFLOG} +0000\tclone\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text("tracked\n", encoding="utf-8")
        return root

    def _tracking(self, root: Path, revision: str) -> None:
        path = root / ".git/refs/remotes/origin/main"
        path.parent.mkdir(parents=True)
        path.write_text(revision + "\n", encoding="ascii")

    def _index(
        self, root: Path, *, tracked_mtime: int = FIXED_MTIME, git: Path | None = None
    ) -> None:
        os.utime(root / "README.md", (FIXED_MTIME, FIXED_MTIME))
        path_bytes = b"README.md"
        entry = bytearray(62 + len(path_bytes))
        struct.pack_into(">I", entry, 8, tracked_mtime)
        struct.pack_into(">I", entry, 24, 0o100644)
        entry[62 : 62 + len(path_bytes)] = path_bytes
        padding = (8 - ((62 + len(path_bytes) + 1) % 8)) % 8
        target = git or root / ".git"
        (target / "index").write_bytes(
            struct.pack(">4sII", b"DIRC", 2, 1) + bytes(entry) + b"\0" + b"\0" * padding
        )

    def _linked(self, common_root: Path, root: Path) -> None:
        git = common_root / ".git/worktrees/linked"
        git.mkdir(parents=True)
        (git / "HEAD").write_text(REV_A + "\n", encoding="ascii")
        (git / "commondir").write_text("../..\n", encoding="ascii")
        (git / "logs").mkdir()
        (git / "logs/HEAD").write_text(
            f"{REV_A} {REV_A} Test <test@example.invalid> "
            f"{FIXED_REFLOG} +0000\tcheckout\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True)
        (root / ".git").write_text(f"gitdir: {git}\n", encoding="utf-8")
        (root / "README.md").write_text("tracked\n", encoding="utf-8")

    def _markdown(self) -> tuple[Path, ...]:
        planning = self.root / "planning"
        planning.mkdir()
        files = {
            "TODO.md": (
                "# Tasks\n- [ ] migrate me\n- [x] already done\nTODO: plain task\n"
            ),
            "FENCED.md": "## Notes\n```text\n- [ ] not a task\nTODO: not a task\n```\n",
            "EMPTY.md": "# Context\nThere is no work in this file.\n",
        }
        result = []
        for name, content in sorted(files.items()):
            path = planning / name
            path.write_text(content, encoding="utf-8")
            os.utime(path, (FIXED_MTIME, FIXED_MTIME))
            result.append(path)
        return tuple(result)
