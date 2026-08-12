# Cadastre — Design

Status: revised application-stack design, 2026-08-09. The SQLite-backed
catalog, HTTP/API server, Streamable HTTP MCP server, and remote agent bridge
are implemented locally. The GUI is a required product component implemented
under `ui/`; external publication remains a release concern. See [ARCHITECTURE.md](ARCHITECTURE.md) and
[DEPLOYMENT.md](DEPLOYMENT.md) for the target stack and deployment contract.
Audience: contributors, and agents working in this repo.

---

## 1. Framing

### 1.1 The problem being solved

Agents are competent at building software and incompetent at deploying it into a
specific real environment. The gap is not tooling. It is that the environment's
shape and the policy for choosing within it are **tacit knowledge** — they live in
an operator's head, distributed across a VPN dashboard, a DNS panel, a secret
manager, and a set of habits nobody wrote down.

Given no ground truth, an LLM does not stop. It produces a plausible answer. The
failure mode is confident, well-formed, and wrong: the right-shaped Compose file
pointed at the wrong host, on a taken port, with a secret reference in a format
that doesn't exist.

### 1.2 The design consequence

Two distinct sub-problems, deliberately not fused:

| Sub-problem | Component | Nature |
|---|---|---|
| "What is true about my estate?" | **Catalog** | Knowledge. Safe. Public-ish. |
| "How does the agent reach it without a human?" | **Broker** | Access. Dangerous. Audited. |

Fusing these produces a tool that stores infrastructure knowledge *and* holds
credentials for everything it describes — a single artifact whose compromise
yields both the map and the keys. They remain separate concerns and security
boundaries inside the application stack: the Catalog owns the map, while any
Broker integration remains an explicitly separate capability.

### 1.3 Non-goals

Cadastre is a map, not the territory. It maintains the record; it never operates
the thing recorded.

- **No estate runtime.** It does not schedule, deploy, reconcile, or own
  workloads. Its HTTP/API, MCP, and GUI processes are product interfaces to the
  catalog, not a workload control plane.
- **No mutation of infrastructure.** No plugin has a write path to a live
  system. Editing the *catalog* is expected and supported; editing the *estate*
  is something the Catalog cannot do at all.
- **No reconciliation.** Does not converge reality toward declaration or the
  reverse. Reports divergence, stops.
- **No automatic resolution.** When two sources disagree, the disagreement
  becomes a durable state on the entity and waits for a human. Cadastre never
  picks a side (§2.9).
- **No secret storage.** References and existence only.
- **No estate deployment.** Emits knowledge; an operator or agent writes and
  applies deployment artifacts outside Cadastre.

**"Read-only" was the wrong word and has been retired.** It conflated two
different writes: writing the *map*, which was always meant to happen and needs
no credentials to anything, and writing the *territory*, which is the Broker's
problem and is default-denied. The Catalog is **writable by design and inert by
construction**. `catalog.sqlite3` is edited constantly, through a gate (§2.3); nothing
Cadastre can do changes a DNS record, a container, or a tailnet.

The temptation to grow into a platform will be constant. Each of the above is a
line that, once crossed, changes what the tool is and who can safely run it.

---

## 2. The Catalog

### 2.1 Authority is per entity class

The operational catalog is SQLite. `catalog.sqlite3` stores declared intent,
policy, annotations, revisions, and audit records; `observed.sqlite3` stores
current source payloads and observation history. YAML/JSON trees are explicit
interchange and fixture formats only. Git is source-control for Cadastre's
source and release process, never a runtime dependency or catalog backend.
An initialized empty SQLite catalog is valid and needs no plugin.

The single most important structural decision, and the inverse of what a platform
would do.

```
catalog.sqlite3   declared intent and policy; writes are audited transactions
observed.sqlite3  evidence collected from live systems and its history
bundle/           explicit YAML/JSON interchange, never an implicit backend
```

A platform reconciles reality toward the declaration. Cadastre does **neither
direction automatically**. A host running something absent from catalog intent
is a *finding*, surfaced by `cadastre drift`, resolved by a human deciding which
side is wrong.

Rationale: an auto-reconciling catalog is a control plane, and a control plane
needs write credentials to everything it describes. Refusing to reconcile is what
keeps Cadastre safe to open-source and safe to grant read access broadly.

**"Declared is authoritative" is not globally true**, and pretending it is
produces nonsense. Authority depends on whether a source of record exists outside
the catalog at all.

| | **Source-authoritative** | **Catalog-authoritative** |
|---|---|---|
| Examples | tailnet membership, DNS records, orchestrator stacks, forge repos, CI pipelines | deployment topology, exposure tiers, grants, naming conventions, host roles |
| Truth lives | in the upstream system, reachable by API | in the catalog database, because there is nowhere else |
| Catalog's role | mirrors | authors |
| `add` / `delete` | **refused** (§2.4) | allowed |

Declaring a tailnet node into existence is either redundant — the API already
agrees — or a lie, when the API disagrees. The second is worse than having no
entry, so it is refused rather than accepted and later reconciled.

Each plugin declares `authority: source | catalog` per entity type it owns
(§4.2). Being partially source-authoritative is the normal case, not an
exception: an orchestrator knows which stacks exist and will never know which of
them you consider critical.

### 2.2 Three field classes

Making whole entities read-only would cost two things worth keeping, so the split
is per *field*, not per entity.

| Class | Owned by | Catalog may write | Example |
|---|---|---|---|
| **reflected** | the upstream system | no | tailnet node id, stack status, DNS record value |
| **intended** | the catalog | yes | "this stack *should* run on nodeb" |
| **annotated** | the catalog | yes, but cannot create the entity | owner, criticality, `role: workstation`, tags |

**intended** is what makes absence detectable. With reflected fields alone, a
stack that silently disappeared is indistinguishable from one that never existed.

