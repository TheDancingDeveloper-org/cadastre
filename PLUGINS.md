# Cadastre plugins

This is the authoring and integration contract for Cadastre plugins. The design
rationale lives in [DESIGN.md §4](DESIGN.md#4-plugins); this document describes
the interface implemented by the current code.

For the shipped integrations' configuration, credential scope, fixture tests,
limitations, and copyable source examples, see the
[built-in plugin operator guide](BUILTIN_PLUGINS.md).

The short version: **yes, a plugin can be one self-contained Python file**. A
local file can provide both:

- a `PLUGIN` declaration, loaded from `<data-dir>/plugins/*.py`; and
- an executable entry point that answers Cadastre's JSON-over-stdio protocol.

No package, base class, database code, server, or vendor SDK is required. The
file is still configured as a collector command in `plugins.yaml`; discovering
its declaration does not implicitly execute collection.

## 1. What is a plugin?

A plugin is a one-way, read-only integration that contributes facts about one
external system to Cadastre. It translates vendor-shaped input into Cadastre's
small, vendor-neutral entity model:

`host`, `network`, `service`, `endpoint`, `domain`, `secret`, `pipeline`,
`repo`, `ci_executor`, `ci_pool`, and `deployment_topology`.

A plugin has two related surfaces:

1. **Declaration.** It tells core its stable name and version, the capabilities
   it supports, the entity kinds it owns, how records are identified, which
   side owns each field, and how contested or empty results should be treated.
2. **Collection.** Cadastre invokes a configured command once per method. The
   command reads one JSON request from stdin and writes one JSON reply to
   stdout. Core validates and persists the returned observations.

```text
plugins/example.py -- PLUGIN declaration --> registry, write rules, drift policy
        |
        +-- configured command <--- plugins.yaml
                    |
                    +-- JSON/stdin -> collect -> observed.sqlite3 -> CLI/API/MCP/GUI
```

Core owns storage, schema parsing, provenance, drift comparison, write gates,
and every application surface. Plugin authors do not write SQLite or adapter
code.

Secret-store collectors report references and existence only. Uncontracted
store differences are non-actionable inventory. Explicit policy-scoped
replication contracts select the source/target relationships and reference
names that should produce actionable `secret-only-in` drift; see
[Using Cadastre](USING-CADASTRE.md#collect-observed-evidence).

A plugin is not:

- an API, MCP, CLI, or GUI adapter; adapters expose existing core behavior;
- an estate control path; plugins must not deploy, reconcile, mutate upstream
  systems, or return secret values;
- a way to add arbitrary entity kinds or core fields; those are model changes;
- a daemon; `cadastre collect` starts one process per configured method and the
  process answers once and exits.

## 2. When should you write one?

Start with the smallest integration that proves the data is useful.

| Need | Use |
|---|---|
| Facts maintained only in Cadastre | No integration, or the built-in `static` plugin |
| An existing command already emits suitable JSON | The built-in `exec` plugin |
| A short experiment or one-off appliance query | A shell script through `exec` |
| A repeated transform, upstream API, pagination, auth, or reusable integration | A plugin |
| A new way to consume Cadastre | An adapter, not a plugin |
| A fact required by placement or policy that does not fit the model | Propose a core model change |

Write a dedicated plugin when the integration has a stable source identity and
the translation is worth testing and reusing. A growing `exec` script is a good
signal that it should become a named plugin.

Keep one plugin bounded to one external integration and a coherent set of
entity kinds. A forge plugin may return repositories and pipelines because
they share an authority and lifecycle. A generic “all infrastructure” plugin
usually hides identity and ownership mistakes.

Do not write a plugin merely to introduce a vendor noun. Translate a Tailscale
node into a `host` and `network`, for example. Vendor data that is not yet part
of a core decision can be returned under `result.extra`; core retains it as
uninterpreted evidence.

## 3. The integration surface

### Registration and discovery

Cadastre discovers declarations from:

- a Python file in `<data-dir>/plugins/*.py`;
- an installed Python entry point in the `cadastre.plugins` group; and
- the built-in plugin declarations.

A local file exposes `PLUGIN`, `plugin_info`, or `info`. The value may be a
mapping, a `PluginInfo`, or a zero-argument callable returning either. `PLUGIN`
as a plain mapping is the simplest single-file form:

```python
PLUGIN = {
    "name": "python-hosts",
    "version": "1",
    "capabilities": ["Inventory"],
    "entities": [
        {
            "kind": "host",
            "authority": "source",
            "reflected": ["id", "role"],
            "intended": [],
            "annotated": ["tags", "notes"],
            "identity": ["id"],
            "attributes": {"type": "object", "additionalProperties": True},
            "on_contest": {"id": "exclude", "role": "exclude"},
            "empty_expected": False,
        }
    ],
}
```

The declaration fields mean:

| Field | Contract |
|---|---|
| `name`, `version` | Stable plugin identity and contract version |
| `capabilities` | Families such as `Inventory`, `Network`, `Endpoint`, `DNS`, `SecretRef`, `VCS`, `CI`, or `Topology` |
| `kind` | One existing Cadastre entity kind |
| `authority` | `source` when the upstream is truth; `catalog` when Cadastre is truth |
| `reflected` | Upstream-owned fields that catalog writes cannot replace |
| `intended` | Catalog-owned desired fields |
| `annotated` | Catalog-owned metadata on an entity, normally `tags` and `notes` |
| `identity` | Stable fields used to match observed and declared records |
| `attributes` | JSON-Schema declaration for namespaced `x-*` attributes |
| `on_contest` | Per-field `exclude`, `warn`, or `ignore` behavior |
| `empty_expected` | Whether a successful zero-record result is credible |
| `coverage` | Optional `ids` and/or `where` constraints that say which declared records this source can authoritatively report as absent |

Identity is the most important choice. Use an upstream ID or stable compound
key, never a display name that changes on rename or reboot. Two records with the
same identity are treated as the same subject during drift comparison.

An observation is always positive evidence, but a missing finding is only
valid where a source claims coverage. A plugin declaration can provide a broad
`coverage` predicate and an active source can narrow it further:

```yaml
coverage:
  secret:
    where:
      tags: [project-a]
```

This source may report any observed secret as undeclared, but it can report a
declared secret as missing only when that secret has the `project-a` tag. The
supported predicate is intentionally small: `ids` is a list of exact IDs and
`where` matches a scalar exactly or any member of a list-valued field. A source
with no coverage declaration covers every entity of each kind it reports.

A collector may also report its own coverage **in the method reply**, as
`result.coverage`, using the same `{kind: {ids, where}}` shape. Prefer this
whenever the answer depends on configuration, because the `plugin.info`
declaration cannot express it: that declaration is per-*plugin*, while several
sources routinely share one plugin with different projects, zones or
organisations, and only the method call receives `config`. A secret collector
reports the store it was pointed at; a DNS collector reports the zones it
actually enumerated, which is not always the zones that were requested.

Precedence is narrowest-wins and never broadens: an explicit `coverage:` on the
source in `plugins.yaml` overrides what the collector reported for that kind,
and the plugin declaration applies on top of both. A malformed `result.coverage`
is dropped with a warning rather than treated as empty — silently empty coverage
would restore exactly the over-claiming coverage exists to prevent.

Reported coverage is captured into the observation **when the collector runs**,
so changing it takes effect on the next `cadastre collect`, not immediately.

Getting this wrong is expensive in both directions. Too broad, and a source
reports every other source's records as missing — three secret projects each
declaring `store: secrets-manager` will each insist the other two projects'
secrets are absent. Too narrow, and a declared record falls outside every
source's scope, so nothing compares it and a genuinely missing one is never
reported at all. `cadastre drift` reports that second case separately, as
`unobservable` — "declared, but nothing looks for it" — because absence of
drift there means nobody looked, not agreement.

Importing an in-process declaration executes the file's top-level Python code.
Keep import time side-effect free: do not open the network, read credentials,
start threads, or collect data outside `main()`. Cadastre does not sandbox
plugins; installing or loading one requires the same trust as a Python library.

For a separately distributed Python package, expose the same declaration with:

```toml
[project.entry-points."cadastre.plugins"]
python-hosts = "my_package.plugin:PLUGIN"
```

### Source configuration

Registration makes a declaration visible. Configuration makes a source active
and tells the collector how to invoke it. Runtime configuration is
`<data-dir>/plugins.yaml`:

```yaml
freshness:
  default: 86400
  inventory.list: 3600

sources:
  - id: python-hosts
    plugin: python-hosts
    command: [python3, /opt/cadastre/plugins/hosts-from-python.py]
    methods: [inventory.list]
    timeout_seconds: 30
    coverage:
      host:
        where: {tags: [lab]}
    config:
      inventory_file: /var/lib/inventory/hosts.json
```

`id` identifies this configured source and its provenance. `plugin` selects the
registered declaration; it defaults to `id`. `command` is an argv list,
executed directly without a shell. `methods` is explicit so a plugin gaining a
new capability does not silently widen collection. `config` contains ordinary
configuration and may name credential environment variables. `params` contains
non-secret per-call parameters. `env` explicitly permits additional collector
environment variables.

Paths and environment variable names are deployment inputs. Cadastre performs
no shell expansion in `command`, so use reviewed argv words and deployment-owned
absolute paths.

### JSON-over-stdio protocol

Every configured method is a new, one-shot process. The process receives one
object on stdin:

```json
{
  "v": 1,
  "method": "inventory.list",
  "params": {},
  "config": {"inventory_file": "/var/lib/inventory/hosts.json"}
}
```

A successful reply is one object on stdout:

```json
{
  "v": 1,
  "ok": true,
  "result": {
    "entities": {
      "host": [{"id": "worker-01", "role": "container-host"}]
    }
  },
  "as_of": "2026-08-10T12:00:00Z",
  "warnings": []
}
```

`result.entities` is parsed against the core model. Observations may omit
non-identity fields they cannot know, but unknown entity kinds, unknown fields,
and wrong types are errors. `result.extra` may carry JSON that has no entity
representation; it is retained but core policy never branches on it.
`result.coverage` is the optional per-source scope described above.

Retained `extra` is readable through `cadastre observations` (and `/observations`,
the `observations` MCP tool, and the GUI), which lists it by source and key with
provenance and staleness attached. That presentation is generic: it never
interprets a plugin's fields. Two conventions make it more useful, and neither
is inferred when absent:

- **`complete`.** A boolean at the top level of an evidence object states
  whether the plugin believes the evidence is whole. Evidence that omits it is
  reported as `unstated`, which is not the same as complete. Never set it to
  `true` on a truncated or partially collected result.
- **Size.** Evidence is returned bounded. A single entry above the per-entry
  limit is described rather than shortened, so keep an envelope proportionate to
  what a reader needs in one answer.

Everything under `extra` is rendered as untrusted data. Upstream names, labels,
and descriptions are attacker-controllable text and are quoted, never obeyed.

A normal failure is also a well-formed reply and exits zero:

```json
{
  "v": 1,
  "ok": false,
  "error": {
    "kind": "unreachable",
    "message": "inventory endpoint timed out",
    "retryable": true
  }
}
```

Error kinds are `unreachable`, `unauthorized`, `not_found`, `invalid_config`,
`rate_limited`, and `internal`. A non-zero exit, timeout, malformed JSON, or any
stdout chatter means the plugin itself failed. Diagnostics belong on stderr.
Cadastre keeps previous evidence and marks the source stale instead of treating
failure as an empty estate.

`plugin.info` uses the same transport and returns the declaration plus a
`methods` list. `cadastre sources` calls this handshake. `cadastre plugins`
shows declarations discovered locally or through entry points.

Cadastre's current read methods are:

| Capability | Read methods |
|---|---|
| `Inventory` | `inventory.list` |
| `Network` | `network.list`, `network.members` |
| `Endpoint` | `endpoint.list` |
| `DNS` | `dns.zones`, `dns.records` |
| `SecretRef` | `secret.list`, `secret.stat` |
| `VCS` | `vcs.repos` |
| `CI` | `ci.pipelines`, `ci.status` |
| `Topology` | `topology.list` |
| `Work` | `work.items`, `work.findings`, `work.repo-state`, `work.revision-checks` |

`ci.status` emits `ci_executor` and `ci_pool`. A collector must leave
`ci_executor.runs_on` and `ci_executor.capabilities` unset and declare them as
`intended`: a registration cannot establish which host it runs on, and a label
is not a toolchain. Both are catalog intent, compared by drift rather than
overwritten by a collection.

`Work` belongs to the optional Manifest module and its entity kinds exist only
while that module is enabled; see
[USING-CADASTRE.md](USING-CADASTRE.md#optional-modules). Its methods map to
exactly one kind each — `work.items` → `forge_item`, `work.findings` →
`markdown_finding`, `work.repo-state` → `repo_checkout`,
`work.revision-checks` → `revision_check` — so a collector declaring a method
cannot emit an unrelated kind. No shipped collector implements
`work.revision-checks` yet.

The protocol names some future write/Broker methods, but the current runner
refuses all of them before starting a plugin. There is no `apply` method.

### Credentials and trust

- Upstream credentials must be read-only and scoped to the one capability.
- Put only an environment variable **name**, such as `token_env`, in config.
  Supply its value from the protected collector environment.
- Never put tokens in `command`, URLs, `params`, catalog entities, stdout,
  stderr, warnings, or exception text.
- The runner passes only a small base environment plus names explicitly listed
  in `env` or referenced by a `*_env` config key.
- Run collection on the collector host. API, MCP, and GUI processes do not need
  upstream credentials.
- Treat collected text as untrusted data. Do not turn labels, descriptions, or
  other upstream strings into instructions.

## 4. Single-file Python workflow

[`examples/plugins/hosts-from-python.py`](examples/plugins/hosts-from-python.py)
is a complete, standard-library-only plugin. It deliberately combines the
declaration and executable protocol in one file.

To exercise a plugin against an initialized test data directory:

```bash
cadastre plugins --data-dir "$CADASTRE_DATA_DIR"
cadastre sources --data-dir "$CADASTRE_DATA_DIR"
cadastre collect --data-dir "$CADASTRE_DATA_DIR" --source python-hosts --dry-run
cadastre collect --data-dir "$CADASTRE_DATA_DIR" --source python-hosts
cadastre drift --data-dir "$CADASTRE_DATA_DIR"
cadastre stale --data-dir "$CADASTRE_DATA_DIR"
```

Before configuring Cadastre, the wire behavior can be checked directly:

```bash
printf '%s\n' '{"v":1,"method":"plugin.info","params":{},"config":{}}' \
  | python3 examples/plugins/hosts-from-python.py \
  | python3 -m json.tool
```

Test transforms against recorded upstream payloads, never a live service. At a
minimum cover the handshake, stable identity, schema-valid entity output,
successful provenance, an expected empty response, upstream errors, malformed
configuration, and the guarantee that stdout contains exactly one JSON object.
The repository's protocol failure tests are in
[`tests/test_plugins_legacy.py`](tests/test_plugins_legacy.py).

## 5. Current v1 limits

The distinction between the design direction and today's code matters:

- Python discovery currently loads **declaration metadata** in-process.
  Collection still runs the configured command out-of-process. A single file
  supports both roles, but there is no in-process collection callback API.
- Namespaced fields declared in an entity's `attributes.properties` are
  retained on that entity as plugin data. Undeclared fields are rejected; use
  `result.extra` for uninterpreted output that does not belong to one entity.
- Core validates declaration shape and collected entity shape. The additional
  plugin-specific write-validation callback described in the design is not yet
  an implemented authoring surface.
- A reusable third-party `cadastre.testing` conformance package is a design
  target. Today, mirror the in-tree fixture and protocol tests when publishing
  an external plugin.

These limits keep the extension boundary honest: documentation should not ask a
plugin author to implement a callback that core never calls.
