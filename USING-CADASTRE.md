# Using Cadastre

This guide covers local installation, catalog basics, and the command surface.
See [DEPLOYMENT.md](DEPLOYMENT.md) for a persistent or remotely accessible
installation, and [SECURITY.md](SECURITY.md) before exposing a listener.

## Install

Cadastre requires Python 3.11 or newer.

```bash
uv tool install cadastre
cadastre --version
```

`pipx install cadastre` is an equivalent option. MCP clients and servers use the
package extras documented in [AGENT-CLIENT.md](AGENT-CLIENT.md).

Upgrade with `uv tool upgrade cadastre`. If you use `cadastre-mcp-remote` as a
stdio bridge, it checks once at startup whether it is older than the server's
minimum supported client and writes a single line to stderr if it is. It never
refuses to start over version skew.

## Try the example catalog

The repository includes a fictional catalog under `examples/catalog`. Nothing
in it points to a real estate.

```bash
cadastre --catalog examples/catalog brief
cadastre --catalog examples/catalog context-for \
  "an internal worker that needs a GPU"
```

To check a proposed Compose file:

```bash
cadastre --catalog examples/catalog check compose.yaml --kind compose
```

## Start a live catalog

A live installation is an initialized data directory containing separate
SQLite databases for declared catalog data and observed evidence.

```bash
cadastre init --data-dir ./cadastre-data --empty
cadastre status --data-dir ./cadastre-data --json
cadastre brief --data-dir ./cadastre-data
```

Use `cadastre import` to load a bundle and `cadastre export` to create a
deterministic, reviewable bundle. File-tree catalogs such as the example remain
useful as fixtures and import sources; they are not silently converted to live
runtime stores.

## Commands

### Asking questions

| Command | Purpose |
|---|---|
| `brief` | Summarize the estate. Call it once at the start of a session. |
| `context-for <intent>` | Return relevant candidates, exclusions, conventions, and conflicts. |
| `check <artifact>` | Check a proposed Compose, ingress, pipeline, or grants file. |
| `lookup <id>` | Show one entity and its relationships. |
| `question <id>` | Answer one explicit operational migration question. |
| `drift` | Show where declarations and observations disagree. |
| `observations` | Show retained collector evidence that has no entity form. |
| `stale` | Show stale, unverified, and contested information. |

### Changing the map

| Command | Purpose |
|---|---|
| `add`, `update`, `delete` | Make a gated, audited catalog edit. |
| `annotate` | Add catalog-owned facts to an observed entity. |
| `accept`, `leave-contested` | Resolve a contest, or record that it stands. |
| `acknowledge` | Defer a contest until a stated date, with a reason. |

### Collecting and configuring

| Command | Purpose |
|---|---|
| `collect` | Run configured read-only collectors. |
| `sources` | List configured sources and their `plugin.info` handshake. |
| `plugins` | List registered plugins and their active state. |
| `fmt` | Canonicalize a file-tree catalog. |
| `schema` | Print JSON Schema, or OpenAPI with `--openapi`. |

### Running the catalog

| Command | Purpose |
|---|---|
| `init` | Initialize an empty SQLite catalog, or one from a bundle. |
| `status` | Show revision, format version, and whether the catalog is empty. |
| `integrity-check` | Check both SQLite databases. |
| `migrate` | Run approved forward migrations. |
| `backup`, `restore` | Take and restore a transaction-consistent copy. |
| `export`, `import` | Write and read a deterministic interchange bundle. |
| `load --from <catalog>` | Reload a corrected `declared/` tree into a live catalog. |
| `serve` | Start the HTTP API. |
| `mcp`, `mcp-http` | Start the MCP endpoint over stdio, or Streamable HTTP. |
| `security-check` | Check a network security profile without starting a listener. |

`manifest` is added by the optional module below.

Most commands accept `--json`. Exit code `0` means the command answered, `1`
means it found a requested policy or drift finding, and `2` means the invocation
or catalog was invalid.

`load` is the repair path when a `declared/` tree was wrong and a live catalog
has already accepted it — the missing half of `export`/`import`. It replaces the
declared catalog wholesale from the tree in one audited transaction stamped with
`--principal` and `--reason`. Two things follow from that. It is deliberately
not the per-entity write gate, so it can correct a field the gate would refuse
because a source owns it; and annotations survive, because they are catalog
edits with no representation in `declared/`. Run `--dry-run` first: it reports
what would be added, removed, and updated, and writes nothing.

## Collect observed evidence

Copy `examples/plugins.sample.yaml` to the configured plugin location and run
collection on the host that has the required read-only access:

```bash
cadastre collect --data-dir ./cadastre-data
```

The query server does not need collector credentials. Nothing schedules
collection for you: the systemd example under `examples/collector/` is the host
install, and section 8 of [DEPLOYMENT.md](DEPLOYMENT.md) is the same job for the
containerised stack.

