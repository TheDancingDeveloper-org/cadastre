"""Explicit operational question contracts used by migration acceptance.

``context-for`` remains a placement query.  This module provides the small,
stable question surface needed when an operator asks about inventory,
provenance, topology, or a retained procedure.  It never turns missing or
stale evidence into a fact and it never executes a procedure.
"""

from __future__ import annotations

import json
from typing import Any

from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.drift import compare
from cadastre.core.model import Host
from cadastre.core.placement import placement_for
from cadastre.core.topology import select as select_topologies
from cadastre.core.trust import load_records
from cadastre.render.document import Document, Fields, Para, Section

QUESTION_IDS = (
    "Q-H01",
    "Q-H02",
    "Q-H03",
    "Q-H04",
    "Q-H05",
    "Q-S01",
    "Q-S02",
    "Q-S03",
    "Q-S04",
    "Q-P01",
    "Q-P02",
    "Q-P03",
    "Q-P04",
    "Q-D01",
    "Q-D02",
    "Q-D03",
    "Q-D04",
    "Q-D05",
    "Q-D06",
    "Q-R01",
    "Q-R02",
    "Q-R03",
    "Q-R04",
    "Q-R05",
    "Q-T01",
    "Q-T02",
    "Q-T03",
    "Q-T04",
)

DEFAULT_SUBJECTS = {
    "Q-H01": "node-b",
    "Q-H02": "node-b",
    "Q-H03": "node-b",
    "Q-H04": "node-b",
    "Q-H05": "node-b",
    "Q-S01": "node-b",
    "Q-S02": "aidevenv-feat",
    "Q-S03": "aidevenv-feat",
    "Q-S04": "aidevenv-feat",
    "Q-P01": "node-b",
    "Q-P02": "node-b",
    "Q-P03": "aidevenv.example.invalid",
    "Q-P04": "aidevenv-feat",
    "Q-D01": "aidevenv-feat",
    "Q-D02": "aidevenv-feat",
    "Q-D03": "aidevenv-feat",
    "Q-D04": "aidevenv-feat",
    "Q-D05": "aidevenv-feat",
    "Q-D06": "aidevenv-feat",
    "Q-R01": "aidevenv-feat",
    "Q-R02": "aidevenv-feat",
    "Q-R03": "aidevenv-feat",
    "Q-R04": "aidevenv-feat",
    "Q-R05": "aidevenv-feat",
}

# Subjects are deliberately typed at the question boundary.  A migration
# question about a host must not quietly interpret a service id as a host.
SUBJECT_KINDS = {
    **{
        question_id: "host"
        for question_id in (
            "Q-H01",
            "Q-H02",
            "Q-H03",
            "Q-H04",
            "Q-H05",
            "Q-S01",
            "Q-P01",
        )
    },
    **{
        question_id: "service"
        for question_id in ("Q-S02", "Q-S03", "Q-S04", "Q-P04", "Q-D01", "Q-D03")
    },
}


def _invalid_subject(
    session: Session, question_id: str, subject: str
) -> dict[str, Any] | None:
    expected = SUBJECT_KINDS.get(question_id)
    if expected is None:
        return None
    if not subject:
        return {
            "status": "invalid",
            "error": {
                "kind": "invalid_argument",
                "message": "subject must not be empty",
            },
        }
    entity = session.catalog.get(expected, subject)
    if entity is not None:
        return None
    if any(session.catalog.get(kind, subject) is not None for kind in model.KINDS):
        return {
            "status": "invalid",
            "error": {
                "kind": "wrong_kind",
                "message": f"subject {subject!r} is not a {expected}",
            },
        }
    return {
        "status": "unknown",
        "error": {
            "kind": "unknown_entity",
            "message": f"unknown {expected} {subject!r}",
        },
    }


def _invalid_value(question_id: str, value: str | None) -> dict[str, Any] | None:
    if question_id != "Q-P02" or value is None:
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return {
            "status": "invalid",
            "error": {
                "kind": "invalid_value",
                "message": "port must be an integer from 1 to 65535",
            },
        }
    if not 1 <= port <= 65535:
        return {
            "status": "invalid",
            "error": {
                "kind": "invalid_value",
                "message": "port must be an integer from 1 to 65535",
            },
        }
    return None


def _source(session: Session, name: str) -> Any:
    return session.observed_source(name)


