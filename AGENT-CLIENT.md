# Cadastre agent-client integration

Status: client contract, 2026-08-10. The agent and Cadastre server may run in
different environments. The agent consumes Cadastre through an operator-supplied,
authenticated remote MCP endpoint; no host, port, or network is a product
default.

## Two supported modes

### Native remote MCP client

If the agent product supports Streamable HTTP MCP, configure it with the
authenticated remote endpoint:

```text
https://<approved-host>:<approved-port>/mcp
```

The client performs TLS hostname/trust validation and uses its supported
credential mechanism. It does not need the Cadastre Python package or a local
catalog. The endpoint is not the ordinary HTTP API at `/brief`.

### Local stdio bridge

If the agent product only starts local MCP processes, install and configure
`cadastre-mcp-remote` on the agent host. The bridge speaks MCP over stdin/stdout
and forwards to the remote Streamable HTTP endpoint.

```text
agent MCP client ──stdio──> cadastre-mcp-remote ──TLS/HTTPS──> Cadastre /mcp
```

The bridge is a client, not a local Cadastre server. Set `CADASTRE_MCP_URL` to
the `/mcp` endpoint, `CADASTRE_REMOTE_ONLY=1`, and use a protected token file or
workload identity as configured by the deployment. `CADASTRE_HTTP_URL` names
the ordinary API base when an HTTP client also needs it; it is not a substitute
for the MCP endpoint.

## Write mode

Read operations are always available once a client is connected and scoped
`mcp`. Writes are a separate, off-by-default opt-in: the operator must start
the Streamable HTTP server with `--allow-write` (or, for a local stdio
server, set `CADASTRE_MCP_ALLOW_WRITE=1`), and the connecting principal must
hold the `catalog.write` scope. With either missing, `add`, `update`,
`annotate`, `accept`, `leave_contested`, and `acknowledge` are absent from
`tools/list` and refused if called anyway — an agent should not assume a
Cadastre endpoint supports writes without seeing them listed. `delete` is
never exposed over MCP; use the CLI or HTTP API for that.

Every write tool takes a `reason` argument (free text — record *why*, since
`principal` is stamped for you from your authentication and is never a tool
argument you can set). A refusal on a source-authoritative kind (e.g.
`add`-ing a `host` a collector already owns) returns a structured error
naming the `cadastre collect --source <plugin>` path instead — that is an
answer, not a permission gap to route around.

The remote bridge does not add a write surface of its own: it lists exactly
what the remote server's `tools/list` returns for the connecting token, so
write tools appear through the bridge only when the remote is configured for
them and the bridge's token is scoped for them.

## Module tools

A deployment may enable an optional module, which adds its own read tools to
the same endpoint under its own prefix. `manifest` — the work register — adds
`manifest_brief`, `manifest_backlog`, `manifest_next`, `manifest_why`,
`manifest_drift`, `manifest_repo`, and `manifest_projects`.

Discover them; do not assume them. They are absent from `tools/list` for a
catalog that has not enabled the module, exactly as write tools are absent
without `--allow-write`, and both are absent or present through the bridge for
the same reason. An agent working in a repository that Cadastre tracks can call
`manifest_next` for what to pick up and `manifest_why` for the arithmetic behind
that ranking; a `manifest_drift` row is a disagreement between a declaration and
a forge, reported for a human to resolve, not a task to reconcile.

## Security requirements

- never put token values in URLs, argv, client JSON, catalog data, or logs;
- fail closed when endpoint, TLS, identity, or authorization configuration is
  missing or invalid;
- never fall back to a local catalog in remote-only mode;
- grant only the required MCP scope and operations; and
- record endpoint identity, result, provenance, and command hashes—not secrets—
  in migration evidence.

The bridge package is installed in the agent environment. The Cadastre server
stack separately runs SQLite, HTTP/API, MCP, and the GUI. Cadastre does not
install or control the agent product's native MCP client.

See [DEPLOYMENT.md](DEPLOYMENT.md), [SECURITY.md](SECURITY.md), and the tested
examples in [`examples/clients/`](examples/clients/).
