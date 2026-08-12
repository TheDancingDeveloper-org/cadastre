# Testing

How each layer is tested, and the end-to-end harness that drives the SQLite,
HTTP/API, MCP, GUI-contract, and agent-client surfaces against test data.

| Doc | Answers |
|---|---|
| `DESIGN.md` | How it works, and why it refuses certain things |
| **`TESTING.md`** | **How each layer is proven, up to a running server** |

---

## Principles

**No network, ever.** A suite requiring network access is a suite nobody runs,
and one that silently reaches a live estate is worse. Collectors are exercised
against recorded responses and fixture processes; servers bind loopback on an
OS-selected port.

This is currently a convention stated in `tests/conftest.py` and held by
construction, **not** an assertion: no autouse socket or subprocess guard
exists. The `no_network` fixture in the table below is a design target. Until it
exists, a new collector test that reaches a live service fails nothing.

**Determinism is a product property, so it is a test property.** Same inputs ⇒
byte-identical output (DESIGN §7). The clock is injected, never read; so are
identity of the running user, the working directory, and any ordering that would
otherwise leak dict iteration.

**Refusals are contracts.** A refused `add` on a source-authoritative entity, a
denied grant, a rejected write — each needs a test as much as the success path.
The refusal *text* is part of the contract too: it names a next step, and an
agent acts on that text.

**Never the operator's catalog.** Write-path tests run against a throwaway
SQLite data directory, created and destroyed per test.

---

## The layers

```
L6  scenario     the full loop over HTTP/API, MCP, and the browser GUI
L5  parity       CLI ≡ MCP ≡ HTTP/API, byte for byte
L4  servers      running MCP and HTTP/API servers against SQLite fixtures
L4G GUI          a real browser consuming the live HTTP/API server
L4C agent       native remote MCP client and Cadastre bridge
L3  cli          command surface, exit codes, golden output
L2  core         the estate fixture, loaded and queried
L1  contract     plugin conformance kit, in-process and stdio
L0  unit         model, policy, placement, identity, serialisation
```

Every layer has implemented coverage. Some of the time-stepped scenarios below
remain the direction for deeper temporal coverage; they are not claims about the
current full-stack fixture.

## The full-stack E2E harness

[`scripts/e2e_stack.py`](scripts/e2e_stack.py) copies the anonymised example
catalog into a temporary directory, initializes its SQLite stores, and starts
the real HTTP/API and Streamable HTTP MCP servers on loopback using
OS-selected ports. It never reads deployment endpoints, credentials, or an
operator catalog.

[`scripts/run-e2e.sh`](scripts/run-e2e.sh) owns the server lifecycle and passes
only the generated loopback origins to Playwright. The tests in
[`ui/tests/e2e/full-stack.spec.ts`](ui/tests/e2e/full-stack.spec.ts) verify:

- health/readiness and the OpenAPI surface;
- an ordinary `/check` API call;
- the same synthetic catalog and provenance through HTTP and MCP; and
- the live HTTP result rendered in a real Chromium browser, with provenance
  visible and no direct SQLite path in the GUI.

Run it locally after installing the development and MCP dependencies plus the
Playwright Chromium browser:

```bash
sh scripts/run-e2e.sh
```

GitHub Actions runs this script on self-hosted runners for pull requests; it is
the only CI that executes for this repository. Failure diagnostics are retained
under `.ci-artifacts/e2e/`.

---

## Deeper scenario fixture design

The current full-stack lane uses an isolated copy of `examples/catalog`. Deeper
temporal scenarios should use **synthetic estates** under `tests/estate/`. One
directory is one estate; a suite may use several.

```
tests/estate/reference/
  catalog.sqlite3           # the catalog under test
    hosts/ services/ networks/ topologies/ policy/
  upstream/                 # what each plugin's source would return
    t0/  tailscale.json  dns.json  orchestrator.json  ci.json  …
    t1/  …                # a later observation — same shape, changed facts
    t2/  …
  artifacts/                # proposed files for `check`, valid and invalid
  expected/                 # golden output, per command, per adapter
  estate.yaml               # variant metadata: which t-steps exist, what each asserts
```

Three properties earn their keep:

**Time steps are first-class.** `t0/t1/t2` are successive observations of the
same estate. Contested state, `first_seen`, acknowledgement expiry and flapping
detection (DESIGN §2.9) are all *temporal* — they cannot be tested by a single
snapshot, and a harness that only supports one is the reason those features would
ship broken.

**Estates are deliberately awkward.** The reference estate carries dual-homed
repos, a repo with two CI systems and no declared authority, an entity nothing
can verify, a source that legitimately returns zero records, and a container
label containing instruction-shaped text. Every one of those is a defect class
found in the real estate rather than an invention.