**annotated** is half of what `context-for` needs. No API will ever tell you
which host you never put stateful workloads on.

Neither lets you conjure an entity the upstream system has never heard of. An
`add` of an intent or an annotation *keyed to* an existing entity is allowed; an
`add` that would create a reflected entity is refused.

An annotation whose entity has disappeared is not silently dropped. It becomes an
orphaned-annotation finding and resolves like any other contested state (§2.9).

### 2.3 Writing to the catalog

Every write — human, agent, or CLI — goes through one gated SQLite transaction:

```
schema validation
  → plugin validation      (is that node in inventory? is that ref format real?)
  → cadastre check         (the same rules the CI gate runs)
  → edit provenance stamp  (who, when, why, which source)
  → audit record and revision in the same transaction
```

A bad write fails **at the write**, not at review time. Operators may export a
bundle for review in any source-control system.

Storage, serialisation, identity matching, the audit transaction and the schema
gate are **core-provided**. Plugin authors write no persistence code, and there
is exactly one operational format. This is why every plugin gets uniform CRUD for free,
including plugins holding a read-only token upstream and plugins with no
credentials configured at all.

`observed.sqlite3` refuses writes from this path entirely. It is produced by `collect`
and nothing else.

### 2.4 Refusals are structured

A refused write is told where to go instead, in the same style as a Broker
denial (§5.6):

```
REFUSED  add tailscale.node
  Entity 'node' is reflected from the Tailscale API; the catalog mirrors it.
  To add a node, add it in Tailscale, then: cadastre collect --source tailscale
  You may annotate an existing node:       cadastre annotate node:<id> owner=...
```

A vague failure is precisely what makes an agent start improvising. A specific
one with a named next step gets reported cleanly.

The corollary: **correcting a collector is never an `update`.** If the mirror is
wrong, the upstream API is still right by definition — fix the collector, or file
the disagreement and let it sit contested.

### 2.5 Collectors

Read-only processes that observe reality and write timestamped evidence.

- Scoped, read-only credentials. Always. A DNS collector gets zone-read, never
  zone-edit.
- Output is content-addressed and timestamped per source.
- Failure is non-fatal and *visible*: a collector that cannot reach its upstream
  marks that source stale rather than silently serving old data as current.
- A collector that succeeds and returns **zero records** is not the same as a
  genuinely empty set. Each source declares `empty_expected: true | false`; an
  unexpected empty result is a finding, not a silent wipe.
- Run on a schedule (cron, CI, systemd timer) — Cadastre does not daemonize.

### 2.6 The model

The model is the product; everything else is plumbing. Keep it small. An
over-modeled catalog is one nobody updates, and an un-updated source of truth is
worse than none.

**Entities**

| Entity | Meaning |
|---|---|
| `host` | Anything that can run something. Physical, VM, container host, WSL instance. |
| `network` | A reachability domain with a class (`private` / `public` / mixed). |
| `service` | A deployed unit of work. |
| `endpoint` | An address a service is reachable at, within some network. |
| `domain` | DNS zone or record. |
| `secret` | A **reference**. Never a value. |
| `pipeline` | A CI definition that deploys some class of thing. |
| `repo` | A VCS repository, and where it lives. |
| `ci_executor` | A registration a CI system may schedule work onto. |
| `ci_pool` | A routing and access boundary containing executors. |
| `deployment_topology` | A repeatable path from repository to running workload (§2.7). |

**Relations**

`runs_on` · `reachable_from` · `resolves_to` · `fronted_by` · `consumes_secret` ·
`deployed_by` · `hosted_in` · `follows_topology` · `pool`

`ci_executor.runs_on` is the one relation that is never inferred. A
registration's name, operating system, labels, or address cannot establish
which host it runs on; the placement is declared and compared against
independent host-side evidence.

Add a field only when a real question cannot be answered without it.

This is the **base** model, and it is what a catalog with no `modules.yaml`
has. An enabled module contributes further kinds and relations to the active
registry (§4.9) without appearing here, because a kind that only some catalogs
have is not part of the model every catalog shares.

### 2.7 `deployment_topology`

The missing half of `context-for`. `context-for` answers *where can this go*; a
topology answers *how does it get there* — the join across repo → pipeline →
artifact → target → exposure that otherwise exists only as an operator's habit.

Catalog-authoritative by definition: no upstream system holds it, because it
describes the relationship *between* upstream systems.

```yaml
# bundle/topologies/orchestrated-stack.yaml
id: orchestrated-stack
repo:    {forge: selfhosted, path_pattern: "apps/*"}
build:   {pipeline: ci-apps, produces: oci_image, registry: registry-0}
deploy:  {target: {kind: orchestrator, id: orchestrator-0, node: nodeb},
          artifact: compose}
expose:  {tier: internal, hostname: "<service>.<private-zone>"}
secrets: {ref_format: "<scheme>://<project>/<env>/<key>"}
```

Three things fall out of it:

- **`check` gains teeth.** Validate an artifact against *the topology it claims*,
  not only against generic rules.
- **A new drift class: topology drift.** Does that pipeline still exist? Is that
  node still in inventory? Is the registry still the one being pushed to?
- **Phase 4 gets most of its input.** The Broker's intent → target → role →
  backend mapping is very nearly a topology, and **D3** (tag taxonomy) is
  expected to resolve here — tags acquire meaning when a topology selects on
  them.

### 2.8 Vocabulary neutrality

No vendor nouns in the core model.

| Write this | Not this |
|---|---|
| `network: {class: private, id: tailnet-0}` | `tailnet:` |
| `dns_zone` | `cloudflare_zone` |
| `secret_ref` | `infisical_path` |
| `pipeline` | `workflow` / `woodpecker_pipeline` |
| `ci_executor` | `github_runner` / `agent` |
| `ci_pool.selectors` | `runs-on` / `runner_group` |

