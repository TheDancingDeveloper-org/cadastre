"""M9 — the MCP adapter.

The property under test is that it stays a shim. Logic here is untestable and
hostage to protocol churn; it belongs in the CLI (DESIGN §3.4, §7).
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path
from urllib.request import urlopen

import pytest

from cadastre.adapters.http import CadastreHTTPServer
from cadastre.mcp import server

SOURCE = Path(server.__file__)


def test_it_exposes_exactly_the_documented_tools() -> None:
    assert {tool.__name__ for tool in server.TOOLS} == {
        "brief",
        "context_for",
        "check",
        "lookup",
        "drift",
        "question",
        "observations",
        "version",
    }


def test_every_tool_documents_itself_for_the_agent_reading_it() -> None:
    for tool in server.TOOLS:
        assert tool.__doc__, tool.__name__
        # The docstring is the tool description an agent sees; it has to say
        # when to call it, not just what it is.
        assert len(tool.__doc__.split()) > 10, tool.__name__


def test_the_adapter_stays_around_two_hundred_lines() -> None:
    """DESIGN §3.4 gives a target of ~200 lines. It is a budget, not a joke:
    the number going up is the signal that logic has leaked in."""
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 200, f"{len(lines)} lines — has logic leaked in?"


def test_each_tool_is_one_call_into_the_cli_and_one_render() -> None:
    """No branching, no joining, no formatting of its own."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in {t.__name__ for t in server.TOOLS}:
            continue
        body = [n for n in node.body if not isinstance(n, ast.Expr)]
        assert len(body) == 1, f"{node.name} does more than delegate"
        assert isinstance(body[0], ast.Return), node.name
        branches = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.If | ast.For | ast.While | ast.Try)
        ]
        assert not branches, f"{node.name} contains control flow"


def test_a_catalog_error_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An MCP tool that raises gives the agent a stack trace to improvise
    against. It gets the located error instead."""
    monkeypatch.setenv("CADASTRE_CATALOG", str(tmp_path))
    answer = server.brief()
    payload = json.loads(answer)
    assert payload["error"]["kind"] == "catalog_error"
    assert "declared/" in answer


def test_tools_answer_against_a_real_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import EXAMPLE_CATALOG

    monkeypatch.setenv("CADASTRE_CATALOG", str(EXAMPLE_CATALOG))
    assert json.loads(server.brief())["command"] == "cadastre brief"
    context = json.loads(server.context_for("an internal service with a gpu"))
    assert context["command"] == "cadastre context-for an internal service with a gpu"
    assert json.loads(server.lookup("app-01"))["command"] == "cadastre lookup app-01"
    assert json.loads(server.drift())["command"] == "cadastre drift"


def test_version_tool_answers_locally_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: mcp/server.py's version() passed HealthService's plain dict
    straight to json_out.render(), which requires a Document and raised
    AttributeError ('dict' object has no attribute 'title') on every call."""
    from tests.conftest import EXAMPLE_CATALOG

    monkeypatch.setenv("CADASTRE_CATALOG", str(EXAMPLE_CATALOG))
    payload = json.loads(server.version())
    assert payload["command"] == "cadastre version"
    assert payload["result"]["name"] == "cadastre"
    assert "version" in payload["result"]


def test_version_is_dispatchable_through_the_query_service() -> None:
    """Regression: the Streamable HTTP and remote-bridge transports both
    resolve a tool by name through QueryService.dispatch(), which had no
    `version` case at all and raised "unknown query operation `version`"."""
    from datetime import UTC, datetime

    from cadastre.application.context import ApplicationContext
    from cadastre.application.queries import QueryService
    from tests.conftest import EXAMPLE_CATALOG

    context = ApplicationContext(
        root=EXAMPLE_CATALOG, now=datetime.now(tz=UTC), runtime=False
    )
    document = QueryService(context).dispatch("version", {})
    assert document.data["name"] == "cadastre"


def test_remote_only_mode_never_opens_a_local_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CADASTRE_CATALOG", str(tmp_path))
    monkeypatch.setenv("CADASTRE_REMOTE_ONLY", "1")
    monkeypatch.delenv("CADASTRE_HTTP_URL", raising=False)
    answer = json.loads(server.brief())
    assert answer["error"]["kind"] == "CadastreError"
    assert "CADASTRE_HTTP_URL is required" in answer["error"]["message"]


def test_remote_only_rejects_plain_http_for_non_local_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CADASTRE_HTTP_URL", "http://cadastre.internal.example")
    monkeypatch.setenv("CADASTRE_REMOTE_ONLY", "1")
    answer = json.loads(server.brief())
    assert answer["error"]["kind"] == "RemoteClientError"
    assert "requires an https endpoint" in answer["error"]["message"]


def test_mcp_can_use_the_networked_http_topology(
    monkeypatch: pytest.MonkeyPatch,
    catalog_copy: Path,
    tmp_path: Path,
) -> None:
    server_instance = CadastreHTTPServer(
        ("127.0.0.1", 0), catalog_copy, allow_write=False
    )
    thread = threading.Thread(target=server_instance.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "CADASTRE_HTTP_URL", f"http://127.0.0.1:{server_instance.server_port}"
    )
    monkeypatch.setenv("CADASTRE_REMOTE_ONLY", "1")
    monkeypatch.setenv("CADASTRE_CATALOG", str(tmp_path / "not-a-catalog"))
    try:
        remote = json.loads(server.brief())
        with urlopen(
            f"http://127.0.0.1:{server_instance.server_port}/brief", timeout=3
        ) as response:
            direct = json.loads(response.read())
        assert remote == direct
    finally:
        server_instance.shutdown()
        thread.join(timeout=3)
        server_instance.server_close()
