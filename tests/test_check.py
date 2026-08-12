"""M7 — `check`.

Every rule ships with a failing artifact. A rule without a negative case is
untested, and a rule that fires on the clean artifact is worse than no rule.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cadastre.cli.check import check
from cadastre.cli.session import Session
from cadastre.core.artifacts import infer_kind, parse
from cadastre.core.errors import UsageError
from cadastre.core.rules import RULES
from cadastre.render import text
from tests.conftest import ARTIFACTS, DECLARED_AS_OF, EXAMPLE_CATALOG, NOW


def _codes(session: Session, name: str, **kwargs: object) -> list[str]:
    document = check(session, ARTIFACTS / name, **kwargs)  # type: ignore[arg-type]
    return [f["code"] for f in document.data["findings"]]


def test_clean_artifact_reports_missing_live_port_evidence(
    session: Session,
) -> None:
    document = check(session, ARTIFACTS / "compose-clean.yaml")
    assert [finding["code"] for finding in document.data["findings"]] == [
        "port-collision-unchecked"
    ]
    assert document.exit_code == 0


def test_check_document_uses_stable_artifact_identity(session: Session) -> None:
    document = check(session, ARTIFACTS / "compose-clean.yaml")
    assert document.title == "cadastre check compose-clean.yaml"
    assert document.data["artifact"]["path"] == "compose-clean.yaml"


# -- one failing artifact per rule ------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "code"),
    [
        ("compose-unknown-host.yaml", "unknown-host"),
        ("compose-public-on-private.yaml", "exposure-network-class"),
        ("compose-unknown-tier.yaml", "unknown-exposure-tier"),
        ("compose-needs-ingress.yaml", "exposure-requires-ingress"),
        ("compose-none-with-port.yaml", "exposure-none-conflict"),
        ("compose-internal-collision.yaml", "artifact-internal-collision"),
        ("compose-invalid-frontend.yaml", "fronted-by-validation"),
        ("compose-hostname-collision.yaml", "hostname-collision"),
        ("compose-port-collision.yaml", "port-collision"),
        ("compose-bad-secret-format.yaml", "secret-ref-format"),
        ("compose-unknown-secret.yaml", "secret-ref-unknown"),
        ("compose-bad-name.yaml", "service-name-convention"),
        ("compose-name-collision.yaml", "service-name-collision"),
        ("ingress-hostname-collision.json", "hostname-collision"),
        ("grants-wildcard.yaml", "grant-wildcard"),
        ("grants-wildcard-principal.yaml", "grant-wildcard-principal"),
        ("pipeline-dual-ci.yaml", "pipeline-authority"),
        ("pipeline-unsatisfiable.yaml", "execution-unsatisfiable"),
        ("pipeline-unknown-pool.yaml", "execution-unknown-pool"),
        ("pipeline-hosted-runner.yaml", "execution-hosted-pool"),
        ("pipeline-dynamic-runner.yaml", "execution-indeterminate"),
        ("pipeline-missing-capability.yaml", "execution-capability"),
        ("pipeline-offline-runner.yaml", "execution-availability"),
    ],
)
def test_a_rule_fires_on_its_failing_artifact(
    session: Session, artifact: str, code: str
) -> None:
    assert code in _codes(session, artifact)


#: A copy of the example estate with two deliberate execution-policy faults.
#: Some rules are shaped by the catalog rather than by the artifact — a pool
#: that public repositories may use, an executor claiming to live on a
#: workstation — and their negative case has to live somewhere the good example
#: estate does not.
FLAWED_POOLS = """\
- id: build-pool
  system: ci-selfhosted
  visibility: all
  public_repositories: true
"""

FLAWED_EXECUTORS = """\
- id: build-linux-01
  system: ci-selfhosted
  pool: build-pool
  status: online
  selectors: [self-hosted, linux, build]
  capabilities: [docker]
  runs_on: ws-01
