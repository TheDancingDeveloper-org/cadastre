"""The transport-neutral operation and route registry.

Adapters consume this metadata; they do not define a second list of public
operations. Business execution is exposed through the application services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cadastre.api.contract import RouteContract
from cadastre.core.artifacts import artifact_kinds

# The one place an argument's JSON type is decided. Schema generation and
# transport validation both read it, so the schema a client is handed and the
# check that client's call is measured against cannot drift apart.
ARGUMENT_TYPES: dict[str, str] = {
    "limit": "integer",
    "summary_only": "boolean",
    "record": "object",
}

_JSON_TO_PYTHON: dict[str, type] = {
    "integer": int,
    "boolean": bool,
    "object": dict,
    "string": str,
}


#: What each argument means, published in every tool schema. A required field
#: an agent cannot read the purpose of is a required field it will guess at —
#: 27 of 27 logged `lookup` failures passed a phrase where an id was required.
ARGUMENT_DESCRIPTIONS: dict[str, str] = {
    "intent": "Plain-language operational intent, e.g. 'deploy a public web "
    "service with a gpu'.",
    "artifact": "The artifact text (Streamable HTTP) or a local file path "
    "(stdio), e.g. a compose file.",
    "path": "Display name for the artifact, used in findings.",
    "entity_id": "An exact entity id, e.g. 'node-b'. Use `query` instead "
    "when you only have words.",
    "query": "Free text over ids, names, addresses and secret references, "
    "e.g. 'komodo api key'. Returns ranked candidates.",
    "service": "A service name, e.g. 'komodo'.",
    "action": "What you want to do with the service, e.g. 'deploy', 'read', "
    "'ssh', 'push'. Optional; ranks candidates.",
    "hostname": "A fully qualified hostname, e.g. 'app.example.com'.",
    "ref": "A secret reference (scheme://project/env/KEY), a catalog id, or "
    "a bare key name.",
    "question_id": "One of the documented migration question ids.",
}

#: A ready call per operation, quoted in the error a malformed call receives.
OPERATION_EXAMPLES: dict[str, str] = {
    "context_for": '{"intent": "deploy a public web service"}',
    "check": '{"artifact": "<compose file text>", "kind": "compose"}',
    "lookup": '{"query": "komodo api key"} or {"entity_id": "node-b"}',
    "credential_for": '{"service": "komodo", "action": "deploy"}',
    "dns_chain": '{"hostname": "app.example.com"}',
    "secret_describe": '{"ref": "infisical://apps/prod/API_KEY"}',
    "question": '{"question_id": "<id>", "subject": "<entity>"}',
}

#: Argument names callers commonly send by mistake, and what they meant.
ARGUMENT_ALIASES: dict[tuple[str, str], str] = {
    ("context_for", "query"): "intent",
    ("context_for", "text"): "intent",
    ("lookup", "id"): "entity_id",
    ("lookup", "name"): "query",
    ("lookup", "q"): "query",
    ("credential_for", "name"): "service",
    ("dns_chain", "name"): "hostname",
    ("dns_chain", "host"): "hostname",
    ("secret_describe", "name"): "ref",
    ("secret_describe", "secret"): "ref",
    ("check", "content"): "artifact",
}


def argument_type(name: str) -> type:
    """The Python type an argument's JSON type maps to."""
    return _JSON_TO_PYTHON[ARGUMENT_TYPES.get(name, "string")]


@dataclass(frozen=True)
class Operation:
    name: str
    scope: str
    mutating: bool = False
    arguments: tuple[str, ...] = ()
    required_arguments: tuple[str, ...] | None = None
    route: str | None = None
    method: str = "GET"
    request_fields: tuple[str, ...] = ()
    required_request_fields: tuple[str, ...] = ()
    # Closed value sets, keyed by argument name. Generated from the module that
    # owns the set, never restated here.
    argument_enums: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def required_argument_names(self) -> tuple[str, ...]:
        if self.required_arguments is None:
            return self.arguments
        return self.required_arguments

    def input_schema(self) -> dict[str, Any]:
        required = self.required_argument_names()
        enums = dict(self.argument_enums)
        properties: dict[str, Any] = {}
        for name in self.arguments:
            schema: dict[str, Any] = {"type": ARGUMENT_TYPES.get(name, "string")}
            if name in ARGUMENT_DESCRIPTIONS:
                schema["description"] = ARGUMENT_DESCRIPTIONS[name]
            if name in enums:
                schema["enum"] = list(enums[name])
            if name not in required:
                # An optional argument is one a client may send as an explicit
                # null. Saying so in the schema is what lets a client that
                # materialises defaults rather than omitting keys call the
                # tool at all — and the transport accepts exactly this.
                schema = {"anyOf": [schema, {"type": "null"}], "default": None}
            properties[name] = schema
        return {
            "type": "object",
            "properties": properties,
            "required": list(required),
        }

    def request_field_schema(self, field: str) -> dict[str, Any]:
        """The published schema for one HTTP request field.

        Shares its type and enum with `input_schema` so the OpenAPI document
        and the MCP tool list describe the same operation.
        """
        schema: dict[str, Any] = {"type": ARGUMENT_TYPES.get(field, "string")}
        enums = dict(self.argument_enums)
        if field in enums:
            schema["enum"] = list(enums[field])
        return schema

    def contract(self) -> RouteContract:
        if self.route is None:
            raise ValueError(f"operation {self.name} has no HTTP route")
        return RouteContract(
            self.name,
            self.method,  # type: ignore[arg-type]
            self.route,
            self.scope,
            self.mutating,
            self.request_fields,
            self.required_request_fields,
        )


