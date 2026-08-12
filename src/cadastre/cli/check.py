"""`cadastre check` — consult the map about a proposed artifact.

Read-only. It touches nothing, changes nothing, and runs before the commit.
The same validator runs in CI, so a policy violation cannot merge even if the
agent ignored the tool (DESIGN §3.3).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cadastre.cli.session import Session
from cadastre.core.artifacts import parse
from cadastre.core.rules import RuleContext, run
from cadastre.core.topology import check_artifact
from cadastre.core.topology import drift as topology_drift
from cadastre.render.document import Bullets, Document, Para, Section


def check(
    session: Session,
    artifact_path: Path,
    *,
    kind: str | None = None,
    warnings_as_errors: bool = False,
    display_path: str | None = None,
) -> Document:
    artifact = parse(artifact_path, kind)
    if display_path:
        artifact = replace(artifact, path=Path(display_path).name)
    context = RuleContext(
        catalog=session.catalog, artifact=artifact, observed=session.observed
    )
    findings = run(context)
    findings.extend(topology_drift(session.catalog))
    findings.extend(check_artifact(session.catalog, artifact))
    findings.sort(key=lambda finding: (finding.level, finding.code, finding.subject))
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warn"]

    sections: list[Section] = []
    if artifact.warnings:
        sections.append(
            Section(
                "Could not be checked",
                (Bullets(artifact.warnings),),
                note=(
                    "These are gaps in the artifact, not passes. A rule that had no "
                    "input did not run."
                ),
            )
        )
    if findings:
        sections.append(Section("Findings", tuple(findings)))
    else:
        sections.append(
            Section(
                "No findings",
                (
                    Para(
                        f"{artifact.path} does not violate any rule Cadastre can check "
                        "against the current catalog. That is a statement about the "
                        "rules and the catalog, not a guarantee the change is right."
                    ),
                ),
            )
        )

    failed = bool(errors) or (warnings_as_errors and bool(warnings))
    return Document(
        title=f"cadastre check {artifact.path}",
        sections=tuple(sections),
        provenance=session.provenance(),
        data={
            "artifact": {"path": artifact.path, "kind": artifact.kind},
            "findings": [f.to_dict() for f in findings],
            "counts": {
                "error": len(errors),
                "warn": len(warnings),
                "info": len(findings) - len(errors) - len(warnings),
            },
            "unchecked": list(artifact.warnings),
        },
        exit_code=1 if failed else 0,
    )
