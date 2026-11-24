"""`cadastre context-for` — the subset of truth relevant to one decision.

Not "here are 12 hosts". Candidates *and why the others were excluded*, the
conventions in force, the conflicts already present, and per-source provenance.

The exclusion list is not filler. If an exclusion looks wrong to the reader,
that is a signal the catalog is wrong — which is the whole point of printing
it (DESIGN §3.2).
"""

from __future__ import annotations

import re
from typing import Any

from cadastre.cli.lookup import lookup
from cadastre.cli.session import Session
from cadastre.core.errors import AmbiguousEntityError, MissingEntityError
from cadastre.core.placement import Exclusion, placement_for
from cadastre.core.topology import select as select_topologies
from cadastre.core.trust import contest_policy, presented_records
from cadastre.render.document import Bullets, Document, Fields, Para, Section, Table

#: Verbs that ask about reaching an existing thing, not placing a new one.
_ACCESS_VERBS = re.compile(
    r"\b(?:ssh|sftp|scp|connect|reach|access|log\s?in|login|shell|rdp|console)\b"
)
_ACCESS_FILLER = {
    "ssh", "sftp", "scp", "connect", "reach", "access", "log", "login", "in",
    "into", "to", "onto", "shell", "rdp", "console", "the", "a", "an", "host",
    "server", "service", "machine", "box", "node", "via", "from", "how", "do",
    "i", "can", "me", "my", "please", "want", "need", "should", "using", "with",
}  # fmt: skip


def _access_document(session: Session, intent: str) -> Document | None:
    """Answer an access-verb intent about an existing entity, never placement.

    Returns None when the intent has no access verb, so placement proceeds.
    """
    text = intent.lower()
    if not _ACCESS_VERBS.search(text):
        return None
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9._-]*", text)]
    names = [w for w in words if w not in _ACCESS_FILLER]
    for name in names:
        try:
            return lookup(session, name)
        except (MissingEntityError, AmbiguousEntityError) as error:
            if isinstance(error, AmbiguousEntityError):
                raise
    return Document(
        title=f"cadastre context-for {intent}",
        sections=(
            Section(
                "Interpreted as",
                (
                    Para(
                        "An access request for an existing host or service. "
                        "No placement candidates are offered for it."
                    ),
                ),
            ),
            Section(
                "Not found",
                (
                    Para(
                        "No catalog or observed entity matches: "
                        + (", ".join(names) or "(no name in the intent)")
                        + ". If it should exist, the catalog is wrong; say so "
                        "rather than assuming a name."
                    ),
                ),
            ),
        ),
        provenance=session.provenance(),
        data={"intent": intent, "access": True, "matched": None, "names": names},
    )


def context_for(session: Session, intent: str) -> Document:
    access = _access_document(session, intent)
    if access is not None:
        return access
    catalog = session.catalog
    placement = placement_for(catalog, intent)
    contests = {
        (record.kind, record.id): record
        for record in presented_records(session.root, session.now)
        if record.state == "contested"
    }
    contested_exclusions = []
    for host in list(placement.candidates):
        record = contests.get(("host", host.host))
        if record and contest_policy(session.root, "host", record.field) == "exclude":
            contested_exclusions.append(
                (host.host, "contested", f"since {record.first_seen}")
            )
    if contested_exclusions:
        placement = placement.__class__(
            requirements=placement.requirements,
            candidates=tuple(
                c
                for c in placement.candidates
                if c.host not in {row[0] for row in contested_exclusions}
            ),
            exclusions=placement.exclusions
            + tuple(Exclusion(*row) for row in contested_exclusions),
            conflicts=placement.conflicts,
            notes=placement.notes,
        )
    requirements = placement.requirements
    topologies = select_topologies(catalog, intent)

    interpreted: list[Any] = [
        Fields(requirements.summary() or (("", "nothing specific"),))
    ]
    if requirements.unrecognised:
        interpreted.append(
            Para(
                "Not understood, and therefore not applied: "
                + ", ".join(requirements.unrecognised)
                + ". Intent parsing here is keyword-based on purpose. If one of "
                "those words carried a requirement, state it as a flag or a tag "
                "rather than assuming it was honoured."
            )
        )

    candidate_rows = tuple(
        (candidate.host, "; ".join(candidate.because))
        for candidate in placement.candidates
    )
    exclusion_rows = tuple(
        (exclusion.host, exclusion.reason, exclusion.detail or "")
        for exclusion in placement.exclusions
    )

    conventions = catalog.policy.conventions
    convention_items = tuple(
        (label, value)
        for label, value in (
            ("host name", conventions.host_name),
            ("service name", conventions.service_name),
            ("secret ref", conventions.secret_ref),
            ("endpoint address", conventions.endpoint_address),
        )
        if value
    )

    sections = [
        Section("Interpreted as", tuple(interpreted)),
        Section(
            "Candidates",
            (
                Table(
                    ("host", "why"),
                    candidate_rows,
                    empty_note=(
                        "(no host qualifies — read the exclusions below; if one "
                        "looks wrong, the catalog is wrong)"
                    ),
                ),
            ),
        ),
        Section(
            "Excluded",
            (Table(("host", "reason", "detail"), exclusion_rows),),
            note=(
                "These carry equal weight to the candidates. An exclusion you "
                "believe is wrong is a catalog bug — surface it, do not route "
                "around it."
            ),
        ),
        Section(
            "Conventions in force",
            (Fields(convention_items),),
            note="`cadastre check` enforces these. Match them before committing.",
        ),
        Section(
            "Conflicts",
            (
                Table(
                    ("kind", "subject", "detail"),
                    tuple((c.kind, c.subject, c.detail) for c in placement.conflicts),
                    empty_note="(none detected for what the intent specified)",
                ),
            ),
        ),
    ]
    if placement.notes:
        sections.append(Section("Notes", (Bullets(placement.notes),)))
    if topologies:
        sections.append(
            Section(
                "Deployment topology",
                (
                    Table(
                        ("topology", "path"),
                        tuple(
                            (
                                m.topology.id,
                                (
                                    f"{m.topology.repo} → {m.topology.pipeline} "
                                    f"→ {m.topology.target}"
                                ),
                            )
                            for m in topologies
                        ),
                    ),
                ),
                note=(
                    "This is the catalog's declared path from source to workload; "
                    "verify topology drift before following it."
                ),
            )
        )

    data: dict[str, Any] = {
        "intent": intent,
        "requirements": dict(requirements.summary()),
        "unrecognised": list(requirements.unrecognised),
        "candidates": [c.to_dict() for c in placement.candidates],
        "exclusions": [e.to_dict() for e in placement.exclusions],
        "conflicts": [c.to_dict() for c in placement.conflicts],
        "conventions": {
            "host_name": conventions.host_name,
            "service_name": conventions.service_name,
            "secret_ref": conventions.secret_ref,
            "endpoint_address": conventions.endpoint_address,
        },
        "notes": list(placement.notes),
        "topologies": [
            {
                "id": match.topology.id,
                "because": list(match.because),
                "topology": {
                    "repo": match.topology.repo,
                    "pipeline": match.topology.pipeline,
                    "target": match.topology.target,
                    "node": match.topology.node,
                    "artifact": match.topology.artifact,
                    "exposure": match.topology.exposure,
                },
            }
            for match in topologies
        ],
    }
    return Document(
        title=f"cadastre context-for {intent}",
        sections=tuple(sections),
        provenance=session.provenance(),
        data=data,
    )
