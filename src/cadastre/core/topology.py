"""Selection and health checks for catalog deployment topologies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cadastre.core import model
from cadastre.core.catalog import Catalog
from cadastre.render.document import Finding


@dataclass(frozen=True)
class TopologyMatch:
    topology: model.DeploymentTopology
    because: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"topology": self.topology.id, "because": list(self.because)}


def select(catalog: Catalog, intent: str) -> tuple[TopologyMatch, ...]:
    """Return topologies explicitly suggested by the operator's intent."""
    text = intent.lower()
    matches: list[TopologyMatch] = []
    for topology in catalog.all("deployment_topology"):
        assert isinstance(topology, model.DeploymentTopology)
        terms = (topology.id, topology.repo, topology.path_pattern)
        matched = next((term for term in terms if term and term.lower() in text), None)
        if matched:
            matches.append(TopologyMatch(topology, (f"intent names {matched}",)))
    return tuple(matches)


def drift(catalog: Catalog) -> tuple[Finding, ...]:
    """Find topology claims whose referenced estate has disappeared."""
    findings: list[Finding] = []
    for topology in catalog.all("deployment_topology"):
        assert isinstance(topology, model.DeploymentTopology)
        if topology.pipeline and catalog.get("pipeline", topology.pipeline) is None:
            findings.append(
                Finding(
                    "error",
                    "topology-missing-pipeline",
                    topology.id,
                    f"Topology names missing pipeline `{topology.pipeline}`.",
                    why=(
                        "A topology cannot deliver an artifact through a pipeline "
                        "that is absent from the catalog."
                    ),
                    fix=(
                        "Update the topology or declare the pipeline through its "
                        "source plugin."
                    ),
                )
            )
        if topology.repo and catalog.get("repo", topology.repo) is None:
            findings.append(
                Finding(
                    "error",
                    "topology-missing-repo",
                    topology.id,
                    f"Topology names missing repo `{topology.repo}`.",
                    why=(
                        "The topology's source repository is not represented in the "
                        "catalog."
                    ),
                    fix=(
                        "Correct the topology or collect the repository from its forge."
                    ),
                )
            )
        if topology.node and catalog.get("host", topology.node) is None:
            findings.append(
                Finding(
                    "error",
                    "topology-missing-node",
                    topology.id,
                    f"Topology names missing node `{topology.node}`.",
                    why=(
                        "Following a topology toward a decommissioned node would "
                        "silently route a deployment incorrectly."
                    ),
                    fix=(
                        "Update the topology to a declared host or collect the host "
                        "inventory."
                    ),
                )
            )
        if (
            topology.target_kind in model.KINDS
            and catalog.get(topology.target_kind, topology.target) is None
        ):
            findings.append(
                Finding(
                    "error",
                    "topology-missing-target",
                    topology.id,
                    f"Topology names missing {topology.target_kind} "
                    f"`{topology.target}`.",
                    why=(
                        "The topology's workload target is not represented in the "
                        "catalog."
                    ),
                    fix=(
                        "Update the topology or declare the target through its "
                        "source plugin."
                    ),
                )
            )
        if topology.exposure and catalog.policy.tier(topology.exposure) is None:
            findings.append(
                Finding(
                    "error",
                    "topology-missing-exposure",
                    topology.id,
                    f"Topology names missing exposure tier `{topology.exposure}`.",
                    why=(
                        "The topology's intended exposure cannot be checked against "
                        "policy."
                    ),
                    fix="Use a declared exposure tier.",
                )
            )
    return tuple(sorted(findings, key=lambda finding: (finding.code, finding.subject)))


def check_artifact(catalog: Catalog, artifact: Any) -> tuple[Finding, ...]:
    """Check the explicit topology claim carried by a proposed artifact."""
    topology_id = getattr(artifact, "topology", None)
    if not topology_id:
        return ()
    topology = catalog.get("deployment_topology", topology_id)
    if not isinstance(topology, model.DeploymentTopology):
        return (
            Finding(
                "error",
                "topology-unknown",
                f"artifact:{topology_id}",
                f"Artifact claims unknown topology `{topology_id}`.",
                why=(
                    "A deployment artifact must name a catalog topology before "
                    "its path can be checked."
                ),
                fix=(
                    "Use a declared topology id or add the topology through the "
                    "gated write path."
                ),
            ),
        )
    findings: list[Finding] = []
    artifact_repo = getattr(artifact, "repo", None)
    if artifact_repo and artifact_repo != topology.repo:
        findings.append(
            Finding(
                "error",
                "topology-repo-mismatch",
                f"artifact:{topology_id}",
                (
                    f"Artifact repo `{artifact_repo}` differs from topology repo "
                    f"`{topology.repo}`."
                ),
                why=(
                    "Following a topology for a different repository could deploy "
                    "the wrong source."
                ),
                fix=f"Use repo `{topology.repo}` or claim the matching topology.",
            )
        )
    if topology.target_kind == "service":
        names = {service.name for service in getattr(artifact, "services", ())}
        if names and topology.target not in names:
            findings.append(
                Finding(
                    "error",
                    "topology-target-mismatch",
                    f"artifact:{topology_id}",
                    f"Artifact does not contain topology target `{topology.target}`.",
                    why=(
                        "The declared path ends at a workload different from the "
                        "artifact's workload."
                    ),
                    fix=(
                        f"Include service `{topology.target}` or claim the matching "
                        "topology."
                    ),
                )
            )
    return tuple(findings)
