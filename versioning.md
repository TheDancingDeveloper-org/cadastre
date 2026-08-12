# Version identity, update notification, and release channels

Status: P0 and P1 delivered; P2 partly delivered. See the Unreleased section of
[CHANGELOG.md](CHANGELOG.md) for what shipped — one true version enforced by
`tests/test_version_identity.py`, the `version` MCP tool on both transports, the
bridge's one-line skew warning, semver image aliases over an immutable digest,
and PyPI publication from the tag-gated release workflow.
Date: 2026-08-10 (plan), status revised 2026-08-11

> **Superseded in one respect, 2026-08-11.** This document refers throughout to
> `.woodpecker/production.yaml` as the authoritative release pipeline. It was
> not: Woodpecker had no `cadastre` repository, so that pipeline had never run
> and every image the estate deployed was hand-built outside it. The repository
> is single-homed on GitHub, and the gates now live in
> `.github/workflows/release-images.yml` (images) and `release-pypi.yml` (the
> wheel), both still calling `scripts/release-gates.sh`. Read every
> `.woodpecker/production.yaml` reference below as naming that gate script,
> which is unchanged; the analysis of what the gates do still holds.

Decision: make the version string true before building anything on top of it,
then give clients one authoritative thing to ask, then fix the release
channels operators subscribe to. In that order — P1 and P2 are worthless while
every build reports `0.1.0`.

## 1. Executive decision

Cadastre ships a self-contained server image and a separately-installed client
bridge (`cadastre-mcp-remote`). These have independent lifecycles, and MCP has
no mechanism for a remote server to push an update notice to a client. It
should not acquire one: the correct design goal is **version-skew tolerance**,
not update notification. The server stays authoritative and backward
compatible; a client that is behind keeps working and is *told once*.

That yields three deliverables:

1. **P0 — one true version.** `__version__` is a hardcoded constant that CI
   never bumps and that two of the three MCP surfaces do not even read. Every
   image ever published reports `0.1.0`. Fix the literal drift, make the
   release tag the gate, and add a test that keeps it fixed.
2. **P1 — one thing to ask.** Add a `version` MCP tool carrying
   `minimum_client_version`, and have the bridge check it once at startup and
   warn on stderr. This is the only client-facing notification channel that is
   honest about what MCP actually guarantees.
3. **P2 — channels operators can subscribe to.** The authoritative Woodpecker
   pipeline is already strong. The gaps are an *ungated second publish path*,
   the absence of any tag a watcher can track, and a documented client install
   path that does not exist.

Non-goals, explicitly: no in-application "check for updates" phone-home, no
auto-update, no lockstep client/server version requirement, and no change to
the `sha-*` immutable-tag rule.

## 2. Verified current state

### 2.1 Version identity is a constant with seven copies

`__version__ = "0.1.0"` at `src/cadastre/__init__.py:6` is the nominal source
of truth. Nothing in either pipeline derives or bumps it. The same literal is
independently repeated at:

| Location | Consequence |
|---|---|
| `src/cadastre/mcp/streamable.py:213` | The network `/mcp` endpoint — the one remote agents actually hit — reports a literal `0.1.0` in `serverInfo`, not `__version__`. |
| `src/cadastre/adapters/client.py:189` | `clientInfo.version` in the outbound MCP handshake. |
| `Dockerfile:9` | `org.opencontainers.image.version` label. |
| `Dockerfile.gui:12` | Same, for the GUI image. |
| `release-compatibility.json:2` | `application_version` in the attested compatibility document. |
| `scripts/release-metadata.sh:32` | `application_version` in the release metadata artifact. |
| `ui/package.json:4` | GUI package version. |

Plus the derived GUI artifact name `cadastre-gui-0.1.0.tar.gz`, hardcoded in
`.woodpecker/production.yaml` (`gui-package` step) and defaulted again in
`scripts/release-gates.sh`.

`src/cadastre/mcp/server.py:171` and `src/cadastre/mcp/remote.py:44` *do* read
`__version__`, which is what makes the drift invisible: stdio MCP is correct
and Streamable HTTP is not.

The release pipeline is tag-gated on `refs/tags/v*`
(`.woodpecker/production.yaml`) but nothing checks that the tag agrees with any
of the seven copies. A `v0.3.0` tag today produces an image labelled `0.1.0`
reporting `0.1.0` with an attested compatibility document claiming `0.1.0`.