def _source_stale(session: Session, name: str) -> bool:
    item = _source(session, name)
    if item is None:
        return True
    return item.provenance(ttl_overrides=session.plugins.freshness).stale


def _entities(session: Session, source: str, kind: str) -> list[model.Entity]:
    item = _source(session, source)
    return list(item.entities.get(kind, ())) if item else []


def _entity_dict(entity: model.Entity | None) -> dict[str, Any] | None:
    if entity is None:
        return None
    from cadastre.core.serialize import entity_to_dict

    return entity_to_dict(entity)


def _procedure(session: Session, question_id: str) -> dict[str, Any]:
    path = session.root / "migration-procedures.json"
    if not path.is_file():
        return {
            "status": "unknown",
            "cadastre_facts": [],
            "warnings": ["No retained procedure reference is configured."],
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "cadastre_facts": [],
            "warnings": ["The procedure reference file could not be read."],
        }
    procedure = raw.get("procedures", {}).get(question_id)
    if not isinstance(procedure, dict):
        return {
            "status": "unknown",
            "cadastre_facts": [],
            "warnings": ["No retained procedure reference is configured."],
        }
    return {
        "status": "documented-fallback",
        "procedure": {
            "title": str(procedure.get("title", "Retained runbook")),
            "source": str(procedure.get("source", "")),
            "anchor": str(procedure.get("anchor", "")),
            "last_verified": str(procedure.get("last_verified", "unknown")),
        },
        "cadastre_facts": [],
        "warnings": [
            "Cadastre does not execute this procedure; consult the retained document."
        ],
    }