MCP_OPERATIONS: tuple[Operation, ...] = (
    Operation("brief", "catalog.read"),
    # The one authoritative thing a client can ask about compatibility. The
    # `/mcp` endpoint is already gated by MCP_SCOPE as a whole, so this needs
    # no lower scope of its own.
    Operation("version", "catalog.read"),
    Operation("context_for", "catalog.read", arguments=("intent",)),
    Operation(
        "check",
        "catalog.check",
        arguments=("artifact", "kind", "path"),
        required_arguments=("artifact",),
        argument_enums=(("kind", artifact_kinds()),),
    ),
    # `entity_id` or `query`, at least one — enforced by the use case, which
    # names both in its error. Neither is schema-required, so a caller with
    # only words is not forced to invent an id (WI-848).
    Operation(
        "lookup",
        "catalog.read",
        arguments=("entity_id", "kind", "query", "limit"),
        required_arguments=(),
    ),
    Operation(
        "credential_for",
        "catalog.read",
        arguments=("service", "action"),
        required_arguments=("service",),
    ),
    Operation(
        "dns_chain",
        "catalog.read",
        arguments=("hostname",),
        required_arguments=("hostname",),
    ),
    # Metadata only. The value never reaches the query layer: the secrets
    # collector reduced it to counts in its own process.
    Operation(
        "secret_describe",
        "catalog.read",
        arguments=("ref",),
        required_arguments=("ref",),
    ),
    Operation(
        "drift",
        "catalog.read",
        arguments=(
            "category",
            "kind",
            "source",
            "entity_id",
            "limit",
            "cursor",
            "summary_only",
        ),
        required_arguments=(),
    ),
    Operation(
        "question",
        "catalog.read",
        arguments=("question_id", "subject", "value"),
        required_arguments=("question_id",),
    ),
    # Evidence an agent may need to see, bounded so it cannot flood a context.
    # `summary_only` exists for exactly that reason.
    Operation(
        "observations",
        "catalog.read",
        arguments=("source", "method", "key", "limit", "summary_only"),
        required_arguments=(),
    ),
)

MANIFEST_MCP_OPERATIONS: tuple[Operation, ...] = (
    Operation("manifest_brief", "catalog.read"),
    Operation("manifest_projects", "catalog.read"),
    Operation(
        "manifest_backlog",
        "catalog.read",
        arguments=("state", "initiative", "repo", "limit"),
        required_arguments=(),
    ),
    Operation(
        "manifest_next",
        "catalog.read",
        arguments=("limit",),
        required_arguments=(),
    ),
    Operation(
        "manifest_drift",
        "catalog.read",
        arguments=("repo",),
        required_arguments=(),
    ),
    Operation(
        "manifest_repo",
        "catalog.read",
        arguments=("repo",),
        required_arguments=("repo",),
    ),
    Operation(
        "manifest_why",
        "catalog.read",
        arguments=("entity_id",),
        required_arguments=("entity_id",),
    ),
)


#: The MCP write surface (§1 of the 2026-08-11 issue review). Mirrors the
#: HTTP write routes one for one, `catalog.write` scoped and mutating.
#: `delete` is deliberately absent — least reversible, lowest agent need; a
#: refused `add`/`delete` on a source-authoritative kind already names the
#: CLI/HTTP path in its structured refusal (DESIGN §2.4), which still holds
#: for the operations that are exposed. `principal` is never an argument:
#: it comes from authentication only, or any `mcp`-scoped caller could forge
#: the §2.3 provenance stamp.
MCP_WRITE_OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "add",
        "catalog.write",
        mutating=True,
        arguments=("kind", "record", "reason"),
        required_arguments=("kind", "record"),
    ),
    Operation(
        "update",
        "catalog.write",
        mutating=True,
        arguments=("kind", "id", "record", "reason"),
        required_arguments=("kind", "id", "record"),
    ),
    Operation(
        "annotate",
        "catalog.write",
        mutating=True,
        arguments=("kind", "id", "record", "reason"),
        required_arguments=("kind", "id", "record"),
    ),
    Operation(
        "accept",
        "catalog.write",
        mutating=True,
        arguments=("target", "source", "field", "reason"),
        required_arguments=("target", "source"),
    ),
    Operation(
        "leave_contested",
        "catalog.write",
        mutating=True,
        arguments=("target", "source", "field", "reason"),
        required_arguments=("target", "source"),
    ),
    Operation(
        "acknowledge",
        "catalog.write",
        mutating=True,
        arguments=("target", "source", "until", "reason"),
        required_arguments=("target", "source", "until"),
    ),
)


