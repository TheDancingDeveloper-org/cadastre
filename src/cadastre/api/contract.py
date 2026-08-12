"""Typed transport contract shared by HTTP, MCP, and generated clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class DocumentEnvelope:
    command: str
    result: Any
    provenance: tuple[dict[str, Any], ...] = ()
    stale: tuple[str, ...] = ()


@dataclass(frozen=True)
class ErrorEnvelope:
    kind: str
    message: str

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {"error": {"kind": self.kind, "message": self.message}}


@dataclass(frozen=True)
class RouteContract:
    operation: str
    method: Literal["GET", "POST"]
    path: str
    scope: str
    mutating: bool
    request_fields: tuple[str, ...] = ()
    required_request_fields: tuple[str, ...] = ()


def error_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["kind", "message"],
                "properties": {
                    "kind": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }
