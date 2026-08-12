# Built-in plugin operator guide

This guide documents the plugins distributed with Cadastre as implemented
today. It is for operating collectors, not authoring a new plugin; see
[PLUGINS.md](PLUGINS.md) for the general contract. All integrations use the
one-shot JSON-over-stdio collector protocol, run on the collector host, and
only use read endpoints. They never deploy, reconcile, or mutate an upstream
system. `cadastre plugins` shows what is registered; `cadastre sources` shows
configured commands and their handshakes; `cadastre collect --dry-run` safely
tests a source without writing evidence.

All examples below use placeholder-only endpoints and credential *names*.
Set values in the protected collector environment. Freshness defaults to 24
hours unless `freshness` in `plugins.yaml` overrides a method; prefer one hour
for endpoints/DNS and a day for inventory. A failed or incredible empty source
keeps the prior snapshot and is marked stale. Sources retain their registered
plugin name for identity matching and their configured source id for provenance.

| Plugin | Upstream | Methods | Entity output | Auth | Suggested freshness |
|---|---|---|---|---|---|
| `static` | Cadastre catalog | inventory, network, endpoint, DNS, secret, VCS, CI reads | host, service, network, endpoint, domain, secret, repo, pipeline, ci_executor, ci_pool | none | catalog revision |
| `exec` | operator command | configured methods | entities or `extra` | command-owned | command-dependent |
| `ingress-caddy` | Caddy admin API | `endpoint.list` | endpoint | optional admin API auth via header config | 1 hour |
| `forge-forgejo` | Forgejo/Gitea API | `vcs.repos`, `secret.list` | repo; secret names in `extra` | read-only API token | 1 day |
| `forge-github` | GitHub API | `vcs.repos`, `ci.pipelines`, `ci.status` | repo, pipeline, ci_executor, ci_pool; runner evidence, optional workflow selectors and job history in `extra` | read-only metadata token; org self-hosted-runner read for `ci.status`; repo `Contents`/`Actions: read` for the optional sections | 1 day; 15 min for `ci.status` |
| `ci-woodpecker` | Woodpecker API | `ci.pipelines`, `secret.list` | pipeline; secret names in `extra` | read-only API token | 1 day |
| `secrets-infisical` | Infisical API | `secret.list` | secret and secret names | read-only workspace token | 1 day |
| `orchestrator-gitops` | local GitOps checkout | `inventory.list` | service | filesystem read | 1 hour |
| `dns-cloudflare` | Cloudflare API | `dns.zones`, `dns.records` | domain | Zone:Read token | 1 hour |
| `vpn-tailscale` | Tailscale API | `network.list`, `network.members` | network, host | devices read token | 1 day |
| `hypervisor-proxmox` | Proxmox API | `inventory.list` | host | `PVEAuditor`-equivalent token | 1 day |
| `registry-crates` | crates.io | `inventory.list` | `extra.published` | none | 1 day |
| `work-markdown` † | local Markdown files | `work.findings` | markdown_finding | filesystem read | 1 hour |
| `work-git` † | local Git checkout | `work.repo-state` | repo_checkout | filesystem read | 15 min |
| `work-github` † | GitHub API | `work.items` | forge_item | read-only issues/PR token, separate from `forge-github`'s | 1 hour |

