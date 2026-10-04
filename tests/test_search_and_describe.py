"""WI-848 / WI-864: find by words, credentials for a service, secret metadata.

The evidence behind these: 27 of 27 logged `lookup` failures were callers
passing a phrase where an exact id was required; the Infisical project id an
API call needs was not discoverable from Cadastre at all; and production
deploy #49 failed on a secret with raw newlines that only a hand-written,
value-reading script could find.

The property that must never regress: no secret value, and no fragment of
one, appears in any answer.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from cadastre.application.context import ApplicationContext
from cadastre.cli.search import credential_for, search
from cadastre.cli.secret_describe import secret_describe
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import Located, MissingEntityError, UsageError
from cadastre.core.observed import ObservedSource, parse_source
from cadastre.core.secretshape import describe_value, shape_problems
from cadastre.mcp.streamable import _Handler
from cadastre.plugins.collectors import secrets_infisical
from cadastre.plugins.config import PluginsFile, SourceConfig
from cadastre.render import json_out
from cadastre.render.text import render

AS_OF = "2026-08-07T11:55:00Z"

#: Distinctive fake values. Three raw newlines and a CR in the first — the
#: deploy-#49 shape; the second is single-line JSON.
FAKE_MULTILINE = "Zq9#Xk!V3pQ\nLmT7@wR2sJd\r\nQ8&yH4^nB6\nVv0%Tt5"
FAKE_JSON = '{"k9Qzw":"Wv7Lx2NpRr"}'
FAKE_KOMODO = "kmd_9fJ2xQ7vLp3Rz8Tw"

APPS_PAYLOAD: dict[str, list[dict[str, Any]]] = {
    "secrets": [
        {
            "secretKey": "HOMELAB_KOMODO_API_KEY",
            "secretPath": "/",
            "secretValue": FAKE_KOMODO,
            "version": 4,
            "updatedAt": "2026-05-13T08:00:00Z",
        },
        {
            "secretKey": "HOMELAB_KOMODO_API_SECRET",
            "secretPath": "/",
            "secretValue": FAKE_JSON,
            "version": 2,
            "updatedAt": "2026-05-13T08:00:00Z",
        },
        {
            "secretKey": "HOMELAB_KOMODO_API_KEY_REVOKED",
            "secretPath": "/",
            "secretValue": "old-value-xyz",
        },
        {
            "secretKey": "LINEAR_API_KEY",
            "secretPath": "/",
            "secretValue": "lin_api_q8w7e6r5t4",
        },
        {
            "secretKey": "FCM_SERVICE_ACCOUNT_JSON_COMPACT",
            "secretPath": "/",
            "secretValue": FAKE_MULTILINE,
            "version": 7,
            "updatedAt": "2026-10-04T05:00:00Z",
        },
    ]
}

APPS_OPTIONS = {
    "store": "infisical:apps",
    "environment": "prod",
    "ref_prefix": "infisical://apps/",
    "workspace_id": "3f2b9c1e-0000-4a5b-9c8d-123456789abc",
    "project_slug": "apps-x1y2",
    "endpoint": "https://secrets.example.invalid/",
}


def _secrets_source(
    payload: dict[str, Any], options: dict[str, Any], source: str
) -> ObservedSource:
    """Run the real collector transform, then the real observed parser."""
    result = secrets_infisical.transform(payload, options)
    secrets_infisical._assert_no_values(result)
    parsed = parse_source(
        {"entities": result["entities"]},
        Located(source),
        extensions={"secret": {"x-secret-store"}},
    )
    return dataclasses.replace(
        parsed,
        source=source,
        plugin="secrets-infisical",
        as_of=AS_OF,
        capabilities=("secret.list",),
    )


def _stack(ident: str, refs: list[str]) -> ObservedSource:
    return ObservedSource(
        source="orchestrator",
        plugin="orchestrator-gitops",
        as_of=AS_OF,
        capabilities=("inventory.list",),
        entities={
            "service": [
                model.Service(
                    id=ident,
                    extra={
                        "x-orchestrator": {
                            "compose_services": [{"name": ident}],
                            "variable_refs": refs,
                        }
                    },
                )
            ]
        },
    )


@pytest.fixture
def estate(session: Session) -> Session:
    return dataclasses.replace(
        session,
        observed=(
            _secrets_source(APPS_PAYLOAD, APPS_OPTIONS, "secrets-apps"),
            _secrets_source(
                {
                    "secrets": [
                        {
                            "secretKey": "db-password",
                            "secretPath": "/notes-api",
                            "secretValue": FAKE_MULTILINE,
                            "version": 3,
                        }
                    ]
                },
                {
                    "store": "secrets-manager",
                    "environment": "prod",
                    "workspace_id": "ws-manager",
                },
                "secrets-manager",
            ),
            _stack("komodo-periphery", ["HOMELAB_KOMODO_API_KEY", "TZ"]),
            _stack("notes-stack", ["DB_PASSWORD"]),
        ),
    )


def _everything(document: Any) -> str:
    return json_out.render(document) + "\n" + render(document)


def _assert_no_fragment(text: str, *values: str, width: int = 5) -> None:
    for value in values:
        for start in range(len(value) - width + 1):
            fragment = value[start : start + width]
            assert fragment not in text, f"value fragment {fragment!r} leaked"


# --------------------------------------------------------------------------
# The shape function
# --------------------------------------------------------------------------


def test_shape_counts_newlines_and_returns_nothing_of_the_value() -> None:
    shape = describe_value(FAKE_MULTILINE)
    assert shape["newlines"] == 3
    assert shape["carriage_returns"] == 1
    assert shape["single_line"] is False
    assert shape["length"] == len(FAKE_MULTILINE)
    assert shape["parses_as_json"] is False
    _assert_no_fragment(json.dumps(shape), FAKE_MULTILINE, width=3)
    assert any("newline" in problem for problem in shape_problems(shape))


def test_shape_recognises_json() -> None:
    shape = describe_value(FAKE_JSON)
    assert shape["parses_as_json"] is True
    assert shape["json_type"] == "object"
    assert shape["single_line"] is True
    assert shape_problems(shape) == []


def test_the_collector_emits_shape_and_location_but_no_value() -> None:
    result = secrets_infisical.transform(APPS_PAYLOAD, APPS_OPTIONS)
    blocks = {
        item["x-secret-store"]["key"]: item["x-secret-store"]
        for item in result["entities"]["secret"]
    }
    block = blocks["HOMELAB_KOMODO_API_KEY"]
    assert block["project_id"] == APPS_OPTIONS["workspace_id"]
    assert block["server"] == "https://secrets.example.invalid"
    assert block["key"] == "HOMELAB_KOMODO_API_KEY"
    assert block["version"] == 4
    assert block["shape"]["length"] == len(FAKE_KOMODO)
    text = json.dumps(result)
    for item in APPS_PAYLOAD["secrets"]:
        _assert_no_fragment(text, str(item["secretValue"]))


# --------------------------------------------------------------------------
# search — lookup(query=...)
# --------------------------------------------------------------------------


def test_a_phrase_finds_the_secret_with_a_ready_manifest_line(
    estate: Session,
) -> None:
    """The WI-848 acceptance: `komodo api key` -> a usable manifest line,
    no entity_id."""
    document = search(estate, "komodo api key")
    top = document.data["results"][0]
    assert top["kind"] == "secret"
    assert top["secret"]["key"] == "HOMELAB_KOMODO_API_KEY"
    assert top["secret"]["manifest_line"] == (
        "HOMELAB_KOMODO_API_KEY 3f2b9c1e-0000-4a5b-9c8d-123456789abc "
        "HOMELAB_KOMODO_API_KEY"
    )
    assert top["secret"]["server"] == "https://secrets.example.invalid"
    assert top["secret"]["project_slug"] == "apps-x1y2"
    assert all(r["match"] == "all" for r in document.data["results"])
    assert "linear-api-key" not in json.dumps(document.data).lower()
    _assert_no_fragment(_everything(document), FAKE_KOMODO, FAKE_JSON)


def test_search_merges_a_declared_secret_with_its_observation(
    estate: Session,
) -> None:
    document = search(estate, "notes api db password")
    secrets = [r for r in document.data["results"] if r["kind"] == "secret"]
    assert len(secrets) == 1
    assert secrets[0]["declared"] is True
    assert secrets[0]["observed_by"] == ["secrets-manager"]
    assert secrets[0]["secret"]["project_id"] == "ws-manager"


def test_search_falls_back_to_source_configuration_for_the_project(
    session: Session,
) -> None:
    """A collector older than `x-secret-store` still yields a project id when
    the source's configuration is visible to this process."""
    configured = dataclasses.replace(
        session,
        plugins=PluginsFile(
            sources=(
                SourceConfig(
                    id="secrets-manager",
                    command=("cadastre-plugin-secrets-infisical",),
                    config={"store": "secrets-manager", "workspace_id": "ws-cfg"},
                ),
            )
        ),
    )
    document = search(configured, "registry read token")
    hit = document.data["results"][0]
    assert hit["id"] == "registry-read-token"
    assert hit["secret"]["project_id"] == "ws-cfg"
    assert hit["secret"]["manifest_line"].startswith("registry-read-token ws-cfg")


