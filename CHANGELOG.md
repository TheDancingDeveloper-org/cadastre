# Changelog

Every `vX.Y.Z` tag gets a section here and a GitHub release whose body is that
section. `releases.atom` on the GitHub remote is then a zero-maintenance feed
for operators, and Renovate attaches the entry to consumers' bump PRs. See
"Cutting a release" in [contributing.md](contributing.md).

The version recorded here is `application_version` in
`src/cadastre/release-compatibility.json`, which is attested to every released
image as the schema-compatibility predicate.

## v0.2.2

### Fixed

- **`lookup` resolves against observed evidence, not just `declared/`.**
  Cadastre returned `missing_entity` for infrastructure it had itself observed
  from a fresh collector run, and the message told the caller the catalog was
  wrong. The observation was retained the whole time, reachable only inside a
  `drift` dump large enough to exceed an MCP tool result limit. Resolution is
  now declared, then observed, then containment. An observed-only hit is
  labelled as one, carrying its source and `as_of` with `declared: false`, no
  `declared_at` and no relations — reachable, never promoted (DESIGN §1.3).
  The `missing_entity` message is unchanged for the case it was written for,
  and now also says that nothing observed the id either. ([#19])
- **A container inside a stack is reachable by the name a human uses.**
  `orchestrator-gitops` emits one entity per compose stack, which is the right
  altitude, but the constituent names survived only inside an attribute block
  nothing indexed — so `lookup loki` failed even though `grafanaloki` was
  known. Any `x-*` block listing mappings with a `name` is now indexed as
  member names. No plugin key is special-cased, no value is interpreted, and
  the answer is the containing entity marked as a containment hit, never a new
  entity. ([#19])
- **An unattributable host is stated rather than left empty.** A GitOps repo
  does not know its deployment target, so `runs_on` was empty for every
  observed service — which compares as agreement with a declared host rather
  than as a gap, and made "what runs on this host?" unanswerable from
  observation. `orchestrator-gitops` now records `host_attribution: unknown`
  in its `x-orchestrator` block and warns how many stacks it could not place,
  and `lookup <host>` reports both what collectors attributed to that host and
  how many observations could not be attributed at all. Guessing a host from a
  directory name stays refused. ([#19])

[#19]: https://github.com/TheDancingDeveloper-org/cadastre/issues/19

## v0.2.1

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

- `manifest brief` no longer carries the register in its payload. It was
  documented as the compressed session preamble, and its JSON projection
  embedded every ranked work item with no limit — 157,967 characters on a
  register of 444 open items, which exceeded MCP client result limits and
  failed the call outright. The command every agent is told to run first was
  the one most likely to fail, and it failed where the agent had the least
  context to recover. `brief` is now counts and confidence, which is what the
  documentation always described; `backlog` and `next` serve item lists and
  have taken a bounded `limit` all along.

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

### Fixed

- **The MCP surface no longer disagrees with its own schema.** The transport
  rejected an explicit `null` for every optional argument while the published
  schema advertised those arguments as nullable with a `null` default and the
  core accepted `None`, so a client that materialises defaults rather than
  omitting absent keys was refused for sending exactly what it was told to
  send. `check` also never published the four `kind` values it accepts, so the
  only way to discover the set was to send a wrong one and read the error;
  together the two meant a client following only the published schema could
  not call `check` at all. Argument types are now decided in one place that
  both the schema generator and the transport validator read, and the `kind`
  enum is generated from the parser registry, so a new parser cannot be added
  without the schema following it. The OpenAPI document for `POST /check`
  carries the same enum.
- **`/health/ready` can now detect stale observations.** `observed_freshness`
  counted sources and how many failed at their last recorded attempt, but never
  compared `as_of + ttl_seconds` against now, so a collector that stopped
  running entirely left readiness reporting `ok` indefinitely — observed on a
  live instance five days stale against a 24h TTL, with every query answered by
  that same instance correctly reporting eight of nine sources as stale. It now
  reports `stale_sources` and `oldest_as_of` and degrades lifecycle on
  staleness as it already did on failure, reusing the TTL evaluation the query
  path performs rather than repeating it. A stale catalog still serves reads
  while announcing that it is stale.

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
