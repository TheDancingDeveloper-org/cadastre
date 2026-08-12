# What Cadastre does

Cadastre is an address book and rulebook for infrastructure.

Most teams have important operational facts scattered across people's memory,
configuration files, dashboards, DNS, CI systems, secret stores, and deployment
tools. That makes ordinary questions unexpectedly hard:

- What machines and services do we have?
- Where is a service allowed to run?
- Which things may be public, and which must stay private?
- Is a port already in use?
- Which deployment pipeline owns this application?
- Is this fact current, or is it an old assumption?

Cadastre gathers those facts into one queryable catalog, applies the policies
you define, and explains what it knows and how trustworthy each answer is.

## A simple example

Suppose you want to add an internal application that needs a GPU and a database.
Instead of searching several systems or guessing, a person or AI agent can ask:

```text
cadastre context-for "an internal application that needs a GPU and PostgreSQL"
```

Cadastre can answer with:

- suitable hosts and why they qualify;
- rejected hosts and the rule that excluded each one;
- the network and naming conventions that apply;
- conflicts such as an occupied port; and
- the source, age, and trust state of the supporting facts.

The caller can then create a Compose, ingress, or pipeline file and ask Cadastre
to check it before anyone deploys it.

## What goes into the catalog

Cadastre can record:

- hosts, networks, services, endpoints, and domains;
- repositories, pipelines, and deployment relationships;
- policy such as exposure levels, naming rules, and permissions;
- references to secrets, but never secret values; and
- observations collected read-only from external systems.

The live catalog uses SQLite. YAML and JSON bundles provide a portable,
reviewable way to import, export, and test catalog data.

## How the information stays useful

Declared facts and observed facts are kept distinct. Cadastre does not silently
decide that one side is correct when they disagree. Every answer can report
three independent trust signals:

| Signal | What it means |
|---|---|
| `stale` | The observation is older than its allowed lifetime. |
| `unverified` | No configured source can currently confirm the declaration. |
| `contested` | Sources that can see the fact disagree. |

This matters because a neat-looking answer is not necessarily a reliable one.
A human chooses whether to accept an observation, keep a declaration, or
acknowledge a disagreement until a review date.

## What Cadastre deliberately does not do

Cadastre changes the map, not the estate.

- It does not deploy applications or schedule workloads.
- It does not change DNS, VPNs, hosts, containers, or orchestrators.
- It does not reconcile reality to match a declaration.
- It does not choose which side of a disagreement is correct.
- It does not store or return secret values.

Catalog edits are validated, attributed, audited, and written only to the
catalog. Collectors have a one-way, read-only role: they report observations
without gaining a path to mutate the systems they inspect.

## An optional extra: tracking the work itself

Cadastre answers *where does this belong, and may it run here*. An optional
module called **Manifest** answers a second question — *what is outstanding,
what matters most, and did it ship* — using the same provenance and trust
machinery.

It records work items and the initiatives they belong to, links them to issues
and pull requests on a forge, and collects task lists from planning files, local
checkout state, and forge issue/PR state. It then reports where those disagree:
a required issue that no longer exists, a forge item nothing tracks, a checkbox
ticked in a file while the pull request is still open. As everywhere else in
Cadastre, it reports the disagreement and stops. It never edits a planning file,
never closes an issue, and never writes to a forge.

The module is off unless a catalog turns it on, and a catalog that has not
turned it on behaves exactly as though it did not exist.

## Main ways to use it

Cadastre exposes the same application behavior through several interfaces:

- the `cadastre` command-line tool for local use and administration;
- an ordinary HTTP API for applications and the browser GUI;
- an MCP server designed for AI agents; and
- a remote bridge for agent products that support only local stdio MCP servers.

All interfaces sit over the same catalog and policy logic. The GUI and agents do
not open the SQLite database directly.

## Why it is AI friendly

Cadastre is designed to give AI agents useful context without giving them an
infrastructure control plane.

- MCP, HTTP, CLI JSON, JSON Schema, and OpenAPI provide structured interfaces.
- Answers include provenance and uncertainty instead of presenting guesses as
  facts.
- `context-for` returns the facts and exclusions relevant to one decision.
- `check` evaluates a proposed artifact before it is committed or deployed.
- Stable error shapes let agents distinguish missing, invalid, and ambiguous
  requests.
- [`AGENTS.md`](AGENTS.md) tells coding agents how to consume and contribute to
  the project safely.

The result is a bounded source of context: an agent can make a better proposal,
while deployment authority remains elsewhere.

## Product components

The repository contains the SQLite catalog, HTTP API, Streamable HTTP MCP
server, remote stdio bridge, read-only collector/plugin system, browser GUI, and
release tooling. The major boundaries and component relationships are described
in [ARCHITECTURE.md](ARCHITECTURE.md); the rationale behind them is in
[DESIGN.md](DESIGN.md).