def test_search_without_words_says_what_it_needs(session: Session) -> None:
    with pytest.raises(UsageError, match="komodo api key"):
        search(session, "  ")


def test_lookup_accepts_query_or_entity_id_and_names_both_when_neither(
    example_catalog: Any,
) -> None:
    from cadastre.application.queries import QueryService

    service = QueryService(ApplicationContext.open(example_catalog, runtime=False))
    assert service.dispatch("lookup", {"query": "notes api"}).data["resolution"] == (
        "search"
    )
    by_id = service.dispatch("lookup", {"entity_id": "notes-api"})
    assert by_id.data["resolution"] == "declared"
    with pytest.raises(UsageError, match=r"entity_id.*query"):
        service.dispatch("lookup", {})


# --------------------------------------------------------------------------
# Forgiving MCP schemas
# --------------------------------------------------------------------------


def test_streamable_lookup_takes_query_without_an_entity_id() -> None:
    _Handler._validate_tool_arguments("lookup", {"query": "komodo api key"})


def test_a_wrong_argument_name_gets_a_did_you_mean_and_an_example() -> None:
    with pytest.raises(UsageError) as caught:
        _Handler._validate_tool_arguments("context_for", {"query": "deploy x"})
    message = str(caught.value)
    assert "did you mean 'intent'" in message
    assert "required: intent" in message
    assert "example:" in message