### 2.2 MCP clients have nothing to ask

`/version` exists as an HTTP route (`src/cadastre/api/registry.py:137` →
`src/cadastre/adapters/http.py:351` → `HealthService.version()` at
`src/cadastre/application/health.py:24`), returning `{name, version}`.

It is **not** in `MCP_OPERATIONS` (`src/cadastre/api/registry.py:56`) and not in
`TOOLS` (`src/cadastre/mcp/server.py:156`), so no MCP client can call it. An
MCP client's only version signal is `serverInfo` from `initialize` — which per
§2.1 is a hardcoded literal, and which clients are not required to surface.

`release-compatibility.json` declares `catalog_format_version` and
`observed_format_version`, and `release-gates.sh` attests it to the image under the
`schema-compatibility/v1` predicate type. Nothing reads it at runtime, and no field in it
addresses client compatibility.

`cadastre-mcp-remote` (`src/cadastre/mcp/remote.py`) performs no version
negotiation beyond MCP `protocolVersion`
(`src/cadastre/mcp/streamable.py:190`). A bridge arbitrarily older than its
server starts silently.

### 2.3 The release pipeline is strong; the channels around it are not

`.woodpecker/production.yaml` + `scripts/release-gates.sh` already do
tag-gating, rootless OCI build, forbidden-runtime-path verification, container
and full-stack smoke, syft SBOM, sha256 checksums,
`cosign sign`, and three `cosign attest` predicates (SBOM, schema-compatibility,
SLSA provenance) for both backend and GUI images. `tests/test_release_workflow.py`
locks all of this down. This is good and needs no rework.

Three things sit outside it:

- **An ungated second publish path.** `.github/workflows/publish.yml` runs on
  every push to `main`, holds `packages: write`, pushes via
  `docker/build-push-action` to `ghcr.io/<owner>/cadastre:sha-<sha>` and
  `:main`, and then deploys to Komodo. It has no cosign, no SBOM, no
  `release-gates.sh`, and no tag gate. `test_mirror_ci_cannot_publish_or_deploy`
  asserts exactly these properties are absent — but it only reads
  `.github/workflows/ci.yaml`, so `publish.yml` slips past the guard entirely.
- **No tag a watcher can track.** `release-gates.sh` *requires* `*:sha-*` and
  rejects anything else. Renovate, Dependabot, Watchtower, and Komodo all track
  registry tags; none can do anything with `sha-<sha>` plus a floating `main`.
  There is no semver tag, and the only git tag in the repo is
  `pre-cadastre-knowledge-migration`.
- **The documented client install path does not exist.**
  `USING-CADASTRE.md` says `uv tool install cadastre`; `DEPLOYMENT.md` says
  "The Python package and `cadastre-mcp-remote` are also published". The
  pipeline builds the wheel and sdist, smoke-tests them
  (`scripts/package-install-smoke.sh`), and copies them into the release
  directory — but never pushes them to an index. `uv tool upgrade` therefore
  has nothing to upgrade from.

## 3. P0 — one true version

### 3.1 Bump mechanism