Vendor-shaped facts that do not fit the core model live in a **namespaced
attribute block** — `x-komodo.stack_id`, `x-tailscale.acl_tag` — validated by a
JSON-Schema fragment the plugin ships (§4.2).

The rule that makes this safe: **core logic reads core fields only.** Placement,
`check`, drift and rendering never branch on an `x-*` value. If a decision needs
one, the core model is missing a field and that is a model change, not an
attribute lookup.

Exposure tiers are **user-defined** in catalog policy (or an exported policy
bundle). Cadastre
ships no opinion about what tiers exist. One operator's `public|tailnet|lan` is
another's `dmz|corp|lab`.

### 2.9 Trust: freshness, verifiability, agreement

Three independent axes. Collapsing them loses information a human needs.

| Axis | Question | Failure state |
|---|---|---|
| **Freshness** | when was this last observed? | `stale` |
| **Verifiability** | can *any* configured source confirm it? | `unverified` |
| **Agreement** | do the sources that can see it agree? | `contested` |

`unverified` deliberately stays separate from `contested`. A declaration nobody
can check is more dangerous than one two sources argue about — it looks clean —
and the two need different human responses. Merging them hides the first behind
the noise of the second.

**Entity/field state is durable, not a report.** `drift` was a diff you generate
and read; the condition an entity is *in* persists across collects.

| State | Meaning |
|---|---|
| `agreed` | sources match |
| `unverified` | no configured source can confirm it either way |
| `contested` | checked state ≠ known state, unresolved |
| `acknowledged` | contested, a human looked, chose to leave it, with a reason and a review-by date |

`acknowledged` is what stops the contested set becoming a permanently-red
dashboard nobody reads. It is not a resolution — it is an explicit deferral, and
when its review-by date passes it reverts to `contested`. Acknowledgements are
human decisions, so they live in the catalog database and may be exported for
review through the normal repository workflow. The
contested state itself is derived, never hand-written.

**Resolution — three paths, all human-initiated:**

1. **Reality was right** → `cadastre accept observed <entity>` writes the
   observed value into the catalog database through the §2.3 gate. The common case, and
   cheap.
2. **The declaration was right** → the world is wrong. Cadastre records the
   decision and the entity stays contested until the world changes. Phase 4 is
   what eventually makes this actionable.
3. **Both are fine** → `cadastre acknowledge <entity> --reason ... --until ...`.

There is deliberately **no fourth path where Cadastre picks.** The moment it
silently prefers one side, it is the reconciler §1.3 refuses to be.

**What the query layer does meanwhile** is declared per field by the plugin —
`on_contest: exclude | warn | ignore`, defaulting to `warn`:

- A contested **port map** should make the host ineligible in `context-for`.
  Acting on a value known to be disputed is worse than having no value.
- A contested **owner annotation** should block nothing.

When it excludes, it says so in the §3.2 style — *"excluded: contested port map,
unresolved since 2026-07-14"* — never a silent drop.

**Age is the signal, not existence.** A disagreement that appeared this morning
is a collector hiccup; the same one three weeks old is a real divergence nobody
owns. Divergences carry `first_seen` and survive collects, which requires
observation *history* rather than last-value-only storage (§2.11).

**Flapping is its own finding.** A field oscillating between `agreed` and
`contested` across runs usually means the collector is wrong or the plugin's
identity function is unstable — not that the estate is unstable. Detected
separately, so it is not "resolved" repeatedly by accepting observed.

### 2.10 Provenance

Every response carries per-source provenance. This is not decoration.

```json
{
  "result": { },
  "provenance": [
    {"source":"dns","plugin":"cloudflare","as_of":"2026-08-05T22:00:00Z",
     "ttl_seconds":86400,"stale":true,"state":"agreed"}
  ]
}
```

An agent handed a two-week-old port map will act on it without hesitating. The
`stale` flag, and any `contested` state, must be prominent enough in rendered
output that a model reading prose notices — not buried in a JSON tail.

Freshness thresholds are per-capability and configurable; a hardware inventory
tolerates days, a port map does not.

### 2.11 Storage

Two stores, and the distinction is authority, not convenience.

| | `catalog.sqlite3` | `observed.sqlite3` |
|---|---|---|
| Format | SQLite | SQLite, plus JSON bundle sections |
| Authority | source of record for catalog-authoritative entities | evidence, never truth |
| Lifetime | permanent, reviewed | rebuildable cache |

**The catalog database is the operational source of truth.** Exported bundles
provide reviewable diffs and offline transfer without making Git a runtime
dependency. Grant authorization remains explicit and default-deny.

**Observed evidence is SQLite.** It is generated, high-churn, timestamped and
joined against constantly; in a source repository it produces history nobody
reviews. A single
stdlib SQLite file breaks nothing stated here — no daemon, no server, the process
still starts, answers and exits — and it buys three things the JSON-only form
cannot give:

- observation **history** rather than last-value-only, which is what `first_seen`
  and flapping detection in §2.9 require
- cheap joins for `context-for`
- `drift` becoming a query instead of a generated file

The constraint that keeps it honest: **the database is a cache, rebuildable from
snapshots, never authoritative.** JSON snapshots remain the interchange format.
Delete the file and `cadastre collect` reconstitutes it.

---

## 3. Query surface and adapters

### 3.1 Answers, not entities

The distinction between a useful agent interface and a YAML dump behind JSON-RPC.

Granular tools (`list_hosts`, `get_host`, `list_dns`…) force the agent to chain
calls, burn context on intermediate results, and reason badly across the joins.
Cadastre does the joining **server-side** and returns decisions.

