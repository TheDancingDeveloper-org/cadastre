"""M4/M5 — collection into `observed/`, and drift against `declared/`.

Every plugin here is a fixture process. A test suite that needs network
access is a test suite nobody runs.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from cadastre.cli.collect import collect
from cadastre.cli.drift import drift
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.drift import _covers, _secret_key, _secret_store_diff, compare
from cadastre.core.errors import CatalogError, Located
from cadastre.core.observed import load_observed, parse_source
from cadastre.plugins import EntityDeclaration
from tests.conftest import DECLARED_AS_OF, FIXTURES, NOW

PLUGINS = """\
freshness:
  default: 86400
  endpoint.list: 3600
sources:
- id: fixture
  command: [{python}, {script}]
  methods: [inventory.list, endpoint.list, secret.list]
  config:
    mode: {mode}
"""


def _configure(root: Path, mode: str = "ok") -> None:
    (root / "declared" / "plugins.yaml").write_text(
        PLUGINS.format(
            python=json.dumps(sys.executable),
            script=json.dumps(str(FIXTURES / "plugin_fixture.py")),
            mode=mode,
        ),
        encoding="utf-8",
    )


def _session(root: Path, **kwargs: object) -> Session:
    return Session.open(root, now=kwargs.pop("now", NOW), as_of=DECLARED_AS_OF)  # type: ignore[arg-type]


def test_collect_writes_observed(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    document = collect(_session(catalog_copy))
    assert document.data["written"] == ["observed/fixture.json"]
    sources = load_observed(catalog_copy)
    assert len(sources) == 1
    assert {h.id for h in sources[0].entities["host"]} == {"app-01", "app-99"}


def test_collect_never_writes_to_declared(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    before = {
        path: path.read_bytes()
        for path in (catalog_copy / "declared").rglob("*")
        if path.is_file()
    }
    collect(_session(catalog_copy))
    after = {
        path: path.read_bytes()
        for path in (catalog_copy / "declared").rglob("*")
        if path.is_file()
    }
    assert before == after


def test_a_failing_collector_keeps_its_previous_evidence_and_goes_stale(
    catalog_copy: Path,
) -> None:
    _configure(catalog_copy)
    collect(_session(catalog_copy))

    _configure(catalog_copy, mode="crash")
    document = collect(_session(catalog_copy))
    assert document.data["sources"][0]["ok"] is False
    # Not deleted: absence would render as "nothing there", which reads as fact.
    assert document.data["sources"][0]["counts"]["host"] == 2

    session = _session(catalog_copy)
    stale = [p for p in session.provenance() if p.stale]
    assert [p.source for p in stale] == ["fixture"]
    assert stale[0].error


def test_a_failed_source_exits_non_zero_only_when_the_caller_asks(
    catalog_copy: Path,
) -> None:
    """A scheduler detects a failed collection the only way it can: exit status.

    cron's mail-on-failure, a systemd `OnFailure=` and a Kubernetes
    `restartPolicy: OnFailure` all read the exit code and nothing else, so a
    collector that always exits 0 is invisible to every one of them. Opt-in,
    like `drift --exit-code`, because a STALE source is a designed outcome and
    the default must not start failing runs that today succeed.
    """
    _configure(catalog_copy, mode="crash")

    assert collect(_session(catalog_copy)).exit_code == 0
    document = collect(_session(catalog_copy), exit_code=True)
    assert document.exit_code == 1
    assert document.data["failed"] == ["fixture"]


def test_a_clean_run_exits_zero_with_exit_code_asked_for(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    document = collect(_session(catalog_copy), exit_code=True)
    assert document.exit_code == 0
    assert document.data["failed"] == []


def test_a_partial_failure_is_a_failed_collection(catalog_copy: Path) -> None:
    """One source of several going quiet is the case that is otherwise invisible."""
    source = """\
- id: {id}
  command: [{python}, {script}]
  methods: [inventory.list, endpoint.list, secret.list]
  config:
    mode: {mode}
