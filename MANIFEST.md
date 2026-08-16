# Manifest: an opt-in work-tracking module

Status: **F01–F03 and R01–R09 have shipped**, verified against the tree as of
2026-08-11. O01–O03, D01–D04, and P01 have not been started. Four of the
shipped tasks landed narrower than their own "Done when" clauses accept; those
reductions, and everything below R0, are marked on the tasks themselves in
§8. Read the **Status** lines there before relying on any behaviour promised
here.

Read §1–§10 as the design argument and the plan they were written as, not as a
description of the running code. Where the two disagree, the code is current
and this document is the intent. §5.3 in particular
describes a nine-input score and a versioned policy fragment; five inputs are
implemented and the weights are constants.

§1–§6 describe why the module belongs inside this repository rather than in a
second application, and the three existing mechanisms that let it fit without
amending the non-goals in [DESIGN.md](DESIGN.md) §1.3. §7 records what the tree
contained before implementation, including five findings that
changed the work — one of which (§7.2.1, the divergence identity) was never
decided and is why Manifest drift still does not reach the trust ledger. §8 is
the task list, §9 the release gates, §10 the delivery sequence.

What an operator gets today: `modules.yaml` activation, seven Manifest entity
kinds, catalog-authoritative work items/initiatives/links, three read-only work
collectors (`work-markdown`, `work-git`, `work-github`), a cross-kind drift
join over the seven categories in §3.2, deterministic declared-only ranking,
and `brief`/`backlog`/`next`/`why`/`drift`/`repo` over CLI, HTTP, MCP, and the
GUI. What is absent: the deployment join, the sync plan, revision checks,
Projects v2, and durable divergence history.

> **This document designs for a narrower problem than the one that prompted
> it.** §1 states the need — many working folders and repositories, varying
> state, intent scattered through incoherent Markdown — and then specifies a
> *declaration-first* register whose collectors exist to check declarations
> against a forge. Collected findings are not work: they cannot be ranked,
> cannot appear in `brief`, `backlog`, or `next`, and reach a query only as
> drift. A workspace where nothing has been declared yet therefore reads as
> empty, and a planning file that does not use `- [ ]` checkbox syntax collects
> as nothing at all. Neither is a bug against this specification; both are the
> specification. Closing that gap does not require amending the §1.3
> non-goals, but the surfaces that would close it are not built.

A documented proposal is not a completed feature, and a plan that has not been
checked against the code is a second proposal wearing a schedule. A plan that
shipped and was never marked as shipped is the same failure in the other
direction.

## 1. The problem

Cadastre answers *where does this belong, and may it run here*. It has no answer
for *what is outstanding, what matters most, and did it ship*. That second
question is currently answered by drifted Markdown scattered across a workspace.

Measured on the authoring estate, 2026-08-10:

| Signal | Count |
|---|---|
| Local git repositories under the workspace root | 112 |
| Project directories under the workspace's application root | 130 |
| Repositories in the GitHub organisation | 48 |
| Open issues, organisation-wide | 85 |
| Open pull requests, organisation-wide | 41 |
| GitHub Projects v2 boards | 2, barely used |
| Markdown files under the workspace root, depth 4 | 944 |
| `TODO` / `PLAN` / `STATUS` / `BACKLOG` / `nextsteps` files | dozens |

Those files drift because nothing reconciles them against real issue, pull
request, or CI state. A file asserting twelve open items, last touched forty-one
days ago, four of whose items reference closed issues, is indistinguishable from
an accurate one until a human reads both it and the forge.

This is the same class of problem Cadastre already solves for infrastructure:
scattered knowledge, no provenance, no way to tell current from stale. The
machinery for solving it — content-addressed evidence, per-source freshness,
contested state, structured refusals — is already built here.

## 2. Why a module and not a second application

The decisive argument is the deployment join. The question worth answering is
*is this finished work actually running anywhere*, which means correlating:

```
work item → issue / pull request → merge commit → image ref → GitOps compose
                                                        → orchestrator stack
                                                        → server / host
                                                        → ingress route
                                                        → DNS record
```

The host, ingress, DNS, and network portions are already collected by shipped
Cadastre plugins: `ingress-caddy`, `dns-cloudflare`, `hypervisor-proxmox`, and
`vpn-tailscale`. The repository does **not** yet retain enough forge,
artifact, GitOps, or orchestrator identity to prove the earlier hops. Those are
real prerequisites in the task plan below, not joins the current catalog can
already perform. Once collected, the useful property remains: the answer is one
query over one provenance and trust model. Split across two applications it
becomes a federated call between two databases with independent freshness,
independent authorisation, and independent failure.

That is not hypothetical. At the time of writing, this catalog's observed
sources were last collected three days prior and all were marked stale, and the
catalog knew five repositories against the organisation's forty-eight. A second
application consuming this one would inherit both facts as structural
properties rather than as a scheduling bug to fix.

The secondary argument is duplication. A separate application would carry its
own storage layer, provenance model, freshness model, plugin protocol, CLI,
MCP server, authentication, backup story, and release pipeline — and would
collect from the same forge with a second credential.

## 3. How it fits without amending the non-goals

[DESIGN.md](DESIGN.md) §1.3 forbids estate mutation, reconciliation, automatic
resolution, and estate deployment. A work tracker appears to want at least two
of those. It does not, because of three mechanisms that already exist.

### 3.1 Authority is per entity type

A plugin declares each entity type as `authority: source | catalog`
([DESIGN.md](DESIGN.md) §4.2). Infrastructure entities are source-authoritative:
the upstream API is right by definition, and the catalog mirrors it. That is why
§2.4 refuses `add` on a Tailscale node.

A work item inverts the polarity. **The catalog is authoritative for the work
register.** Priority, ordering, dependency edges, effort, and initiative
membership are intent; they are not facts about a forge that the catalog is
mirroring badly. A forge remains authoritative for its own issue and pull
request state. `work_link` explicitly joins the two instead of pretending they
have one identity. This is the position `static` already occupies for declared
entities, but the cross-kind join is new module logic.

Nothing here asks Cadastre to converge reality toward declaration. It asks it to
hold a declaration whose subject happens to be work rather than a host.

### 3.2 Divergence is already the behaviour

Per §4.3, catalog CRUD and collection write to different stores and are
*compared*, never merged:

| Situation | Resulting state |
|---|---|
| Declared work item with no forge link | valid, declared-only |
| Required link absent from fresh, complete forge coverage | `link-target-missing` |
| Forge issue or pull request never linked | `unlinked-forge-item` |
| Linked records disagree on a selected reflected field | contested (§2.9) |

The separate declared/observed storage and durable trust behavior behind that
table already exist. The matching behavior does not: current drift compares
declared and observed records of the same entity kind, while Manifest must join
a declared `work_item` through a `work_link` to forge and Markdown evidence of
different shapes. A Markdown finding is collected evidence; it is not silently
promoted into a work item. Manifest adds that cross-kind drift query and still
never picks a side, which is exactly the correct behavior for a stale
`BACKLOG.md`.

### 3.3 The upstream write stays outside, as a Broker

The one operation that does not fit is pushing a work item into a forge as a
real issue. That is writing the territory, and no plugin may do it (§1.3).