| Command | Returns |
|---|---|
| `brief` | ~1.5k tokens. Whole estate, compressed. Session preamble. |
| `context-for <intent>` | The subset of truth relevant to *one decision*, pre-joined. |
| `check <artifact>` | Structured violations in a not-yet-committed file. |
| `lookup <entity>` / `neighbors` | Drill-down. |
| `drift` | Current divergence. |
| `stale` | What a human owes attention: stale, unverified, contested (§2.9). |
| `add` / `update` / `delete` / `annotate` | Gated catalog writes (§2.3). |
| `accept` / `acknowledge` | Resolve or defer contested state (§2.9). |
| `plugins` | Registered plugins, active or not, and what they want (§4.4). |

### 3.2 `context-for` — the function that earns the project

Input is an intent, not a query. Output is not "here are 12 hosts" but:

- **Candidates** — which hosts qualify, *and why the others were excluded*
- **Conventions in force** — naming, exposure, secret-ref format
- **Conflicts** — port collisions, name collisions, capacity
- **Provenance** — per source, with staleness

The rejected-alternatives list matters as much as the candidates. It stops the
agent re-deriving the exclusion badly, and it makes a wrong catalog *visibly*
wrong ("it excluded prox-01 for no GPU — but prox-01 has a GPU") instead of
silently wrong.

### 3.3 `check` — the highest-value command

Read-only context helps. What makes an agent *competent* is a fast feedback loop
with specific errors. `check` consults the map about a proposed artifact; it
touches nothing.

```
ERROR  services.whisper.expose
  "public" requires an exposure tier with class=public.
  Host `nodeb` is reachable only from network `tailnet-0` (class=private).
  Fix: set expose: "internal", or place on a host in a public-class network.
```

An error of that shape gets self-corrected in one turn. Ten read-only tools do
not buy what one good error message does.

The same validator runs in CI, so a policy violation cannot merge even if the
agent ignored the tool.

### 3.4 Adapters — the second extension axis

Cadastre has two extension points and they are not interchangeable.

```
     plugins  ──▶  [ core: model · policy · storage · check ]  ◀──  adapters
    (data in)                                                    (access out)
  static, exec, komodo,                                    cli · mcp · http · gui
  tailscale, dns, ci, …
```

**Plugins contribute data.** They declare entity types, authority, an identity
function, an attribute schema, validation (§4.2).

**Adapters expose the core.** They own no entities, hold no state, and contain
**no logic** — they translate one calling convention into core calls and render
the result. Logic appearing in an adapter belongs in the core.

A server is therefore an application surface, not a plugin. The local CLI and
the deployed HTTP/API and MCP processes all call the same core. The GUI is a
browser client of the HTTP/API surface; it must not contain catalog logic or a
direct database path. Adapter/server dependencies remain optional for a local
CLI installation, while a remote product deployment requires the server
components.

| Surface/component | Transport | Product role |
|---|---|---|
| `cli` | process invocation | local/admin surface; always available |
| HTTP/API server | HTTP(S) | application backend and GUI/API contract |
| MCP server | stdio or Streamable HTTP | agent-facing application surface |
| remote bridge | local stdio → remote MCP | compatibility client for stdio-only agents |
| GUI | browser → HTTP(S) | versioned static client under `ui/`; publication is release evidence |

### 3.5 MCP server and agent client

A thin server surface — target ~200 lines of transport code — exposes the query
surface as tools over MCP. The MCP server may be spawned over stdio or exposed
as Streamable HTTP. It is a server-side component in a remote deployment; an
agent may additionally need a local MCP client or the Cadastre remote bridge:

- A trusted catalog/collector environment may use the local CLI/core path.
- An agent runtime inside a workload stack uses the HTTP client path, configured
  with `CADASTRE_HTTP_URL` and `CADASTRE_REMOTE_ONLY=1`. It has no catalog
  checkout and fails closed when the endpoint is missing.

The networked MCP client returns the HTTP/API server's canonical JSON directly.
It does not reimplement catalog logic, cache answers, or silently fall back to a
local catalog. The current raw stdlib listener is loopback/read-only by default
and is not a production remote edge. A remote deployment must select and
document its TLS, authentication, ingress, and port profile before migration.

`brief` is intended for session-preamble injection rather than on-demand calling.

**Write parity.** The MCP tool surface mirrors the HTTP write routes —
`add`, `update`, `annotate`, `accept`, `leave_contested`, `acknowledge` — all
`catalog.write` scoped and routed through the same `WriteService.dispatch()`
boundary CLI and HTTP already use (§4.3). `delete` is not exposed over MCP:
least reversible, lowest agent need, and a refused `add`/`delete` on a
source-authoritative kind already names the CLI/HTTP path in its structured
refusal (§2.4). Write tools are off by default (a server flag for Streamable
HTTP, an env var for stdio) and, once enabled, are listed and callable only
for an *authenticated* principal holding `catalog.write` — a mutation must
be attributable even when the rest of the endpoint runs unauthenticated
(loopback development). `principal` is never a tool argument: it comes from
the transport's own authentication (a bearer token's identity, or the local
CLI-equivalent default), because a caller-supplied `principal` would let any
`mcp`-scoped token forge the §2.3 provenance stamp. The remote bridge adds no
tools of its own — it lists exactly what the remote server's `tools/list`
returns, so it inherits the write surface (or its absence) automatically.

A transport-parity guard (`tests/test_transport_parity.py`, L5) walks the
registered MCP operations across stdio, Streamable HTTP, and the bridge and
asserts equivalent success/error envelopes, plus an inventory check that
every HTTP route has either an MCP operation or a named exclusion. New
surface that forgets a transport fails that test, not a future audit.

### 3.5.1 Open-source client consumption

Cadastre is an open-source product, so an agent client must be able to consume
the supported query surface without each operator writing an estate-specific
wrapper. This is a distribution requirement, not a change to the catalog model
or a reason to add vendor SDKs to core.

The supported consumption contract is:

1. **Primary remote transport:** standard Streamable HTTP MCP at `/mcp`. A
   client that supports remote MCP connects directly to this endpoint, verifies
   TLS and the configured hostname, and supplies the configured authentication
   mechanism.
