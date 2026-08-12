"""Small HTTP client for unprivileged Cadastre consumers.

The client returns the server's canonical JSON document rather than rebuilding
one locally.  That keeps a networked MCP consumer on the same answer surface as
the CLI and HTTP adapter, while keeping catalog access on the server side.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from cadastre import __version__
from cadastre.core.errors import CadastreError


class RemoteClientError(CadastreError):
    """The configured Cadastre HTTP endpoint could not be consulted."""


def mcp_endpoint(endpoint: str, *, remote_only: bool = True) -> str:
    """Validate and normalize the standard Streamable HTTP MCP endpoint."""
    validate_endpoint(endpoint, remote_only=remote_only)
    parsed = urlsplit(endpoint)
    if parsed.query or parsed.fragment:
        raise RemoteClientError("MCP endpoint must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if path != "/mcp":
        raise RemoteClientError("MCP endpoint path must be `/mcp`")
    return endpoint.rstrip("/")


def validate_endpoint(endpoint: str, *, remote_only: bool = False) -> None:
    """Reject an insecure non-local endpoint for the agent-runtime path."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RemoteClientError("CADASTRE_HTTP_URL must be an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteClientError("Cadastre endpoints must not contain URL userinfo")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if remote_only and parsed.scheme != "https" and not local:
        raise RemoteClientError(
            "CADASTRE_REMOTE_ONLY requires an https endpoint outside localhost"
        )


def request(
    endpoint: str,
    route: str,
    *,
    method: str = "GET",
    query: Mapping[str, str | None] | None = None,
    body: Mapping[str, Any] | None = None,
    token: str | None = None,
) -> str:
    """Call a read-only HTTP route and return canonical JSON."""
    base = endpoint.rstrip("/")
    validate_endpoint(base)
    params = {key: value for key, value in (query or {}).items() if value is not None}
    route_path, separator, route_query = route.partition("?")
    url = f"{base}/{quote(route_path.lstrip('/'), safe='/{}')}"
    if separator:
        url += "?" + route_query
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    encoded = None
    headers: dict[str, str] = {}
    if body is not None:
        encoded = json.dumps(dict(body)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request_ = urllib.request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request_, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if not raw:
            raise RemoteClientError(
                f"HTTP endpoint returned status {exc.code}"
            ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RemoteClientError(
            f"could not reach Cadastre HTTP endpoint: {exc}"
        ) from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteClientError("Cadastre HTTP endpoint returned invalid JSON") from exc
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def question(
    endpoint: str,
    question_id: str,
    *,
    subject: str | None = None,
    value: str | None = None,
    token: str | None = None,
) -> str:
    return request(
        endpoint,
        "/question",
        query={"id": question_id, "subject": subject, "value": value},
        token=token,
    )


class StreamableClient:
    """Minimal synchronous client for the MCP Streamable HTTP transport."""

    def __init__(self, endpoint: str, token: str | None = None) -> None:
        self.endpoint = mcp_endpoint(endpoint)
        self.token = token
        self.session_id: str | None = None

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": dict(params or {}),
        }
        request_ = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request_, timeout=10) as response:
                raw = response.read()
                session = response.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("error", {}).get("message", "")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise RemoteClientError(
                f"MCP endpoint returned HTTP {exc.code}{suffix}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteClientError(
                f"could not reach Cadastre MCP endpoint: {exc}"
            ) from exc
        if session:
            self.session_id = session
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteClientError(
                "Cadastre MCP endpoint returned invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise RemoteClientError("Cadastre MCP endpoint returned a non-object")
        if "error" in result:
            error = result["error"]
            message = (
                error.get("message", "MCP request failed")
                if isinstance(error, dict)
                else str(error)
            )
            raise RemoteClientError(str(message))
        return result

    def initialize(self) -> None:
        self.call(
            "initialize",
            {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "cadastre", "version": __version__},
            },
        )
        self.call("notifications/initialized")

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's declared tools after MCP initialization."""
        if self.session_id is None:
            self.initialize()
        result = self.call("tools/list")
        tools = result.get("result", {}).get("tools", [])
        if not isinstance(tools, list) or not all(
            isinstance(item, dict) for item in tools
        ):
            raise RemoteClientError("MCP tools/list returned an invalid result")
        return tools

    def tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> str:
        if self.session_id is None:
            self.initialize()
        result = self.call(
            "tools/call", {"name": name, "arguments": dict(arguments or {})}
        )
        value = result.get("result", {})
        if not isinstance(value, dict):
            raise RemoteClientError("MCP tools/call returned an invalid result")
        structured = value.get("structuredContent")
        if isinstance(structured, dict):
            return json.dumps(structured, indent=2, sort_keys=False) + "\n"
        content = value.get("content", [])
        if (
            content
            and isinstance(content[0], dict)
            and isinstance(content[0].get("text"), str)
        ):
            return content[0]["text"]
        raise RemoteClientError("MCP tools/call returned no structured content")


def token_from_file() -> str | None:
    """Read an optional bearer token without placing it in argv or URLs."""
    path = os.environ.get("CADASTRE_HTTP_TOKEN_FILE", "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise RemoteClientError(
            f"could not read CADASTRE_HTTP_TOKEN_FILE: {exc}"
        ) from exc
    return token or None