Secret collectors return references and existence only. They never read or
return values. Without a replication contract, drift still shows pairwise store
differences as non-actionable inventory; `drift --exit-code` does not fail on
them. To describe an intentional replication path, add
`declared/policy/replication.yaml` with explicit source and target stores plus
optional glob `selectors` or exact source-to-target `mappings`:

```yaml
replication:
- source: secrets-manager
  target: ci-store
  selectors:
  - /prod/ci/*
- source: forge-secrets
  target: ci-store
  mappings:
    DEPLOY_TOKEN: FORGE_DEPLOY_TOKEN
```

Once contracts exist, only their store relationships and selected names are
compared. Contracted differences are actionable; unrelated store contents
remain outside replication drift. Store names must match collector output,
selectors use shell-style globs, and mappings contain reference names only.

## Optional modules

A **module** is different from a plugin: it adds entity kinds and query logic to
the core, not just an integration. Modules are off unless a catalog turns one on,
and activation is configuration — never the side effect of having installed a
package.

`manifest` is the only module today. It is a work register: catalog-owned work
items, initiatives, and links to forge issues and pull requests, joined against
collected Markdown task lists, Git checkout state, and GitHub issue/PR evidence.
It never writes to a forge.

Enable it with a `modules.yaml` beside the databases in the data directory, or
under `declared/` in a file-tree catalog — the same two locations
`plugins.yaml` uses:

```yaml
modules:
  manifest:
    enabled: true
```

That adds seven entity kinds (`work_item`, `work_initiative`, `work_link`,
`forge_item`, `markdown_finding`, `repo_checkout`, `revision_check`), the
`cadastre manifest` subcommand, `/manifest/*` HTTP routes, `manifest_*` MCP
tools, and Manifest navigation in the GUI. With the file absent or
`enabled: false`, none of that exists on any surface and the JSON Schema,
OpenAPI document, CLI help, and `brief` output are byte-identical to a build
without the module.

| Command | Purpose |
|---|---|
| `manifest brief` | The register, compressed. The module's session preamble. |
| `manifest projects` | One row per declared repository, including empty backlogs, checkout liveness, and unmatched checkout findings. |
| `manifest backlog` | Ranked work, filtered by `--state`, `--initiative`, `--repo`, bounded by `--limit`. |
| `manifest next` | The top eligible, unblocked items. |
| `manifest why <id>` | Every contribution to one item's score, and the arithmetic. |
| `manifest drift` | Where declared work and collected forge/Markdown evidence disagree. |
| `manifest repo <name>` | Work, checkouts, and drift for one repository. |

Work items are **catalog-authoritative**: priority, ordering, dependencies,
effort, and initiative membership are your intent, edited through the ordinary
`add`/`update`/`delete` gate. Forge issues and pull requests are
source-authoritative evidence and cannot be invented by a catalog write.
`work_link` joins the two rather than pretending they are one record, and drift
reports the disagreement without ever picking a side.

Once a catalog holds Manifest rows it records that durably. A build with the
module disabled can still serve base reads, but refuses catalog writes, export,
and import, naming `modules.yaml` as the fix — so a disabled module can never
silently drop your work data.

Ranking today uses declared inputs only (priority, initiative weight, age, and
how many open items an item blocks) and reports its confidence as
`declared-only`. [MANIFEST.md](MANIFEST.md) marks which parts of its design are
shipped and which are not yet built; read it before depending on a Manifest
answer.

Collector configuration for `work-markdown`, `work-git`, and `work-github` is in
[BUILTIN_PLUGINS.md](BUILTIN_PLUGINS.md). Those three plugins register only
while the module is enabled.

### Migrating a planning register

Cadastre does not scan, extract, reconcile, rewrite, or delete workspace
Markdown. The migration is an operator-owned review:

1. Declare every repository externally; the id must match `work_item.repo` and
   each configured `work-git` checkout's `repo`.
2. Extract and review source lines outside Cadastre. Accepted items include one
   or more `origin` records with workspace-relative `path`, positive `line`,
   lowercase SHA-256 `digest`, and non-empty extraction `run`.
3. Write through authenticated API/MCP `add`/`update`. `load` is only an
   optional reviewed bulk interchange path, not a migration command.
4. Read records through API/MCP `lookup`, or `cadastre export --output DIR`
   and `DIR/declared.json`. Compare the exact multiset of
   `(path, line, digest, run)` origin records with the external extraction; a
   dropped source record must fail reconciliation.
5. Delete source files only after reconciliation succeeds, then retire the
   `work-markdown` source so the old Markdown is no longer treated as current.

## Remote and AI-agent access

Use the ordinary HTTP API for scripts and the GUI. Use the `/mcp` Streamable
HTTP endpoint for MCP clients; `/brief` is not an MCP transport. Remote listeners
require an explicit secure deployment profile, authentication, and reviewed
network exposure.

Start every agent session with `brief`, then call `context_for` with the current
intent. Read provenance and trust state on every answer, report stale,
unverified, or contested data before relying on it, and honor the exclusions
returned by `context_for`.

Before committing a deployment artifact, call `check`. A rejected candidate or
write is a result to report, not an invitation to bypass policy.