2. **Portable compatibility transport:** a Cadastre-provided stdio remote
   bridge for clients or environments that require a local MCP process. The
   bridge forwards to the configured remote endpoint, remains remote-only, and
   fails closed when the endpoint or credentials are unavailable. It is a
   supported Cadastre distribution artifact, not a wrapper the user is expected
   to invent.
3. **Supported client examples:** the release documentation provides tested,
   copyable configurations for Claude Code, Codex CLI, and OpenCode. Examples
   select one of the two transports, document the token-file or environment
   contract without embedding a token, and enable only the read operations
   appropriate to the deployment.
4. **Conformance tests:** the distribution tests a clean client connection,
   MCP initialization, tool discovery, `brief`, `context-for`, provenance in
   the response, authentication failure, and remote-only failure behavior.

Infisical is deliberately not part of this contract. It may provide a token to
one operator's deployment, but Cadastre accepts operator-provided token files,
workload identity, mTLS, or a configured trusted proxy. Client examples must
not require Tailscale, Cloudflare, Caddy, Infisical, or any other
estate-specific service.

Client-specific files and launchers belong in the distribution/examples layer;
the core remains vendor-neutral and all clients route through the same adapter
contracts and canonical response renderer. Ordinary HTTP at `/brief` is not a
substitute for the MCP contract at `/mcp`.

### 3.6 HTTP API

For consumers that should not all clone the repository: multiple agents, a
dashboard, a team, and the GUI (§3.7).

`cadastre serve` is the HTTP/API application server. It is optional for an
offline/local CLI installation, but required for the GUI and for remote agent
access. It is stateless with respect to business state: SQLite remains the
durable catalog store. What the original local-first rule bought, and what
survives:

| Property | Preserved by |
|---|---|
| Local use remains simple | `serve` is optional for offline CLI use; remote/GUI use requires the application services |
| No always-on process holds infrastructure credentials | `serve` holds none; collection stays on the collector host |
| Trivial replication, offline use | a clone remains a full working copy |

Constraints, all load-bearing:

- **Database-backed.** It owns no second store; it uses the configured catalog
  and observed SQLite databases. Restarting loses no catalog state.
- **Complete.** Every core capability is expressible over the API. The GUI being
  an API client (§3.7) is the forcing function: if the GUI can do something the
  API cannot express, the API is incomplete.
- **Default loopback, default read-only.** A non-loopback bind and the write
  endpoints (§2.3) are each explicit opt-in.
- **Authenticated writes.** Bearer tokens map to principals supplied by server
  configuration. Catalog policy can be exported for review, like grants. No
  second identity system, and it gives §2.3's edit-provenance stamp a real
  principal to record.
- **Authenticated reads for networked agents.** A non-loopback deployment must
  require authentication supplied outside the catalog. Prefer workload identity
  or an authenticated sidecar/proxy at the edge. A protected bearer-token file
  is supported as a fallback, but the token is then available to the MCP process
  by design and must be short-lived and read-scoped. Token values are never put
  in the catalog, argv, or URLs. Reads and writes are authenticated; writes
  remain disabled by default.
- **Never fronts the Broker** unless separately and explicitly enabled.
  Otherwise this is a remote execution service.
- **Documented from the model.** OpenAPI 3.1 emitted by the same code that emits
  the entity JSON Schema — `cadastre schema --openapi`, served at `/docs` — so
  the API documentation cannot drift from the model. "The model" means the
  *active* registry: a catalog with a module enabled documents that module's
  routes and component schemas, and one without it documents neither. The
  document a build emits with no module enabled is the compatibility artifact.

Stated plainly, because it is a real cost: an authenticated HTTP surface with
write endpoints is new attack surface and a new thing to operate. It buys
multi-consumer access and separates an agent workload from the catalog data
directory.
The HTTP endpoint is optional for local/offline use. A non-loopback deployment
must use the supported secure transport and identity profile defined by M16–M20:
Cadastre-owned HTTPS is available without a reverse proxy, while an existing
proxy is an optional deployment boundary rather than a product prerequisite.

### 3.7 GUI

A required product component and a client of the HTTP API with **no privileged
path to the core** — same tokens, same endpoints, same authorisation as any
other consumer. It is separately deployable and must not access SQLite directly.
The repository contains the versioned GUI implementation under `ui/`; external
publication and deployment evidence remain release concerns, not an optional
adapter decision.

That constraint is the point. A GUI wired directly into the core would quietly
become the place where logic accumulates and where the API's gaps stay
invisible.

---

## 4. Plugins

### 4.1 One concept

An integration is a **plugin**. There is no second word for it: Komodo is a
plugin, Tailscale is a plugin, `static` is a plugin. "Provider" is retired as a
separate concept — an out-of-process plugin is a plugin that happens to be an
executable.

Cadastre has never been deployed. The repository is therefore the only supported
state: renaming the configuration to `plugins.yaml` and moving observed storage
need zero migration compatibility, legacy aliases, or upgrade-path code for an
existing installation.

A plugin is **one integration, one bounded set of entity types**, and can be a
single self-contained Python file.

The test that keeps the contract honest: **if a shipped plugin needs a core
change to work, the contract is wrong.** In-tree plugins are in-tree for
distribution convenience only; they get no privileges a third-party plugin
lacks. `static` and `exec` are the exception, and not really integrations —
they are the substrate everything else stands on (§4.8).

### 4.2 What a plugin declares

Core owns storage, serialisation, the transaction audit, schema gate and identity
matching (§2.3). A plugin therefore supplies only what core cannot infer:

**1. Entity types it owns**, each with `authority: source | catalog` (§2.1), and
for source-authoritative types, which fields are `intended` and which are
`annotated` (§2.2).

