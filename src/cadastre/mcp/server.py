from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cadastre import __version__
from cadastre.adapters import client
from cadastre.application.checks import CheckService
from cadastre.application.context import ApplicationContext
from cadastre.application.queries import QueryService
from cadastre.core.errors import CadastreError
from cadastre.mcp.drift import drift
from cadastre.mcp.observations import observations
from cadastre.mcp.sdk import error_kind, register
from cadastre.mcp.writes import WRITE_TOOLS, write_mode_enabled
from cadastre.render import json_out

CATALOG_ENV = "CADASTRE_CATALOG"
HTTP_URL_ENV = "CADASTRE_HTTP_URL"
REMOTE_ONLY_ENV = "CADASTRE_REMOTE_ONLY"


def _root() -> Path:
    return Path(os.environ.get(CATALOG_ENV, ".")).expanduser()


def _queries() -> QueryService:
    return QueryService(ApplicationContext.open(_root(), runtime=False))


def _checks() -> CheckService:
    return CheckService(ApplicationContext.open(_root(), runtime=False))


def _safely(build: Any) -> str:
    try:
        return json_out.render(build())
    except (CadastreError, OSError, ValueError, TypeError) as exc:
        return _error_payload(exc)


def _error(exc: Exception) -> str:
    return _error_payload(exc)


def _error_payload(exc: Exception) -> str:
    return (
        json.dumps(
            {"error": {"kind": error_kind(exc), "message": str(exc)}},
            indent=2,
        )
        + "\n"
    )


def _remote_only() -> bool:
    return os.environ.get(REMOTE_ONLY_ENV, "").lower() in {"1", "true", "yes"}


def _answer(remote: Any, local: Any) -> str:
    endpoint = os.environ.get(HTTP_URL_ENV, "").strip()
    if endpoint:
        try:
            client.validate_endpoint(endpoint, remote_only=_remote_only())
            return remote(endpoint, client.token_from_file())
        except CadastreError as exc:
            return _error(exc)
    if _remote_only():
        return _error(
            CadastreError(
                f"{HTTP_URL_ENV} is required when {REMOTE_ONLY_ENV} is enabled"
            )
        )
    return _safely(local)


def _remote_check(
    endpoint: str, artifact: str, kind: str | None, token: str | None
) -> str:
    try:
        content = Path(artifact).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise CadastreError(f"cannot read check artifact {artifact!r}: {exc}") from exc
    return client.request(
        endpoint,
        "/check",
        method="POST",
        body={
            "artifact": content,
            "kind": kind,
            "path": Path(artifact).name,
        },
        token=token,
    )


def brief() -> str:
    """Summarize the estate for session context and always inspect its
    provenance block."""
    return _answer(
        lambda endpoint, token: client.request(endpoint, "/brief", token=token),
        lambda: _queries().brief(),
    )


def context_for(intent: str) -> str:
    """Return placement context, candidates, constraints, exclusions, and
    provenance for an operational intent."""
    return _answer(
        lambda endpoint, token: client.request(
            endpoint, "/context-for", query={"intent": intent}, token=token
        ),
        lambda: _queries().context_for(intent),
    )


def check(artifact: str, kind: str | None = None) -> str:
    """Check an artifact before commit and report actionable policy and
    placement findings."""
    return _answer(
        lambda endpoint, token: _remote_check(endpoint, artifact, kind, token),
        lambda: _checks().artifact(
            Path(artifact).expanduser().read_text(encoding="utf-8"),
            kind=kind,
            display_path=str(Path(artifact).expanduser()),
        ),
    )


def lookup(entity_id: str, kind: str | None = None) -> str:
    """Look up one entity and its related estate connections, provenance, and
    trust state."""
    return _answer(
        lambda endpoint, token: client.request(
            endpoint, f"/lookup/{entity_id}", query={"kind": kind}, token=token
        ),
        lambda: _queries().lookup(entity_id, kind=kind),
    )


def question(
    question_id: str, subject: str | None = None, value: str | None = None
) -> str:
    """Answer a migration question with provenance, warnings, constraints, and
    safe unknowns before deployment."""
    return _answer(
        lambda endpoint, token: client.question(
            endpoint, question_id, subject=subject, value=value, token=token
        ),
        lambda: _queries().question(question_id, subject=subject, value=value),
    )


def version() -> str:
    """Report this server's version and the oldest client bridge it supports."""
    return _answer(
        lambda endpoint, token: client.request(endpoint, "/version", token=token),
        lambda: _queries().version(),
    )


TOOLS = (brief, version, context_for, check, lookup, drift, question, observations)


def build_server() -> Any:
    """Register tools with the optional MCP SDK. Write tools join the
    read-only set only when write mode is enabled (DESIGN §2.4)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer
        except ImportError as exc:
            raise CadastreError(
                "the MCP adapter needs the optional dependency: "
                "`uv tool install 'cadastre[mcp-server]'`"
            ) from exc
        server = MCPServer("cadastre", version=__version__)
    else:
        server = FastMCP("cadastre")
    from cadastre.mcp.manifest import enabled_tools

    tools: tuple[Callable[..., str], ...] = TOOLS + enabled_tools(str(_root()))
    register(server, tools + WRITE_TOOLS if write_mode_enabled() else tools)
    return server


def main() -> int:  # pragma: no cover - runs a server loop
    try:
        build_server().run()
    except CadastreError as exc:
        print(f"cadastre-mcp: {exc}", file=sys.stderr)
        return 2
    return 0
