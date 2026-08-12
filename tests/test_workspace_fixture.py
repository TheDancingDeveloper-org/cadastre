from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from cadastre.plugins.collectors.work_git import inspect_checkout
from tests.fixtures.workspace import REV_A, WorkspaceBuilder


def test_nine_repository_workspace_has_every_claimed_shape(
    mock_workspace: WorkspaceBuilder,
) -> None:
    workspace = mock_workspace.nine_repositories()
    assert set(workspace.repos) == {
        "clean",
        "dirty",
        "untracked",
        "detached",
        "packed",
        "no-upstream",
        "in-sync",
        "diverged",
        "linked",
    }
    results = {
        name: inspect_checkout(ident=name, repo=name, path=path)
        for name, path in workspace.repos.items()
    }
    assert results["clean"]["dirty"] is False
    assert results["dirty"]["dirty"] is True
    assert results["untracked"]["dirty"] is True
    assert results["detached"]["branch"] is None
    assert results["packed"]["head_revision"] == REV_A
    assert "tracking_ref_matches" not in results["no-upstream"]
    assert results["in-sync"]["tracking_ref_matches"] is True
    assert results["diverged"]["tracking_ref_matches"] is False
    assert (workspace.repos["linked"] / ".git").is_file()
    assert [path.name for path in workspace.markdown] == [
        "EMPTY.md",
        "FENCED.md",
        "TODO.md",
    ]


def test_external_network_guard_fails_in_process_and_python_child() -> None:
    with pytest.raises(AssertionError, match="non-loopback"):
        socket.create_connection(("192.0.2.1", 80), timeout=0.01)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('192.0.2.1', 80), timeout=.01)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "non-loopback" in completed.stderr