**2. A stable identity function.** The load-bearing one. Given a collected record
and a declared record, are they the same thing? Without it `update` cannot find
its target and every collect reports phantom divergence. Identity must be stable
across renames and reboots — an orchestrator stack is `(node, stack_name)`, a
VPN node is its node id, never its hostname.

**3. A JSON-Schema fragment** for its `x-<plugin>.*` attribute block (§2.8).

**4. Validation beyond schema** — "that node is not in inventory", "that secret
ref does not match the declared format" — surfaced as `check` errors at write
time (§2.3), not at review time.

**5. Per-field `on_contest`** — `exclude | warn | ignore`, default `warn`
(§2.9).

**6. `empty_expected`** per source (§2.5).

**Read is mandatory. Nothing else is.** A plugin holding a read-only DNS token
has no upstream mutation to implement, and forcing one produces stubs that raise
or, worse, plugins requesting write credentials they do not need.

### 4.3 CRUD is catalog-level, always

`add` / `update` / `delete` operate on **the record, never on the world.**
`delete` removes a declaration; it does not decommission a host. `add` asserts
that something exists; it does not spawn a container.

Routing is by store, which is what stops CRUD and collection from fighting:

| Operation | Writes to | On next `collect` |
|---|---|---|
| `add` / `update` / `delete` | `catalog.sqlite3` | untouched |
| `collect` | `observed.sqlite3` | current source replaced, history retained |

They never overwrite each other — they are compared. A hand-added record the
collector cannot see does not vanish on the next run; it becomes *declared, not
observed*. A deleted declaration whose subject is still running becomes
*observed, not declared*. Both are contested state (§2.9), which is the correct
outcome and usually exactly what you wanted to know.

For source-authoritative entity types, `add` and `delete` are refused (§2.4).

### 4.4 Registered by default, active when configured

A plugin with no credentials is **not** an error and **not** absent. It is
registered and inactive, and `cadastre plugins` shows it with the configuration
it wants. Discovery without noise.

This has a useful consequence: an inactive plugin still accepts catalog-side
writes. You can declare tailnet facts by hand with no Tailscale token
configured, wire up collection later, and let contested state tell you where the
hand-written model was wrong. Declare first, integrate later.

### 4.5 In-process and out-of-process

Both are supported, on the same contract, and the trust difference is stated
rather than papered over.

| | **In-process** (Python) | **Out-of-process** (any language) |
|---|---|---|
| Discovery | `importlib.metadata` entry point `cadastre.plugins`, or a file in `plugins/` | executable named in configuration |
| Authoring | one self-contained `.py` file | any language, including a shell script |
| Isolation | **none** — runs with core privileges | process boundary; a crash is a stale source |
| Dependencies | shares the core interpreter | entirely its own |

Installing an in-process plugin is equivalent to installing a library, and
should be treated with the same suspicion. Out-of-process remains the right
choice for anything untrusted, anything that needs a vendor SDK, and anything
not written in Python — core takes **zero vendor SDK dependencies** either way.

In the current v1 implementation, in-process discovery supplies declaration
metadata only; collection always invokes the command configured in
`plugins.yaml`. A local plugin can still be one self-contained Python file: its
top-level `PLUGIN` value is discovered for policy and identity, and its guarded
`main()` speaks the subprocess protocol for collection. See
[PLUGINS.md](PLUGINS.md) for the implemented authoring contract and current
limits.

### 4.6 Capability interfaces

| Capability | Methods | Notes |
|---|---|---|
| `Inventory` | `inventory.list` | |
| `Network` | `network.list`, `network.members` | |
| `Endpoint` | `endpoint.list` | What an ingress collector returns. Separate from `Inventory` because calling an address an "inventory item" loses the distinction drift needs |
| `DNS` | `dns.zones`, `dns.records` | |
| `SecretRef` | `secret.list`, `secret.stat` | |
| `VCS` | `vcs.repos`, `vcs.open_pr` | |
| `CI` | `ci.pipelines`, `ci.status`, `ci.trigger` | |
| `Topology` | `topology.list` | |
| `Work` | `work.items`, `work.findings`, `work.repo-state`, `work.revision-checks` | Contributed by the Manifest module (§4.9); its kinds exist only in a catalog that enabled it |
| `Broker` | `broker.mint`, `broker.exec` | |

A plugin implements any subset and declares which via handshake. `vcs.open_pr`,
`ci.trigger`, and both `broker.*` methods are named but refused by the runner
before a plugin starts; they are reserved shape, not available behaviour.

**There is no `apply` capability, and adding one is a design change, not a
feature.** Mutating a live system is Broker territory (§5) — grant-gated,
audited, and deliberately outside the plugin contract so that a plugin can never
be the thing that changes your estate.

### 4.7 Wire protocol

One-shot exec per call for v1: simple, trivially correct, language-agnostic.
Optimize to a long-lived newline-delimited-JSON session only if profiling demands
it.

Request on stdin — a single JSON object:

```json
{
  "v": 1,
  "method": "inventory.list",
  "params": {},
  "config": {"endpoint": "https://...", "token_env": "CADASTRE_P_FOO_TOKEN"}
}
```

Response on stdout — a single JSON object:

```json
{
  "v": 1,
  "ok": true,
  "result": {"hosts": []},
  "as_of": "2026-08-07T01:12:00Z",
  "warnings": []
}
```

Errors:

```json
{"v":1,"ok":false,"error":{"kind":"unreachable","message":"...","retryable":true}}
```

`error.kind` ∈ `unreachable` · `unauthorized` · `not_found` · `invalid_config` ·
`rate_limited` · `internal`.

**Rules.** Exit code 0 on a well-formed response, including `ok:false`. Non-zero
means the plugin itself is broken. Nothing but the JSON object on stdout;
diagnostics go to stderr. Credentials arrive by environment variable named in
`config`, never in `params` — `params` may be logged.

