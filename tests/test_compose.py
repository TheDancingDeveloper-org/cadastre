"""Regression coverage for the shipped production Compose profiles."""

from __future__ import annotations

from pathlib import Path

import yaml

from cadastre.adapters.http import openapi_schema


def test_compose_profiles_are_isolated() -> None:
    path = Path(__file__).parents[1] / "compose.production.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = document["services"]

    assert services["cadastre-local"]["profiles"] == ["local"]
    assert services["cadastre-api"]["profiles"] == ["direct-https"]
    assert services["cadastre-mcp"]["profiles"] == ["direct-mcp"]
    assert services["cadastre-gui"]["profiles"] == ["direct-https", "direct-mcp"]
    assert services["cadastre-proxy"]["profiles"] == ["proxy"]
    assert services["cadastre-collector"]["profiles"] == ["collector"]
    assert services["cadastre-local"]["ports"] == [
        "127.0.0.1:${CADASTRE_LOCAL_PORT:-8000}:8000"
    ]
    assert services["cadastre-collector"]["restart"] == "no"
    assert "/health/ready" in str(services["cadastre-api"]["healthcheck"])
    assert services["cadastre-mcp"]["healthcheck"]["test"][1:3] == [
        "cadastre",
        "status",
    ]


def test_remote_compose_profiles_keep_runtime_safety_defaults() -> None:
    path = Path(__file__).parents[1] / "compose.production.yaml"
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]

    for name in (
        "cadastre-local",
        "cadastre-api",
        "cadastre-mcp",
        "cadastre-proxy",
        "cadastre-collector",
    ):
        service = services[name]
        assert service["read_only"] is True
        assert service["user"] == "10001:10001"
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["init"] is True
        assert service["stop_grace_period"] == "30s"
        assert service["deploy"]["resources"]["limits"] == {
            "cpus": "2.0",
            "memory": "1G",
        }
        assert "/var/run/docker.sock" not in str(service)


def test_mcp_compose_profile_is_distinct_from_the_ordinary_http_api() -> None:
    path = Path(__file__).parents[1] / "compose.production.yaml"
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]
    assert services["cadastre-api"]["command"][0] == "serve"
    assert services["cadastre-mcp"]["command"][0] == "mcp-http"
    assert "--tls-cert" in services["cadastre-api"]["command"]
    assert "--tls-key" in services["cadastre-api"]["command"]
    assert "--tls-cert" in services["cadastre-mcp"]["command"]
    assert "--tls-key" in services["cadastre-mcp"]["command"]
    assert "volumes" not in services["cadastre-gui"]


def test_compose_declares_portable_catalog_placement() -> None:
    path = Path(__file__).parents[1] / "compose.production.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["x-cadastre"]["host"] == "app-01"


def test_openapi_registry_contains_every_route_once() -> None:
    document = openapi_schema()
    assert all(len(methods) == 1 for methods in document["paths"].values())
    assert document["paths"]["/check"]["post"]["requestBody"]