def _answer(
    session: Session, question_id: str, subject: str, value: str | None
) -> dict[str, Any]:
    catalog = session.catalog
    if question_id == "Q-H01":
        host = catalog.get("host", subject)
        status = "confirmed" if host else "unknown"
        if _source_stale(session, "static"):
            status = "unverified"
        return {
            "status": status,
            "entity": _entity_dict(host),
            "source": "declared+static",
        }

    if question_id == "Q-H02":
        host = catalog.get("host", subject)
        if not host:
            return {
                "status": "unknown",
                "entity": None,
                "access": [],
                "warnings": ["The target host is not declared in this catalog."],
            }
        if not isinstance(host, Host):
            return {
                "status": "unknown",
                "entity": None,
                "access": [],
                "warnings": ["The target is not a host entity."],
            }
        if _source_stale(session, "static"):
            return {
                "status": "unverified",
                "entity": {
                    "id": host.id,
                    "role": host.role,
                    "reachable_from": list(host.reachable_from),
                    "access": [],
                },
                "access": [],
                "warnings": [
                    "Host access evidence is stale or unavailable; do not attempt "
                    "SSH or VPN access from this answer."
                ],
            }
        access = [
            {
                "kind": item.kind,
                "via": item.via,
                "role": item.role,
                "reachable_from": list(item.reachable_from),
            }
            for item in host.access
        ]
        return {
            "status": "confirmed",
            "entity": {
                "id": host.id,
                "role": host.role,
                "reachable_from": list(host.reachable_from),
                "access": access,
            },
            "access": access,
            "source": "declared+static",
        }

    if question_id == "Q-H03":
        placement = placement_for(catalog, f"on {subject}")
        eligible = any(candidate.host == subject for candidate in placement.candidates)
        if _source_stale(session, "static"):
            return {
                "status": "unverified",
                "eligible": False,
                "exclusions": [e.to_dict() for e in placement.exclusions],
            }
        return {
            "status": "confirmed",
            "eligible": eligible,
            "exclusions": [e.to_dict() for e in placement.exclusions],
        }

    if question_id == "Q-H04":
        observed_entities = _entities(session, "orchestrator", "service")
        observed = [
            e.id
            for e in observed_entities
            if isinstance(e, model.Service) and e.runs_on == subject
        ]
        declared = [s.id for s in catalog.services if s.runs_on == subject]
        return {
            "status": "stale" if _source_stale(session, "orchestrator") else "observed",
            "services": sorted(set(declared) | set(observed)),
            "declared": declared,
            "observed": observed,
            "unplaced_observed": sorted(
                e.id
                for e in observed_entities
                if isinstance(e, model.Service) and not e.runs_on
            ),
            "warnings": [
                "Observed services without runs_on are not attributed to this host."
            ],
        }

    if question_id == "Q-H05":
        return {
            "status": "unknown",
            "migration_status": "unknown",
            "warnings": ["No current migration/decommission entity is declared."],
        }

    if question_id == "Q-S01":
        observed_entities = _entities(session, "orchestrator", "service")
        declared = [s.id for s in catalog.services if s.runs_on == subject]
        observed = [
            e.id
            for e in observed_entities
            if isinstance(e, model.Service) and e.runs_on == subject
        ]
        return {
            "status": (
                "incomplete" if _source_stale(session, "orchestrator") else "observed"
            ),
            "services": sorted(set(declared) | set(observed)),
            "declared": declared,
            "observed": observed,
            "unplaced_observed": sorted(
                e.id
                for e in observed_entities
                if isinstance(e, model.Service) and not e.runs_on
            ),
            "warnings": [
                "Observed services without runs_on are not attributed to this host."
            ],
        }

    if question_id == "Q-S02":
        if catalog.get("service", subject) is None:
            return {
                "status": "unknown",
                "dependencies": [],
                "warnings": ["The target service is not declared in this catalog."],
            }
        relations = catalog.neighbors("service", subject)
        return {
            "status": "complete",
            "dependencies": [
                {
                    "relation": n.relation,
                    "direction": n.direction,
                    "kind": n.kind,
                    "id": n.id,
                }
                for n in relations
            ],
        }

    if question_id == "Q-S03":
        service = catalog.get("service", subject)
        pipelines = catalog.pipelines_for(subject)
        authoritative = catalog.authoritative_pipeline(subject)
        if authoritative:
            return {
                "status": "confirmed",
                "pipeline": authoritative,
                "candidates": [p.id for p in pipelines],
            }
        return {
            "status": "ambiguous" if len(pipelines) > 1 else "unknown",
            "candidates": [p.id for p in pipelines],
            "service": _entity_dict(service),
        }

    if question_id == "Q-S04":
        matches = [t for t in catalog.deployment_topologies if t.target == subject]
        if len(matches) != 1:
            return {
                "status": "unknown",
                "topologies": [t.id for t in matches],
                "warnings": ["No unique deployment topology is declared."],
            }
        topology_s04 = matches[0]
        return {"status": "confirmed", "entity": _entity_dict(topology_s04)}

    if question_id == "Q-P01":
        ports = catalog.ports_on_host(subject)
        return {
            "status": "unknown",
            "candidates": [],
            "observed": sorted(ports),
            "occupancy": ports,
            "warnings": ["No current host port inventory is available."],
        }

    if question_id == "Q-P02":
        port = int(value or "8910")
        uses = [
            {
                "service": e.service,
                "network": e.network,
                "address": e.address,
                "protocol": e.protocol,
                "source": "declared",
                "as_of": session.declared_as_of,
            }
            for e in catalog.endpoints
            if e.port == port
        ]
        return {
            "status": "unknown",
            "port": port,
            "uses": uses,
            "warnings": [
                "Endpoint evidence is not current enough to establish ownership."
            ],
        }

    if question_id == "Q-P03":
        hostname = value or subject
        collisions = [d.id for d in catalog.domains if d.name == hostname]
        return {
            "status": "unknown",
            "hostname": hostname,
            "collisions": collisions,
            "warnings": ["DNS evidence is stale; availability is not inferred."],
        }

    if question_id == "Q-P04":
        service = catalog.get("service", subject)
        tier = (
            catalog.policy.tier(service.expose)
            if isinstance(service, model.Service) and service.expose
            else None
        )
        if tier is None:
            return {
                "status": "unknown",
                "tier": None,
                "network_class": None,
                "requires_ingress": None,
            }
        return {
            "status": "confirmed",
            "tier": tier.name,
            "network_class": tier.network_class,
            "requires_ingress": tier.requires_ingress,
        }

    if question_id == "Q-D01":
        matches_d01 = select_topologies(catalog, f"deploy {subject}")
        return {
            "status": "confirmed" if len(matches_d01) == 1 else "blocked",
            "topologies": [
                {"topology": _entity_dict(m.topology), "because": list(m.because)}
                for m in matches_d01
            ],
            "warnings": (
                []
                if len(matches_d01) == 1
                else [
                    "No unique topology is declared for this target; deployment stops."
                ]
            ),
        }

    if question_id in {"Q-D02", "Q-D06", "Q-R01", "Q-R03", "Q-R04", "Q-R05"}:
        return _procedure(session, question_id)

    if question_id == "Q-D03":
        service = catalog.get("service", subject)
        typed_service = service if isinstance(service, model.Service) else None
        return {
            "status": "blocked",
            "target": subject,
            "port": None,
            "hostname": None,
            "secrets": list(typed_service.consumes_secret) if typed_service else [],
            "pipeline": (
                typed_service.deployed_by[0].pipeline
                if typed_service and typed_service.deployed_by
                else None
            ),
            "health_check": None,
            "backup": None,
            "missing": [
                "unique topology",
                "current port",
                "hostname",
                "health check",
                "backup",
            ],
        }

    if question_id == "Q-D04":
        return {
            "status": "blocked",
            "artifact_diff": [],
            "catalog_diff": [],
            "effects": [],
            "risk": (
                "live impact cannot be enumerated without an approved canary artifact"
            ),
        }

    if question_id == "Q-D05":
        return {
            "status": "blocked",
            "isolated": False,
            "shared_resources": [
                "unknown workspace ownership",
                "unknown live port occupancy",
                "unknown DNS collision",
            ],
        }

    if question_id == "Q-R02":
        return {
            "status": "unknown",
            "version": None,
            "confirmed_at": None,
            "warnings": ["No current known-good artifact has been verified."],
        }

    if question_id == "Q-T01":
        return {
            "claims": [
                {
                    "kind": d.kind,
                    "id": d.id,
                    "field": d.field,
                    "classification": d.category,
                    "declared": d.declared,
                    "observed": d.observed,
                    "source": d.source,
                }
                for d in compare(catalog, list(session.observed))
            ]
        }

    if question_id == "Q-T02":
        return {
            "comparison": {
                "cli": "shadow acceptance result is recorded in parity-report.json",
                "mcp": "shadow acceptance result is recorded in parity-report.json",
                "documentation": (
                    "retained fallback remains authoritative for procedures"
                ),
            },
            "status": "shadow-confirmed-live-unestablished",
            "evidence": "migration-evidence/parity-report.json",
        }

    if question_id == "Q-T03":
        stale = [p.source for p in session.provenance() if p.stale]
        contested = [
            r.to_dict() for r in load_records(session.root) if r.state == "contested"
        ]
        return {"stale": stale, "contested": contested}

    if question_id == "Q-T04":
        return {
            "omissions": [
                {
                    "area": "live Node B state",
                    "status": "not-imported",
                    "reason": "requires current collector evidence",
                },
                {
                    "area": "deployment and rollback procedures",
                    "status": "retained-document",
                    "reason": "Cadastre does not execute runbooks",
                },
                {
                    "area": "AiDevEnv runtime identity",
                    "status": "cadastre-missing",
                    "reason": (
                        "ops identity is not yet represented as a catalog service"
                    ),
                },
            ],
            "retained_documents": [
                "DEPLOYMENT.md",
                "Active/apps/aidevenv-oss/.github/workflows/deploy-internal.yml",
                "Active/apps/ops/README.md",
            ],
        }

    return {
        "status": "unknown",
        "warnings": [f"No contract is defined for {question_id}."],
    }


def question(
    session: Session,
    question_id: str,
    *,
    subject: str | None = None,
    value: str | None = None,
) -> Document:
    if question_id not in QUESTION_IDS:
        raise ValueError(
            f"unknown question id {question_id!r}; expected one of "
            f"{', '.join(QUESTION_IDS)}"
        )
    resolved_subject = (
        DEFAULT_SUBJECTS.get(question_id, "") if subject is None else subject
    )
    validation = (
        _invalid_subject(session, question_id, resolved_subject)
        if subject is not None
        else None
    )
    if validation is None:
        validation = _invalid_value(question_id, value)
    data = {
        "question_id": question_id,
        "subject": resolved_subject,
        "value": value,
        **(validation or _answer(session, question_id, resolved_subject, value)),
    }
    return Document(
        title=f"cadastre question {question_id}",
        sections=(
            Section(
                "Question",
                (Fields((("id", question_id), ("subject", resolved_subject))),),
            ),
            Section("Answer", (Para(json.dumps(data, sort_keys=True)),)),
        ),
        provenance=session.provenance(),
        data=data,
    )