def test_a_missing_required_argument_lists_required_fields() -> None:
    with pytest.raises(UsageError) as caught:
        _Handler._validate_tool_arguments("context_for", {})
    assert "needs argument 'intent'" in str(caught.value)
    with pytest.raises(UsageError, match="required: hostname"):
        _Handler._validate_tool_arguments("dns_chain", {})


def test_tool_schemas_describe_their_arguments() -> None:
    from cadastre.api.registry import operation_for_mcp

    schema = operation_for_mcp("lookup").input_schema()
    assert schema["required"] == []
    query = schema["properties"]["query"]["anyOf"][0]
    assert "komodo api key" in query["description"]
    intent = operation_for_mcp("context_for").input_schema()
    assert intent["required"] == ["intent"]
    assert "description" in intent["properties"]["intent"]


# --------------------------------------------------------------------------
# credential_for
# --------------------------------------------------------------------------


def test_credential_for_ranks_the_service_pair_first_and_retired_last(
    estate: Session,
) -> None:
    document = credential_for(estate, "komodo", "deploy")
    keys = [c["secret"]["key"] for c in document.data["candidates"]]
    assert set(keys[:2]) == {"HOMELAB_KOMODO_API_KEY", "HOMELAB_KOMODO_API_SECRET"}
    assert keys[-1] == "HOMELAB_KOMODO_API_KEY_REVOKED"
    assert "LINEAR_API_KEY" not in keys
    first = document.data["candidates"][0]
    assert first["secret"]["project_id"] == APPS_OPTIONS["workspace_id"]
    assert first["secret"]["manifest_line"]
    _assert_no_fragment(_everything(document), FAKE_KOMODO, FAKE_JSON)


def test_credential_for_uses_declared_consumption(estate: Session) -> None:
    document = credential_for(estate, "notes-api")
    top = document.data["candidates"][0]
    assert top["id"] == "notes-api-db-password"
    assert "declared consumes_secret of the service" in top["why"]
    assert document.data["service_found"] == ["notes-api"]


def test_credential_for_with_nothing_related_is_an_answer_not_an_error(
    estate: Session,
) -> None:
    document = credential_for(estate, "no-such-thing")
    assert document.data["candidates"] == []
    assert "lookup(query=" in render(document)


# --------------------------------------------------------------------------
# secret_describe
# --------------------------------------------------------------------------


def test_describe_reports_the_deploy_49_shape_without_the_value(
    estate: Session,
) -> None:
    document = secret_describe(estate, "FCM_SERVICE_ACCOUNT_JSON_COMPACT")
    item = document.data["secrets"][0]
    assert item["project_id"] == APPS_OPTIONS["workspace_id"]
    assert item["project_slug"] == "apps-x1y2"
    assert item["environment"] == "prod"
    assert item["path"] == "/"
    assert item["version"] == 7
    assert item["updated_at"] == "2026-10-04T05:00:00Z"
    shape = item["value_shape"]
    assert shape["newlines"] == 3
    assert shape["carriage_returns"] == 1
    assert shape["parses_as_json"] is False
    assert any("newline" in p for p in item["value_shape_problems"])
    _assert_no_fragment(_everything(document), FAKE_MULTILINE)


