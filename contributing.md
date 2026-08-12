# Contributing to Cadastre

Cadastre records infrastructure facts and policy; it does not operate the
estate. Contributions should preserve that boundary, keep the core deterministic
and vendor-neutral, and make trust and provenance visible to the people and
agents consuming its output.

## Changes enter through pull requests

`main` is protected. Do not push directly to `main` or force-push any shared
branch. Start from an up-to-date `main`, create a focused branch, and open a
pull request back to `main`:

```bash
git fetch origin main
git switch -c <type>/<short-description> origin/main
# make and test the change
git push -u origin <type>/<short-description>
```

Useful branch prefixes include `feat/`, `fix/`, `docs/`, `test/`, and
`refactor/`. Keep a pull request reviewable: one coherent change, a useful
description of the behavior or contract being changed, and tests or fixtures
that demonstrate it. Resolve review feedback with new commits while the PR is
under review; do not rewrite a shared branch unless the reviewers have agreed.

Pull requests must target `main`. A PR is ready to merge when the required CI
checks pass, review comments are resolved, and the description explains any
intentional behavior, schema, output, or compatibility changes. Merging to
`main` builds and verifies the reviewed revision; it does not publish it. A
`vX.Y.Z` tag is the release boundary — see "Cutting a release" below.

## Cutting a release

`src/cadastre/__init__.py` holds the single source of truth for the version.
Every other copy is derived from it or checked against it by
`tests/test_version_identity.py`, so bumping it alone will fail the suite with
a precise list of what still disagrees.

1. Open a PR that bumps `__version__`, `version` in `pyproject.toml`,
   `version` in `ui/package.json`, and `application_version` in
   `src/cadastre/release-compatibility.json`. Bump
   `minimum_client_version` in the same file **only** on a genuine
   client-visible break — it is what makes an installed
   `cadastre-mcp-remote` warn on startup, and raising it casually trains
   operators to ignore the warning.
2. Add the release's section to `CHANGELOG.md` in the same PR.
3. Merge, then tag the merge commit `vX.Y.Z`. `scripts/release-gates.sh`
   requires the tag to equal `v` + the declared version and refuses to push or
   sign anything if it does not.
4. Cut a GitHub release for the tag whose body is that `CHANGELOG.md` section.
   This is what makes `releases.atom` a usable feed for operators and what
   Renovate attaches to consumers' bump PRs. Use a GitHub Security Advisory
   for a security release so it reaches consumers' existing alerting rather
   than only the feed.

## Before opening a pull request

Read [DESIGN.md](DESIGN.md) before changing the model, storage, authorization,
provenance, Broker boundary, or plugin contract. Read [TESTING.md](TESTING.md)
for the layer-specific test strategy. For the normal local gate, install the
development and MCP-server extras, then run:

```bash
uv sync --extra dev --extra mcp-server --extra manifest
ruff check src tests
ruff format --check src tests
mypy
pytest -q
cadastre --catalog examples/catalog fmt --check
cadastre --catalog examples/catalog check compose.production.yaml --kind compose
```

Run `mypy` bare, not `mypy src`: its configured `files` is `["src", "tests"]`,
which is what CI runs, and narrowing it locally lets test type errors through.

Also run focused tests for the code you changed. If you change the catalog
schema or generated output, update the corresponding schema/golden fixtures and
run the schema comparison used by CI:

```bash
cadastre --catalog examples/catalog schema > /tmp/schema.json
diff -u schema/catalog.schema.json /tmp/schema.json
```

That comparison runs against a catalog with no `modules.yaml`, and that is the
point: `schema/catalog.schema.json` is the **base** registry's rendering.
Optional modules (see [MANIFEST.md](MANIFEST.md)) contribute entity kinds,
routes, and MCP tools only to a catalog that enables them, and an enabled
catalog's schema is a runtime document that is never committed. If a change
makes enabling a module alter the committed schema, the golden `brief` output,
or the CLI help, the module is leaking into base behaviour — fix the registry
rather than the fixture.

If you find that a document promises behaviour the code does not have, open an
issue recording the gap rather than quietly narrowing the document.

If the MCP stdio surface is affected, install the MCP extra and run its SDK
lane as described in [TESTING.md](TESTING.md). Do not skip a failing test by
loosening a default-deny rule; fix the behavior or document an explicitly
reviewed contract change.

## Design and safety rules

- Cadastre is a map, not the territory. It must not deploy workloads, mutate
  DNS, VPNs, orchestrators, registries, or hosts, and plugins must not acquire
  estate-control side effects.
- Keep business logic in the core/application layer. Adapters translate and
  render; plugins collect through their declared contract.
- Preserve deterministic output and stable identities. Inject time and other
  environmental inputs where the design requires it; do not depend on ambient
  network access, host identity, or dictionary ordering in tests or output.
- Treat `stale`, `unverified`, and `contested` provenance as first-class. Do not
  hide it, silently promote it to trusted data, or route around exclusions
  returned by `context-for`.
- Keep authorization default-deny. A refusal is part of the API contract and
  should explain the safe next step.
- Do not edit generated observed data by hand. Use the supported gated catalog
  write operation when a declaration or annotation needs to change.

## Secrets and private estate data

Never commit tokens, passwords, private keys, real secret values, or
estate-specific hostnames, addresses, zones, repository lists, or capabilities.
Use protected environment variables/files for credentials. Keep examples
synthetic and use secret *references*, never secret values. Local reconnaissance
and audit material belongs in the ignored paths documented in `.gitignore`.

If a change could expose a credential or private estate fact, stop and remove it
from the branch before pushing. Report an already-exposed credential through the
appropriate security channel; do not put it in an issue or pull request.

## Documentation and review notes

Document user-visible changes, migration implications, new configuration, and
security or trust assumptions in the same PR. Changes that alter what an agent
reads—rendered text, JSON, MCP results, errors, exclusions, or provenance—need
reviewed fixtures or tests so the diff makes that contract change explicit.

Please keep commits small and descriptive. A good PR description answers:

1. What changed and why?
2. Which interfaces, schemas, or output contracts changed?
3. What checks were run, and what was intentionally not run?
4. Does the change affect release artifacts or deployment configuration?

Questions and proposed design changes are welcome in a PR discussion. For
security issues, use the repository's security reporting process rather than a
public issue.