Handshake: `plugin.info` → `{name, version, capabilities, entities}`, where
`entities` carries the §4.2 declarations — authority, field classes, identity
function, attribute schema, `on_contest`, `empty_expected`.

In-process plugins expose the same declarations through the Python contract
rather than over stdio; the core treats the two identically from that point on.

### 4.8 Core plugins

Not integrations — the substrate. These are the only plugins that ship in core.

- **`static`** — reads catalog intent. Makes Cadastre useful with zero integrations.
- **`exec`** — runs a configured command, parses JSON from stdout. The adoption
  unlock: anyone integrates anything with a short shell script and nobody writes
  Rust to try the tool. Also the discovery mechanism for which plugins deserve to
  be written properly.

Everything else — the VPN, DNS, both forges, CI, secrets, the orchestrator — is
an ordinary plugin on the ordinary contract, in-tree for distribution only.

### 4.9 Modules

A **module** is different from a plugin: it adds core logic — new entity
kinds, cross-kind joins, adapters — not just an integration on the existing
contract. §4.1's honesty test ("a plugin needing a core change means the
contract is wrong") does not apply to a module, because a module is not
claiming plugin status; the cost is real and it is core growth, accepted
deliberately rather than smuggled in as a plugin.

Manifest (see [MANIFEST.md](MANIFEST.md)) is the first module. Its
architecture decision, recorded here because it is a base-contract change and
not Manifest-specific:

- **Activation is explicit configuration, never package discovery.** A module
  is enabled only by `modules.yaml` (mirroring `plugins.yaml`'s two legal
  locations: catalog root, falling back to `declared/`). Code must not decide
  a module is active because its optional dependency happened to import
  successfully — that would make `pip install` a security-relevant action and
  make a disabled catalog's behavior depend on what happens to be installed
  on the machine running it, not on what the catalog declares.
- **An installable extra is necessary but not sufficient for isolation.** A
  Python extra can add dependencies; it cannot conditionally remove entity
  kinds, schema fragments, routes, or CLI subcommands from the same wheel.
  Feature isolation is therefore a runtime concern — the active entity
  registry (`src/cadastre/modules/registry.py`) and `modules.yaml`
  (`src/cadastre/modules/config.py`) — not a packaging concern. Extras name
  optional *dependencies*; they never name optional *behavior*.
  `pyproject.toml`'s `manifest` extra exists to install collector
  dependencies (currently none — see MANIFEST.md F01) and installs nothing
  that activates the module by itself.
- **An inactive module's data is never silently dropped.** A database that
  contains module-owned rows records that requirement durably, and an
  application without the module refuses to open it rather than reading a
  catalog that looks smaller than it is (MANIFEST.md F03).
- **The base surface is a byte-for-byte compatibility contract, not a
  convention.** With no `modules.yaml`, or every module disabled, JSON
  Schema, OpenAPI, CLI help, HTTP routes, MCP tool list, and `brief`
  output must be identical to a build that never heard of the module.
  `tests/test_model.py` enforces this today for the schema; the same bar
  applies to every other enumerated surface.

---

## 5. The Broker

### 5.1 The problem

An agent needs to reach a host, push a branch, read a secret, trigger a pipeline.
Today that means either long-lived credentials sitting in `~/.ssh` and the
environment, or a human pasting something. The first is unsafe, the second is not
autonomous.

### 5.2 Core principle: the agent never holds a credential

The agent asks the Broker to *perform a scoped action*:

```
cadastre exec --target nodeb --role deploy -- docker ps
```

The Broker resolves the target through the Catalog, evaluates grants, mints a
short-lived credential, runs the command in a subprocess with the credential
injected out-of-band, streams stdout back, and writes an audit record.

The agent receives **output**. It cannot exfiltrate a credential it was never
given, and cannot reuse access after the call returns.

Injection rules:
- Never in `argv` — visible in `ps`, in shell history, in agent transcripts.
- Never in an environment variable the agent's own process can read.
- Preferred: file descriptor, ephemeral credential file with `0600` in a
  per-invocation tmpdir removed on exit, or an agent-forwarding socket.

### 5.3 Capability descriptors live in the Catalog

The Catalog says *that a path exists and its shape* — never the credential. This
is safe in a public repository:

```yaml
# bundle/hosts/nodeb.yaml
access:
  - kind: shell
    via: ssh-ca          # broker backend id
    role: deploy         # what the broker will mint
    reachable_from: [tailnet-0]
```

### 5.4 Short-lived credentials, not stored ones

The mechanism the whole design leans on. Nearly every relevant system has a
mint-on-demand path:

| System class | Mechanism | Typical TTL |
|---|---|---|
| SSH | Certificate from an SSH CA | 5 min |
| Git forge | App/installation token | 1 h |
| Cloud | OIDC federation | 15–60 min |
| VPN | Ephemeral auth key / OAuth client | single-use |
| Secret manager | Machine identity, scoped lease | minutes |

**Do not build the cryptographic core.** Teleport, Vault, Boundary, step-ca, and
SPIFFE/SPIRE already do short-lived scoped credentials properly; rolling your own
is how OSS security tools acquire CVEs. The Broker is a thin, uniform façade over
whichever the operator already runs — plus a deliberately unglamorous local
fallback for operators running none.

The novel contribution is the **intent → target → role → backend** mapping, and
that is Catalog data.

### 5.5 Grants: pre-authorized scopes, approved by explicit catalog policy

This is the actual answer to "without user interaction." You do not approve each
action. You approve **classes of action ahead of time**, in a reviewable file:

```yaml
# bundle/policy/grants.yaml
- principal: agent
  role: deploy
  targets: [tag:app-tier]
  actions: [shell.read, container.restart]
  deny: [tag:persistent-data]
  ttl: 5m
```

