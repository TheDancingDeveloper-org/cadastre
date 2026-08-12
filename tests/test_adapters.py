"""M14/M15 black-box checks for the rebuildable cache and HTTP seam."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from cadastre.adapters.http import CadastreHTTPServer, openapi_schema
from cadastre.adapters.security import CHECK_SCOPE, READ_SCOPE, WRITE_SCOPE, credential
from cadastre.core.errors import UsageError
from cadastre.core.observed import load_observed
from cadastre.core.observed_db import database_path, history, sync_snapshots
from tests.conftest import EXAMPLE_CATALOG, NOW


def _request(
    server: CadastreHTTPServer,
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        encoded = json.dumps(body).encode() if body is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, encoded, request_headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _raw_request(
    server: CadastreHTTPServer,
    method: str,
    path: str,
) -> tuple[int, str, bytes]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "")
        payload = response.read()
        connection.close()
        return response.status, content_type, payload
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_sqlite_cache_is_queryable_and_history_survives_rebuild(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    import shutil

    shutil.copytree(EXAMPLE_CATALOG, root)
    observed = root / "observed"
    observed.mkdir()
    (observed / "fixture.json").write_text(
        json.dumps(
            {
                "v": 1,
                "source": "fixture",
                "plugin": "fixture",
                "as_of": "2026-08-07T12:00:00Z",
                "ok": True,
                "capabilities": ["inventory.list"],
                "entities": {"host": [{"id": "app-01", "role": "server"}]},
            }
        ),
        encoding="utf-8",
    )
    sync_snapshots(root)
    assert database_path(root).exists()
    before = load_observed(root, now=NOW)
    assert before
    assert history(root)
    database_path(root).unlink()
    rebuilt = sync_snapshots(root)
    assert rebuilt.exists()
    assert load_observed(root, now=NOW) == before
    assert history(root)


def test_sqlite_sessions_apply_observed_freshness(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from cadastre.cli.session import Session
    from cadastre.core.observed import ObservedSource, write_source

    root = tmp_path / "catalog"
    import shutil

    shutil.copytree(EXAMPLE_CATALOG, root)
    source = ObservedSource(
        source="fixture",
        plugin="fixture",
        as_of="2026-08-07T00:00:00Z",
        capabilities=("inventory.list",),
        entities={},
    )
    write_source(root, source)
    from cadastre.core.storage import import_legacy, initialize

    initialize(root)
    import_legacy(root, root)
    session = Session.open(root, now=datetime(2026, 8, 8, 1, tzinfo=UTC))
    stale = {item.source for item in session.provenance() if item.stale}
    assert "fixture" in stale


def test_http_read_surface_and_check_do_not_require_write_auth(
    catalog_copy: Path,
) -> None:
    server = CadastreHTTPServer(("127.0.0.1", 0), catalog_copy, allow_write=False)
    status, result = _request(server, "GET", "/brief")
    assert status == 200
    assert result["command"] == "cadastre brief"
    server = CadastreHTTPServer(("127.0.0.1", 0), catalog_copy, allow_write=False)
    status, result = _request(
        server,
        "POST",
        "/check",
        {
            "artifact": "services:\n  example: {}\n",
            "kind": "compose",
            "path": "compose-clean.yaml",
        },
    )
    assert status == 200
    assert result["command"] == "cadastre check compose-clean.yaml"
    assert result["result"]["artifact"]["path"] == "compose-clean.yaml"


def test_non_loopback_http_requires_explicit_read_auth(
    catalog_copy: Path,
) -> None:
    with pytest.raises(UsageError, match="require authentication"):
        CadastreHTTPServer(("0.0.0.0", 0), catalog_copy, allow_write=False)

    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=False,
        tokens={"read-token": credential("agent-runtime", scopes={READ_SCOPE})},
        require_auth=True,
    )
    status, result = _request(server, "GET", "/brief")
    assert status == 403
    assert result["error"]["kind"] == "PermissionError"

    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=False,
        tokens={"read-token": credential("agent-runtime", scopes={READ_SCOPE})},
        require_auth=True,
    )
    status, result = _request(
        server, "GET", "/brief", headers={"Authorization": "Bearer read-token"}
    )
    assert status == 200
    assert result["command"] == "cadastre brief"


def test_http_write_is_explicit_and_attributed(
    catalog_copy: Path,
) -> None:
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={
            "test-token": credential(
                "tester", scopes={READ_SCOPE, CHECK_SCOPE, WRITE_SCOPE}
            )
        },
    )
    status, result = _request(
        server,
        "POST",
        "/annotate",
        {"kind": "host", "id": "app-01", "record": {"tags": ["reviewed"]}},
    )
    assert status == 403
    assert result["error"]["kind"] == "PermissionError"


def test_authenticated_http_write_uses_the_token_principal(
    catalog_copy: Path,
) -> None:
    from cadastre.core.storage import import_legacy, initialize

    initialize(catalog_copy)
    import_legacy(catalog_copy, catalog_copy)
    server = CadastreHTTPServer(
        ("127.0.0.1", 0),
        catalog_copy,
        allow_write=True,
        tokens={
            "test-token": credential(
                "tester", scopes={READ_SCOPE, CHECK_SCOPE, WRITE_SCOPE}
            )
        },
    )
    status, result = _request(
        server,
        "POST",
        "/annotate",
        {"kind": "host", "id": "app-01", "record": {"tags": ["reviewed"]}},
        {"Authorization": "Bearer test-token"},
    )
    assert status == 200
    assert result["result"]["principal"] == "tester"
    from cadastre.core.storage import CatalogStore

    with CatalogStore.open(catalog_copy, read_only=True) as store:
        assert store.audit()[-1]["principal"] == "tester"


def test_openapi_declares_every_http_route() -> None:
    document = openapi_schema()
    expected = {
        "/brief",
        "/context-for",
        "/lookup/{id}",
        "/check",
        "/drift",
        "/observations",
        "/stale",
        "/plugins",
        "/sources",
        "/security-check",
        "/schema",
        "/add",
        "/update",
        "/delete",
        "/annotate",
        "/accept",
        "/leave-contested",
        "/acknowledge",
        "/question",
        "/health/live",
        "/health/ready",
        "/version",
    }
    assert set(document["paths"]) == expected


def test_schema_route_includes_manifest_kinds_when_enabled(catalog_copy: Path) -> None:
    """Regression: `/schema` called catalog_schema() with no registry, so an
    enabled catalog's live schema route never actually reflected Manifest —
    same bug class as openapi_schema(), fixed alongside it here."""
    (catalog_copy / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    server = CadastreHTTPServer(("127.0.0.1", 0), catalog_copy, allow_write=False)
    status, payload = _request(server, "GET", "/schema")
    assert status == 200
    assert "work_item" in payload["$defs"]


def test_openapi_schema_components_include_manifest_kinds_when_enabled() -> None:
    """Regression: openapi_schema(manifest_enabled=True) listed the
    /manifest/* paths but its embedded `components.schemas` still came from
    catalog_schema() with no registry, so the referenced entity schemas
    (work_item, etc.) were absent from the same document that pointed at
    them."""
    disabled = openapi_schema()
    assert "work_item" not in disabled["components"]["schemas"]
    enabled = openapi_schema(manifest_enabled=True)
    assert "work_item" in enabled["components"]["schemas"]


def test_docs_is_a_documentation_page_and_openapi_is_json(catalog_copy: Path) -> None:
    server = CadastreHTTPServer(("127.0.0.1", 0), catalog_copy, allow_write=False)
    status, payload = _request(server, "GET", "/openapi.json")
    assert status == 200 and payload["openapi"] == "3.1.0"
    server = CadastreHTTPServer(("127.0.0.1", 0), catalog_copy, allow_write=False)
    status, content_type, raw_payload = _raw_request(server, "GET", "/docs")
    assert status == 200
    assert content_type.startswith("text/html")
    assert b"Cadastre API" in raw_payload
    assert b"/openapi.json" in raw_payload