"""
    common = {
        "python": json.dumps(sys.executable),
        "script": json.dumps(str(FIXTURES / "plugin_fixture.py")),
    }
    (catalog_copy / "declared" / "plugins.yaml").write_text(
        "freshness:\n  default: 86400\nsources:\n"
        + source.format(id="fixture", mode="ok", **common)
        + source.format(id="second", mode="crash", **common),
        encoding="utf-8",
    )
    document = collect(_session(catalog_copy), exit_code=True)

    assert [source["ok"] for source in document.data["sources"]] == [True, False]
    assert document.data["failed"] == ["second"]
    assert document.exit_code == 1


def test_a_source_goes_stale_once_it_passes_its_ttl(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    collect(_session(catalog_copy))
    later = _session(catalog_copy, now=NOW + timedelta(days=3))
    assert [p.source for p in later.provenance() if p.stale] == ["fixture"]


EVIDENCE_ONLY = """\
freshness:
  default: 86400
  ci.status: 900
sources:
- id: github-runners-example
  plugin: forge-github
  command: [{python}, {script}]
  methods: [ci.status]
  config:
    mode: ok
"""


def test_a_source_that_returns_only_evidence_is_not_an_empty_estate(
    catalog_copy: Path,
) -> None:
    """`ci.status` has no core entity, so a healthy collection returns none.

    The empty-result guard exists because zero entities from an inventory
    source is usually a broken response rather than an empty estate. It must
    not fire here, or a runner source would be permanently stale.
    """
    (catalog_copy / "declared" / "plugins.yaml").write_text(
        EVIDENCE_ONLY.format(
            python=json.dumps(sys.executable),
            script=json.dumps(str(FIXTURES / "plugin_fixture.py")),
        ),
        encoding="utf-8",
    )
    document = collect(_session(catalog_copy))
    assert document.data["sources"][0]["ok"] is True
    assert document.data["sources"][0]["counts"] == {}

    source = load_observed(catalog_copy)[0]
    # Provenance names the plugin, not `unknown`: drift picks its identity
    # function from that name.
    assert source.plugin == "forge-github"
    assert source.capabilities == ("ci.status",)
    assert source.extra["ci_status"]["complete"] is True

    provenance = next(
        p
        for p in _session(catalog_copy).provenance()
        if p.source == "github-runners-example"
    )
    # Runner status ages in minutes, not days. Nothing failed, so whatever
    # staleness it has is age — which is why the plan gives it its own source
    # rather than sharing a daily one.
    assert provenance.ttl_seconds == 900
    assert provenance.error is None


HYPERVISOR = """\
freshness:
  default: 86400
sources:
- id: pve
  plugin: hypervisor-proxmox
  command: [{python}, {script}]
  methods: [inventory.list]
  config:
    mode: empty_inventory
