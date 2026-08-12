# Cadastre

**Know what infrastructure you have, where things belong, and which rules apply.**

Cadastre is an address book and rulebook for your servers, services, networks,
domains, repositories, and deployment policies. It gives people and AI agents
one place to ask questions such as:

- Where can this application run?
- Must it stay private, or may it be public?
- Is the port or hostname already in use?
- Which pipeline and repository own it?
- Is the answer current, verified, or disputed?

Cadastre brings those facts together, explains where each answer came from, and
checks proposed deployment files against your rules.

It is a **map, not a control panel**. Cadastre does not deploy workloads, change
DNS or VPNs, operate containers, or reveal secret values. Updating Cadastre
updates the map only; it never changes your live infrastructure.

## Why use it?

Infrastructure knowledge is usually split across configuration files,
dashboards, tools, and people's memory. That works until someone new—or an AI
assistant—needs to make a safe decision.

Cadastre turns that scattered knowledge into answers with provenance and clear
warnings when information is stale, unverified, or contested. Instead of
guessing, a caller gets suitable choices, rejected choices, the rules involved,
and the evidence behind them.

Read [What Cadastre does](PRODUCT-GUIDE.md) for a plain-language walkthrough and
example.

## Quickstart with Docker

Create an empty persistent catalog with the project image:

```bash
docker run --rm \
  -v cadastre-data:/var/lib/cadastre \
  ghcr.io/thedancingdeveloper-org/cadastre:main \
  init --data-dir /var/lib/cadastre --empty
```

Then confirm it is ready:

```bash
docker run --rm \
  -v cadastre-data:/var/lib/cadastre \
  ghcr.io/thedancingdeveloper-org/cadastre:main \
  status --data-dir /var/lib/cadastre
```

The `main` image follows the latest successful main-branch build. Production
deployments should pin the signed immutable digest published by the release
pipeline. If the package is not visible to your Docker client, authenticate to
`ghcr.io` or [build the image locally](DEPLOYMENT.md#user-paths-published-artifacts-and-self-build).

To explore without creating a live catalog, clone the repository and query its
fictional example:

```bash
uv tool install cadastre
cadastre --catalog examples/catalog brief
cadastre --catalog examples/catalog context-for \
  "an internal application that needs a GPU"
```

Continue with [Using Cadastre](USING-CADASTRE.md) for catalog setup, common
commands, collectors, and remote access.

## Optional: the Manifest work register

Cadastre also ships an opt-in module that answers *what is outstanding, what
matters most, and did it ship*. It records work items, initiatives, and links
to forge issues and pull requests, collects local Markdown task lists, Git
checkout state, and GitHub issue/PR evidence, and reports where the declaration
and the forge disagree — without ever writing to a forge.

It is **off unless a catalog enables it** in `modules.yaml`. With no such file,
the schema, OpenAPI document, CLI help, HTTP routes, and MCP tool list are
identical to a build that never heard of it. See
[Using Cadastre](USING-CADASTRE.md#optional-modules) to turn it on and
[MANIFEST.md](MANIFEST.md) for the design.

## Built for AI agents

**This repository and product are explicitly AI friendly.**

Cadastre offers structured MCP, HTTP, CLI JSON, JSON Schema, and OpenAPI
interfaces. Answers carry provenance and trust state, while `context-for`
returns the facts and exclusions relevant to one decision and `check` reviews a
proposed artifact before deployment. [`AGENTS.md`](AGENTS.md) provides
repository-level instructions for coding agents.

This design gives an agent useful ground truth without turning Cadastre into an
infrastructure control plane.

## Learn more

| Guide | What it covers |
|---|---|
| [What Cadastre does](PRODUCT-GUIDE.md) | Plain-language concepts, examples, boundaries, and trust signals |
| [Using Cadastre](USING-CADASTRE.md) | Installation, catalogs, commands, collection, and remote access |
| [Architecture](ARCHITECTURE.md) | Components, data ownership, and interface boundaries |
| [Deployment](DEPLOYMENT.md) | Containers, supported topologies, persistence, and operations |
| [Agent clients](AGENT-CLIENT.md) | Native MCP and the remote stdio bridge |
| [Plugin authoring](PLUGINS.md) | The read-only plugin contract and a single-file example |
| [Built-in plugins](BUILTIN_PLUGINS.md) | Configuration, credentials, outputs, and limits for shipped integrations |
| [Manifest module](MANIFEST.md) | The optional, default-off work register: design, entity kinds, and delivery plan |
| [Security](SECURITY.md) | Network identity and secure deployment profiles |
| [Design](DESIGN.md) | Detailed rationale and non-goals |
| [Testing](TESTING.md) | Test layers and local quality gates |
| [Examples](examples/README.md) | Fictional catalog and client configurations |
| [Contributing](contributing.md) | How changes are reviewed and merged through pull requests |

## License

Cadastre is available under the [MIT License](LICENSE).
