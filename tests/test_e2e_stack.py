"""The synthetic E2E environment must be isolated from the checked-in bundle."""

from __future__ import annotations

import io
from pathlib import Path

from scripts.e2e_stack import build_fake_catalog, write_environment

from cadastre.adapters.http import CadastreHTTPServer
from cadastre.mcp.streamable import MCPHTTPServer


def test_build_fake_catalog_creates_runtime_databases(tmp_path: Path) -> None:
    root = build_fake_catalog(tmp_path / "runtime")
    assert (root / "catalog.sqlite3").exists()
    assert (root / "observed.sqlite3").exists()
    assert not list(root.glob("*.local.*"))
    with (root / "catalog.sqlite3").open("rb") as database:
        assert database.read(16) == b"SQLite format 3\x00"
    assert (root / "declared/hosts/hosts.yaml").exists()


def test_environment_uses_selected_loopback_ports(tmp_path: Path) -> None:
    root = build_fake_catalog(tmp_path / "runtime")
    api = CadastreHTTPServer(("127.0.0.1", 0), root, allow_write=False)
    mcp = MCPHTTPServer(("127.0.0.1", 0), root, require_auth=False)
    try:
        destination = io.StringIO()
        write_environment(destination, api, mcp)
        assert destination.getvalue().splitlines() == [
            f"CADASTRE_E2E_API_ORIGIN=http://127.0.0.1:{api.server_port}",
            f"CADASTRE_E2E_MCP_ORIGIN=http://127.0.0.1:{mcp.server_port}/mcp",
        ]
        assert api.server_port != mcp.server_port
    finally:
        api.server_close()
        mcp.server_close()
