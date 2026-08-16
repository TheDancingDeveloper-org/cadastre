"""Registration helpers for SDK-backed stdio MCP without business logic."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Annotated, Any

from pydantic import Field

from cadastre.core.artifacts import artifact_kinds
from cadastre.core.errors import (
    AmbiguousEntityError,
    MissingEntityError,
    UnknownKindError,
    UsageError,
)

# The artifact kinds `check` accepts, as an annotation the SDK renders into the
# published schema. The set is closed, so saying so is what keeps a client from
# having to discover it by sending a wrong value and reading the error. Built
# from the parser registry, so a new parser cannot skip the schema.
ArtifactKind = Annotated[str, Field(json_schema_extra={"enum": list(artifact_kinds())})]


def error_kind(exc: Exception) -> str:
    if isinstance(exc, UnknownKindError):
        return "unknown_kind"
    if isinstance(exc, MissingEntityError):
        return "missing_entity"
    if isinstance(exc, AmbiguousEntityError):
        return "ambiguous_entity"
    if isinstance(exc, (UsageError, OSError, ValueError, TypeError)):
        return "invalid_argument"
    if type(exc).__name__ == "CatalogError":
        return "catalog_error"
    return type(exc).__name__


def _registered(tool: Callable[..., str]) -> Callable[..., Any]:
    """Turn Cadastre's JSON string into an MCP error result when necessary."""

    @wraps(tool)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        rendered = tool(*args, **kwargs)
        try:
            payload = json.loads(rendered)
        except (TypeError, json.JSONDecodeError):
            return rendered
        error = payload.get("error") if isinstance(payload, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        if error is None and isinstance(result, dict):
            error = result.get("error")
        if not isinstance(error, dict):
            return rendered

        from mcp.types import CallToolResult, TextContent

        message = str(error.get("message", "tool failed"))
        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            structured_content={"error": error},
            is_error=True,
        )

    return wrapped


def register(server: Any, tools: Iterable[Callable[..., str]]) -> None:
    for tool in tools:
        # The MCP SDK may infer an output schema from the wrapped function's
        # annotation.  Cadastre returns an already-serialized JSON document;
        # force the unstructured path so protocol-level error results retain
        # their structuredContent instead of being validated as `{result: ...}`.
        wrapped = _registered(tool)
        try:
            server.add_tool(wrapped, structured_output=False)
        except TypeError:  # Older optional SDKs do not expose this parameter.
            server.add_tool(wrapped)
