# examples/

`catalog/` is an anonymised file-tree interchange fixture. A live installation
uses an initialized SQLite data directory instead.

`collector/` is the scheduled job that populates `observed.sqlite3` on the
collector host. `plugins.sample.yaml` and `plugins/` are the sample plugin
configuration it runs.

Everything here is fictional and uses `.invalid` names and `TEST-NET-3`
addresses. Nothing in this directory should ever be edited to match a real
estate; put that in your own catalog root and point Cadastre at it:

```
cadastre --catalog /path/to/your/catalog brief
```

`clients/` contains copyable configurations for Claude Code, Codex CLI, and
OpenCode. They launch the Cadastre-provided `cadastre-mcp-remote` bridge, which
speaks stdio locally and forwards only to the configured HTTPS `/mcp` endpoint.
An agent with native remote MCP support may connect directly and does not need
the bridge; stdio-only clients do. The Cadastre server stack is deployed
separately and must provide the persistent SQLite database, HTTP/API surface,
authenticated MCP endpoint, and—once released—the GUI.

Install the bridge from the released Cadastre package (or the checked-out
repository during development), then export the endpoint and protected token-file path from
`clients/cadastre-remote.env.sample` outside the repository. The Codex example
uses the supported `codex mcp add` command; Claude Code and OpenCode use their
native configuration forms. No client example uses an estate-specific wrapper
or places a token in a URL or command argument.

`renovate.json5` keeps a digest-pinned `compose.production.yaml` mechanically
updatable. Copy it into the repository that holds your deployment, not into
this one. It tracks the semver alias a release publishes and writes back the
signed digest, so what is deployed stays a digest. See `DEPLOYMENT.md` §7.1 for
the verification steps that belong around such an update.

## Why there are no comments in these YAML files

The files are in Cadastre's **canonical form**: field order from the model spec,
entities sorted by id, no comments. `cadastre fmt` produces it and
`cadastre fmt --check` verifies it. The M1 round-trip test loads this catalog,
re-serialises it, and asserts the bytes are unchanged — which only means
something if there is exactly one way to write a given catalog.

File-tree bundle fixtures are not required to be in canonical form. Comments
there are welcome; `cadastre fmt` is opt-in.