"""


def test_an_empty_hypervisor_inventory_does_not_empty_the_estate(
    catalog_copy: Path,
) -> None:
    """The #52 chain, end to end: 200-with-nothing must not read as absence.

    A Proxmox token with privilege separation and no ACL is answered `200
    {"data": []}`. Every step downstream was individually reasonable and the
    result was a green collection followed by every declared host reported
    `missing` -- the estate announced as gone by a credential problem.
    """
    (catalog_copy / "declared" / "plugins.yaml").write_text(
        HYPERVISOR.format(
            python=json.dumps(sys.executable),
            script=json.dumps(str(FIXTURES / "plugin_fixture.py")),
        ),
        encoding="utf-8",
    )
    document = collect(_session(catalog_copy))

    source = document.data["sources"][0]
    assert source["ok"] is False
    assert any("empty" in warning for warning in document.data.get("warnings", []))

    findings = drift(_session(catalog_copy)).data["divergences"]
    assert [f for f in findings if f["kind"] == "missing"] == []


def test_an_empty_secret_store_is_still_believed(catalog_copy: Path) -> None:
    """The counterweight: most sources *can* truthfully return nothing.

    Marking emptiness incredible costs a genuinely empty source a staleness
    warning it can never clear, so it belongs only where the upstream cannot
    honestly answer zero.
    """
    from cadastre.plugins.contract import declaration_for

    assert declaration_for("secret", plugin="secrets-infisical").empty_expected
    assert declaration_for("endpoint", plugin="ingress-caddy").empty_expected
    assert declaration_for("host", plugin="vpn-tailscale").empty_expected
    assert not declaration_for("host", plugin="hypervisor-proxmox").empty_expected


def test_the_registry_and_the_handshake_agree_about_emptiness() -> None:
    """Both paths must narrow it, or `collect` and the plugin disagree.

    `collect` consults the registry, so a plugin whose own handshake said
    otherwise would be judged by a declaration it never made.
    """
    from cadastre.plugins.contract import declaration_for
    from cadastre.plugins.registry import PluginRegistry

    registered = PluginRegistry.discover().get("hypervisor-proxmox")
    assert registered is not None
    declared = registered.info.entity("host")
    assert declared is not None
    assert declared.empty_expected is False
    assert declared == declaration_for(
        "host", authority="source", plugin="hypervisor-proxmox"
    )


def test_a_collector_cannot_overwrite_declared_placement(catalog_copy: Path) -> None:
    """Field ownership, end to end.

    `runs_on` and `capabilities` are declared intent: a registration cannot
    establish which host it runs on, and a label is not a toolchain. A
    collector that reported them would replace a human's statement with a
    guess, so both are declared `intended` and drift compares the rest.
    """
    from cadastre.plugins.contract import declaration_for

    declaration = declaration_for("ci_executor")
    assert set(declaration.intended) == {"runs_on", "capabilities"}
    assert "runs_on" not in declaration.reflected
    assert "capabilities" not in declaration.reflected

    # What a collector reports about the executor the example estate declares:
    # a different status, and no claim at all about placement or toolchains.
    observed = parse_source(
        {
            "source": "github-runners-example",
            "plugin": "forge-github",
            "as_of": DECLARED_AS_OF,
            "capabilities": ["ci.status"],
            "entities": {
                "ci_executor": [
                    {
                        "id": "build-linux-01",
                        "system": "ci-selfhosted",
                        "status": "offline",
                        "selectors": ["build", "linux", "self-hosted"],
                    }
                ]
            },
        },
        Located("fixture"),
    )
    found = compare(_session(catalog_copy).catalog, [observed])
    fields = {d.field for d in found if d.id == "build-linux-01"}
    # The reflected disagreement is reported; the catalog-owned fields are not,
    # because the collector never claimed them.
    assert "status" in fields
    assert "runs_on" not in fields
    assert "capabilities" not in fields


def test_dry_run_touches_nothing(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    collect(_session(catalog_copy), dry_run=True)
    assert not (catalog_copy / "observed").exists()


# -- drift ------------------------------------------------------------------


def _drift_data(root: Path) -> dict:
    _configure(root)
    collect(_session(root))
    return drift(_session(root)).data


def test_drift_finds_an_undeclared_host(catalog_copy: Path) -> None:
    found = [
        d
        for d in _drift_data(catalog_copy)["divergences"]
        if d["category"] == "undeclared" and d["id"] == "app-99"
    ]
    assert found


def test_drift_finds_a_field_level_disagreement(catalog_copy: Path) -> None:
    found = [
        d
        for d in _drift_data(catalog_copy)["divergences"]
        if d["category"] == "differs" and d["id"] == "notes-api-internal"
    ]
    assert found[0]["field"] == "port"
    assert found[0]["declared"] == "8080"
    assert found[0]["observed"] == "9090"


def test_drift_ignores_fields_that_are_intent_rather_than_fact(
    catalog_copy: Path,
) -> None:
    """`notes` and `tags` are the operator's, not the world's."""
    fields = {
        d.get("field")
        for d in _drift_data(catalog_copy)["divergences"]
        if d["category"] == "differs"
    }
    assert "notes" not in fields
    assert "tags" not in fields


def test_a_field_the_catalog_never_declared_is_not_a_disagreement(
    catalog_copy: Path,
) -> None:
    """Absent is not empty, in either direction.

    Every collector returns fields the catalog chose not to declare, and every
    catalog declares fields a given collector cannot see. Treating either as a
    divergence buries the real ones — found against the real estate, where a
    DNS collector's 66 records produced a wall of rows with a blank `declared`
    column, and a GitOps collector that cannot know a host produced the mirror
    image with a blank `observed` one.
    """
    blank = [
        d
        for d in _drift_data(catalog_copy)["divergences"]
        if d["category"] == "differs" and not (d.get("declared") and d.get("observed"))
    ]
    assert blank == []


def test_drift_reports_a_secret_present_in_one_store_only(catalog_copy: Path) -> None:
    found = [
        d
        for d in _drift_data(catalog_copy)["divergences"]
        if d["category"] == "secret-only-in"
    ]
    names = {d["id"] for d in found}
    assert "/prod/notes-api/legacy-key" in names
    assert "/prod/ingress/acme-token" in names


def test_declared_replication_contract_scopes_the_live_estate_diff(
    catalog_copy: Path,
) -> None:
    """L2: the estate fixture, with a real replication.yaml on disk.

    Three secret stores are in evidence (`secrets-manager`, `ci-store`,
    `audit-store`), but only `secrets-manager` <-> `ci-store` is a declared
    replication contract. `audit-store`'s names must never surface as
    `secret-only-in` noise, and the declared pair's real divergences must
    still be reported.
    """
    _configure(catalog_copy, mode="extra_secret_store")
    (catalog_copy / "declared" / "policy" / "replication.yaml").write_text(
        "replication:\n- source: secrets-manager\n  target: ci-store\n",
        encoding="utf-8",
    )
    collect(_session(catalog_copy))
    found = [
        d
        for d in _drift_data(catalog_copy)["divergences"]
        if d["category"] == "secret-only-in"
    ]
    ids = {d["id"] for d in found}
    assert "/prod/notes-api/legacy-key" in ids
    assert "/prod/ingress/acme-token" in ids
    assert "/prod/audit/undeclared-pair-secret" not in ids
    assert all(d["field"] in {"secrets-manager", "ci-store"} for d in found)


def test_uncontracted_secret_stores_are_inventory_only() -> None:
    a = parse_source(
        {
            "source": "a",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"a": ["app-key"]}},
        },
        Located("a"),
    )
    b = parse_source(
        {
            "source": "b",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"b": ["ci-key"]}},
        },
        Located("b"),
    )
    rows = _secret_store_diff([a, b], ())
    assert rows and all(not row.actionable for row in rows)


def test_replication_contract_limits_secret_diff() -> None:
    a = parse_source(
        {
            "source": "a",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"a": ["app-key", "ignored"]}},
        },
        Located("a"),
    )
    b = parse_source(
        {
            "source": "b",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"b": ["app-key"]}},
        },
        Located("b"),
    )
    contract = model.ReplicationContract("a", "b", selectors=("app-*",))
    rows = _secret_store_diff([a, b], (contract,))
    assert rows == []


def test_replication_mapping_compares_different_reference_names() -> None:
    a = parse_source(
        {
            "source": "a",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"a": ["DEPLOY_TOKEN"]}},
        },
        Located("a"),
    )
    b = parse_source(
        {
            "source": "b",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"b": ["FORGE_DEPLOY_TOKEN"]}},
        },
        Located("b"),
    )
    contract = model.ReplicationContract(
        "a", "b", mappings={"DEPLOY_TOKEN": "FORGE_DEPLOY_TOKEN"}
    )
    assert _secret_store_diff([a, b], (contract,)) == []


def test_replication_mapping_reports_missing_target_reference() -> None:
    a = parse_source(
        {
            "source": "a",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"a": ["DEPLOY_TOKEN"]}},
        },
        Located("a"),
    )
    b = parse_source(
        {
            "source": "b",
            "plugin": "x",
            "as_of": "now",
            "entities": {},
            "extra": {"secret_names": {"b": []}},
        },
        Located("b"),
    )
    contract = model.ReplicationContract(
        "a", "b", mappings={"DEPLOY_TOKEN": "FORGE_DEPLOY_TOKEN"}
    )
    rows = _secret_store_diff([a, b], (contract,))
    assert len(rows) == 1
    assert rows[0].id == "DEPLOY_TOKEN"
    assert rows[0].actionable


def test_source_coverage_cannot_broaden_plugin_coverage() -> None:
    declaration = EntityDeclaration(
        kind="host",
        authority="source",
        coverage={"ids": ["app-01"]},
    )
    source = parse_source(
        {
            "source": "project-a",
            "plugin": "fixture",
            "as_of": "now",
            "coverage": {"host": {"ids": ["edge-01"]}},
            "entities": {},
        },
        Located("coverage"),
    )
    candidate = model.Host(id="edge-01", role="edge")
    assert not _covers(source, candidate, declaration)


def test_drift_never_writes_anything(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    collect(_session(catalog_copy))
    before = {
        path: path.read_bytes() for path in catalog_copy.rglob("*") if path.is_file()
    }
    drift(_session(catalog_copy))
    after = {
        path: path.read_bytes() for path in catalog_copy.rglob("*") if path.is_file()
    }
    assert before == after


def test_exit_code_is_opt_in(catalog_copy: Path) -> None:
    _configure(catalog_copy)
    collect(_session(catalog_copy))
    assert drift(_session(catalog_copy)).exit_code == 0
    assert drift(_session(catalog_copy), exit_code=True).exit_code == 1


def test_no_evidence_is_not_agreement(catalog_copy: Path) -> None:
    document = drift(_session(catalog_copy))
    assert document.data["divergences"] == []
    from cadastre.render import text

    assert "not the same as agreement" in text.render(document)


def test_observed_entities_are_not_reference_checked(catalog_copy: Path) -> None:
    """An observed endpoint on an undeclared service is a finding, not an error."""
    _configure(catalog_copy)
    collect(_session(catalog_copy))
    sources = load_observed(catalog_copy)
    services = {e.service for e in sources[0].entities["endpoint"]}  # type: ignore[attr-defined]
    assert "unknown-service" in services


def test_the_same_secret_spelled_two_ways_is_one_secret() -> None:
    """Stores disagree about spelling, not about identity.

    A secret manager holding `infisical://cicd/prod/GIT_AUTH_TOKEN` and a CI
    store holding `git_auth_token` have the same secret in two places. Reading
    those as two names makes every store totally diverge from every other,
    which is a report nobody can act on.
    """
    assert _secret_key("infisical://cicd/prod/GIT_AUTH_TOKEN") == _secret_key(
        "woodpecker://acme/org/git_auth_token"
    )
    assert _secret_key("/prod/DEPLOY_SSH_KEY") == _secret_key("deploy-ssh-key")
    assert _secret_key("a/FORGEJO_TOKEN") != _secret_key("a/GITHUB_TOKEN")


def test_observed_evidence_may_be_partial(catalog_copy: Path) -> None:
    """A collector that cannot know a required field must not lose its source.

    A GitOps repo names a service before it can say which host runs it. When
    `runs_on` was enforced against observed evidence, the choice was to invent
    a host or discard 129 real services — and the collector had been inventing
    one from the directory name.
    """
    source = parse_source({"entities": {"service": [{"id": "web"}]}}, Located("<test>"))
    assert [s.id for s in source.entities["service"]] == ["web"]


def test_observed_evidence_still_needs_an_id(catalog_copy: Path) -> None:
    """Partial is not formless. Evidence about an entity nobody can name is
    not evidence, and an unknown field is still a plugin inventing model."""
    with pytest.raises(CatalogError):
        parse_source({"entities": {"service": [{"expose": "public"}]}}, Located("<t>"))
    with pytest.raises(CatalogError):
        parse_source(
            {"entities": {"service": [{"id": "web", "nope": 1}]}}, Located("<t>")
        )


def test_collect_preserves_registered_plugin_identity_for_drift(
    catalog_copy: Path,
) -> None:
    """A source label is provenance, not a replacement for plugin identity."""
    configured = """\
