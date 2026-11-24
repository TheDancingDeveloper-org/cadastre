"""`cadastre access` — how to connect to one host, from declared data only.

Answers the five things a caller needs before it opens a shell: the login
user, the credential (a secret reference plus a ready agent-auth manifest
line), the path (network, address, tailnet/LAN/public), whether Tailscale SSH
is on, and the skill that covers it.

Nothing here is guessed. A fact the catalog does not declare is named in
`missing` rather than filled in. Declared sources are the host's `access`
entries, `reachable_from` networks and `aliases`, the secrets catalog, and
three tag conventions for what the model has no field for (a field needs a
question, and this is that question; see USING-CADASTRE.md):

* `ssh-user:NAME`   the login user
* `tailscale-ssh`   Tailscale SSH is enabled on the host
* `skill:NAME`      the agent skill that covers connecting to it

It never returns a secret value and never probes the host.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from cadastre.cli.search import (
    Hit,
    _hit_strings,
    all_hits,
    secret_location,
    tokens,
)
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import AmbiguousEntityError, MissingEntityError, UsageError
from cadastre.render.document import Bullets, Document, Fields, Para, Section, Table

_TAILNET = ipaddress.ip_network("100.64.0.0/10")
_USER_TAG = "ssh-user:"
_SKILL_TAG = "skill:"
_TAILSCALE_SSH_TAG = "tailscale-ssh"


def _tag_values(host: model.Host, prefix: str) -> list[str]:
    return sorted(
        t[len(prefix) :] for t in host.tags if t.startswith(prefix) and t[len(prefix) :]
    )


def resolve_host(session: Session, ident: str) -> model.Host:
    """A declared host by id, else by alias. Never guesses between two."""
    catalog = session.catalog
    found = catalog.get("host", ident)
    if isinstance(found, model.Host):
        return found
    aliased = catalog.find_by_alias(ident)
    if len(aliased) > 1:
        raise AmbiguousEntityError(
            f"{ident!r} is an alias of more than one host "
            f"({', '.join(h.id for h in aliased)}); ask about the host id instead."
        )
    if aliased:
        return aliased[0]
    raise MissingEntityError(
        f"no declared host answers to {ident!r} (id or alias). If it should "
        "exist, the catalog is wrong; say so rather than assuming a name."
    )


def _address_scope(value: str) -> str:
    """tailnet / lan / name, from the shape of an alias alone."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "tailnet" if value.lower().endswith(".ts.net") else "name"
    if address in _TAILNET:
        return "tailnet"
    if address.is_private:
        return "lan"
    return "public"


def _tailscale_evidence(session: Session, host: model.Host) -> dict[str, Any] | None:
    for source in session.observed:
        for entity in source.entities.get("host", ()):
            if entity.id == host.id:
                block = entity.extra.get("x-tailscale")
                if isinstance(block, dict):
                    return block
    return None


def _credentials(session: Session, host: model.Host) -> list[dict[str, Any]]:
    """Secrets whose id, reference or name carries the host's id as a token run."""
    wanted = tokens(host.id)
    found: list[dict[str, Any]] = []
    if not wanted:
        return found
    for hit in all_hits(session, kind="secret"):
        text = "-".join(tokens(" ".join(_hit_strings(hit))))
        if not _contains_run(text.split("-"), wanted):
            continue
        found.append(_credential_record(session, hit))
    return sorted(found, key=lambda record: str(record["id"]))


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    size = len(needle)
    return any(
        haystack[i : i + size] == needle for i in range(len(haystack) - size + 1)
    )


def _credential_record(session: Session, hit: Hit) -> dict[str, Any]:
    location = secret_location(session, hit)
    return {
        "id": hit.id,
        "ref": location["ref"],
        "store": location["store"],
        "project_id": location["project_id"],
        "environment": location["environment"],
        "path": location["path"],
        "manifest_line": location["manifest_line"],
    }


