"""Static checks for the copyable, estate-independent client examples."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
CLIENTS = ROOT / "examples" / "clients"


def test_client_examples_exist_and_use_the_remote_bridge() -> None:
    claude = json.loads((CLIENTS / "claude-code.json").read_text())
    codex = (CLIENTS / "codex-cli.sh.example").read_text()
    opencode = json.loads((CLIENTS / "opencode.json").read_text())
    env = (CLIENTS / "cadastre-remote.env.sample").read_text()

    assert claude["mcpServers"]["cadastre"]["command"] == "cadastre-mcp-remote"
    assert claude["mcpServers"]["cadastre"]["env"]["CADASTRE_MCP_URL"] == (
        "${CADASTRE_MCP_URL}"
    )
    assert "codex mcp add cadastre" in codex
    assert "-- cadastre-mcp-remote" in codex
    assert opencode["mcp"]["cadastre"]["command"] == ["cadastre-mcp-remote"]
    assert "CADASTRE_MCP_URL=https://" in env
    assert "CADASTRE_HTTP_TOKEN_FILE=" in env
    assert "CADASTRE_REMOTE_ONLY=1" in env


def test_client_examples_have_no_secret_values_or_estate_dependencies() -> None:
    content = "\n".join(path.read_text() for path in CLIENTS.iterdir())
    assert "Infisical" not in content
    assert "Tailscale" not in content
    assert "Node B" not in content
    assert "Bearer " not in content
    assert 'token="' not in content


def test_security_profile_example_is_valid_yaml() -> None:
    profile = yaml.safe_load((ROOT / "examples" / "security-profile.yaml").read_text())
    assert profile["endpoint"].endswith("/mcp")
    assert profile["client_identity"] == "scoped-bearer-token"


def test_manifest_examples_are_valid_and_present() -> None:
    modules = yaml.safe_load((ROOT / "examples/modules.sample.yaml").read_text())
    plugins = (ROOT / "examples/plugins.sample.yaml").read_text()
    assert modules == {"modules": {"manifest": {"enabled": True}}}
    assert "cadastre-plugin-work-git" in plugins
    assert "cadastre-plugin-work-markdown" in plugins