Cadastre already reserves the seam: the **Broker** is external and not shipped
as the Cadastre app ([ARCHITECTURE.md](ARCHITECTURE.md) §2). `manifest-sync`
would be a separately packaged and deployed worker that reads an immutable sync
plan and applies it to the forge under its own credential. Cadastre does not
invoke it. No forge write token enters the Cadastre server or collector
environment, so the security property in [DESIGN.md](DESIGN.md) §6 — compromise
yields topology disclosure, not mutation — is preserved unchanged.

The module is therefore useful with the Broker absent. Without it, Manifest is a
register that reports divergence and stops, which is the house style.

## 4. What it is not

- **Not a project-management product.** No sprints, burndown, time tracking,
  velocity, or workflow states beyond what a forge already models.
- **Not a reconciler.** It never converges a forge toward the catalog. The
  optional Broker applies explicit, audited, allow-listed changes.
- **Not a writer of Markdown.** Files under the workspace are collected evidence
  and are never rewritten, moved, or deleted by collection.
- **Not a ranking oracle.** The score is arithmetic over declared inputs and is
  fully printable. No model participates in ranking.
- **Not mandatory.** With the module disabled, Cadastre behaves exactly as it
  does today. Disabled output, schemas, routes, and MCP tools are regression
  contracts, not intentions.

## 5. Shape

Manifest is shipped in the Cadastre source distribution but is disabled by
default. The `manifest` extra installs collector-only dependencies; it does not
activate the module. Python extras cannot conditionally remove files from the
same wheel, so installation and activation are deliberately separate:

```toml
[project.optional-dependencies]
manifest = [...]      # collector dependencies, absent by default
```

```yaml
# modules.yaml, beside plugins.yaml in the runtime data directory
modules:
  manifest:
    enabled: true
```

Activation is resolved from the selected runtime directory before the full CLI
parser or server is built, then frozen in `ApplicationContext` and passed to the
model, schema, storage, application, and adapter layers. Code must not inspect
whether an optional package happens to import successfully. With no
configuration, or with `enabled: false`, the active entity registry, JSON
Schema, OpenAPI document, CLI help, HTTP routes, MCP tool list, `brief` output,
and backup/export shape remain byte-for-byte compatible with base Cadastre.

```text
src/cadastre/
  modules/                      module discovery and active registry
  manifest/                     model, projections, ranking, drift, deploy join
  plugins/collectors/
    work_markdown.py            TODO/PLAN/STATUS/BACKLOG findings
    work_git.py                 local checkout state; never fetches
    work_github.py              issues, pull requests, Projects v2, revision checks
    orchestrator_komodo.py      read-only stack and server state
  cli/  api/  mcp/              conditional adapters over one query service
```

Storage reuses the existing split; there is no third database. Declared work
lives in `catalog.sqlite3`, collected work evidence in `observed.sqlite3`.
The generic SQLite entity tables do not require a work-specific table. Export,
import, backup, restore, audit, and revision handling must use the active entity
registry rather than a second persistence path.

### 5.1 Entity types

The register MVP needs both declaration and evidence kinds. Without the source
kinds, a forge issue would either be forced into a catalog-owned `work_item` or
left as uninterpreted `extra`, and neither can support a trustworthy join.

| Entity | Authority | Stable identity | Required core fields |
|---|---|---|---|
| `work_initiative` | catalog | assigned `id` | `title`, integer `weight` |
| `work_item` | catalog | assigned `id`, stable across retitling | `title`, `state`, `priority`, `created_at`; optional `initiative`, `order`, `effort`, `repo`, `blocked_by`, `origin` |
| `work_link` | catalog | assigned `id`; unique target key | `work_item`, `forge`, `repo`, `kind`, `ref`, `completion`, `required`, `reflect` |
| `forge_item` | source | `(forge, repo, kind, ref)` rendered into `id` | `title`, `state`, timestamps; optional `draft`, `head_revision`, `merge_revision`, `url` |
| `markdown_finding` | source | explicit marker, else `(repo, path, normalized-text digest)` | `repo`, `path`, `line`, `text`, `checked`; optional `work_item`, `forge_ref`, `heading` |
| `repo_checkout` | source | configured checkout id | `repo`, `head_revision`, `branch`, `dirty`; optional `upstream`, `tracking_ref_matches`, `last_head_change`, `worktree` |
| `revision_check` | source | `(system, repo, revision, check-id)` | `state`, `started_at`; optional `completed_at`, `url` |

`work_item.state` is the deliberately small `open | done | cancelled` set.
`priority` is `p0 | p1 | p2 | p3 | p4`; `effort` is a non-negative integer
point estimate with no implied time unit. `blocked_by` contains other work item
ids and must be acyclic. `work_link.kind` is `issue | pull_request`, and
`completion` is `closed | merged`. `reflect` is an explicit subset of
`title | completion`; fields not named there are allowed to differ without
creating drift. Two links may not claim the same `(forge, repo, kind, ref)`.

Source ids are constructed by collectors from the stable tuple and are not
accepted from upstream display text. Revision values are full object ids where
the upstream provides them; a short hash is retained as evidence but is never
accepted as proof of identity.

The deployment phase adds three more kinds only after its identity contract is
approved:

| Entity | Authority | Stable identity | Purpose |
|---|---|---|---|
| `artifact_build` | source | `(repository, revision, OCI digest)` | Proves which immutable artifact a revision produced |
| `deployment_spec` | source | `(source repo, path, unit)` | Records the image and orchestrator target requested by GitOps |
| `runtime_deployment` | source | `(orchestrator instance, upstream stack id)` | Records actual stack, target host, revision, units, image digests, state, and deployment event time |

The previous `(server, stack_name)` proposal is rejected: both placement and a
display name can change. For Komodo, the emitted Cadastre id is derived from the
configured orchestrator instance and Komodo's immutable stack resource id. The
stack name and server are reflected fields. Vendor ids may also survive in an
`x-komodo.*` block, but deployment logic reads only the neutral core fields.

### 5.2 Collectors

The plugin protocol gains a `Work` capability with `work.items`,
`work.findings`, `work.repo-state`, and `work.revision-checks` methods, plus a
`Deployment` capability with `deployment.specs` and `deployment.runtime`.
Each method has an explicit entity-kind mapping so the runner can reject a
collector that emits an unrelated kind.

| Method | Allowed entity kinds |
|---|---|
| `work.items` | `forge_item` |
| `work.findings` | `markdown_finding` |
| `work.repo-state` | `repo_checkout` |
| `work.revision-checks` | `revision_check` |
| `deployment.specs` | `deployment_spec` |
| `deployment.runtime` | `runtime_deployment` |

`work-github` collects issues and pull requests because shipped
`forge-github` does not. It does **not** duplicate that plugin's optional CI job
history, which answers executor-routing questions. `work.revision-checks`
instead retains the aggregate check result for an explicitly selected commit.
Issue/PR, Project v2, and revision-check collection use separate configured
methods so they may have different credentials, scopes, TTLs, and failure
states.

All enumeration is bounded and complete-or-stale. A collector that reaches its
page, repository, file, byte, or API-call ceiling fails the method and retains
the prior evidence; it never publishes a partial set from which absence could
be inferred.

`work-markdown` scans only explicitly configured repository roots. The MVP
parses GitHub-style task-list items from explicitly named planning files; it
does not treat every occurrence of `TODO` as work. It does not follow symlinks
outside a configured root, read files above the configured size and total-byte
budgets, execute links or code blocks, or retain file content beyond the
allow-listed finding fields. Observed text is rendered inertly.

