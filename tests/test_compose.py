"""Regression coverage for the shipped production Compose profiles."""

from __future__ import annotations

import re
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


def _compose_substitution_variables() -> set[str]:
    """Every ${CADASTRE_*} name substituted into the production stack."""
    path = Path(__file__).parents[1] / "compose.production.yaml"
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"\$\{(CADASTRE_[A-Z0-9_]+)[:\-}]", text))


def test_every_compose_variable_is_documented_for_operators() -> None:
    """A variable an operator must set is useless if only the compose file names it.

    Three copies drift apart silently, so this is the check that keeps
    `compose.production.yaml`, `.env.example`, and the DEPLOYMENT.md
    configuration reference in agreement.
    """
    root = Path(__file__).parents[1]
    variables = _compose_substitution_variables()
    assert variables, "no substitutions found — the regex or the file changed"

    sample = (root / ".env.example").read_text(encoding="utf-8")
    deployment = (root / "DEPLOYMENT.md").read_text(encoding="utf-8")

    missing_from_sample = sorted(v for v in variables if v not in sample)
    missing_from_docs = sorted(v for v in variables if v not in deployment)

    assert not missing_from_sample, f"absent from .env.example: {missing_from_sample}"
    assert not missing_from_docs, f"absent from DEPLOYMENT.md: {missing_from_docs}"


def test_env_example_invents_no_variable_the_stack_ignores() -> None:
    """The reverse direction: a documented variable that substitutes nowhere is a lie."""
    root = Path(__file__).parents[1]
    declared = set(
        re.findall(
            r"^(CADASTRE_[A-Z0-9_]+)=",
            (root / ".env.example").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    assert declared - _compose_substitution_variables() == set()


def test_collector_can_receive_per_plugin_credentials() -> None:
    """Infisical mints its own token; every other plugin needs env_file.

    Without this the containerised collector authenticates to Infisical, reads
    nothing from the forge, CI, DNS, VPN or hypervisor, and still exits 0.
    """
    root = Path(__file__).parents[1]
    services = yaml.safe_load(
        (root / "compose.production.yaml").read_text(encoding="utf-8")
    )["services"]

    entries = services["cadastre-collector"]["env_file"]
    assert entries == [
        {
            "path": "${CADASTRE_COLLECT_ENV_FILE:-/etc/cadastre/collect.env}",
            "required": False,
        }
    ], "an Infisical-only estate must still start, so this stays required: false"


def test_sample_credentials_cover_every_sample_plugin() -> None:
    """`collect.env.sample` is the operator's checklist; a gap in it is a silent source."""
    root = Path(__file__).parents[1]
    plugins = (root / "examples" / "plugins.sample.yaml").read_text(encoding="utf-8")
    sample = (root / "examples" / "collector" / "collect.env.sample").read_text(
        encoding="utf-8"
    )
    token_envs = set(re.findall(r"token_env:\s*([A-Z0-9_]+)", plugins))
    assert token_envs, "no token_env keys found in the sample plugin config"
    assert sorted(v for v in token_envs if v not in sample) == []