**Expected output is committed.** Golden files, reviewed in the diff. A rendering
change that alters what an agent reads should be visible in a pull request.

---

## L1 — Plugin contract conformance

A reusable kit, shipped as `cadastre.testing`, that any plugin author can run
against their own plugin. Third parties get the same conformance guarantee the
in-tree plugins get; that is the whole point of DESIGN §4.1.

Given a plugin and a directory of recorded upstream responses, the kit asserts:

- **Handshake.** `plugin.info` declares capabilities and entity types; every
  declared entity type has an `authority`, field classes, and an `x-*` schema.
- **Identity stability.** The same upstream record yields the same identity
  across runs, across a rename of any non-identity field, and across process
  restarts. Then the inverse: two genuinely different records never collide.
  *This is the single highest-value test in the kit* — an unstable identity
  function produces phantom divergence on every collect.
- **Schema conformance.** Emitted entities validate against the core schema, and
  every `x-*` block validates against the fragment the plugin shipped.
- **No vendor leakage.** No core field carries a vendor noun.
- **Authority enforcement.** `add`/`delete` on a source-authoritative type is
  refused, and the refusal names the upstream system and a next step. Intent and
  annotation writes on the same entity are accepted.
- **Degradation.** Bad JSON, non-zero exit, timeout, stdout pollution, partial
  response, and an unreachable upstream each mark the source stale — never crash,
  never serve old data as current.
- **Empty handling.** A zero-record response is a finding where
  `empty_expected: false`, and silent where it is true.

The kit runs each plugin **both ways** where applicable — in-process and over
stdio — and asserts identical results. The core must not be able to tell.

---

## L4 — Running servers

The shipped server surfaces are exercised in-process against temporary SQLite
catalogs. The migration shadow additionally drives the HTTP and Streamable MCP
servers over loopback and records CLI/HTTP/MCP parity. The stdio subprocess
lane is covered by `tests/test_mcp_stdio_sdk.py` when the optional `mcp` extra
is installed; the default dependency set skips that test because MCP is not a
core dependency. Run that lane with:

```text
uv run --extra mcp pytest -q tests/test_mcp_stdio_sdk.py
```

### MCP over stdio

A pytest fixture spawns `cadastre mcp` as a real subprocess against an estate
fixture and drives it with the **official MCP client SDK** — not a hand-rolled
JSON-RPC harness. Hand-rolling tests the harness's idea of the protocol; the SDK
tests the protocol.

```python
@pytest.fixture
def mcp(estate):                      # estate = a prepared tests/estate/<name>
    with mcp_stdio_client(["cadastre", "--catalog", estate.path, "mcp"]) as c:
        yield c
```

Asserted:

- `initialize` succeeds and the tool list matches the declared surface exactly —
  no tool appears that the CLI cannot do, and none is missing.
- Each tool's input schema rejects a malformed call *before* reaching the core.
- Tool results carry provenance, and staleness is visible in the rendered text —
  not only in a JSON tail (DESIGN §2.10). A regression that buries the stale
  banner is a real defect and needs a test that fails on it.
- A `check` failure comes back as a structured tool result, not a transport
  error.
- The process exits cleanly on stdin close, and leaves no child behind.
- Nothing but protocol frames on stdout — a stray `print()` in the adapter
  corrupts the stream, which is a class of bug that only a real client catches.

### HTTP/API

A fixture starts `cadastre serve` bound to loopback on an ephemeral port, polls
readiness with a timeout, tears it down, and asserts the port is released.

```python
@pytest.fixture
def api(estate, tmp_path):
    with serve(estate, bind="127.0.0.1:0", tokens={"agent": "t-test"}) as base:
        yield ApiClient(base, token="t-test")
```

Asserted:

- **OpenAPI conformance.** Every route appears in the generated spec; every
  response validates against its declared schema. Property-based fuzzing over
  the spec (schemathesis-style) on the read surface.
- **Docs cannot drift.** The spec emitted by `cadastre schema --openapi` matches
  the one served at `/docs`, and both match the entity JSON Schema — same
  generator, asserted equal.
- **Defaults hold.** Unauthenticated read on loopback works; write without a
  token is refused; a non-loopback bind requires the explicit flag. A test that
  fails if a default ever loosens.
- **Principals.** A write over HTTP lands in the SQLite audit log attributed to the token's
  principal, and an unknown token is refused without leaking whether the
  principal exists.
- **Statelessness.** Restart the server mid-suite; every subsequent assertion
  still holds. Nothing accumulates in the process.
- **No Broker by default.** Broker routes 404 unless separately enabled.

### Agent-client lane

The server-side MCP implementation is not the same thing as an agent's MCP
client. Test both supported consumption modes against a fixture server:

