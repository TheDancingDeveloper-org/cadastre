"""Canonical application boundary metadata."""

from cadastre.api.contract import DocumentEnvelope, ErrorEnvelope, RouteContract
from cadastre.api.registry import HTTP_ROUTES, MCP_OPERATIONS, Operation

__all__ = [
    "HTTP_ROUTES",
    "MCP_OPERATIONS",
    "DocumentEnvelope",
    "ErrorEnvelope",
    "Operation",
    "RouteContract",
]
