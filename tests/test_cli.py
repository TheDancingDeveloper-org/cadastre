"""The CLI contract: exit codes, `--json`, and never a traceback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadastre.cli.main import main
from tests.conftest import ARTIFACTS, EXAMPLE_CATALOG

CATALOG = ["--catalog", str(EXAMPLE_CATALOG)]
FIXED_CLOCK = ["--now", "2026-08-07T12:00:00Z"]


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main([*CATALOG, *FIXED_CLOCK, *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_is_available_without_a_catalog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0


def test_brief_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["brief"], capsys)
    assert code == 0
    assert "## Hosts" in out


def test_json_output_is_one_object_with_provenance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(["brief", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["command"] == "cadastre brief"
    assert payload["provenance"][0]["source"] == "declared"


def test_lookup_of_an_unknown_id_is_a_usage_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run(["lookup", "nope"], capsys)
    assert code == 2
    assert "no entity with id" in err
    assert "Traceback" not in err


def test_an_ambiguous_id_asks_for_kind(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import shutil

    catalog = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, catalog)
    networks = catalog / "declared" / "networks" / "networks.yaml"
    networks.write_text(
        networks.read_text(encoding="utf-8") + "- id: ingress\n  class: private\n",
        encoding="utf-8",
    )
    code = main(["--catalog", str(catalog), "lookup", "ingress"])
    assert code == 2
    assert "--kind" in capsys.readouterr().err


def test_a_broken_catalog_reports_every_problem_and_exits_two(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    declared = tmp_path / "declared" / "networks"
    declared.mkdir(parents=True)
    (declared / "n.yaml").write_text("- id: n1\n  class: sideways\n", encoding="utf-8")
    code = main(["--catalog", str(tmp_path), "brief"])
    err = capsys.readouterr().err
    assert code == 2
    assert "could not be used as written" in err
    assert "Traceback" not in err


def test_check_exits_one_on_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["check", str(ARTIFACTS / "compose-unknown-host.yaml")], capsys)
    assert code == 1
    assert "ERROR" in out


def test_check_exits_zero_on_the_clean_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = run(["check", str(ARTIFACTS / "compose-clean.yaml")], capsys)
    assert code == 0


def test_drift_does_not_fail_the_build_unless_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = run(["drift"], capsys)
    assert code == 0


def test_context_for_names_candidates_and_exclusions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(["context-for", "an internal worker with a gpu"], capsys)
    assert code == 0
    assert "## Candidates" in out
    assert "## Excluded" in out


def test_schema_prints_json_schema(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["schema"], capsys)
    assert code == 0
    assert json.loads(out)["title"] == "Cadastre catalog"


def test_fmt_check_passes_on_the_example_catalog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = run(["fmt", "--check"], capsys)
    assert code == 0


def test_the_catalog_root_can_come_from_the_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CADASTRE_CATALOG", str(EXAMPLE_CATALOG))
    assert main(["brief"]) == 0


def test_sources_reports_nothing_configured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(["sources"], capsys)
    assert code == 0
    assert "none configured" in out


# -- GitHub #25: drift filters are reachable from the CLI, not just the API --


def test_drift_summary_only_flag_is_wired(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(["drift", "--summary-only", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["result"]["pagination"]["summary_only"] is True


def test_drift_kind_filter_is_wired(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["drift", "--kind", "secret", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    # A filtered call is paged, so it always carries a pagination block.
    assert "pagination" in payload["result"]


def test_drift_rejects_an_out_of_range_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run(["drift", "--limit", "0"], capsys)
    assert code != 0
    assert "between 1 and 1000" in err
