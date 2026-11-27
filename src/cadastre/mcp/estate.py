"""MCP tools for finding things by words: lookup, credentials, DNS, secrets.

Kept beside the stdio adapter rather than in it, like `drift`, so the adapter
stays a thin shim (DESIGN §3.4). Each tool is one call into the application
layer and one render; the logic lives in `cadastre.cli.search`,
`cadastre.cli.dns_chain` and `cadastre.cli.secret_describe`.
"""

from __future__ import annotations

from cadastre.mcp.sdk import (
    Action,
    EntityId,
    FromContext,
    Hostname,
    HostRef,
    Query,
    SecretRef,
    ServiceName,
    Target,
)


def lookup(
    entity_id: EntityId = None,
    kind: str | None = None,
    query: Query = None,
    limit: int | None = None,
) -> str:
    """Look up one entity by exact `entity_id`, or search by words with
    `query` (e.g. lookup(query="infisical api key")) and get ranked candidates.
    Give one of the two. Secret hits carry store, project id, environment,
    path, server and a ready agent-auth manifest line — never a value."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            f"/lookup/{entity_id}" if entity_id else "/search",
            query={
                "kind": kind,
                "query": None if entity_id else query,
                "limit": str(limit) if limit else None,
            },
            token=token,
        ),
        lambda: server._queries().lookup_or_search(
            entity_id, kind=kind, query=query, limit=limit
        ),
    )


def credential_for(service: ServiceName, action: Action = None) -> str:
    """Which secret(s) you need to act on a service, e.g.
    credential_for(service="forgejo", action="deploy"). Ranked candidates with
    store, project id, server and a ready agent-auth manifest line; never a
    value. Use it before logging in to a secret store by hand."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/credential-for",
            query={"service": service, "action": action},
            token=token,
        ),
        lambda: server._queries().credential_for(service, action),
    )


def access(host: HostRef) -> str:
    """How to connect to a host, from declared data: login user, credential
    (secret reference plus a ready agent-auth manifest line), path (network and
    address; tailnet, LAN or public), whether Tailscale SSH is on, and the
    matching skill. `host` is an id or an alias. A fact the catalog does not
    declare is named under Missing, never guessed. Never a secret value.
    Example: access(host="app-01")."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/access", query={"host": host}, token=token
        ),
        lambda: server._queries().access(host),
    )


def dns_chain(hostname: Hostname) -> str:
    """Check a hostname's chain: DNS record -> ingress edge -> node -> service,
    from collected evidence (no live probing). Flags e.g. a proxied hostname
    pointing at a workload node instead of the ingress edge that fronts it.
    Example: dns_chain(hostname="app.example.com")."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/dns-chain", query={"hostname": hostname}, token=token
        ),
        lambda: server._queries().dns_chain(hostname),
    )


def secret_describe(ref: SecretRef) -> str:
    """Describe a secret without its value: project slug and id, environment,
    path, server, value length, newline/CR counts, whether it parses as JSON,
    version, updated_at and known consumers. Accepts a reference, catalog id
    or bare key name, e.g. secret_describe(ref="HOMELAB_INFISICAL_API_KEY")."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint, "/secret-describe", query={"ref": ref}, token=token
        ),
        lambda: server._queries().secret_describe(ref),
    )


def reachable(from_context: FromContext, to: Target) -> str:
    """May a caller's network context reach host:port (a catalog host name
    or an IP)? Evaluates the Kubernetes NetworkPolicies declared in the
    GitOps (ops repo) source and returns allow or deny with the policy and
    rule that decided it. Nothing is probed; the tailnet ACL is not evaluated.
    Example: reachable(from_context="k8s:vogt-sessions", to="winrarhost:22")."""
    from cadastre.mcp import server

    return server._answer(
        lambda endpoint, token: server.client.request(
            endpoint,
            "/reachable",
            query={"from_context": from_context, "to": to},
            token=token,
        ),
        lambda: server._queries().reachable(from_context, to),
    )


ESTATE_TOOLS = (access, credential_for, dns_chain, secret_describe, reachable)