† Registers only while the Manifest module is enabled in `modules.yaml`. With
the module off these three are absent from `cadastre plugins` and their entity
kinds do not exist in the active model, so configure them only alongside the
module. See [USING-CADASTRE.md](USING-CADASTRE.md#optional-modules).

`exec` is a substrate bridge, not a dedicated upstream integration. `static`
is the other substrate: it reads catalog-authoritative intent; the integrations
produce source-authoritative evidence. Entity output participates in schema and
drift; `result.extra` is retained verbatim as uninterpreted evidence and does
not drive policy. Except for `static`, built-ins declare source authority,
default `id` identity, reflected core fields, catalog annotations (`tags`,
`notes`), `on_contest: exclude` for reflected fields, and `empty_expected: true`
— except `hypervisor-proxmox`, whose `host` kind declares `empty_expected:
false` because a hypervisor cannot truthfully report an empty inventory (see
below).
The current built-in declarations do not narrow coverage; configure `coverage`
per source when an organisation/project/region source is not global.

## Shared configuration and troubleshooting

Every source has `id`, `plugin`, `command`, `methods`, `timeout_seconds`
(default `30`), `enabled` (default `true`), `config`, `params`, `env`, and an
optional per-kind `coverage`. `plugin` defaults to `id`; `methods` defaults to
Cadastre's default probe list when omitted, so specify it explicitly. `command`
is argv, never shell. `token_env` and similar `*_env` keys name a protected
environment variable; they do not contain a value. HTTP collectors use
`endpoint` and, where supported by the common HTTP helper, `token_env` and
`auth_scheme`. Check a bad configuration with `cadastre sources`, then run
`cadastre collect --source ID --dry-run`; inspect stale/error provenance with
`cadastre sources` and `cadastre drift`. Fixture transforms are tested in
[`tests/test_collectors.py`](tests/test_collectors.py); no test contacts an
estate.

## `static`

**Substrate.** Use it to expose catalog-authoritative entities through the same
observed pipeline without an upstream. Methods are `inventory.list`,
`network.list`, `network.members`, `endpoint.list`, `dns.records`,
`secret.list`, `vcs.repos`, and `ci.pipelines`; they emit their corresponding
core entity kinds. Its only config key is `catalog` (default `.`), a Cadastre
data directory; it needs no environment variables or network access. It reads
the local SQLite/catalog bundle and has no known data gap beyond fields not
declared in the catalog.

```yaml
- id: static
  plugin: static
  command: [cadastre-plugin-static]
  methods: [inventory.list, network.list, endpoint.list]
  config: {catalog: .}
```

## `exec`

**Substrate bridge.** Use an existing read-only command before writing a named
integration. Its methods are exactly `config.commands` keys; `shape: entities`
(default) requires command JSON with `entities`, while `shape: raw` retains the
JSON under `result.extra.<method>`. `timeout_seconds` defaults to 60 inside the
bridge. Config keys are `commands`, `shape`, and `timeout_seconds`; `env` may
pass required environment variables such as `SSH_AUTH_SOCK`. The bridge itself
does not open a network connection, but its configured command may; use a
read-only command and account. It deliberately cannot infer entity schema,
identity, authority, or an upstream credential scope.

```yaml
- id: router
  plugin: exec
  command: [cadastre-plugin-exec]
  methods: [network.list]
  env: [SSH_AUTH_SOCK]
  config:
    shape: raw
    commands: {network.list: [ssh, router.example.invalid, show-interfaces-json]}
```

## `ingress-caddy`

**Integration.** Use it to inventory running Caddy routes for collision/check
evidence. `endpoint.list` GETs `/config/`, emitting endpoint `address`,
`network` (default `edge-net`), `fronted_by` (default `ingress`), port 443, and
upstream notes. Config keys: `endpoint` (required), `network`, and
`ingress_service`; a protected read-only admin endpoint is required but no
specific environment variable is mandated. It never invokes Caddy's mutation
API. It does not reliably map an upstream dial target to a Cadastre service.

```yaml
- id: ingress
  plugin: ingress-caddy
  command: [cadastre-plugin-ingress-caddy]
  methods: [endpoint.list]
  config: {endpoint: https://caddy.example.invalid, network: edge-net, ingress_service: ingress}
```

## `forge-forgejo`

**Integration.** Use it for Forgejo/Gitea repositories and organisation secret
names. `vcs.repos` paginates GET `/api/v1/repos/search` into repo IDs, remotes,
mirroring and archive tags; `secret.list` GETs organisation Actions-secret
names into `extra.secret_names` only. Config keys: `endpoint` (required),
`token_env` (required for private access; minimum read-only repo/org-secret
metadata scope), `forge` (default `forge-selfhosted`), `mirror_to`,
`mirror_from` (default `forge-public`), `org` (required for secrets), and
`store` (default `forge-secrets`). No secret values or write endpoints are
used. It does not enumerate repository-level secrets.

```yaml
- id: forge
  plugin: forge-forgejo
  command: [cadastre-plugin-forge-forgejo]
  methods: [vcs.repos, secret.list]
  config: {endpoint: https://forge.example.invalid, token_env: CADASTRE_P_FORGE_TOKEN, org: example, store: forge-secrets}
```

## `forge-github`

**Integration.** Use it for GitHub mirror repositories, Actions workflows, and
self-hosted runner inventory. `vcs.repos` lists an organisation or user;
`ci.pipelines` additionally GETs each repository's workflow list, emitting repo
remotes and pipeline `repo/system/file`. Config: `endpoint` (required — there
is no default; use `https://api.github.com`), `token_env` (read-only metadata
scope), exactly one of `org` or `user`, `forge` (default `forge-public`),
`mirror_from` (default `forge-selfhosted`), and `system` (default `ci-public`).
It only GETs APIs and does not collect Actions secrets, runs, branch
protection, or write workflows.

```yaml
- id: github
  plugin: forge-github
  command: [cadastre-plugin-forge-github]
  methods: [vcs.repos, ci.pipelines]
  config: {endpoint: https://api.github.com, token_env: CADASTRE_P_GITHUB_TOKEN, org: example}
```

### `ci.status`: self-hosted runners

`ci.status` GETs organisation runner registrations and runner groups and
returns both halves of one observation: `ci_executor` and `ci_pool` entities,
which are the neutral view policy reads, and `result.extra.ci_status`, which is
the vendor evidence it must not. Nothing in placement, check, drift, or
rendering branches on the second. Requires `config.org`; a
user-scoped source is refused, because runner groups do not exist for a user
account. Use one source per organisation: separate provenance and separate
authorisation scope are the operationally significant part, so their evidence
is never merged.

```yaml
- id: github-runners-example
  plugin: forge-github
  command: [cadastre-plugin-forge-github]
  methods: [ci.status]
  timeout_seconds: 30
  config: {endpoint: https://api.github.com, token_env: CADASTRE_P_GITHUB_RUNNERS_TOKEN, org: example}
```

**Permissions.** Organisation `Self-hosted runners: read` on a fine-grained PAT
or GitHub App installation credential, and nothing else — repository metadata
access is not sufficient. `Actions: read` is a *repository* permission needed
for workflow inventory (`ci.pipelines`), not for this method. Prefer a separate
credential from the repository/workflow source so each has an accurate scope,
independent failure state, and least privilege; prefer a short-lived App
installation credential where practical.

**Neutral entities and field ownership.** An executor's id is built from
GitHub's stable numeric id (`example-executor-7`), so a rename is the same
executor. Reflected from upstream: `system`, `scope`, `pool`, `status`, `busy`,
`ephemeral`, `os`, `version`, `selectors`. Owned by the catalog and never set
by the collector: `runs_on` and `capabilities`. A registration cannot establish
which host it runs on, and a label routes a job rather than installing a
toolchain — declare both in `declared/ci-executors/`, where drift compares them
against independent evidence. Status values other than `online`/`offline`
become `unknown` in the entity; the raw value survives in `extra.ci_status`.

**Freshness.** A source takes its TTL from its first collected method, so
runner status needs its own source rather than a place after `vcs.repos` in a
daily one. Set `freshness.ci.status: 900`, or rely on that default.

**Envelope.** `extra.ci_status` carries `schema`, `provider`, `scope`,
`complete`, `runners`, `runner_groups`, and `counts`. Each runner reports `id`,
`name`, `os`, `status`, `busy`, `ephemeral`, optional `version`, `labels`
(`name` plus GitHub's `read-only`/`custom` type), and `group_ids`. Each group
reports `id`, `name`, `visibility`, `allows_public_repositories`, `runner_ids`,
and — only when `visibility` is `selected` — `selected_repositories` as GitHub
repository ids with `owner/name` for display. Identity is the numeric id, so a
renamed runner is the same runner. Ordering is normalised, so an upstream that
shuffles its pages does not read as a change.

### `ci.pipelines`: optional workflow selectors and job history

Two further sections are available on the repository/workflow source. Both are
off by default, both need repository permissions the runner source does not
have, and both answer routing and utilisation questions rather than inventory
ones.

`config.workflow_selectors: true` downloads each listed workflow file and
records `jobs.<id>.runs-on` as `extra.ci_selectors`. It needs repository
`Contents: read`. Only routing is read: steps, scripts, `if` conditions, names,
and comments are not parsed and never followed. A selector is one of four
things and never a fifth — `labels`, `group`, `indeterminate` when it contains
a `${{ ... }}` expression, or `absent` for a job that delegates to a reusable
workflow. A file that is too large or does not parse is recorded as `unparsed`
and marks the section incomplete: "this workflow selects nothing" is a fact,
and not one the collector established. Only paths under `.github/workflows/`
are fetched, because the path arrives from an upstream listing and is upstream
text.

`config.job_history` records which executor actually ran a job, as
`extra.ci_job_history`. It needs repository `Actions: read`. Repository-by-
repository run enumeration multiplies API calls, so every axis is bounded and
`repositories` is required — job history is opt-in per repository, never
organisation-wide because a flag was set.

```yaml
- id: github-workflows
  plugin: forge-github
  command: [cadastre-plugin-forge-github]
  methods: [ci.pipelines]
  config:
    endpoint: https://api.github.com
    token_env: CADASTRE_P_GITHUB_WORKFLOWS_TOKEN
    org: example
    workflow_selectors: true
    job_history:
      repositories: [example/notes-api]
      lookback_hours: 24
      max_runs: 50
```

`lookback_hours` defaults to 24 (max 168) and `max_runs` to 50 (max 500). When
a bound is reached the section is marked `complete: false` with an explicit
reason; a request that fails outright fails the method, so the previous
evidence is kept and marked stale. Retained per job: repository, run, job and
attempt ids, timestamps, status, conclusion, reported runner and group name,
and routing labels. Not retained: logs, step output, annotations, environment
values, artifacts, or display names. This evidence can show that a job used a
runner. It cannot show that the machine was clean, or that the runner is where
anyone thinks it is.

**Limits, and what it will not tell you.** Every list is fully paginated;
exceeding the page ceiling fails the method rather than publishing a truncated
inventory as complete, and any partial failure fails the whole method so the
previous evidence is kept and marked stale — a half-collected organisation must
not make missing group membership look authoritative. `status` and `busy` are
what GitHub knew at collection time: `busy: false` is not spare capacity, queue
depth, health, or a scheduling guarantee. A label is a routing selector, never
evidence that a toolchain is installed. Nothing here places a runner on a
Cadastre host — a runner name, label, OS, or IP is not a host, and that
relation must be declared and independently verified. Job history, workflow
`runs-on` selectors, logs, and registration material are out of scope; the
method cannot register, remove, relabel, reconfigure, or run anything, and the
only paths it can build are the four GET templates in `CI_STATUS_PATHS`.

## `ci-woodpecker`

**Integration.** Use it for Woodpecker pipeline files and secret-name inventory.
`ci.pipelines` GETs `/api/user/repos`; `secret.list` GETs `/api/secrets` or an
organisation path. It emits pipeline `repo/system/file` and
`extra.secret_names`. Config: `endpoint`, `token_env` (read-only API token),
`system` (default `ci-selfhosted`), `org`, `store` (default `ci-store`), and
`ref_prefix` (default empty). Only GETs occur; trigger, logs, values, and
secret mutation are intentionally absent.

```yaml
- id: ci
  plugin: ci-woodpecker
  command: [cadastre-plugin-ci-woodpecker]
  methods: [ci.pipelines, secret.list]
  config: {endpoint: https://ci.example.invalid, token_env: CADASTRE_P_CI_TOKEN, store: ci-store}
```

## `secrets-infisical`

**Integration.** Use it to inventory secret references without values.
`secret.list` GETs `/api/v3/secrets/raw` and emits secret `id/ref/store` plus
rotation date and `extra.secret_names`; a value-shaped payload is refused.
Config: `endpoint`, `token_env` (minimum read-only workspace-secret metadata),
`workspace_id` (required), `environment` (default `prod`), `path` (default
`/`), `store` (default `secrets-manager`), and `ref_prefix` (default `/`). It
has no mutation path and deliberately does not report values, ciphertext, or
secret permissions.

```yaml
- id: secrets
  plugin: secrets-infisical
  command: [cadastre-plugin-secrets-infisical]
  methods: [secret.list]
  config: {endpoint: https://secrets.example.invalid, token_env: CADASTRE_P_SECRETS_TOKEN, workspace_id: replace-me}
```

## `orchestrator-gitops`

**Integration.** Use it to observe a local GitOps checkout without making it
Cadastre declaration. `inventory.list` recursively reads Compose files and
emits one `service` entity per stack (id = the compose file's directory
name) — matching the catalog's own altitude, since compose-service-level
emission produced compose-service-name noise the declared catalog was never
curated to converge with. Each compose file's own service/container
inventory (name, host, exposure, repository) is retained in full under the
`x-orchestrator.compose_services` attribute. Config: `path` (required
readable checkout), `host_from` (`directory` only when directory names
really are hosts), and `repo`. It needs filesystem read access, no
environment variable or network access, and never runs an orchestrator. It
does not read live controller state, secrets, or infer a host from
directories by default.

```yaml
- id: gitops
  plugin: orchestrator-gitops
  command: [cadastre-plugin-orchestrator-gitops]
  methods: [inventory.list]
  config: {path: /srv/cadastre/gitops-checkout, repo: operations}
```

## `dns-cloudflare`

**Integration.** Use it for Cloudflare zones and DNS records. `dns.zones` and
`dns.records` GET paginated zone/record APIs and emit modelled domains for A,
AAAA, CNAME, TXT, MX, SRV and NS records; zone names are also retained in
`extra.zones`. Config: `endpoint` (default `https://api.cloudflare.com`),
`token_env` (minimum Zone:Read), and `zones` (optional exact allowlist). No DNS
write endpoint is called. SOA and unmodelled records are deliberately omitted;
TXT content remains inert data.

```yaml
- id: dns
  plugin: dns-cloudflare
  command: [cadastre-plugin-dns-cloudflare]
  methods: [dns.records]
  config: {token_env: CADASTRE_P_DNS_TOKEN, zones: [example.invalid]}
```

## `vpn-tailscale`

**Integration.** Use it for Tailscale device membership. Both methods GET the
tailnet device list, producing private network `network` (default `vpn-0`) and
host IDs, roles (default `server`), reachability, and device tags. Config:
`endpoint` (default `https://api.tailscale.com`), `token_env` (minimum device
read), `tailnet` (default `-`), `network`, and `role`. It does not call local
daemon or write APIs, and it intentionally does not emit device key, IP, ACL,
or route state.

```yaml
- id: vpn
  plugin: vpn-tailscale
  command: [cadastre-plugin-vpn-tailscale]
  methods: [network.list]
  config: {token_env: CADASTRE_P_VPN_TOKEN, tailnet: example.invalid, network: vpn-0}
```

## `hypervisor-proxmox`

**Integration.** Use it for Proxmox node/guest inventory. `inventory.list`
GETs `/api2/json/cluster/resources` and emits host resource facts and placement
relations. Config: `endpoint` (required), `token_env` (minimum
`PVEAuditor`-equivalent), `auth_scheme` (default `PVEAPIToken=`), `verify_tls`
(default `true`), `hypervisor`, and `network`. It has no mutation request and
does not collect VM config, console, backup, storage, or guest credentials.

```yaml
- id: proxmox
  plugin: hypervisor-proxmox
  command: [cadastre-plugin-hypervisor-proxmox]
  methods: [inventory.list]
  config:
    endpoint: https://proxmox.example.invalid:8006
    token_env: CADASTRE_P_PVE_TOKEN   # holds user@realm!tokenid=uuid
    verify_tls: false                 # see "certificates" below
```

**Credential format.** `CADASTRE_P_PVE_TOKEN` holds the token *value* only —
`root@pam!cadastre=<uuid>`. The `PVEAPIToken=` prefix comes from `auth_scheme`
and is a complete prefix rather than an RFC 7235 scheme name, so no space is
inserted after it; the shared HTTP helper decides that from the scheme's final
character. Grant the token `PVEAuditor` on `/` and **disable privilege
separation**, or assign it an equivalent ACL — see "an empty inventory" below
for why the difference is not cosmetic.

**Certificates.** Proxmox ships a self-signed certificate, so the documented
configuration above sets `verify_tls: false`. Without it the collector reports
`unreachable`, which reads as "the host is down" rather than "I refused its
certificate". This is a genuinely disabled protection, not a formality: it is
acceptable here because the collector reads only, over a LAN, with a read-only
credential, and it is the weaker option. Prefer installing a CA-signed
certificate on the Proxmox host, or trusting its CA on the collector host, and
leaving verification on.

**An empty inventory is a fault, not an estate.** A Proxmox token with
privilege separation enabled and no ACL is answered `200 {"data": []}` rather
than `403` — you are permitted to ask, you simply cannot see anything. This
plugin therefore declares `empty_expected: false` for `host`: a node always
reports at least itself, so zero hosts is evidence about the credential and
never about the estate. `collect` keeps the previous evidence and marks the
source stale instead of recording a successful empty result, which is what
stops `drift` announcing every declared host as `missing`.

## `registry-crates`

**Integration.** Use it to retain public crates.io publish facts until a
package entity exists. `inventory.list` GETs each configured crate endpoint and
retains name, latest version, update date, and non-yanked versions under
`extra.published`; it emits no core entity. Config: `endpoint` (default
`https://crates.io`) and `crates` (required list). It needs no credentials,
sends an identifying user-agent, and performs no write. It deliberately omits
crate ownership, downloads, advisories, and unpublished local state.

```yaml
- id: crates
  plugin: registry-crates
  command: [cadastre-plugin-registry-crates]
  methods: [inventory.list]
  config: {crates: [example-crate]}
```

## Manifest work collectors

The next three plugins belong to the optional Manifest module and are
registered only while it is enabled. They collect what is outstanding and
whether it shipped; none of them writes to a forge, a file, or a repository.
The `Work` capability's methods are `work.items` → `forge_item`,
`work.findings` → `markdown_finding`, `work.repo-state` → `repo_checkout`, and
`work.revision-checks` → `revision_check`. The fourth has no shipped collector;
do not name it in `methods:`.

## `work-markdown`

**Integration.** Use it to collect GitHub-style task-list items out of planning
files that would otherwise drift unnoticed. `work.findings` reads an explicit
list of files under one configured root and emits a `markdown_finding` per task
line, retaining path, line, checkbox state, the enclosing heading, and the task
text — and nothing else. Surrounding prose, code fences, link targets, and file
bodies are discarded. Text is retained as inert data and rendered escaped.

Config keys: `repo` (required), `root` (default `.`), `files` (required,
non-empty), `max_files` (default 100), `max_file_bytes` (default 256 KiB), and
`max_total_bytes` (default 4 MiB). There is no workspace-wide default and no
directory walk: a file is scanned because it was named.

```yaml
- id: work-markdown
  plugin: work-markdown
  command: [cadastre-plugin-work-markdown]
  methods: [work.findings]
  config:
    repo: org/repo
    root: /srv/checkouts/repo
    files: [PLAN.md, docs/BACKLOG.md]
```

**Identity, and why a marker matters.** Without a marker a finding's id is
`repo:path:line:text`, so editing the wording of a task is honestly a
remove/add pair rather than an update. To keep a finding stable across edits,
and to join it to declared work, add an HTML comment on the task line:

```markdown
- [ ] Ship the thing <!-- cadastre-work item=WORK-12 forge=github:org/repo#41 -->
```

Either attribute may be omitted; unknown attributes are not accepted. A finding
with neither a work marker nor a forge reference is reported as
`unlinked-markdown-finding` by `cadastre manifest drift` — which is the point:
an untracked checkbox in a planning file is exactly the drift this collector
exists to surface.

**Limits are failures, not truncations.** A symlinked configured file, a path
that resolves outside `root`, a file over `max_file_bytes`, a set over
`max_total_bytes`, a list over `max_files`, or a decoding failure fails the
method. The previous evidence is retained and the source is marked stale; a
partial scan is never published, because absence would then look like
completion.

## `work-git`

**Integration.** Use it for local checkout state — what revision a working copy
is actually on, and whether it is clean. `work.repo-state` emits one
`repo_checkout` per explicitly configured checkout with `head_revision`,
`branch` (absent when detached), `dirty`, `upstream`, and `worktree`.

It reads Git's on-disk files — `HEAD`, `packed-refs`, `config`, reflog, and the index —
rather than invoking `git`. That is what makes "no hooks, no fetch, no push, no
network" an enforceable property instead of a promise, and it is why the
`manifest` extra pins no Git library.

Config: `checkouts`, a non-empty list of `{id, repo, path}` mappings. No
credentials, no network, no environment variables.

```yaml
- id: work-git
  plugin: work-git
  command: [cadastre-plugin-work-git]
  methods: [work.repo-state]
  config:
    checkouts:
    - {id: repo-main, repo: org/repo, path: /srv/checkouts/repo}
```

`dirty` covers both tracked modifications and untracked files, and retains
neither diffs nor file contents. Linked worktrees (a `.git` file pointing at a
`gitdir:`) are supported and report their own root as `worktree`.
`tracking_ref_matches` is emitted only with a readable local remote-tracking
ref; it compares revisions without proving ahead or unpushed work.
`last_head_change` is the latest valid `.git/logs/HEAD` timestamp and is
omitted when the reflog is absent or expired. Ahead/behind counts were dropped
because they require a commit-graph walk.

## `work-github`

**Integration.** Use it for issue and pull-request evidence. `work.items` fully
paginates each configured repository's issues and pull requests and emits one
`forge_item` per record with a stable `forge:repo:kind:ref` id, plus `title`,
`state`, `draft`, `created_at`/`updated_at`, `head_revision`,
`merge_revision`, and `url`. Bodies, comments, patches, logs, and reactions are
never retained. A merged pull request reports `state: merged`, which is what
distinguishes it from one that was merely closed.

Config: `repos` (required, non-empty, `owner/name`), `forge` (default
`github`), `max_repos` (default 100), `max_pages` (default 10), plus the shared
HTTP helper's `endpoint`, `token_env`, and `auth_scheme`.

```yaml
- id: work-github
  plugin: work-github
  command: [cadastre-plugin-work-github]
  methods: [work.items]
  config:
    endpoint: https://api.github.com
    token_env: CADASTRE_P_WORK_GITHUB_TOKEN
    repos: [org/repo, org/other]
```

**A second plugin, not a fifth method on `forge-github`.** That plugin's
credential is scoped for repository and runner inventory on a one-day TTL; work
collection wants a different scope, a shorter TTL, and its own failure state.
Give this source its own token — read access to issues and pull requests on the
named repositories, nothing more — and prefer a short-lived app token.

**Reaching a bound fails the method.** Exceeding `max_pages` raises rather than
returning what was fetched, because a truncated page set is indistinguishable
from a repository whose issues were closed. The prior evidence is kept and the
source goes stale.

**Known defect.** GitHub's REST API returns pull requests from the issues
endpoint too, and this collector does not filter them, so each pull request is
also emitted as a `forge_item` of kind `issue`. Expect spurious
`unlinked-forge-item` rows in `cadastre manifest drift` until that is fixed.