HTTP_ROUTES: tuple[Operation, ...] = (
    Operation("brief", "catalog.read", route="/brief"),
    Operation(
        "context_for",
        "catalog.read",
        route="/context-for",
        request_fields=("intent",),
        required_request_fields=("intent",),
    ),
    Operation(
        "question",
        "catalog.read",
        route="/question",
        request_fields=("id", "subject", "value"),
        required_request_fields=("id",),
    ),
    Operation(
        "lookup",
        "catalog.read",
        route="/lookup/{id}",
        request_fields=("id", "kind"),
        required_request_fields=("id",),
    ),
    Operation(
        "search",
        "catalog.read",
        route="/search",
        request_fields=("query", "kind", "limit"),
        required_request_fields=("query",),
    ),
    Operation(
        "credential_for",
        "catalog.read",
        route="/credential-for",
        request_fields=("service", "action"),
        required_request_fields=("service",),
    ),
    Operation(
        "dns_chain",
        "catalog.read",
        route="/dns-chain",
        request_fields=("hostname",),
        required_request_fields=("hostname",),
    ),
    Operation(
        "secret_describe",
        "catalog.read",
        route="/secret-describe",
        request_fields=("ref",),
        required_request_fields=("ref",),
    ),
    Operation("drift", "catalog.read", route="/drift"),
    Operation(
        "observations",
        "catalog.read",
        route="/observations",
        request_fields=("source", "method", "key", "limit", "summary_only"),
    ),
    Operation("stale", "catalog.read", route="/stale"),
    Operation("plugins", "catalog.read", route="/plugins"),
    Operation("sources", "catalog.read", route="/sources"),
    Operation("security-check", "catalog.read", route="/security-check"),
    Operation("schema", "catalog.read", route="/schema"),
    Operation("version", "catalog.read", route="/version"),
    Operation(
        "check",
        "catalog.check",
        route="/check",
        method="POST",
        request_fields=("artifact", "kind", "path"),
        required_request_fields=("artifact",),
        argument_enums=(("kind", artifact_kinds()),),
    ),
    Operation(
        "add",
        "catalog.write",
        mutating=True,
        route="/add",
        method="POST",
        request_fields=("kind", "record", "reason"),
        required_request_fields=("kind", "record"),
    ),
    Operation(
        "update",
        "catalog.write",
        mutating=True,
        route="/update",
        method="POST",
        request_fields=("kind", "id", "record", "reason"),
        required_request_fields=("kind", "id", "record"),
    ),
    Operation(
        "delete",
        "catalog.write",
        mutating=True,
        route="/delete",
        method="POST",
        request_fields=("kind", "id", "reason"),
        required_request_fields=("kind", "id"),
    ),
    Operation(
        "annotate",
        "catalog.write",
        mutating=True,
        route="/annotate",
        method="POST",
        request_fields=("kind", "id", "record", "reason"),
        required_request_fields=("kind", "id", "record"),
    ),
    Operation("accept", "catalog.write", mutating=True, route="/accept", method="POST"),
    Operation(
        "leave-contested",
        "catalog.write",
        mutating=True,
        route="/leave-contested",
        method="POST",
        request_fields=("target", "source", "field", "reason"),
        required_request_fields=("target", "source"),
    ),
    Operation(
        "acknowledge",
        "catalog.write",
        mutating=True,
        route="/acknowledge",
        method="POST",
        request_fields=("target", "source", "until", "reason"),
        required_request_fields=("target", "source", "until"),
    ),
)

MANIFEST_HTTP_ROUTES: tuple[Operation, ...] = (
    Operation("manifest_brief", "catalog.read", route="/manifest/brief"),
    Operation("manifest_projects", "catalog.read", route="/manifest/projects"),
    Operation(
        "manifest_backlog",
        "catalog.read",
        route="/manifest/backlog",
        request_fields=("state", "initiative", "repo", "limit"),
    ),
    Operation(
        "manifest_next",
        "catalog.read",
        route="/manifest/next",
        request_fields=("limit",),
    ),
    Operation(
        "manifest_drift",
        "catalog.read",
        route="/manifest/drift",
        request_fields=("repo",),
    ),
    Operation(
        "manifest_repo",
        "catalog.read",
        route="/manifest/repo/{repo}",
        request_fields=("repo",),
        required_request_fields=("repo",),
    ),
    Operation(
        "manifest_why",
        "catalog.read",
        route="/manifest/why/{id}",
        request_fields=("id",),
        required_request_fields=("id",),
    ),
)


def operation_for_mcp(name: str) -> Operation:
    for operation in MCP_OPERATIONS:
        if operation.name == name:
            return operation
    raise KeyError(name)