Keep the literal in `src/cadastre/__init__.py` as the single source of truth
and gate every other copy against it in CI. Explicitly **do not** adopt
`hatch-vcs` or any VCS-derived version: `Dockerfile` installs from a copied
source tree with no `.git` present, by design ("no VCS, shell tooling,
credentials, or host sockets"), so a VCS-derived version would either break the
image build or reintroduce VCS into the runtime layer.

A release is therefore: one PR bumping the literal and its gated copies, then a
`vX.Y.Z` tag that CI requires to match.

### 3.2 Tasks

1. **Fix the two source-level drifts.**
   - `src/cadastre/mcp/streamable.py:213` — use `__version__`.
   - `src/cadastre/adapters/client.py:189` — use `__version__`.
2. **Parameterise the image labels.** Add `ARG CADASTRE_VERSION` to
   `Dockerfile` and `Dockerfile.gui`, use it for
   `org.opencontainers.image.version`, and pass it from the `image` and
   `gui-image` steps in `.woodpecker/production.yaml` alongside the existing
   `CADASTRE_SOURCE_REVISION` / `CADASTRE_SCHEMA_VERSION` build args.
3. **Make `release-metadata.sh` take the version as required input.** Replace
   the `"application_version": "0.1.0"` literal with a required
   `CADASTRE_VERSION` environment variable, following the existing
   `${VAR:?message}` idiom in that script. Pass it from `release-gates.sh`.
4. **Derive the GUI artifact name.** Replace the literal
   `cadastre-gui-0.1.0.tar.gz` in the `gui-package` step and the
   `CADASTRE_GUI_ARTIFACT` default in `release-gates.sh` with a version
   variable, so a bump cannot silently produce a mismatched filename.
5. **Gate the tag.** In `scripts/release-gates.sh`, next to the existing
   `sha-*` tag checks, assert `CI_COMMIT_TAG` equals `v` +
   `application_version` from `release-compatibility.json`, and that this equals
   the version reported by the built wheel. A tag/version mismatch must fail
   the release, not produce a mislabelled artifact.
6. **Add `tests/test_version_identity.py`.** Assert that:
   - `cadastre.__version__` equals the `version` in `pyproject.toml`;
   - it equals `application_version` in `release-compatibility.json`;
   - it equals `version` in `ui/package.json`;
   - no version literal matching `\d+\.\d+\.\d+` appears anywhere in
     `src/cadastre/` other than `src/cadastre/__init__.py` (this is the
     regression guard for the `streamable.py` class of bug);
   - `Dockerfile`, `Dockerfile.gui`, and `scripts/release-metadata.sh` contain
     no such literal at all;
   - `.woodpecker/production.yaml` contains no such literal in the GUI
     artifact name.

### 3.3 Acceptance

Bumping `src/cadastre/__init__.py` and `pyproject.toml` and nothing else makes
`tests/test_version_identity.py` fail with a precise list of every file that
still disagrees. Tagging `v0.2.0` against a tree declaring `0.1.9` fails
`release-gates.sh` before anything is pushed or signed.

## 4. P1 — one thing to ask

### 4.1 Extend the compatibility document

Add to `release-compatibility.json`:

- `minimum_client_version` — the oldest `cadastre-mcp-remote` this server
  supports. Bumped only on a genuine client-visible break; this is the field
  the whole notification path hangs from.
- `release_url` — where a human goes to read what changed.

These flow into the existing schema-compatibility cosign attestation
for free, because `release-gates.sh` attests the file wholesale.

### 4.2 Widen `HealthService.version()`

`src/cadastre/application/health.py:24` currently returns `{name, version}`.
Return additionally `application_version`, `catalog_format_version`,
`observed_format_version`, `minimum_client_version`, and `release_url`, read
from the packaged compatibility document.

**Keep `name` and `version` present and unchanged.** The addition must be
purely additive: `/version` is in the GUI's generated route contract
(`ui/src/api/generated.ts`) and in `tests/test_adapters.py:263`, and
`scripts/generate-gui-types.py` regenerates from the OpenAPI contract. Run the
schema diff gate (`cadastre schema` vs `schema/catalog.schema.json`) and
regenerate GUI types as part of this change.

Ship `release-compatibility.json` inside the wheel so the running server can
read it — it is currently a repo-root file that the `Dockerfile` never copies.
Add it to the hatch wheel target and read it via `importlib.resources`, with the
constants in `src/cadastre/` as the fallback if absent.

### 4.3 Expose `version` as an MCP tool

The streamable transport builds its tool list by joining `MCP_OPERATIONS`
against `tool_server.TOOLS` (`src/cadastre/mcp/streamable.py:41`), so both
transports are covered by two small additions:

1. `Operation("version", "catalog.read")` — no arguments — in `MCP_OPERATIONS`
   (`src/cadastre/api/registry.py:56`).
2. A `version()` function in `src/cadastre/mcp/server.py` following the
   established `_answer(remote, local)` pattern: remote →
   `client.request(endpoint, "/version", token=token)`, local →
   `HealthService(_root()).version()`. Add it to the `TOOLS` tuple at
   `src/cadastre/mcp/server.py:156`.

Scope stays `catalog.read`; the `/mcp` endpoint is already gated by `MCP_SCOPE`
as a whole, and inventing a lower scope for one tool would complicate the
authorizer for no benefit.

### 4.4 Bridge startup check

In `src/cadastre/mcp/remote.py`, once per process at `build_server()`:

- call the remote `version` tool via the existing `_remote_tool` helper;
- compare `__version__` against `minimum_client_version`;
- if below, emit exactly one line to **stderr**.

Four constraints, each of which is a way this goes wrong if ignored:

- **stderr only, never stdout.** stdout is the MCP framing channel; a
  diagnostic written there corrupts the session. This is why the notice cannot
  simply be printed.
- **Never fail startup on skew alone.** A bridge that refuses to start on a
  cosmetic bump is strictly worse than a stale bridge. Fail closed only when
  the server reports the client as incompatible, not merely old.
- **Tolerate an older server.** A server predating §4.3 has no `version` tool
  and will return an error envelope. Catch it, skip the check, start normally.
- **Do not let it become a second failure mode for startup.** Any exception
  from the probe — network, auth, parse — is swallowed; the bridge's job is to
  proxy, and it must proxy even when it cannot introspect.

Message shape, with no token or endpoint in it:

```text
cadastre-mcp-remote 0.2.0 is older than this server's minimum supported
client 0.3.0. Upgrade with: uv tool upgrade cadastre
```

Add coverage to `tests/test_remote_bridge.py`: below-minimum warns once on
stderr and still starts; at-or-above minimum is silent; an old server without
the tool is silent and still starts; probe failure is silent and still starts.

### 4.5 Deferred: the `brief` upgrade notice

`brief` is called at the start of every agent session by design, which makes
its provenance block the highest-leverage place to surface an upgrade notice to
a *human* reading the agent transcript. It is deferred out of P1 because it
changes `tests/golden/brief.json` and `tests/golden/brief.txt` and puts
deployment-lifecycle noise into an answer about the estate. Revisit once §4.4
is in service and we know whether the stderr line is actually reaching anyone.

## 5. P2 — channels operators can subscribe to

1. **Close the ungated publish path.** Either delete the `publish` and
   `deploy-komodo` jobs from `.github/workflows/publish.yml`, or gate them on
   `refs/tags/v*` and route them through `scripts/release-gates.sh`. Given
   `.woodpecker/production.yaml` is authoritative and
   `test_production_workflow_has_no_implicit_deploy_step` asserts the
   authoritative pipeline never deploys, deletion is the coherent choice and
   the Komodo deploy should live in ops, not in the product repo.
   Then **widen the guard**: change `test_mirror_ci_cannot_publish_or_deploy`
   to iterate every file in `.github/workflows/` rather than reading
   `ci.yaml` alone. The current test passes only because it looks at the one
   file that was never the problem.
2. **Publish semver tags alongside the immutable one.** Keep the `sha-*`
   requirement exactly as it is. After `crane push` and digest capture in
   `release-gates.sh`, add `crane tag` for `X.Y.Z`, `X.Y`, and `latest` against
   the *same digest*, so the signed artifact is unchanged and watchers have
   something to track. Extend `tests/test_release_workflow.py` accordingly.
3. **Publish the Python package.** Push the already-built, already-smoke-tested
   wheel and sdist to PyPI from the tag-gated release step, using trusted
   publishing. Until this lands, `USING-CADASTRE.md` and `DEPLOYMENT.md` are
   describing a path that does not exist — if it is not going to land, correct
   both documents instead.
4. **Cut a release entry per tag** with a changelog. `releases.atom` on the
   GitHub remote is then a zero-maintenance feed for operators, and Renovate
   attaches the changelog to bump PRs automatically. Use GitHub Security
   Advisories for security releases so they reach consumers' existing alerting.
5. **Document the upgrade procedure** in `DEPLOYMENT.md` §7, which currently
   lists "schema migration and readiness behavior" as an operator obligation
   with no procedure, and referenced from `SECURITY.md:26` which mandates a
   backup before upgrades without saying what follows it: backup → pull new
   digest → `cosign verify` signature and attestations → compare attested
   `catalog_format_version` against the on-disk database → start → readiness →
   rollback trigger and procedure.
6. **Ship a Renovate example** under `examples/` for `compose.production.yaml`,
   so the digest-pinned deployment the project recommends is also
   mechanically updatable.

## 6. Sequencing

P0 is self-contained and should land first as one PR; nothing below it is
meaningful until the version string is true. P1 depends only on P0 §3.1 and
splits cleanly into a server PR (§4.1–4.3) and a bridge PR (§4.4), in that
order. P2 items are independent of each other and of P1 — but P2.1 is a
standing supply-chain hole and should not queue behind the version work.