def access(session: Session, host: str) -> Document:
    """How to reach `host`: user, credential, path, Tailscale SSH, skill."""
    ident = (host or "").strip()
    if not ident:
        raise UsageError(
            "access needs `host`: a host id or alias, e.g. access(host='app-01')"
        )
    entity = resolve_host(session, ident)
    catalog = session.catalog
    missing: list[str] = []

    shell = [a for a in entity.access if a.kind == "shell"]
    paths = [
        {
            "kind": a.kind,
            "via": a.via,
            "role": a.role,
            "reachable_from": list(a.reachable_from),
        }
        for a in entity.access
    ]
    if not shell:
        missing.append("no shell access is declared on this host (host.access)")

    users = _tag_values(entity, _USER_TAG)
    if not users:
        missing.append(
            "login user: not declared (tag `ssh-user:NAME`); host.access.role is "
            "what the broker mints, not an account name"
        )

    credentials = _credentials(session, entity)
    if not credentials:
        missing.append(
            f"credential: no declared or observed secret names host {entity.id!r}"
        )
    elif not any(c["manifest_line"] for c in credentials):
        missing.append(
            "credential: the secret's project id is unknown, so no manifest "
            "line can be built"
        )

    network_ids = list(
        dict.fromkeys(
            [*entity.reachable_from, *(n for a in shell for n in a.reachable_from)]
        )
    )
    networks = []
    for network_id in network_ids:
        network = catalog.get("network", network_id)
        networks.append(
            {
                "id": network_id,
                "class": network.class_ if isinstance(network, model.Network) else None,
            }
        )
    addresses = [{"value": a, "scope": _address_scope(a)} for a in entity.aliases]
    evidence = _tailscale_evidence(session, entity)
    on_tailnet = bool(
        evidence is not None
        or "tailscale" in entity.tags
        or any(a["scope"] == "tailnet" for a in addresses)
    )
    if not networks and not addresses:
        missing.append("path: no reachable_from network or alias is declared")

    tailscale_ssh: bool | None
    if _TAILSCALE_SSH_TAG in entity.tags:
        tailscale_ssh = True
    elif isinstance(evidence, dict) and isinstance(evidence.get("ssh"), bool):
        tailscale_ssh = bool(evidence["ssh"])
    else:
        tailscale_ssh = None
        missing.append(
            "tailscale ssh: not declared (tag `tailscale-ssh`) or observed; "
            "unknown, not off"
        )

    skills = _tag_values(entity, _SKILL_TAG)
    if not skills:
        missing.append("skill: not declared (tag `skill:NAME`)")

    data: dict[str, Any] = {
        "host": entity.id,
        "queried_as": ident,
        "aliases": list(entity.aliases),
        "access": paths,
        "user": users[0] if len(users) == 1 else None,
        "users": users,
        "credentials": credentials,
        "networks": networks,
        "addresses": addresses,
        "tailnet": on_tailnet,
        "tailscale_ssh": tailscale_ssh,
        "skill": skills[0] if len(skills) == 1 else None,
        "skills": skills,
        "missing": missing,
    }

    def shown(value: object) -> str:
        return "(not declared)" if value in (None, "", []) else str(value)

    sections = [
        Section(
            f"host {entity.id}",
            (
                Fields(
                    (
                        ("queried as", ident),
                        ("login user", shown(", ".join(users))),
                        (
                            "tailscale ssh",
                            {True: "on", False: "off", None: "unknown"}[tailscale_ssh],
                        ),
                        ("skill", shown(", ".join(skills))),
                    )
                ),
            ),
        ),
        Section(
            "Path",
            (
                Table(
                    ("network", "class"),
                    tuple((str(n["id"]), shown(n["class"])) for n in networks),
                    empty_note="(no reachable_from network declared)",
                ),
                Table(
                    ("address", "scope"),
                    tuple((a["value"], a["scope"]) for a in addresses),
                    empty_note="(no alias declared)",
                ),
            ),
        ),
        Section(
            "Access",
            (
                Table(
                    ("kind", "via", "role", "reachable from"),
                    tuple(
                        (
                            str(p["kind"]),
                            str(p["via"]),
                            str(p["role"]),
                            ", ".join(p["reachable_from"]),
                        )
                        for p in paths
                    ),
                    empty_note="(none declared)",
                ),
            ),
        ),
        Section(
            "Credential",
            (
                Table(
                    ("secret", "store", "project id", "manifest line"),
                    tuple(
                        (
                            c["id"],
                            shown(c["store"]),
                            shown(c["project_id"]),
                            shown(c["manifest_line"]),
                        )
                        for c in credentials
                    ),
                    empty_note="(no secret names this host)",
                ),
                Para(
                    "References only; never a value. Fetch with the secret "
                    "tooling for your session's auth mode."
                ),
            ),
        ),
    ]
    if missing:
        sections.append(
            Section(
                "Missing",
                (Bullets(tuple(missing)),),
                note="Named, not guessed. Declare them to close the gap.",
            )
        )
    return Document(
        title=f"cadastre access {ident}",
        sections=tuple(sections),
        provenance=session.provenance(),
        data=data,
    )