- a native remote MCP client connects directly to the authenticated
  Streamable HTTP `/mcp` endpoint; and
- `cadastre-mcp-remote` runs in the agent environment, speaks stdio locally,
  and forwards to `/mcp` without opening a local catalog fallback.

The clean-install test must verify package/extra installation, endpoint
configuration, TLS/hostname validation, authentication denial, tool discovery,
provenance, and unavailable-endpoint behavior. It must never contact an estate
endpoint or require a catalog checkout.

### GUI contract lane

The implemented Playwright lane builds and serves the released browser artifact
shape, points its runtime configuration at the temporary HTTP/API server, and
verifies live data and provenance rendering. Static UI tests separately cover
API-only write-review behavior. Authentication profiles and richer
stale/contested rendering remain additional scenarios, while the current E2E
gate proves the browser has no direct database path and that it consumes the
same catalog as MCP.

---

## L5 — Adapter parity

The cheapest high-value test in the whole plan, and the one that keeps DESIGN
§3.4's "adapters contain no logic" honest.

For every question in a table of representative queries, ask it three ways — CLI
`--json`, MCP tool call, HTTP endpoint — and assert the payloads are **identical**
after stripping transport envelopes. The migration shadow implements this
networked parity gate for all 28 Q-H01–Q-T04 contracts; its report is evidence
for the shadow catalog only and does not establish live-estate truth.

Any divergence means logic has leaked into an adapter. That is the failure this
test exists to catch, and it will catch it early, because leakage starts small.

**Transport parity, the other failure mode.** `tests/test_transport_parity.py`
guards a different leak: an operation that exists on some transports and not
others, rather than one that answers differently. It walks the registered MCP
operations (read and write) over stdio, Streamable HTTP, and the remote bridge
and asserts equivalent success/error envelopes, plus an inventory assertion
that every HTTP route has either an MCP operation or a named exclusion. R09
shipped exactly this bug twice — Streamable HTTP never got the Manifest tools,
and the bridge never proxied them either — and nothing failed CI; this is the
regression guard for that class.

---

## L6 — Scenarios

Full-loop tests over a time-stepped estate. Each is a narrative, and each asserts
a property the design claims.

**S1 — Deploy correctly, first attempt.** `brief` → `context-for` → write a
Compose file → `check` → fix → `check` passes. Asserts the core policy and that
the exclusion reasoning names the deciding constraint.

**S2 — Divergence appears and ages.** Collect `t0` (agreed) → `t1` (a DNS record
now points at a decommissioned host). Assert the entity becomes `contested` with
`first_seen` at t1; collect `t2`; assert `first_seen` is unchanged and the age
has grown. This is the test that would have caught the design's own weakest
point — divergence age is meaningless if a collect resets it.