`work-git` reads only explicitly configured local checkouts. It does not run
hooks, fetch, push, change refs, or contact a remote. It emits
`tracking_ref_matches` when a local remote-tracking ref is readable; this is an
exact revision comparison, not proof of ahead or unpushed work. It emits
`last_head_change` only when a valid `.git/logs/HEAD` timestamp is established.
Dirty state includes tracked changes and untracked paths but retains neither
file bodies nor diffs.

`orchestrator-komodo` is an ordinary read-only plugin using only `/read/`
operations — initially `ListStacks`, `GetStack`, `ListServers`, and bounded
`ListUpdates`. Komodo uses POST requests for typed read operations, so safety is
enforced by an operation-name allowlist rather than by pretending HTTP POST is
a write. No `/write/`, `/execute/`, or terminal operation can be constructed.
The current upstream API definitions are the implementation reference for
[`ListStacks`/`GetStack`](https://github.com/moghtech/komodo/blob/93cce3fb60ee59673f92b720a24e8ab0d71eff10/client/core/rs/src/api/read/stack.rs),
[`ListServers`](https://github.com/moghtech/komodo/blob/93cce3fb60ee59673f92b720a24e8ab0d71eff10/client/core/rs/src/api/read/server.rs),
and [`ListUpdates`](https://github.com/moghtech/komodo/blob/93cce3fb60ee59673f92b720a24e8ab0d71eff10/client/core/rs/src/api/read/update.rs).
This plugin remains independently useful when Manifest is disabled.

### 5.3 Ranking

Only open, unblocked work is eligible for `manifest next`. A work item is
blocked when any item in `blocked_by` is still open. The default score is:

```text
priority[p0..p4]        = 1000, 500, 250, 100, 0
initiative              = declared initiative.weight, else 0
age                     = min(full UTC days since created_at, 90)
blocking                = 50 * min(open direct dependants, 10)
failed revision check   = 150 when a fresh required check is failed, else 0
open pull-request age   = 2 * min(full UTC days, 30)
proven deployment gap   = 200 when the deployment phase can prove one, else 0
effort                   = effort * 0 by default

score = sum(the contributions above)
```

The weights and caps are catalog policy and may be changed through the audited
write path. Defaults are versioned and printed. Ties sort by declared `order`
(missing last), then work item id. The clock is injected.

Stale, incomplete, or missing evidence contributes **nothing** to an observed
signal and marks ranking confidence `degraded`; it is not silently treated as a
failed build or a deployment gap. Declared-only items remain rankable with
confidence `declared-only`. `manifest why ITEM` prints eligibility, source
freshness, every raw input, policy weight, cap, contribution, tie-break value,
and final sum. Recomputing those printed components must reproduce the result.

## 6. Cost, stated plainly

- Manifest is a **module, not a plugin**. It adds core logic, so the honesty
  test in §4.1 — a plugin needing a core change means the contract is wrong —
  does not apply to it. That test protects the plugin contract, and Manifest is
  not claiming plugin status. The cost is real regardless: core grows.
- Optional dependencies do not provide feature isolation. The active registry
  and explicit module configuration do; implementing that registry is the
  largest prerequisite and benefits any future module.
- The repository is mid-release-hardening. Default-disabled activation keeps
  the existing runtime surface unchanged, but review and test load increase.
- The name collides with container-orchestrator manifests. Chosen deliberately;
  the CLI is always `cadastre manifest ...`, HTTP routes are under `/manifest/`,
  and MCP tools use a `manifest_` prefix.
- The register is useful before the deployment join. `deployed`, `lag`, and
  `blast-radius` are not useful until commit and immutable-artifact identity are
  available end to end; those commands must report `unknown`, never guess.
- Collection is worth little until scheduled and repository coverage is widened
  past the current five. That is existing operational debt this proposal
  surfaces rather than creates.

## 7. Verified current state

§1–§6 were written as an argument. This section records what the tree contained
before implementation, so the tasks below name real functions rather than
intentions. Line numbers are from that point in the tree.

### 7.1 What the argument depends on, and whether it holds

| Claim | Holds | Evidence |
|---|---|---|
| Authority is declared per entity type | yes | `EntityDeclaration.authority`, `src/cadastre/plugins/contract.py:28`, validated against `AUTHORITIES` at `:196` |
| Declared and observed are separate stores, compared and never merged | yes | catalog tables at `src/cadastre/core/storage.py:141`, observed schema at `:715`, `drift.compare` at `src/cadastre/core/drift.py:107` |
| Divergence is durable, with first-seen and flapping | yes | `TrustRecord`, `src/cadastre/core/trust.py:29`; ledger at `:22` and `trust_records` at `storage.py:727` |
| The generic entity tables need no work-specific table | yes | `entities(kind, id, payload)`, `storage.py:141` |
| Coverage narrows but never broadens a plugin contract | yes | `_scope_covers`, `drift.py:184` |
| `forge-github` does not already collect issues or pull requests | yes | its methods are `vcs.repos`, `ci.pipelines`, `ci.status`, `src/cadastre/plugins/collectors/forge_github.py:953` |
| The Broker seam is reserved and unshipped | yes | `WRITE_METHODS` is refused by the runner, `src/cadastre/plugins/protocol.py:69` |
| Drift already joins across kinds | **no** | `compare` loops one kind at a time (`drift.py:116`) and matches on id within that kind. §3.2 is right that this is new logic |
| An extra alone could isolate the feature | **no** | confirmed by construction: nothing in the tree conditions the model on installed packages, and F02 exists because of it |

### 7.2 Five findings that change the task list

**7.2.1 The trust ledger key cannot tell Manifest's categories apart.**
A divergence is keyed for the ledger as `(kind, id, field, source)`
(`trust.py:71`) — `category` is deliberately absent. Base Cadastre is
collision-free by construction: `undeclared` and `missing` are mutually
exclusive for one `(kind, id, source)`, and `differs` always carries a `field`
while the other two never do. Manifest's seven categories break that.
`link-target-missing` and `evidence-incomplete` can both anchor on one
`work_item`, from one source, with no field — and would then share a ledger
row, silently overwriting each other's `first_seen`, `observations`, and
`flapping`. R06's "keyed by the Manifest divergence identity" is therefore a
persisted-format decision, not a sentence: either `category` joins the ledger
key — bumping `LEDGER_VERSION` (`trust.py:22`), the `trust_records` table
(`storage.py:727`), and adding a forward migration — or Manifest synthesises a
composite `id` and documents why. Decide it in F01. The option that looks
cheaper is the one that corrupts divergence history.

**7.2.2 `modules.yaml` has two legal locations and must resolve before the
parser exists.** `load_plugins` resolves `<root>/plugins.yaml` first and falls
back to `<root>/declared/plugins.yaml` (`src/cadastre/plugins/config.py:51`) —
runtime beside the databases, `declared/` retained for interchange fixtures.
`modules.yaml` must mirror that pair exactly, or a bundle that round-trips
plugin configuration silently loses module configuration. It cannot, however,
load where plugin configuration loads: `load_plugins` is called inside
`Session.open` (`src/cadastre/cli/session.py:56`, `:79`), while F02 needs the
active registry before `build_parser` (`src/cadastre/cli/main.py:35`)
constructs subparsers — which is before `main` has parsed `--catalog` or
`--data-dir` at all (`main.py:598`). F02's "two-pass startup" therefore means a
standalone resolver reading `argv` and the two environment variables
(`main.py:31`) ahead of `argparse`, not a new field on `Session`.

**7.2.3 Five zero-argument entry points hold the model globally.** Seventeen
modules reference `model.KINDS`, `ENTITY_SPECS`, `ENTITY_CLASSES`, `KIND_DIRS`,
or `RELATIONS` — two declare them, fifteen read them. The count understates it;
the shape is the problem. `catalog_schema()` (`src/cadastre/core/schema.py:71`),
`render_schema()` (`:104`), and `build_parser()` (`main.py:35`) take no
arguments, and
`MCP_OPERATIONS` (`src/cadastre/api/registry.py:56`) and `HTTP_ROUTES` (`:106`)
are module-level tuples evaluated at import. Every one is a seam an adapter
already calls directly (`src/cadastre/adapters/http.py:51`, `:355`;
`main.py:350`). F02 is a signature change across all five and their callers.
It is the largest single piece of work in this plan, it contains no Manifest
content whatsoever, and that is exactly why it must land alone.

**7.2.4 The default-off contract already has an enforcer, and it is a byte
comparison.** `tests/test_model.py:90` asserts the checked-in
`schema/catalog.schema.json` equals `render_schema()`, and CI repeats it as a
`diff -u` in `.github/workflows/ci.yaml:55` and
`.github/workflows/publish.yml:36`. §9's "schema unchanged while disabled" is
not a gate to build; it is a gate that must keep passing untouched. The
converse binds too: the committed file is the *base* registry's rendering and
must stay so. An enabled catalog's schema is a runtime document, never the
committed artifact. If enabling Manifest changes `schema/catalog.schema.json`,
F02 is wrong.

**7.2.5 There is no network or subprocess guard to reuse.** `tests/conftest.py`
states "No test touches a live service" in its module docstring; nothing
enforces it. R03 and R04 both require a guard that fails on socket or
subprocess use, and `pyproject.toml` sets `fail_under = 80`. Build the guard
once, as a `conftest.py` fixture, in R02 — not twice inside two collector
tasks.

### 7.3 One naming collision the proposal missed

§6 flags the collision with container-orchestrator manifests. There is a second
one, inside this repository: `export_bundle` and `import_bundle` write and read
a bundle `manifest.json` (`storage.py:918`, `:929`), and `backup` writes
another (`:1158`). F03 adds required-module declarations to exactly those
documents. Prose in F03 and in [DEPLOYMENT.md](DEPLOYMENT.md) must say "bundle
manifest" or "the Manifest module" and never a bare "manifest".

## 8. Implementation tasks

**Status of this section.** F01–F03 and R01–R09 have landed; O01–O03, D01–D04,
and P01 have not. The per-task status, including the five shipped tasks whose
"Done when" clauses are not fully met, is on the tasks themselves.
Individual tasks below carry a **Status** line only where the shipped result
differs from what the task specifies; an unmarked R-task shipped as written.

Each task below is intended to be one reviewable pull request unless its
acceptance criteria force a smaller split. The register milestone is **R0**;
the Broker, Projects v2, and deployment join do not block it. Every task
carries a **Touches** block naming the files it changes; those are the review
surface, and a pull request straying outside one is a signal the task was
mis-scoped rather than a note to add later.

### 8.1 Foundation

#### F01 — approve the module and compatibility contract

**Depends on:** nothing.

- Add a short architecture decision recording explicit activation, the active
  entity registry, inactive-module database behavior, and why an extra alone is
  insufficient.
- Define `modules.yaml` schema. Unknown modules and non-boolean `enabled`
  values are located configuration errors; a missing file means no modules.
- Decide the optional Git reader dependency using a fixture spike. It must read
  ordinary repositories and linked worktrees without running `git`, hooks, or
  network operations. Record the chosen pinned dependency in `manifest`.
- Record two later, non-R0 decisions as unresolved gates rather than guessing:
  which system supplies signed `artifact_build` evidence, and which repository
  owns the separately released `manifest-sync` worker.
- Decide the divergence-identity question from §7.2.1 now, because it is a
  persisted-format choice: either `category` joins the trust ledger key, or
  Manifest synthesises a composite id. Record which, and why the other was
  rejected.

**Touches:** this document; `DESIGN.md` §4 for the module concept;
`src/cadastre/modules/config.py` (new) for the `modules.yaml` schema;
`pyproject.toml` for the `manifest` extra and the pinned Git reader.
No behaviour changes; no existing file's output moves.

**Done when:** the decision is reviewed, the configuration schema is fixed,
the divergence-identity choice is recorded, and neither unresolved deployment
decision blocks the register.

#### F02 — replace global kind tables with an active entity registry

**Depends on:** F01.

- Introduce an immutable registry containing entity class, `EntitySpec`, bundle
  directory, relations, module policy fragment, owning module, and activation
  state. Manifest policy lives at `declared/policy/manifest.yaml` in a bundle
  and under the `manifest` key of the SQLite policy payload.
- Build the base registry from today's kinds and let an enabled module
  contribute additional kinds and relations. Reject duplicate kinds,
  directories, or relation definitions at startup.
- Thread the registry through `ApplicationContext`, loader, schema generator,
  catalog traversal, serialization, reference checks, write gate, drift,
  plugin declaration validation, source coverage validation, and storage
  import/export. Remove business decisions based on mutable global `KINDS` or
  `ENTITY_SPECS`.
- Make CLI startup two-pass: resolve `--catalog` / `--data-dir`, load module
  configuration, then construct module subparsers. HTTP and MCP build routes and
  tool lists from the same application registry.

**Touches:** the seventeen modules that read the global kind tables, plus the
five zero-argument seams from §7.2.3. New: `src/cadastre/modules/`
(`registry.py`, `config.py`). Changed, in dependency order —
`core/model.py:341-390` (the four tables become registry inputs),
`core/spec.py:116`, `core/schema.py:71,104`, `core/loader.py:397,476,548,626`,
`core/catalog.py:118,254`, `core/serialize.py:41`, `core/observed.py:64,129`,
`core/writes.py:64,90,186`, `core/drift.py:78,116`, `core/topology.py:91`,
`core/storage.py`, `plugins/config.py:157`, `plugins/contract.py`,
`api/registry.py:56,106`, `application/context.py:28`,
`adapters/http.py:43,51,355`, `mcp/server.py`, `cli/main.py:35,153,350,386,598`,
`cli/lookup.py:55`, `cli/question.py:120`, `cli/fmt.py`. Tests:
`tests/test_model.py`, new `tests/test_modules.py`, fixture module under
`tests/fixtures/`.

**Split it.** This is the one task whose acceptance criteria force a smaller
split, and reviewing it as a single diff is not possible. Land it as three:
(a) introduce the registry and build the base one from today's kinds, with
every existing global still present and delegating to it; (b) thread it through
core and the adapters, deleting the globals; (c) make CLI startup two-pass per
§7.2.2 and build HTTP routes and MCP tools from the application registry. Each
is independently revertible and each must be green on its own.

**Done when:** all existing tests pass against the base registry with
`schema/catalog.schema.json` unchanged byte-for-byte, an isolated fixture
module can add one kind end to end, and a disabled fixture module is absent
from JSON Schema, OpenAPI, CLI help, HTTP routes, MCP tools, and plugin
declaration validation.

#### F03 — make module data durable and fail closed

**Depends on:** F02.

- Add a forward SQLite migration for a `required_modules` capability marker in
  both store schemas. Set it in the same transaction that first writes a
  module-owned catalog record or observed source payload to that store. The two
  databases remain separate transaction domains; neither marker pretends the
  other write committed.
- An application that lacks a required module refuses to open the database; an
  application that has but has disabled it may serve base read queries but must
  refuse catalog writes, trust resolutions, export, and import with a named
  remediation. Physical backup and restore remain available and preserve all
  bytes.
- Include required modules and versions in bundle manifests. Import rejects a
  missing or incompatible module before beginning a transaction.
- Exercise base-to-enabled, enabled-to-disabled, backup/restore, export/import,
  and newer-application/older-application migration shadows.

**Touches:** `core/migrations.py:9,31` (a second entry in `MIGRATIONS`, and
`CURRENT_SCHEMA` to 2), `core/storage.py:113-186` for the catalog bootstrap and
version refusal, `:715-733` for the observed schema, `:882-935` for
`export_bundle` and `import_bundle` — where `import_bundle` already refuses a
`format_version` mismatch at `:933` and gains the module check on the same
path, before the transaction — and `:1109-1158` for `backup`. Tests:
`tests/test_storage.py`.

**Done when:** no supported command can silently drop dormant module rows and
an older format reader refuses the migrated database instead of ignoring work
data.

**Status:** shipped as a `required_modules` key in the existing `metadata`
table (`core/storage.py:99`) rather than as a schema migration;
`CURRENT_SCHEMA` is still 1. The disabled-write/export/import refusal holds;
the older-format-reader refusal does not.

### 8.2 Register milestone R0

#### R01 — add the declared work model and write invariants

**Depends on:** F02, F03.

- Implement `work_initiative`, `work_item`, and `work_link` exactly as §5.1,
  including active schema fragments, relations, serialization, lookup, export,
  and static catalog-authority declarations.
- Add cross-record validation for unique link targets, existing initiative and
  repository references, existing `blocked_by` ids, no self-edge, and no cycle.
- Validate RFC 3339 timestamps, non-negative effort, priority/state enums, and
  link-kind/completion compatibility.
- Add/update/delete use the existing audited transaction. Deleting a work item
  with inbound blockers or links is refused until those references are removed;
  there is no cascade.

**Touches:** new `src/cadastre/manifest/model.py` and `manifest/spec.py`, both
contributed to the registry rather than appended to `core/model.py` or
`core/spec.py` — if a work kind has to be added to a core table, F02 did not
finish. New `declared/policy/manifest.yaml` fragment. Cross-record validation
extends the existing reference checker in `core/loader.py:476`; the write path
is `core/writes.py:186-256` unchanged, reached through the registry. Tests: new
`tests/test_manifest_model.py`; `tests/test_writes.py` for the refusal shapes.

**Done when:** positive and negative write tests cover every invariant, the
structured refusal names the offending path and next action, and one transaction
cannot leave a dangling work graph.

#### R02 — extend the plugin protocol for work evidence

**Depends on:** F02, R01.

- Implement `forge_item`, `markdown_finding`, `repo_checkout`, and
  `revision_check` from §5.1 as source-authoritative active model kinds, then
  add the `Work` methods and method-to-kind allowlists from §5.2.
- Register Manifest collectors only while the module is active. When active but
  the collector extra is absent, `plugins` reports the missing dependency and
  installation command without crashing base queries.
- Require each collector handshake to declare stable identity fields, field
  classes, coverage, and credible-empty behavior for every emitted kind.
- Extend the conformance fixtures to reject write methods, an unrelated entity
  kind, unbounded/truncated success, stdout pollution, and secret-shaped values.

**Touches:** `plugins/protocol.py:35` (`CAPABILITIES` gains `Work`), `:55`
(`METHOD_ENTITY_KINDS` gains the six rows from §5.2) — both of which F02 must
already have made registry-aware, since a disabled module may not contribute
methods. `plugins/collectors/__init__.py:74` for the declared-kind derivation,
`plugins/registry.py:63` for conditional built-in registration, and
`cli/plugins.py` for the missing-dependency report. Also `tests/conftest.py`,
which gains the socket and subprocess guard from §7.2.5 — built here, once, and
used by R03, R04, and R05. Tests: `tests/test_plugins.py`,
`tests/test_collectors.py`.

**Done when:** in-process transforms and JSON-over-stdio fixtures produce the
same normalized evidence and the runner cannot dispatch an upstream write.

#### R03 — implement `work-markdown`

**Depends on:** R02.

- Implement the configured-root, configured-filename, task-list-only scanner
  described in §5.2. Configuration includes `repo`, `root`, `files`,
  `max_files`, `max_file_bytes`, and `max_total_bytes`; no workspace-wide
  default exists.
- Support explicit markers in HTML comments for stable work item and forge
  references. The one accepted form is
  `<!-- cadastre-work item=WORK-ID forge=FORGE:OWNER/REPO#NUMBER -->`, with
  either attribute optional but no unknown attributes. Without a marker,
  identity uses the normalized-text digest and a text edit is honestly a
  remove/add pair.
- Preserve path, line, checkbox, heading, and inert task text; discard surrounding
  prose, code blocks, link targets other than a parsed forge reference, and
  file content.
- Mark the method incomplete/stale on an escaped symlink, unreadable selected
  file, decoding failure, or exhausted budget.

**Touches:** new `src/cadastre/plugins/collectors/work_markdown.py` and its
`cadastre-plugin-work-markdown` console script in `pyproject.toml`. Inert
rendering reuses `src/cadastre/render/inert.py`; no new escaping code.
Fixtures under `tests/fixtures/`; tests in `tests/test_collectors.py`.

**Done when:** fixture tests cover checked/unchecked items, duplicate text,
retitling with and without a marker, code fences, prompt-shaped text, symlink
escape, invalid encoding, and every budget. Tests open no network socket and
write no source file.

#### R04 — implement `work-git`

**Depends on:** F01, R02.

- Emit `repo_checkout` from explicitly configured `{id, repo, path}` records.
- Read HEAD, branch/detached state, upstream tracking ref, exact tracking-ref
  equality, and checkout reflog activity,
  dirty state, and linked worktree identity without invoking a command or
  contacting a remote.
- Treat missing objects, an unsupported repository layout, or an ambiguous
  tracking ref as visible incomplete evidence; omit facts that were not
  established.

**Touches:** new `src/cadastre/plugins/collectors/work_git.py`, its console
script, and the Git reader dependency pinned by F01 — which enters the
`manifest` extra only, never the base `dependencies` list, because
[DESIGN.md](DESIGN.md) §7 makes every base dependency a collector-host
deployment problem. Fixtures build repositories on disk under `tmp_path`.

**Done when:** fixtures cover clean, staged, unstaged, untracked, detached,
ahead, behind, shallow, bare, and linked-worktree repositories. The R02 guard
fails on subprocess or socket use.

**Status:** shipped with exact tracking-ref equality and an explicitly labeled
HEAD-reflog activity timestamp; ahead/behind counts are deliberately dropped
because they require a commit-graph walk.

#### R05 — implement GitHub issues and pull requests

**Depends on:** R02.

- Add `work.items` with separate issue and pull-request transforms. Retain only
  the §5.1 allowlist; do not retain bodies, comments, logs, patch content,
  environment values, or tokens.
- Fully paginate explicit organizations/repositories. Configuration must bound
  repositories, pages, and API calls. Reaching a bound fails the method rather
  than publishing authoritative absence.
- Normalize pull-request state without losing `draft`, `head_revision`,
  `merge_revision`, or merged versus merely closed.
- Keep credential scope and TTL separate from existing repository and runner
  sources; document the minimum read permissions and a short-lived app token as
  the preferred credential.

**Touches:** new `src/cadastre/plugins/collectors/work_github.py` and its
console script. It is a second plugin, not a fifth method on
`forge_github.py:953` — that plugin's credential is scoped for repository and
runner inventory, and §5.2 requires work collection to hold its own scope, TTL,
and failure state. Shared HTTP behaviour comes from
`plugins/collectors/http.py`; recorded responses live under `tests/fixtures/`.

**Done when:** recorded-response tests cover pagination, rate limits, deleted
repositories, draft/closed/merged PRs, renamed titles, incomplete collection,
and malicious upstream text. No test contacts GitHub.

**Status:** shipped, with a defect: GitHub's `/issues` endpoint also returns
pull requests, and the transform does not filter them, so every pull request is
also emitted as a `forge_item` of kind `issue`.

#### R06 — implement Manifest projection and drift

**Depends on:** R01, R03, R05. R04 enriches output but does not block it.

- Build one application-layer projection joining work items, links, forge
  items, Markdown findings, repository checkouts, and provenance. Adapters may
  only render this projection.
- Emit a closed category set:
  `link-target-missing`, `unlinked-forge-item`, `unlinked-markdown-finding`,
  `reflected-field-differs`, `completion-differs`,
  `markdown-completion-differs`, and `evidence-incomplete`.
- A work item without a link is valid declared-only work, not missing drift. A
  required link whose covered, fresh, complete forge source lacks its target is
  `link-target-missing`. Incomplete, stale, or out-of-coverage evidence is
  `unknown`, never absence.
- Apply `reflect` and `completion` from `work_link`; do not compare unselected
  fields. Join a Markdown finding only through its explicit work marker or a
  forge ref claimed by a link.
- Persist trust age and acknowledgement through the existing trust machinery,
  using the divergence identity F01 decided in §7.2.1; do not duplicate a trust
  store. If that decision widened the ledger key, the migration and
  `LEDGER_VERSION` bump land here, with base records reading forward unchanged.

**Touches:** new `src/cadastre/manifest/projection.py` and `manifest/drift.py`.
Core `drift.compare` (`core/drift.py:107`) is not modified — Manifest's join is
a second producer of divergences, not a new branch inside the per-kind loop.
`core/trust.py:29,44,71` and `core/storage.py:727` change only if §7.2.1 chose
the wider key. Tests: new `tests/test_manifest_drift.py`, plus
`tests/test_trust.py` for the ledger-compatibility case.

**Done when:** a time-stepped synthetic estate proves every category, first
seen, resolution, acknowledgement expiry, and flapping. Reordered collector
payloads produce byte-identical output, and a base-only ledger written before
this task still loads.

**Status:** the join and all seven categories shipped
(`manifest/projection.py`); three parts did not. §7.2.1 was never decided, so
no divergence reaches the trust ledger — Manifest drift has no first-seen, no
flapping, and cannot be accepted or acknowledged. Coverage is inferred from
emitted rows rather than declared source coverage, so a genuinely empty
repository can never produce `link-target-missing`. `repo_checkout` and
`revision_check` do not participate in the join.

#### R07 — implement ranking and explanation

**Depends on:** R06.

- Implement §5.3 as pure functions over the projection, injected clock, and
  versioned `manifest` policy. Its v1 keys are `priority`,
  `age_per_day`, `age_cap_days`, `blocker_each`, `blocker_cap`,
  `failed_required_check`, `pull_request_age_per_day`,
  `pull_request_age_cap_days`, `deployment_gap`, and `effort_per_point`.
  Reject unknown keys and validate all weights and caps as non-negative
  integers through the catalog write gate.
- Detect blocked items from the declared DAG. Unknown external evidence affects
  confidence but never invents a score contribution.
- Return typed contribution records rather than formatted arithmetic; the
  renderer prints them and a verifier recomputes the total.

**Touches:** new `src/cadastre/manifest/ranking.py`, pure over the projection.
The clock is already injected as far as `ApplicationContext.now`
(`application/context.py:33`) and `Session.now` (`cli/session.py:27`); ranking
takes it as an argument and reads no wall clock. Policy validation joins the
existing catalog write gate at `core/writes.py:90`. Tests: new
`tests/test_manifest_ranking.py`.

**Done when:** golden tests cover every contribution, cap, tie-break, blocked
item, missing/stale evidence, custom policy, and clock boundary. Property tests
assert permutation stability and `sum(contributions) == score`.

**Status:** shipped as pure functions over the declared model with an injected
clock, but five of nine contributions, no `manifest` policy fragment (weights
and caps are constants in `manifest/ranking.py`), and a `confidence` that is
always the literal `declared-only`.

#### R08 — add the canonical read service and CLI

**Depends on:** R06, R07.

- Add application operations `manifest_brief`, `manifest_backlog`,
  `manifest_repo`, `manifest_drift`, `manifest_next`, and `manifest_why` to the
  transport-neutral registry.
- Expose them as `cadastre manifest brief|backlog|repo|drift|next|why`.
  `backlog` supports state, initiative, repo, cursor, and bounded limit;
  `next` defaults to 10; `why` requires one work item id.
- Every response carries the source provenance and confidence used in the
  answer. Stale, incomplete, contested, and unknown evidence appears before
  ranked or actionable content in text output.
- Base `cadastre brief` stays unchanged. `manifest brief` is the module's
  compressed session preamble: counts and confidence only, in text and in the
  JSON projection, so its size does not grow with the register. Ranked item
  lists come from `backlog` and `next`, which take a bounded limit.

**Touches:** `application/queries.py:19` for the six operations,
`api/registry.py` for their metadata, and new `src/cadastre/cli/manifest.py`
dispatched from `cli/main.py`. The `manifest` subparser is built in the second
pass from §7.2.2, so `cadastre --help` on a disabled catalog does not mention
it. Rendering reuses `render/text.py` and `render/json_out.py`. Tests:
`tests/test_cli.py`, `tests/test_application_services.py`, goldens under
`tests/golden/`.

**Done when:** text and JSON goldens are deterministic, pagination is stable,
invalid ids and filters are structured usage errors, and the base CLI golden
files — `tests/golden/brief.txt` and `brief.json` — are unchanged while the
module is disabled.

**Status:** shipped without the cursor. `backlog` takes `state`, `initiative`,
`repo`, and a 1–100 `limit`; there is no continuation token, so a backlog past
100 items is not fully readable on any adapter.

#### R09 — add HTTP, MCP, and GUI parity

**Depends on:** R08.

- Add authenticated read routes under `/manifest/` and MCP tools prefixed
  `manifest_`, generated from the same operation registry and query service.
- Add a capability response the GUI can use to show Manifest navigation only
  when enabled. The GUI renders backlog, drift, score explanation, provenance,
  and unknown/degraded confidence; it contains no ranking or join logic.
- Extend OpenAPI, SDK, stdio, Streamable HTTP, remote bridge, and browser E2E
  fixtures. No second server, database, endpoint, or authentication profile is
  introduced.

**Touches:** `api/registry.py:56,106` — which F02 already turned from
module-level tuples into functions of the active registry, so this task adds
operations rather than a conditional. `adapters/http.py`, `mcp/server.py`,
`mcp/sdk.py`, `mcp/streamable.py`, `mcp/remote.py`, and `ui/` for the
capability-gated navigation. Tests: `tests/test_adapters.py`,
`tests/test_mcp.py`, `tests/test_mcp_stdio_sdk.py`, `tests/test_streamable.py`,
`tests/test_remote_bridge.py`, `tests/test_gui_contract.py`,
`tests/test_e2e_stack.py`.

**Done when:** CLI JSON, HTTP JSON, MCP structured content, and GUI-visible data
agree after removing transport envelopes; unauthenticated network reads fail
under the existing profile; disabled route/tool/capability snapshots are
unchanged.

**R0 exit:** R01–R09 are complete. An operator can declare work, collect local
Markdown and forge state, inspect deterministic drift, and ask what is next over
every supported interface. Nothing can write to a forge.

**Status:** reached in the sense that all nine tasks landed and every one of
those sentences is true of the shipped code. It was declared without closing
R04–R08's reduced scope, which the **Status** lines above record. Nothing can
write to a forge — that part is enforced, not merely intended (`WRITE_METHODS`,
`plugins/protocol.py:73`).

### 8.3 Optional forge metadata and external Broker

**Status:** none of O01–O03 or D01–D04 has been started. `work.revision-checks`
exists as a declared protocol method with no collector behind it, which is the
only trace of O01 in the tree.

The tasks in §8.3 and §8.4 carry no **Touches** block. Both are gated on
decisions F01 records as unresolved — who owns `manifest-sync`, and which
system supplies signed `artifact_build` evidence — and naming files against an
undecided owner would be invention rather than planning. Each gains its block
in the pull request that closes its gate.

#### O01 — add Projects v2 and revision-check enrichment

**Depends on:** R05, R06.

- Implement Projects v2 as separately configured evidence. Map fields only
  through an explicit catalog mapping; unknown project fields remain inert
  evidence and never become priority or initiative by name guessing.
- Implement `work.revision-checks` for revisions explicitly selected by linked
  pull requests. Aggregate required check state without retaining logs or step
  output, and bound repositories, revisions, pages, and calls.

**Done when:** absence and partial GraphQL responses cannot clear existing
evidence, unmapped fields do not affect ranking, and stale check state degrades
confidence rather than becoming failure.

#### O02 — emit a reviewed sync plan from Cadastre

**Depends on:** R06. Does not require O01.

- Add a read-only `manifest sync-plan` projection containing only allow-listed
  create/update/close proposals derived from selected drift rows. Cadastre does
  not submit it.
- Include schema version, catalog revision, source `as_of` values, expiry,
  repository, expected upstream identity/state, proposed field changes, and a
  canonical SHA-256 plan digest. Omit bodies and credentials.
- Default to no destructive proposal: closing an issue requires an explicit
  catalog policy and a per-item completion link.

**Done when:** the same catalog/evidence produces the same digest, a changed
revision or source snapshot changes it, and stale/incomplete evidence refuses to
produce an applicable plan.

#### O03 — build `manifest-sync` outside the Cadastre runtime

**Depends on:** O02 and an owner/repository decision from F01.

- Package and deploy a separate worker/image with its own forge credential and
  no access to Cadastre SQLite files. Cadastre's images and processes do not
  contain the worker, credential, or invocation route.
- Dry-run is the default. Apply requires an unexpired plan, exact digest,
  explicit `--apply`, repository/action/field allowlists, and an upstream
  compare-and-swap precondition. A changed issue is refused, not overwritten.
- Write an append-only audit record before and after each attempted action with
  principal, plan digest, target, action, decision, result, and upstream request
  id; never log tokens or issue bodies. Retries are idempotent.

**Done when:** contract tests against a fake forge cover dry run, expiry,
digest mismatch, allowlist denial, stale precondition, partial failure,
idempotent retry, audit failure, and token redaction. A Cadastre image scan
proves the worker and forge write credential are absent.

### 8.4 Deployment milestone D0

#### D01 — add neutral deployment entities and protocol methods

**Depends on:** F02, F03. May proceed independently of R0.

- Add `artifact_build`, `deployment_spec`, and `runtime_deployment` from §5.1,
  their relations, and `Deployment` plugin methods.
- Validate full revision ids and OCI digests; mutable tags may be retained as
  display/evidence fields but cannot satisfy a proof edge.
- Define a join result for each hop as `proved | absent | unknown`, with reason,
  provenance, and freshness. `absent` requires fresh, complete source coverage.

**Done when:** model, schema, identity, authority, refusal, and proof-state tests
cover every kind and no vendor field is read by core logic.

#### D02 — implement `orchestrator-komodo`

**Depends on:** D01.

- Implement the pinned read-operation allowlist from §5.2 with full pagination
  and recorded-response transforms. Retain upstream resource id, name, server
  id, stack state, deployed/latest revisions, service image refs, update state,
  and relevant deployment-event time.
- Map an upstream server id to a Cadastre host only through explicit
  configuration. Missing mapping yields an unknown target plus a warning; names
  are never matched heuristically.
- Resolve short revisions to full ids only through an independently collected
  repository source and only when unique. Otherwise retain the short value and
  leave the proof edge unknown.
- If the allowed API cannot supply an immutable running image digest, report
  that limitation and leave artifact-to-runtime proof unknown. Widening the
  allowlist requires its own reviewed task and fixture.

**Done when:** a static test enumerates every constructible upstream operation
and proves none belongs to `/write/`, `/execute/`, or terminal APIs; fixture
tests cover rename, server move, pagination, inaccessible server, short-hash
ambiguity, and stale updates.

#### D03 — collect build and GitOps specification evidence

**Depends on:** D01 and the artifact-source decision from F01.

- Implement at least one reviewed `artifact_build` source based on signed build
  provenance, registry annotations, or immutable release metadata. A tag naming
  convention by itself is not evidence.
- Extend `orchestrator-gitops` with `deployment.specs`: record the source repo,
  full source revision, compose path/unit, exact image ref, and explicitly
  declared orchestrator target. Do not infer a host or stack from directory
  names unless catalog configuration declares that layout.
- Normalize an image tag to a digest only when a collected immutable source
  proves the mapping. Otherwise retain the tag and mark the edge unknown.

**Done when:** a synthetic pipeline proves revision → digest → GitOps spec and
negative fixtures prove that mutable tags, short hashes, conflicting
attestations, and incomplete source coverage never produce a proof.

#### D04 — implement deployment queries

**Depends on:** D02, D03, R06.

- `manifest deployed ITEM` prints every hop from linked merge revision through
  artifact, spec, runtime stack, host, service, endpoint, and domain. It reports
  the first unproved hop and never collapses `unknown` into `no`.
- `manifest lag ITEM` reports merge-to-build, build-to-spec, and spec-to-runtime
  intervals only from comparable UTC event timestamps, along with source age.
- `manifest blast-radius SUBJECT` performs a bounded reverse traversal over
  declared relations and proved deployment edges. It includes exclusions,
  cycles, maximum-depth truncation, and trust state.
- Add HTTP/MCP/GUI parity under the existing conditional surfaces.

**Done when:** time-stepped fixtures cover deployed, superseded, rolled back,
partially deployed, missing, stale, contested, ambiguous, and cyclic graphs;
all adapters are byte-equivalent after transport envelopes.

**D0 exit:** D01–D04 are complete and at least one real artifact source can
establish immutable revision-to-runtime evidence. Until then, deployment
commands remain experimental and answer `unknown` at the missing hop.

### 8.5 Operational rollout

#### P01 — coverage and scheduling

**Depends on:** R0 for register rollout; D0 for deployment rollout.

- Inventory intended repository coverage and declare it per source. The target
  is explicit coverage of all in-scope repositories, not a count inferred from
  one forge.
- Split sources by permission and freshness: repository inventory, work items,
  checks, Markdown, local checkout, GitOps, and runtime deployment do not share
  a TTL merely because one upstream serves them.
- Provide reviewed cron/systemd/CI examples that run `collect`, never a daemon.
  Scheduling remains an operator deployment action outside Cadastre.
- Alert on collection failure, incredible empty results, stale evidence,
  incomplete coverage, and repeated identity churn. Establish a baseline before
  enabling ranking or sync plans for decisions.

**Done when:** two successive scheduled collections cover the declared estate
without unexplained missing sources, all freshness states are current, and a
documented failure drill shows prior evidence retained and marked stale.

## 9. Verification matrix

The standard gates in [TESTING.md](TESTING.md) apply to every task. Manifest
adds these release gates. The first row is not new work: it is the existing
enforcement from §7.2.4, which must keep passing untouched.

| Contract | Required evidence |
|---|---|
| Default-off compatibility | Base dependency install, schema, OpenAPI, CLI help, MCP tool list, HTTP 404s, `brief` text/JSON, backup metadata, and existing full suite are unchanged. Enforced today by `tests/test_model.py:90`, `.github/workflows/ci.yaml:55`, `.github/workflows/publish.yml:36`, and `tests/golden/` — no new gate, and no permitted edit to those artifacts |
| Activation | Enabled module appears on all surfaces from one registry; malformed or unavailable modules fail before serving |
| Persistence | Required-module marker, forward migration, disabled-write refusal, physical backup/restore, bundle round trip, no dropped rows |
| Authority | Catalog CRUD succeeds for declared work kinds; source-kind add/delete and reflected-field update produce structured refusals |
| Identity | Retitling/renaming does not change marked work, forge, checkout, or runtime ids; genuinely different records never collide |
| Collection safety | Recorded fixtures only; socket, subprocess where forbidden, symlink, page, file, byte, and call-budget guards; dry run handshakes and writes nothing |
| Completeness | A bound, partial response, or unexpected empty result retains prior evidence and becomes stale/incomplete; it never proves absence |
| Drift | Every closed category and unknown case has a time-stepped fixture; acknowledgement, first-seen, and flapping survive collection |
| Ranking | Printed typed components reproduce the score; permutations and injected clocks are deterministic; stale evidence changes confidence, not facts |
| Adapter parity | CLI JSON = HTTP JSON = MCP structured result = GUI data after transport envelopes; provenance and degraded confidence stay prominent |
| Security | No forge write operation in plugin protocol; no forge write credential or Broker code in Cadastre images; observed text rendered inertly; no secret values retained |
| Deployment proof | Every hop carries identity, provenance, freshness, and `proved \| absent \| unknown`; short hashes and mutable tags never prove a hop |

The local verification command for an implementation branch becomes:

```bash
uv sync --extra dev --extra mcp-server --extra manifest
ruff check src tests
ruff format --check src tests
mypy
pytest -q
cadastre --catalog examples/catalog fmt --check
cadastre --catalog examples/catalog check compose.production.yaml --kind compose

# The default-off contract, run against a catalog with no modules.yaml.
cadastre --catalog examples/catalog schema > /tmp/schema.json
diff -u schema/catalog.schema.json /tmp/schema.json
```

CI must also run a base install **without** `manifest`, the Manifest-enabled
suite, package-install smoke for both dependency shapes, the existing full-stack
E2E lane with the module disabled, and a Manifest-enabled parity/E2E lane. No
test contacts an operator catalog, workspace, forge, or orchestrator.

## 10. Delivery sequence

### 10.1 Order

This section is now a record of how R0 was delivered rather than a schedule.
The critical path was a straight line and there was no useful way to shorten it:

```text
F01 → F02a → F02b → F02c → F03 → R01 → R02 → ┬ R03 ┐
                                             ├ R04 ┤→ R06 → R07 → R08 → R09
                                             └ R05 ┘
```

Only three things fan out. R03, R04, and R05 are independent collectors once
R02 fixes the protocol, and R06 needs R03 and R05 but merely benefits from R04.
D01 may start any time after F03 and does not join the register path. O01 needs
R05 and R06. Everything else is serial, because each task's tests are written
against the previous task's model.

That is fourteen pull requests to R0 exit: F01, three for F02, F03, and R01–R09
at one each — with R06 likely to become two if §7.2.1 widened the ledger key,
since the migration deserves review separate from the join logic.

### 10.2 What blocks the start

**F02 must not land across a release.** The repository is mid-release-hardening:
`CHANGELOG.md` has an unreleased section covering version identity, the
`version` MCP tool, and PyPI publication, and `versioning.md` sequences P0–P2
explicitly. F02b changes signatures in seventeen modules and every adapter. Land
it after the current version cuts, not through it, or the first bad release
bisects into a diff that touched everything and meant nothing.

**F01 must resolve §7.2.1 before R01 starts.** The divergence-identity choice
determines whether R06 carries a storage migration. Discovering that during R06
means either a rushed format decision or a stalled task.

This is the one blocker that was not respected. F01 landed without recording
the choice, R01–R09 all landed anyway, and R06 shipped its join with the trust
integration simply omitted (`manifest/projection.py:7`). The decision is still
open and still a persisted-format choice, so it is now the prerequisite for
Manifest drift ever having an age.

### 10.3 The stop rule

R0 is worth building only if F02 lands with the base surface provably
unchanged. If landing the active entity registry requires editing
`schema/catalog.schema.json`, `tests/golden/brief.txt`, or the CLI help
snapshots, then module activation is leaking into base behaviour, and every
compatibility promise in §9 is already false. Stop and redesign the registry;
do not proceed to R01 with a weakened first row.

The second stop point is R06. If the projection cannot produce byte-identical
output from reordered collector payloads, the join is reading evidence the
model does not actually make deterministic, and ranking built on it in R07 will
be unexplainable in R08's `why`. Neither stop point is a reason to abandon the
module; both are reasons to stop adding to it.

### 10.4 What is deliberately not scheduled

O01–O03, D01–D04, and P01 have no position in this sequence. O03 has no owning
repository, D03 has no artifact source, and P01 depends on collection coverage
that is operational debt this proposal surfaces rather than creates (§6). They
are specified so R0 is not designed into a corner, and scheduling them before
their gates clear would be a plan for work nobody can start.
