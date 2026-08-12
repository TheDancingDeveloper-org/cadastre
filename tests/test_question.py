"""Contract tests for explicit migration questions."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cadastre.cli.question import question
from cadastre.cli.session import Session
from cadastre.render.json_out import to_dict
from tests.conftest import NOW


def test_host_eligibility_preserves_unverified_state(session: Session) -> None:
    answer = question(session, "Q-H03")
    assert answer.data["status"] == "unverified"
    assert isinstance(answer.data["exclusions"], list)
    assert to_dict(answer)["provenance"]


def test_host_access_fails_closed_when_evidence_is_unverified(
    session: Session,
) -> None:
    answer = question(session, "Q-H02", subject="app-01")
    assert answer.data["status"] == "unverified"
    assert answer.data["access"] == []
    assert answer.data["entity"]["access"] == []
    assert "do not attempt SSH or VPN access" in answer.data["warnings"][0]


def test_known_good_version_is_unknown_without_current_evidence(
    session: Session,
) -> None:
    answer = question(session, "Q-R02")
    assert answer.data == {
        "question_id": "Q-R02",
        "subject": "aidevenv-feat",
        "value": None,
        "status": "unknown",
        "version": None,
        "confirmed_at": None,
        "warnings": ["No current known-good artifact has been verified."],
    }


def test_procedure_question_returns_retained_reference(
    catalog_copy: Path,
) -> None:
    procedure_file = Path(__file__).parents[1] / "migration-procedures.json"
    shutil.copy2(procedure_file, catalog_copy / procedure_file.name)
    session = Session.open(catalog_copy, now=NOW)
    answer = question(session, "Q-R01")
    procedure = answer.data["procedure"]
    assert answer.data["status"] == "documented-fallback"
    assert procedure["source"].endswith("ops/README.md")
    assert procedure["anchor"] == "#deploy-flow"


def test_unknown_question_id_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown question id"):
        question(session, "Q-X99")


def test_unplaced_observed_service_is_not_attributed_to_node_b(
    catalog_copy: Path,
) -> None:
    observed = catalog_copy / "observed"
    observed.mkdir()
    (observed / "orchestrator.json").write_text(
        json.dumps(
            {
                "v": 1,
                "source": "orchestrator",
                "plugin": "fixture",
                "as_of": "2026-08-07T12:00:00Z",
                "ok": True,
                "capabilities": ["inventory.list"],
                "entities": {"service": [{"id": "aidevenv-feat", "runs_on": ""}]},
            }
        ),
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW)
    for question_id in ("Q-H04", "Q-S01"):
        result = question(session, question_id).data
        assert "aidevenv-feat" not in result["services"]
        assert "aidevenv-feat" in result["unplaced_observed"]


def test_missing_target_stops_topology_and_prerequisite_questions(
    session: Session,
) -> None:
    topology = question(session, "Q-D01").data
    prerequisites = question(session, "Q-D03").data
    assert topology["status"] == "blocked"
    assert topology["topologies"] == []
    assert topology["warnings"]
    assert prerequisites["status"] == "blocked"
    assert "unique topology" in prerequisites["missing"]


def test_port_ownership_keeps_structured_endpoint_fields(session: Session) -> None:
    result = question(session, "Q-P02", value="8910").data
    assert result["status"] == "unknown"
    assert all(
        set(item) >= {"service", "network", "address", "protocol", "source", "as_of"}
        for item in result["uses"]
    )


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_port_question_rejects_invalid_values(session: Session, value: str) -> None:
    result = question(session, "Q-P02", subject="app-01", value=value).data
    assert result["status"] == "invalid"
    assert result["error"]["kind"] == "invalid_value"


def test_explicit_empty_subject_does_not_use_default(session: Session) -> None:
    result = question(session, "Q-H01", subject="").data
    assert result["status"] == "invalid"
    assert result["error"]["kind"] == "invalid_argument"


def test_question_rejects_subject_of_wrong_kind(session: Session) -> None:
    result = question(session, "Q-H01", subject="notes-api").data
    assert result["status"] == "invalid"
    assert result["error"]["kind"] == "wrong_kind"