Non-interactive operation is safe exactly to the degree the boundary was drawn
deliberately and in advance. That is a pull request, not a prompt.

Evaluation: **default deny**; explicit `deny` beats explicit `allow`; no
wildcard principal; grants may narrow but never widen at request time.

### 5.6 Structured refusal

An out-of-scope request returns a machine-readable denial:

```json
{"ok":false,"decision":"deny",
 "reason":"role 'deploy' lacks 'dns.write' on zone example.com",
 "grant_id":null,
 "escalation":"add an allow rule to catalog policy and review/export it"}
```

This matters more than it sounds. A vague failure is precisely what makes an
agent start improvising — hunting for keys in `~/.ssh`, trying a different
credential path, working around the boundary it just hit. A specific refusal with
a named escalation path gets reported cleanly instead.

### 5.7 Audit

Append-only, one record per brokered action, written before the action's result
is returned:

```json
{"ts":"...","principal":"agent","action":"shell.read","target":"nodeb",
 "role":"deploy","grant_id":"g-004","decision":"allow",
 "backend":"ssh-ca","cred_ttl":"5m","cmd_hash":"sha256:...","exit":0}
```

Command *hash*, not command text — the command may embed values. Never log
credential material, and never log plugin responses wholesale.

This log is what makes anyone willing to grant an agent standing access at all.

---

## 6. Threat model

Stated plainly because people will point this at production on day three.

**Assumed adversary.** Someone who obtains the catalog repository; a compromised
or manipulated agent; a malicious plugin.

| Asset | Control |
|---|---|
| Catalog contents | Assumed disclosed. Contains no credentials by construction. |
| Collector credentials | Read-only, scoped per capability. Compromise ⇒ topology disclosure, not mutation. |
| Broker backend auth | Local only, never in git, never returned to a caller. |
| Secret values | Never transit the query layer. Broker-injected only. |
| Action authority | Default deny; grants explicit and version-controlled; all use audited. |

**Prompt-injection.** Collector output is untrusted input — a container label or
DNS TXT record is attacker-controllable text that lands in a model's context.
Observed data is rendered as inert data, never as instruction, and never
interpolated into a position where it could read as a directive.

**Plugin trust.** Plugins are code the operator installed; they run with the
operator's privileges and Cadastre does not sandbox them. Documented, not
mitigated. The two forms differ and the difference is not hidden: an
out-of-process plugin is isolated by the process boundary, while an **in-process
Python plugin has none** — installing one is equivalent to installing a library
and deserves the same suspicion. Prefer out-of-process for anything not written
by the operator.

**Catalog writes.** The write path (§2.3) reaches `catalog.sqlite3` and nothing else.
It cannot mutate an upstream system, and every write is gated by schema, plugin
validation and `check`, stamped with a principal, and committed atomically — so the
worst case is a bad transaction, visible in audit history and recoverable from
backup, not a changed estate. `observed.sqlite3` is not writable through it at all.

**HTTP adapter.** Optional, and off by default for a reason: it is the only
component that listens. Loopback and read-only by default; non-loopback binds and
write endpoints are separate explicit opt-ins; bearer tokens map to principals
supplied by server configuration. It holds no upstream credentials, so compromising it yields the
    catalog — already assumed disclosed — and the ability to write audited transactions as its
principal. It never fronts the Broker unless separately enabled; enabling that
turns it into a remote execution service and should be treated as such.

**Explicit non-guarantee.** Cadastre does not defend against an operator writing an
over-broad grant. `targets: ["*"]` with `actions: ["shell.exec"]` is a working
configuration and a bad one. `cadastre check` warns on grants that are wildcard in
both target and action.

---

## 7. Implementation notes

- **CLI-first, always.** MCP, HTTP, the GUI, CI, and humans consume the same
  core. A feature that exists only in one adapter is untestable and hostage to
  that protocol's churn.
- **Python.** Rationale: the MCP SDK is first-party and mature here; YAML,
  JSON-Schema, and subprocess handling are stdlib-or-adjacent; and the plugin
  protocol is subprocess-and-JSON, which is Python's natural register. Plugin
  authors are far more likely to write Python or shell than Rust, and plugin
  contribution is the adoption path — which is also why a plugin may be a single
  self-contained Python file.
- **The cost of that choice, stated plainly.** We lose the single static binary.
  This matters in exactly one place — getting the tool onto an odd host — so the
  design routes around it rather than pretending it away:
  - **Nothing needs to be installed on observed hosts.** A router, a hypervisor,
    or an appliance is reached *from* the collector host via the `exec` plugin
    over SSH. The remote side runs `vtysh`/`pvesh`/`docker ps` and returns JSON.
  - **The application stack has an explicit host.** A deployment host runs the
    SQLite-backed API/MCP services and, when released, the GUI. Collector jobs
    may run there or on a separate trusted worker. Agent runtimes reach the
    stack through native remote MCP or the Cadastre bridge; they do not need a
    catalog checkout.
  - **Distribution is `uv`/`pipx`,** plus a `zipapp` build for constrained
    environments. Pin a floor of Python 3.11.
  - **Dependencies stay minimal and boring.** Every added dependency is a
    deployment problem on the collector host. Adapter dependencies (the HTTP
    server, the OpenAPI emitter) are optional extras, never core requirements.
- **Truth is a local database; services are application surfaces.** The CLI is
  still a process that starts, answers, and exits, and SQLite adds no database
  daemon. A deployed stack runs the HTTP/API and MCP services against the
  durable SQLite files; the GUI reaches them over the authenticated API.
- **Deterministic output.** Same inputs ⇒ byte-identical output. Diffable,
  cacheable, testable.
- **Placement logic is plain constraint filtering.** At homelab-to-small-fleet
  scale no solver is warranted. It is deterministic code with unit tests — never
  ask the model to do the placement arithmetic, only to describe requirements and
  react to the resulting plan.