**S3 — Resolution, all three paths.** From S2's contested state: `accept
observed` writes through the gate and returns to `agreed`; a second entity is
`acknowledged --until` and reverts to `contested` once the clock passes it; a
third is left contested and stays so. Assert no code path resolves anything on
its own.

**S4 — Contest changes an answer.** A contested port map with
`on_contest: exclude` removes a host from `context-for`, with a dated reason in
the exclusion list. A contested owner annotation changes nothing.

**S5 — Refused write.** `add` a VPN node; assert refusal, assert the message
names the upstream system and the annotate alternative; then annotate the node
successfully. Assert the refusal left the database revision and audit log unchanged.

**S6 — Unverified is not stale.** An entity no configured plugin can see is
reported `unverified`, is not reported `stale`, and is not reported `contested`.
Asserts the three axes stayed separate (DESIGN §2.9).

**S7 — Flapping.** A field oscillating across `t0..t3` is reported as flapping
and is *not* offered for `accept`.

**S8 — Prompt injection.** A container label containing instruction-shaped text
renders as inert data through CLI, MCP and HTTP. Run at L5 parity, because the
adapter is exactly where escaping gets forgotten.

**S9 — Topology.** A service deployed by following a `deployment_topology`;
then the topology's node is decommissioned in `t1` and the topology is reported
as drifted rather than silently followed.

**S10 — Cold start.** Remove the `observed.sqlite3` database, re-run `collect` from
snapshots, assert byte-identical query output and preserved divergence age
(DESIGN §2.11).

---

## Fixtures and helpers

This table is the **target** harness that goes with the L6 estate fixture, not
an index of what `tests/conftest.py` exports today. What exists now is
`example_catalog`, `catalog_copy`, `session`, `fixture_plugin`, the pinned
`NOW` clock, `console_script`, and `assert_golden`; the rest are named here so
the shape is agreed before they are built.

| Helper | Does |
|---|---|
| `estate(name, step="t0")` | Materialises an estate into a temporary SQLite data directory |
| `collect_to(step)` | Runs collection against a given time step's recorded upstreams |
| `frozen_clock` | Injected time; scenarios advance it explicitly |
| `mcp_stdio_client(argv)` | Spawns the MCP server, yields a real SDK client |
| `serve(estate, …)` | Spawns the HTTP/API server on an ephemeral port, waits ready |
| `remote_mcp_client(endpoint, …)` | Connects a native client to Streamable HTTP MCP |
| `bridge_client(endpoint, …)` | Spawns the remote stdio bridge and yields a client |
| `assert_parity(question)` | Asks CLI/MCP/HTTP/API, asserts identical payloads |
| `no_network` | *Not implemented.* Intended autouse socket/subprocess guard; loopback allowed, everything else fails |
| `unchanged(estate)` | Asserts a refused write left revision and data unchanged |

---

## Optional modules

A module is tested twice: once for what it does when enabled, and once for
being invisible when it is not. The second is the harder contract and the one
with a bytewise enforcer.

**Default-off is a byte comparison, not a convention.** `tests/test_model.py`
asserts the checked-in `schema/catalog.schema.json` equals `render_schema()`,
and CI repeats it as a `diff -u`. The committed file is the *base* registry's
rendering; an enabled catalog's schema is a runtime document and never that
artifact. If enabling a module changes the committed schema, the golden
`brief` output, or the CLI help snapshots, the registry is leaking and the fix
is the registry, not the fixture.

**Activation is configuration, so tests configure it.** `tests/test_modules.py`
covers `modules.yaml` resolution from both legal locations, unknown module
names and non-boolean `enabled` as located configuration errors, and the
absent-file default of everything off. Manifest's own layers follow the same
split as the rest of this document: `test_manifest_model.py` (kinds, write
invariants, SQLite round trip, export), `test_manifest_projection.py` (the
cross-kind join and its seven categories), `test_manifest_queries.py` (the
read service), `test_manifest_collect_e2e.py` (collection end to end), and
`test_work_git.py` / `test_work_github.py` / `test_work_markdown.py` for the
collectors — the last three against fixtures on disk and recorded responses,
never a live forge or a real workspace.

**Dormant data is a storage test, not a module test.** `test_storage.py` covers
the durable `required_modules` marker: writing
a module-owned row sets it, and a build with that module disabled refuses
writes, export, and import rather than reading a catalog smaller than it is.
Physical backup and restore stay available and preserve every byte.

Run the module-enabled lane with its extra installed:

```bash
uv sync --extra dev --extra mcp-server --extra manifest
pytest -q
```

## CI lanes

Two lanes, because a suite that takes minutes on every push stops being run.

**Fast** — L0–L3 and L5. No process spawning beyond the CLI. Runs on every push,
and gates merges.

**Full** — adds L4 and L6: real server processes, OpenAPI fuzzing, multi-step
scenarios. Runs on pull requests and nightly. Marked `@pytest.mark.e2e` so it can
be selected or excluded locally.

Current evidence boundary: the repository has passing in-process HTTP/MCP
adapter tests, a passing official-SDK stdio subprocess test under the `mcp`
extra, and a passing networked migration shadow. It does not claim the full
time-stepped L6 estate fixture until those dedicated fixtures are added. This
distinction prevents transport parity from being mistaken for live canary or
rollback evidence.

The `check` CI gate (M7) stays separate from both: it runs against the repository's
own catalog, and a policy violation blocks the merge.

---

## Sequencing

The harness is built alongside the milestones it proves, not afterwards:

| Built with | Layer |
|---|---|
| M10 plugin contract | L1 conformance kit, in-process and stdio |
| M11 write path | throwaway-SQLite fixtures, refusal tests, S5 |
| M12 contested state | time-stepped estates, S2/S3/S4/S6/S7 |
| M13 topology | S9 |
| M14 SQLite | S10 |
| M15 HTTP API | L4 HTTP, OpenAPI conformance, L5 parity |
| M16 transport and identity contract | profile/configuration conformance and threat-model fixtures |
| M17 standard remote MCP transport | Streamable HTTP client/server interoperability and Host/Origin rejection |
| M18 direct HTTPS | certificate, hostname, internal-CA, and non-loopback plaintext rejection |
| M19 authentication profiles | scoped-token, mTLS, OAuth/OIDC, expiry, audience, and deny-case tests |
| M20 remote hardening | proxy trust, audit-before-result, limits, DNS rebinding, and security-check end-to-end tests |
| M21 client consumption | native remote client, bridge, clean-install, and failure-mode tests |
| GUI release | browser/API/auth/provenance contract and packaged artifact tests |

L4's MCP half is worth building **before** M15, against the adapter that already
exists. It is the only layer where a shipped feature is currently unproven, and
it costs little.