def test_describe_by_reference_names_declared_and_observed_consumers(
    estate: Session,
) -> None:
    document = secret_describe(estate, "/prod/notes-api/db-password")
    item = document.data["secrets"][0]
    assert item["declared_id"] == "notes-api-db-password"
    consumers = {(c["id"], c["via"]) for c in item["consumers"]}
    assert ("notes-api", "consumes_secret") in consumers
    assert ("notes-stack", "x-orchestrator.variable_refs") in consumers
    assert item["value_shape"]["length"] == len(FAKE_MULTILINE)
    _assert_no_fragment(_everything(document), FAKE_MULTILINE)


def test_describe_by_full_ref_reports_json(estate: Session) -> None:
    document = secret_describe(
        estate, "infisical://apps/prod/HOMELAB_KOMODO_API_SECRET"
    )
    item = document.data["secrets"][0]
    assert item["value_shape"]["parses_as_json"] is True
    assert item["consumers"] == []
    _assert_no_fragment(_everything(document), FAKE_JSON)


def test_describe_without_collected_shape_says_why(session: Session) -> None:
    document = secret_describe(session, "registry-read-token")
    item = document.data["secrets"][0]
    assert item["value_shape"] is None
    assert "x-secret-store" in item["value_shape_note"]


def test_describe_an_unknown_secret_is_a_clear_miss(estate: Session) -> None:
    with pytest.raises(MissingEntityError, match="lookup"):
        secret_describe(estate, "NOPE_NOT_A_SECRET")


# --------------------------------------------------------------------------
# Consumers from the orchestrator, and the tools over Streamable HTTP MCP
# --------------------------------------------------------------------------


def test_the_orchestrator_records_interpolated_variable_names_only() -> None:
    from cadastre.plugins.collectors import orchestrator_gitops

    entity = orchestrator_gitops.transform_stack(
        {
            "services": {
                "app": {
                    "image": "app:1",
                    "environment": {
                        "API_KEY": "${HOMELAB_KOMODO_API_KEY}",
                        "TOKEN": "[[LINEAR_API_KEY]]",
                        "MODE": "literal-value-not-a-ref",
                        "URL": "https://x/$PATH_PART",
                    },
                }
            }
        },
        stack="app",
        host=None,
        options={},
    )
    assert entity is not None
    assert entity["x-orchestrator"]["variable_refs"] == [
        "HOMELAB_KOMODO_API_KEY",
        "LINEAR_API_KEY",
        "PATH_PART",
    ]
    assert "literal-value" not in json.dumps(entity)


def test_the_new_tools_answer_over_streamable_http(tmp_path: Any) -> None:
    import shutil

    from cadastre.core.storage import import_legacy, initialize
    from cadastre.mcp.streamable import MCPHTTPServer
    from tests.conftest import EXAMPLE_CATALOG

    root = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, root)
    initialize(root)
    import_legacy(root, root)
    server = MCPHTTPServer(("127.0.0.1", 0), root, require_auth=False)
    try:
        found = json.loads(server.call_tool("lookup", {"query": "registry read"}))
        assert found["result"]["results"][0]["id"] == "registry-read-token"
        creds = json.loads(server.call_tool("credential_for", {"service": "notes-api"}))
        assert creds["result"]["candidates"][0]["id"] == "notes-api-db-password"
        chain = json.loads(
            server.call_tool("dns_chain", {"hostname": "notes.example.invalid"})
        )
        assert chain["result"]["chain"][0]["type"] == "CNAME"
        described = json.loads(
            server.call_tool("secret_describe", {"ref": "registry-read-token"})
        )
        assert described["result"]["secrets"][0]["declared_id"] == (
            "registry-read-token"
        )
    finally:
        server.server_close()


def test_the_http_routes_serve_the_same_use_cases(tmp_path: Any) -> None:
    import shutil
    import threading
    from http.client import HTTPConnection

    from cadastre.adapters.http import CadastreHTTPServer
    from cadastre.core.storage import import_legacy, initialize
    from tests.conftest import EXAMPLE_CATALOG

    root = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, root)
    initialize(root)
    import_legacy(root, root)
    server = CadastreHTTPServer(("127.0.0.1", 0), root, allow_write=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    answers = {}
    try:
        for route in (
            "/search?query=registry+read",
            "/credential-for?service=notes-api",
            "/dns-chain?hostname=notes.example.invalid",
            "/secret-describe?ref=registry-read-token",
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", route)
            response = connection.getresponse()
            assert response.status == 200, route
            answers[route.split("?")[0]] = json.loads(response.read())
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert answers["/search"]["result"]["results"][0]["id"] == "registry-read-token"
    assert answers["/dns-chain"]["result"]["hostname"] == "notes.example.invalid"
