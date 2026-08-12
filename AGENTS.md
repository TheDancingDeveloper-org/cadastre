# Cadastre agent and contributor guide

Cadastre records infrastructure facts and policy. It does not deploy workloads,
mutate DNS/VPN/orchestrators, execute commands on hosts, or return secret
values. The HTTP API, MCP server, and GUI are application interfaces over the
same SQLite-backed catalog.

## Using Cadastre

Use the networked MCP endpoint when one is supplied by the deployment. For
clients that need stdio, install the published package extra and run the
remote-only bridge. The ordinary HTTP API is useful for scripts and GUI
integrations; `/mcp` is the MCP transport and must not be substituted with
`/brief`.

```text
MCP:  ${CADASTRE_MCP_URL}   (Streamable HTTP, normally ends in /mcp)
API:  ${CADASTRE_HTTP_URL}  (ordinary HTTP/API base, normally serves /brief)
```

The endpoint, TLS identity, and authentication method are deployment inputs.
Never guess a hostname, port, network, or secret path. Tokens belong in a
protected environment variable or file and never in URLs, argv, client JSON,
catalog data, logs, or agent-facing output.

At the start of a session:

```text
MCP:  call brief, then context_for(intent)
API:  GET  $CADASTRE_HTTP_URL/brief
      POST $CADASTRE_HTTP_URL/context-for  {"intent":"..."}
```

Before committing a deployment artifact, run `check` through MCP or the API:

```text
MCP:  check(artifact, kind)
API:  POST $CADASTRE_HTTP_URL/check
```

Read provenance and trust state on every answer. `stale`, `unverified`, and
`contested` data must be reported before it influences a decision. Read the
exclusions returned by `context_for`; do not route around a rejected candidate.

If the catalog is wrong, use the gated write operation (`add`, `update`,
`delete`, or `annotate`) through the supported client/API. A catalog write
changes the map only; it never changes the estate. Source-authoritative
entities and observed evidence may be refused by design. A refusal is an
answer, not an invitation to bypass the gate.

## Client setup

The published package includes `cadastre-mcp-remote`:

```bash
python -m pip install 'cadastre[mcp-client]'
export CADASTRE_MCP_URL='https://host.example/mcp'
export CADASTRE_HTTP_TOKEN_FILE='/run/secrets/cadastre-http-token'
export CADASTRE_REMOTE_ONLY=1
cadastre-mcp-remote
```

Native Streamable HTTP clients may connect directly to `CADASTRE_MCP_URL`.
Claude Code, Codex, and OpenCode examples are maintained in
`examples/clients/`; they reference protected environment variables/files and
never embed credentials.

## Contributing

Read `DESIGN.md` before changing the model, storage, Broker boundary,
authorization, provenance, or plugin contract. The core must remain vendor
neutral, deterministic, default-deny, and free of estate-control side effects.
Adapters translate and render; business logic belongs in the application/core
layer. Do not edit generated observed data by hand or add secret values to
fixtures.

Run the local gates before submitting changes:

```bash
uv sync --extra dev --extra mcp-server --extra manifest
ruff check src tests
ruff format --check src tests
mypy
pytest -q
cadastre --catalog examples/catalog fmt --check
cadastre --catalog examples/catalog check compose.production.yaml --kind compose
```

`mypy` is run bare on purpose: its `files` is `["src", "tests"]`, and
`mypy src` alone lets type errors in tests reach a green build.

Optional modules (currently only `manifest`, see `MANIFEST.md`) are activated by
a catalog's `modules.yaml`, never by an installed extra. If you touch the model,
schema, routes, CLI help, or MCP tool list, the default-off contract is a
byte comparison: with no module enabled those surfaces must be identical to a
build without the module, and `schema/catalog.schema.json` is the base
registry's rendering. Behaviour a document promises and the code does not do
belongs in a GitHub issue — search the tracker before implementing something
that may already be a known gap.

The release pipeline also builds the Python package, backend OCI image, GUI
artifact/image, SBOMs, scan reports, signatures, attestations, and immutable
digest metadata. Those release artifacts are the normal user path; self-build
instructions are documented in `README.md` and `DEPLOYMENT.md` for operators
who need a local or private build.

### Bootstrap exception

An operator may use a read-only identity check and reviewed immutable artifact
over protected SSH solely to establish the first Cadastre endpoint. Credentials
stay in the protected access mechanism, and the exception expires once the
authenticated MCP/API path is verified. It does not authorize general estate
access or workload migration.