sources:
- id: a-scoped-source
  plugin: ingress-caddy
  command: [{python}, {script}]
  methods: [inventory.list]
""".format(
        python=json.dumps(sys.executable),
        script=json.dumps(str(FIXTURES / "plugin_fixture.py")),
    )
    (catalog_copy / "declared" / "plugins.yaml").write_text(configured)
    # The fixture declares itself as `fixture`, which is unregistered. The
    # configured registered plugin identity remains the safe fallback.
    source = collect(_session(catalog_copy)).data["sources"]
    assert source[0]["id"] == "a-scoped-source"
    observed = load_observed(catalog_copy)[0]
    assert observed.source == "a-scoped-source"
    assert observed.plugin == "ingress-caddy"


def test_a_collector_can_declare_its_own_scope_in_the_reply(
    catalog_copy: Path,
) -> None:
    """The per-source coverage channel.

    Several sources can share one plugin with different projects, zones or
    orgs, so a per-plugin `plugin.info` declaration cannot express which of
    them saw what. Without this, three Infisical projects each reported every
    other project's secrets `missing`.
    """
    _configure(catalog_copy, mode="reports_coverage")
    collect(_session(catalog_copy))
    observed = load_observed(catalog_copy)[0]
    assert observed.coverage == {"host": {"ids": ["app-01"]}}


def test_explicit_source_coverage_overrides_what_the_collector_reports(
    catalog_copy: Path,
) -> None:
    """An operator can always narrow further than the plugin claims."""
    _configure(catalog_copy, mode="reports_coverage")
    path = catalog_copy / "declared" / "plugins.yaml"
    path.write_text(
        path.read_text() + "  coverage:\n    host:\n      ids: [app-99]\n",
        encoding="utf-8",
    )
    collect(_session(catalog_copy))
    observed = load_observed(catalog_copy)[0]
    assert observed.coverage == {"host": {"ids": ["app-99"]}}


def test_a_malformed_reported_coverage_is_refused_not_silently_emptied(
    catalog_copy: Path,
) -> None:
    """Coverage SHRINKS what a source may claim absence about.

    A malformed block that quietly parsed as empty would restore exactly the
    over-claiming it exists to prevent, so it is dropped with a warning that
    names the source and method rather than accepted.
    """
    _configure(catalog_copy, mode="bad_coverage")
    document = collect(_session(catalog_copy))
    observed = load_observed(catalog_copy)[0]
    assert observed.coverage == {}
    warnings = " ".join(document.data.get("warnings", []))
    assert "nonexistent_field" in warnings


def test_an_entity_no_collector_covers_is_reported_as_a_blind_spot(
    catalog_copy: Path,
) -> None:
    """The hole coverage digs.

    Narrowing is what stops one source reporting another's entities `missing`,
    but it cuts both ways: an entity outside EVERY source's scope is compared
    by nothing, so a genuinely absent one silently stops being reported. A
    mistyped `store` then reads as "no drift" rather than "nobody looked" —
    the worst possible answer from a tool whose value is being trustworthy.
    """
    from cadastre.core.drift import unobservable

    source = parse_source(
        {
            "source": "project-a",
            "plugin": "fixture",
            "as_of": "2026-08-07T09:00:00Z",
            "coverage": {"host": {"where": {"tags": ["project-a"]}}},
            "entities": {"host": [{"id": "app-01", "role": "server"}]},
        },
        Located("<test>"),
    )
    catalog = _session(catalog_copy).catalog
    blind = unobservable(catalog, [source])
    assert "edge-01" in {row["id"] for row in blind if row["kind"] == "host"}
    assert all(row["sources"] == "project-a" for row in blind)


def test_a_kind_with_no_collector_at_all_is_not_a_blind_spot(
    catalog_copy: Path,
) -> None:
    """An unwired estate is a different problem, already reported elsewhere.

    Flagging every declared entity of every uncollected kind would bury the
    scoping mistakes this exists to surface.
    """
    from cadastre.core.drift import unobservable

    source = parse_source(
        {
            "source": "project-a",
            "plugin": "fixture",
            "as_of": "2026-08-07T09:00:00Z",
            "entities": {"host": [{"id": "app-01", "role": "server"}]},
        },
        Located("<test>"),
    )
    catalog = _session(catalog_copy).catalog
    blind = unobservable(catalog, [source])
    assert all(row["kind"] == "host" for row in blind)


def test_scoped_source_does_not_report_out_of_scope_entities_missing(
    catalog_copy: Path,
) -> None:
    source = parse_source(
        {
            "source": "project-a",
            "plugin": "fixture",
            "as_of": "2026-08-07T09:00:00Z",
            "coverage": {"host": {"where": {"tags": ["project-a"]}}},
            "entities": {"host": [{"id": "app-01", "role": "server"}]},
        },
        Located("<test>"),
    )
    catalog = _session(catalog_copy).catalog
    # Fixture catalog's edge host is unrelated to project-a, and thus this
    # collector's absence must not turn it into a phantom missing finding.
    from cadastre.core.drift import compare

    missing = [
        row
        for row in compare(catalog, [source])
        if row.category == "missing" and row.kind == "host"
    ]
    assert all(row.id != "edge-01" for row in missing)


def test_repo_correlates_across_forges_on_shared_remote(catalog_copy: Path) -> None:
    """§2d, L2: `notes-api-repo` is declared dual-homed (a selfhosted origin
    and a public mirror). An observed record under a collector-chosen id,
    carrying only the mirror remote, must correlate to it rather than
    reading as one undeclared repo plus one missing repo."""
    source = parse_source(
        {
            "source": "github-example",
            "plugin": "forge-github",
            "as_of": "2026-08-07T09:00:00Z",
            "entities": {
                "repo": [
                    {
                        "id": "gh-notes-api-repo",
                        "remotes": [
                            {
                                "forge": "forge-public",
                                "url": "https://public.example.invalid/example/notes-api.git",
                                "role": "origin",
                            }
                        ],
                    }
                ]
            },
        },
        Located("<test>"),
    )
    catalog = _session(catalog_copy).catalog
    rows = compare(catalog, [source])
    assert not any(row.kind == "repo" and row.id == "gh-notes-api-repo" for row in rows)
    assert not any(
        row.category == "missing" and row.kind == "repo" and row.id == "notes-api-repo"
        for row in rows
    )


def test_orchestrator_undeclared_class_collapses_to_stack_level(
    tmp_path: Path, catalog_copy: Path
) -> None:
    """§2e, L2: two compose services in a declared stack must not each read as
    an undeclared entity, and an actually-undeclared stack reports once, not
    once per container inside it."""
    from cadastre.plugins.collectors import orchestrator_gitops

    declared_stack = tmp_path / "notes-api"
    declared_stack.mkdir()
    (declared_stack / "compose.yaml").write_text(
        "services:\n  web:\n    image: x\n  worker:\n    image: y\n",
        encoding="utf-8",
    )
    undeclared_stack = tmp_path / "unrelated-stack"
    undeclared_stack.mkdir()
    (undeclared_stack / "compose.yaml").write_text(
        "services:\n  a:\n    image: x\n  b:\n    image: y\n",
        encoding="utf-8",
    )
    scanned = orchestrator_gitops.scan(tmp_path, {})
    source = parse_source(
        {
            "source": "gitops",
            "plugin": "orchestrator-gitops",
            "as_of": "2026-08-07T09:00:00Z",
            "entities": scanned["entities"],
        },
        Located("<test>"),
        extensions={"service": {"x-orchestrator"}},
    )
    catalog = _session(catalog_copy).catalog
    rows = compare(catalog, [source])
    undeclared = [
        row for row in rows if row.category == "undeclared" and row.kind == "service"
    ]
    assert {row.id for row in undeclared} == {"unrelated-stack"}
    assert not any(row.id == "notes-api" for row in undeclared)


def test_credible_empty_source_reports_only_covered_entities_missing(
    catalog_copy: Path,
) -> None:
    source = parse_source(
        {
            "source": "project-a",
            "plugin": "fixture",
            "as_of": "2026-08-07T09:00:00Z",
            "coverage": {"host": {"ids": ["app-01"]}},
            "entities": {"host": []},
        },
        Located("<test>"),
    )
    catalog = _session(catalog_copy).catalog
    missing = [
        row.id
        for row in compare(catalog, [source])
        if row.category == "missing" and row.kind == "host"
    ]
    assert missing == ["app-01"]


def test_empty_entity_kind_survives_observed_round_trip() -> None:
    source = parse_source(
        {
            "source": "empty",
            "plugin": "fixture",
            "as_of": "now",
            "entities": {"host": []},
        },
        Located("empty"),
    )
    from cadastre.core.observed import source_to_dict

    restored = parse_source(source_to_dict(source), Located("restored"))
    assert restored.entities == {"host": []}
