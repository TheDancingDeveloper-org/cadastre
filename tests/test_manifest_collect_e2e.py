"""End-to-end regression: `cadastre collect` for a real Manifest collector.

Every layer between the subprocess handshake and the read-back cache used to
default to the base-only entity registry independently, so unit tests against
each layer in isolation passed while the real pipeline
(collect -> observed.sqlite3 cache -> collect again -> query) silently
dropped every Manifest entity. This drives the real work-git console entry
point as a subprocess, twice, through the actual `collect` command.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from cadastre.cli.collect import collect
from cadastre.cli.manifest import repo
from cadastre.cli.session import Session


def _checkout(root: Path) -> str:
    git = root / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    revision = "a" * 40
    (git / "refs/heads/main").write_text(revision + "\n", encoding="ascii")
    return revision


def _catalog(tmp_path: Path) -> Path:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _checkout(checkout)
    root = tmp_path / "catalog"
    (root / "declared").mkdir(parents=True)
    (root / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (root / "plugins.yaml").write_text(
        "sources:\n"
        "  - id: my-work-git\n"
        "    plugin: work-git\n"
        # `-m`, not the file path directly: running the file directly puts
        # its own directory on sys.path[0], which shadows the stdlib `http`
        # package with plugins/collectors/http.py and breaks the import of
        # urllib inside the collector's own dependency chain.
        f"    command: [{json.dumps(sys.executable)}, -m, "
        f"{json.dumps('cadastre.plugins.collectors.work_git')}]\n"
        "    methods: [work.repo-state]\n"
        "    config:\n"
        "      checkouts:\n"
        "        - id: repo1\n"
        "          repo: org/repo\n"
        f"          path: {json.dumps(str(checkout))}\n",
        encoding="utf-8",
    )
    return root


def test_collect_then_query_round_trips_a_real_work_git_checkout(
    tmp_path: Path,
) -> None:
    root = _catalog(tmp_path)
    now = datetime(2026, 8, 11, tzinfo=UTC)

    document = collect(Session.open(root, now=now))
    assert document.data["written"] == ["observed/my-work-git.json"]

    # Collect again to exercise the cache-read path (observed.sqlite3 without
    # a catalog.sqlite3 beside it), which is a distinct code path from the
    # first, snapshot-only collection.
    collect(Session.open(root, now=now))

    result = repo(Session.open(root, now=now), "org/repo")
    checkouts = result.data["checkouts"]
    assert len(checkouts) == 1
    assert checkouts[0]["id"] == "repo1"
    assert checkouts[0]["head_revision"] == "a" * 40


def test_a_live_sqlite_catalog_can_read_back_its_own_manifest_evidence(
    tmp_path: Path,
) -> None:
    """The documented deployment shape: BOTH databases present.

    `Session.open` branches on `catalog.sqlite3` existing, and the SQLite
    branch omitted the active registry when loading observed evidence. Every
    test above builds a catalog directory with no `catalog.sqlite3`, so they
    all took the other branch and this was green while collecting Manifest
    evidence into a real data directory made every command — `brief` and
    `drift` included — fail with `unknown kind`.
    """
    from cadastre.cli.brief import brief
    from cadastre.core import storage

    root = _catalog(tmp_path)
    storage.initialize(root)
    assert (root / storage.CATALOG_DB).exists()
    now = datetime(2026, 8, 11, tzinfo=UTC)

    collect(Session.open(root, now=now))

    # The module's own query, and a base one that has nothing to do with
    # Manifest: the defect failed both, because it failed opening the session.
    result = repo(Session.open(root, now=now), "org/repo")
    assert [item["id"] for item in result.data["checkouts"]] == ["repo1"]
    assert brief(Session.open(root, now=now)).data
