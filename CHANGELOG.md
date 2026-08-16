# Changelog

Every `vX.Y.Z` tag gets a section here and a GitHub release whose body is that
section. `releases.atom` on the GitHub remote is then a zero-maintenance feed
for operators, and Renovate attaches the entry to consumers' bump PRs. See
"Cutting a release" in [contributing.md](contributing.md).

The version recorded here is `application_version` in
`src/cadastre/release-compatibility.json`, which is attested to every released
image as the schema-compatibility predicate.

## Unreleased

### Added

- **The Manifest module**, an opt-in work register, off unless a catalog's
  `modules.yaml` enables it. It adds seven entity kinds (`work_item`,
  `work_initiative`, `work_link`, `forge_item`, `markdown_finding`,
  `repo_checkout`, `revision_check`), the `cadastre manifest
  brief|projects|backlog|next|why|drift|repo` subcommand, `/manifest/*` HTTP routes,
  `manifest_*` MCP tools on stdio and Streamable HTTP, and capability-gated GUI
  navigation. Work items are catalog-authoritative and forge items are
  source-authoritative; `work_link` joins them and drift reports the
  disagreement without picking a side. Nothing in it can write to a forge.
  [MANIFEST.md](MANIFEST.md) is the design; it marks which parts of that design
  are shipped and which are not built yet.
- An **active entity registry** and `modules.yaml`, so a module contributes
  kinds, relations, schema, routes, and tools by configuration rather than by
  what happens to be installed. With no module enabled, the JSON Schema,
  OpenAPI document, CLI help, HTTP routes, MCP tool list, and `brief` output
  are byte-identical to a build without the module.
- A database that holds module-owned rows records that durably. An application
  with the module disabled still serves base reads but refuses catalog writes,
  export, and import, naming the fix, so dormant module data cannot be silently
  dropped.
- Three read-only work collectors, registered only while the module is enabled:
  `work-markdown` (bounded task-list findings from named files),
  `work-git` (checkout state read from Git's on-disk format, never by invoking
  `git`), and `work-github` (fully paginated issues and pull requests under a
  credential separate from `forge-github`'s).
- **The MCP write surface** — `add`, `update`, `annotate`, `accept`,
  `leave_contested`, `acknowledge` — mirroring the HTTP write routes through
  the same write gate. Off by default; listed only for an authenticated
  principal holding `catalog.write`. `delete` is deliberately not exposed.
  `principal` is never a tool argument: it comes from authentication.
- `cadastre load --from <catalog>` reloads a corrected `declared/` tree into a
  live catalog in one audited transaction, preserving annotations, with
  `--dry-run`. It is the missing half of `export`/`import` and the only in-band
  fix for a field the per-entity write gate refuses because a source owns it.
- `declared/policy/replication.yaml` describes intended secret replication
  paths, so `drift` can distinguish a contracted difference from two stores
  that were never meant to agree.
- A `version` MCP tool on both transports, reporting `application_version`,
  `catalog_format_version`, `observed_format_version`,
  `minimum_client_version`, and `release_url`. `/version` over HTTP reports the
  same fields; `name` and `version` are unchanged.
- `cadastre-mcp-remote` checks that version once at startup and writes a single
  line to stderr if it is older than the server's `minimum_client_version`.
  Version skew never fails startup, and a server without the tool is not an
  error.
- Releases publish `X.Y.Z`, `X.Y`, and `latest` as aliases of the immutable
  `sha-<commit>` tag, all resolving to the same signed digest, so registry
  watchers have something to track.
- `DEPLOYMENT.md` §7.1 documents the upgrade procedure: backup, pull digest,
  verify signature and attestations, compare attested formats against the
  database on disk, start, readiness, rollback.
- `examples/renovate.json5` keeps a digest-pinned `compose.production.yaml`
  mechanically updatable.
- The release pipeline publishes the already-smoke-tested wheel and sdist to
  PyPI, last, after every other gate — so `uv tool install cadastre` and
  `uv tool upgrade cadastre` describe a path that exists.

### Changed

- `orchestrator-gitops` dates its evidence by the checkout it read, not by the
  run that read it. `as_of` is now the resolved commit's committer date
  (falling back to when HEAD last moved locally), and `extra.checkout` records
  the commit, branch and which of the two the age came from. Nothing fetches
  the clone, so the previous run-time stamp reported an arbitrarily old tree as
  fresh, and a scheduled collection reset the TTL clock while the data stood
  still. The existing staleness rule now does the right thing unchanged: a
  checkout older than the source's TTL is stale. A directory that is not a
  readable checkout keeps the run time and says so in a warning.

- **`DEPLOYMENT.md` now documents how the containerised collector gets run.**
  The compose stack defines `cadastre-collector` as a profiled, run-to-completion
  job and contains nothing that would ever start it, while
  `examples/collector/` only solves scheduling for a host install — so a stack
  deployed as documented was configured to collect and never did, silently.
  Section 8 gives the invocation, cron/systemd/Kubernetes recipes, why one shot
  per run is what keeps the credential-minting entrypoint's tokens short-lived,
  and how to verify a first successful run. The upgrade checklist now asks
  whether anything still collects. Documentation only; no behaviour changed.
- `src/cadastre/__init__.py` is the single source of truth for the version.
  The Streamable HTTP `serverInfo`, the outbound MCP `clientInfo`, the OpenAPI
  document, both image labels, the release metadata document, and the GUI
  artifact name are now derived from it rather than restating it.
  `tests/test_version_identity.py` fails with a precise list of any file that
  disagrees.
- `scripts/release-gates.sh` fails a release whose `vX.Y.Z` tag does not match
  the declared version and the built wheel, before anything is pushed or
  signed.
- `release-compatibility.json` moved to `src/cadastre/` so it ships inside the
  wheel and the running server can answer from it.

### Removed

- The ungated publish and deploy jobs from `.github/workflows/publish.yml`,
  which pushed images on every commit to `main` with no tag gate, no
  vulnerability gate, and no signing. Deployment belongs in ops.
- `.woodpecker/`. Both files claimed authority — one over merges, one over
  releases — and neither had ever run: Woodpecker has no `cadastre` repository.
  The release gates (`scripts/release-gates.sh`, unchanged) now run from
  `.github/workflows/release-images.yml` on a `v*` tag, where CI actually
  executes, with keyless signing bound to the workflow identity. Any document
  still naming `.woodpecker/production.yaml` as the authoritative pipeline
  means that gate script.