"""


def _flawed_estate(tmp_path: Path) -> Session:
    root = tmp_path / "flawed"
    shutil.copytree(EXAMPLE_CATALOG, root)
    declared = root / "declared"
    (declared / "ci-pools" / "ci-pools.yaml").write_text(FLAWED_POOLS, encoding="utf-8")
    (declared / "ci-executors" / "ci-executors.yaml").write_text(
        FLAWED_EXECUTORS, encoding="utf-8"
    )
    return Session.open(root, now=NOW, as_of=DECLARED_AS_OF)


def test_a_pool_public_repositories_may_use_is_an_error(tmp_path: Path) -> None:
    """A persistent executor reachable from a public repository can be
    persistently compromised. Cadastre cannot secure the machine; it can refuse
    to let this be invisible."""
    codes = [
        f["code"]
        for f in check(
            _flawed_estate(tmp_path), ARTIFACTS / "pipeline-missing-capability.yaml"
        ).data["findings"]
    ]
    assert "execution-public-access" in codes


def test_an_executor_placed_on_a_workstation_is_an_error(tmp_path: Path) -> None:
    """`runs_on` is intent somebody stated, and a registration that claims to
    live on a workstation is either wrong or a policy problem."""
    document = check(
        _flawed_estate(tmp_path), ARTIFACTS / "pipeline-missing-capability.yaml"
    )
    finding = next(
        f for f in document.data["findings"] if f["code"] == "execution-placement"
    )
    assert "ws-01" in finding["message"]
    assert "workstation" in finding["why"]


def test_the_write_gate_sees_execution_policy_without_an_artifact(
    tmp_path: Path,
) -> None:
    """`check` scopes these to the artifact so unrelated runs stay quiet. The
    write gate has no artifact and still needs the invariant."""
    from cadastre.core.rules import check_catalog

    codes = {f.code for f in check_catalog(_flawed_estate(tmp_path).catalog)}
    assert {"execution-public-access", "execution-placement"} <= codes


def test_every_registered_rule_has_a_failing_artifact(
    session: Session, tmp_path: Path
) -> None:
    """The guard that keeps the table above honest as rules are added."""
    fired: set[str] = set()
    for estate in (session, _flawed_estate(tmp_path)):
        for artifact in sorted(ARTIFACTS.iterdir()):
            kind = "grants" if "grants" in artifact.name else None
            document = check(estate, artifact, kind=kind)
            fired.update(f["code"] for f in document.data["findings"])
    assert {code for code, _ in RULES} - fired == set()


# -- the shape of an error --------------------------------------------------


def test_an_error_says_what_why_and_the_fix(session: Session) -> None:
    document = check(session, ARTIFACTS / "compose-public-on-private.yaml")
    finding = document.data["findings"][0]
    assert finding["message"].startswith('"public" requires')
    assert "reachable only from" in finding["why"]
    assert "place on a host in a public-class network" in finding["fix"]
    assert "edge-01" in finding["fix"]


def test_the_fix_names_free_ports_on_the_host(session: Session) -> None:
    document = check(session, ARTIFACTS / "compose-port-collision.yaml")
    finding = next(
        f for f in document.data["findings"] if f["code"] == "port-collision"
    )
    assert "8080" in finding["why"] or "notes-api" in finding["why"]
    assert "taken on app-01" in finding["fix"]


# -- exit codes and the CI gate ---------------------------------------------


def test_errors_exit_non_zero(session: Session) -> None:
    assert check(session, ARTIFACTS / "compose-unknown-host.yaml").exit_code == 1


def test_warnings_alone_do_not_fail_by_default(session: Session) -> None:
    document = check(session, ARTIFACTS / "compose-needs-ingress.yaml")
    assert all(f["level"] == "warn" for f in document.data["findings"])
    assert document.exit_code == 0
    assert (
        check(
            session, ARTIFACTS / "compose-needs-ingress.yaml", warnings_as_errors=True
        ).exit_code
        == 1
    )


# -- gaps are reported as gaps, not as passes -------------------------------


def test_a_compose_file_with_no_host_says_so(session: Session) -> None:
    document = check(session, ARTIFACTS / "compose-no-host.yaml")
    assert document.data["unchecked"]
    rendered = text.render(document)
    assert "Could not be checked" in rendered
    assert "will not guess" in rendered


# -- artifact parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("docker-compose.yml", "compose"),
        ("caddy.json", "ingress"),
        ("grants.yaml", "grants"),
        (".woodpecker/deploy.yaml", "pipeline"),
    ],
)
def test_artifact_kind_is_inferred_from_the_filename(filename: str, kind: str) -> None:
    assert infer_kind(Path(filename)) == kind


def test_an_uninferable_filename_asks_for_kind() -> None:
    with pytest.raises(UsageError) as caught:
        infer_kind(Path("thing.yaml"))
    assert "--kind" in str(caught.value)


def test_port_mappings_are_read_as_the_published_port() -> None:
    artifact = parse(ARTIFACTS / "compose-port-collision.yaml")
    assert artifact.services[0].ports == (8080,)


def test_absolute_runtime_paths_are_not_secret_references() -> None:
    artifact = parse(Path(__file__).parents[1] / "compose.production.yaml")
    assert artifact.secret_refs == ()


def test_a_missing_artifact_is_a_usage_error(session: Session) -> None:
    with pytest.raises(UsageError):
        check(session, ARTIFACTS / "does-not-exist.yaml", kind="compose")


# -- rules that need a differently-shaped catalog ---------------------------


def test_ambiguous_pipeline_authority_is_an_error(catalog_copy: Path) -> None:
    """The dual-CI case: two pipelines claim a service and neither is marked."""
    services = catalog_copy / "declared" / "services" / "services.yaml"
    services.write_text(
        services.read_text(encoding="utf-8").replace(
            "    - pipeline: notes-api-selfhosted\n      authoritative: true\n",
            "    - pipeline: notes-api-selfhosted\n      authoritative: false\n",
        ),
        encoding="utf-8",
    )
    pipelines = catalog_copy / "declared" / "pipelines" / "pipelines.yaml"
    pipelines.write_text(
        pipelines.read_text(encoding="utf-8").replace(
            "- id: notes-api-public\n"
            "  notes: Mirror-side build. Runs tests, deploys nothing.\n",
            "- id: notes-api-public\n  deploys:\n    - notes-api\n",
        ),
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    document = check(session, ARTIFACTS / "compose-name-collision.yaml")
    finding = next(
        f for f in document.data["findings"] if f["code"] == "pipeline-authority"
    )
    assert finding["level"] == "error"
    assert "authoritative: true" in finding["fix"]


def test_a_hostname_only_seen_by_a_collector_still_collides(
    catalog_copy: Path,
) -> None:
    """A collision with running configuration is a collision, whatever
    declared/ says — and the undeclared route is itself worth reporting."""
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "ingress.json").write_text(
        """{
  "v": 1,
  "source": "ingress",
  "plugin": "caddy",
  "as_of": "2026-08-07T11:00:00Z",
  "ok": true,
  "capabilities": ["endpoint.list"],
  "entities": {
    "endpoint": [
      {"id": "undeclared-route", "service": "mystery", "network": "edge-net",
       "address": "reports.example.invalid", "port": 443, "protocol": "https"}
    ]
  }
}
""",
        encoding="utf-8",
    )
    artifact = catalog_copy / "compose.yaml"
    artifact.write_text(
        "x-cadastre:\n  host: app-01\n  expose: internal\n"
        "services:\n  reports:\n    image: x\n"
        "    x-cadastre:\n      hostnames: [reports.example.invalid]\n",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    document = check(session, artifact)
    finding = next(
        f for f in document.data["findings"] if f["code"] == "hostname-collision"
    )
    assert "already being served" in finding["message"]
    assert "cadastre drift" in finding["why"]


def test_a_port_seen_by_a_collector_still_collides(catalog_copy: Path) -> None:
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "orchestrator.json").write_text(
        """{
  "v": 1, "source": "orchestrator", "plugin": "fixture",
  "as_of": "2026-08-07T11:00:00Z", "ok": true,
  "capabilities": ["endpoint.list"],
  "entities": {"endpoint": [{"id": "live", "service": "notes-api",
    "network": "lab-net", "address": "notes.internal.example.invalid",
    "port": 9090, "protocol": "http"}]}
}
""",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    artifact = ARTIFACTS / "compose-observed-port.yaml"
    finding = next(
        f
        for f in check(session, artifact).data["findings"]
        if f["code"] == "port-collision"
    )
    assert "orchestrator:notes-api" in finding["why"]


def test_published_port_is_unchecked_without_a_fresh_endpoint_collector(
    session: Session,
) -> None:
    assert "port-collision-unchecked" in _codes(session, "compose-clean.yaml")


def test_an_undeclared_observed_listener_still_blocks_its_host(
    catalog_copy: Path,
) -> None:
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "orchestrator.json").write_text(
        """{
  "v": 1, "source": "orchestrator", "plugin": "fixture",
  "as_of": "2026-08-07T11:00:00Z", "ok": true,
  "capabilities": ["endpoint.list"],
  "entities": {"endpoint": [{"id": "unknown-live", "service": "unknown",
    "host": "app-01", "network": "lab-net", "address": "0.0.0.0",
    "bind_address": "0.0.0.0", "port": 12345, "protocol": "tcp"}]}
}
""",
        encoding="utf-8",
    )
    artifact = catalog_copy / "compose.yaml"
    artifact.write_text(
        "x-cadastre:\n  host: app-01\n  expose: internal\n"
        "services:\n  reports:\n    image: x\n    ports: ['12345:8080']\n",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    finding = next(
        f
        for f in check(session, artifact).data["findings"]
        if f["code"] == "port-collision"
    )
    assert "orchestrator:unknown" in finding["why"]


def test_port_collision_identity_includes_bind_address_and_protocol(
    catalog_copy: Path,
) -> None:
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "orchestrator.json").write_text(
        """{
  "v": 1, "source": "orchestrator", "plugin": "fixture",
  "as_of": "2026-08-07T11:00:00Z", "ok": true,
  "capabilities": ["endpoint.list"],
  "entities": {"endpoint": [{"id": "udp", "service": "unknown",
    "host": "app-01", "network": "lab-net", "address": "127.0.0.1",
    "bind_address": "127.0.0.1", "port": 12346, "protocol": "udp"}]}
}
""",
        encoding="utf-8",
    )
    artifact = catalog_copy / "compose.yaml"
    artifact.write_text(
        "x-cadastre:\n  host: app-01\n  expose: internal\nservices:\n"
        "  tcp:\n    image: x\n    ports: ['127.0.0.1:12346:8080/tcp']\n"
        "  other-address:\n    image: x\n    ports: ['127.0.0.2:12346:8080/udp']\n",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    assert "port-collision" not in [
        finding["code"] for finding in check(session, artifact).data["findings"]
    ]


def test_frontend_must_be_able_to_supply_the_requested_tier(
    catalog_copy: Path,
) -> None:
    services = catalog_copy / "declared" / "services" / "services.yaml"
    services.write_text(
        services.read_text(encoding="utf-8").replace(
            "  runs_on: edge-01\n  repo: ops-repo",
            "  runs_on: app-01\n  repo: ops-repo",
        ),
        encoding="utf-8",
    )
    artifact = catalog_copy / "compose.yaml"
    artifact.write_text(
        "x-cadastre:\n  host: edge-01\n  expose: public\n  fronted_by: ingress\n"
        "services:\n  reports:\n    image: x\n",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    findings = check(session, artifact).data["findings"]
    assert any(
        f["code"] == "fronted-by-validation" and "cannot provide tier" in f["message"]
        for f in findings
    )
