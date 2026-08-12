"""Parsing a not-yet-committed artifact into the few facts the rules need.

`check` is read-only and touches nothing. It reads a proposed Compose file,
ingress config, pipeline definition, or grants file, and reduces it to: what is
being placed, where, on which ports and hostnames, consuming which secret
references.

Deliberately shallow. This is not a Compose implementation — it extracts the
handful of fields the policy rules ask about, and says so when it cannot find
them rather than inventing a default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cadastre.core import model
from cadastre.core.errors import UsageError
from cadastre.core.yamlio import load_yaml

#: The key an operator uses to tell Cadastre what a Compose file does not say:
#: which host, which exposure tier, what the service is called in the catalog.
CADASTRE_KEY = "x-cadastre"

_PORT_MAPPING = re.compile(
    r"^(?:(?P<host>\[[0-9a-f:]+\]|[\d.]+):)?"
    r"(?P<published>\d+):(?P<target>\d+)(?:/(?P<protocol>\w+))?$",
    re.IGNORECASE,
)
_SECRET_SHAPED = re.compile(r"^(?:/|secret:|secret_ref:)")
_FILESYSTEM_PATH_PREFIXES = (
    "/app/",
    "/etc/",
    "/home/",
    "/mnt/",
    "/opt/",
    "/run/",
    "/srv/",
    "/tmp/",
    "/usr/",
    "/var/",
)


@dataclass(frozen=True)
class ProposedService:
    """One unit of work the artifact proposes to run."""

    name: str
    host: str | None = None
    expose: str | None = None
    ports: tuple[int, ...] = ()
    bindings: tuple[PortBinding, ...] = ()
    hostnames: tuple[str, ...] = ()
    secret_refs: tuple[str, ...] = ()
    image: str | None = None
    fronted_by: str | None = None
    repo: str | None = None


@dataclass(frozen=True, order=True)
class PortBinding:
    """A published host listener, rather than merely a port number."""

    port: int
    protocol: str = "tcp"
    bind_address: str | None = None


@dataclass(frozen=True)
class ExecutionRequirement:
    """Where one job of a proposed pipeline says it wants to run.

    Neutral by construction. `runs-on` is GitHub's spelling and Woodpecker has
    another; what reaches a rule is a kind, a label set, and an optional pool.

    `kind` is one of `labels`, `pool`, `indeterminate` (the selector contains an
    expression whose value needs runtime context Cadastre does not have),
    `absent` (the job delegates elsewhere), or `unrecognised`.
    """

    job: str
    kind: str = "absent"
    labels: tuple[str, ...] = ()
    pool: str | None = None
    expressions: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Artifact:
    """The parsed proposal."""

    kind: str
    path: str
    services: tuple[ProposedService, ...] = ()
    hostnames: tuple[tuple[str, str], ...] = ()  # (hostname, what claims it)
    secret_refs: tuple[str, ...] = ()
    grants: tuple[model.Grant, ...] = ()
    pipeline_systems: tuple[str, ...] = ()
    repo: str | None = None
    topology: str | None = None
    executions: tuple[ExecutionRequirement, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)


def infer_kind(path: Path) -> str:
    """Guess the artifact type from the filename. Wrong guesses are cheap —
    `--kind` overrides — but a silent wrong guess is not, so this is narrow."""
    name = path.name.lower()
    if "compose" in name:
        return "compose"
    if "grants" in name:
        return "grants"
    if "caddy" in name or "ingress" in name or "proxy" in name:
        return "ingress"
    if any(part in name for part in (".ci", "workflow", "pipeline", "woodpecker")):
        return "pipeline"
    if path.parent.name in (".github", "workflows", ".woodpecker", ".ci"):
        return "pipeline"
    raise UsageError(
        f"cannot tell what kind of artifact {path} is. "
        "Pass --kind compose|ingress|pipeline|grants."
    )


def _load(path: Path) -> Any:
    if not path.exists():
        raise UsageError(f"no such artifact: {path}")
    if path.suffix == ".json":
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UsageError(f"{path}: not valid JSON: {exc}") from exc
    return load_yaml(path, rel=str(path))


def _port_bindings(raw: Any) -> tuple[PortBinding, ...]:
    out: list[PortBinding] = []
    for item in raw or []:
        if isinstance(item, int):
            out.append(PortBinding(item))
        elif isinstance(item, str):
            found = _PORT_MAPPING.match(item.strip())
            if found:
                address = found.group("host")
                out.append(
                    PortBinding(
                        int(found.group("published")),
                        (found.group("protocol") or "tcp").lower(),
                        address.strip("[]") if address else None,
                    )
                )
            elif item.strip().isdigit():
                out.append(PortBinding(int(item.strip())))
        elif isinstance(item, dict) and "published" in item:
            try:
                address = item.get("host_ip")
                out.append(
                    PortBinding(
                        int(item["published"]),
                        str(item.get("protocol") or "tcp").lower(),
                        str(address).strip("[]") if address else None,
                    )
                )
            except (TypeError, ValueError):
                continue
    return tuple(sorted(set(out)))


def _ports(raw: Any) -> tuple[int, ...]:
    """Compatibility view used by callers that only need port numbers."""
    return tuple(sorted({binding.port for binding in _port_bindings(raw)}))


def _environment_values(raw: Any) -> list[tuple[str, str]]:
    if isinstance(raw, dict):
        return [
            (str(key), str(value)) for key, value in raw.items() if value is not None
        ]
    if isinstance(raw, list):
        return [
            (str(item).split("=", 1)[0], str(item).split("=", 1)[1])
            for item in raw
            if "=" in str(item)
        ]
    return []


def _looks_like_secret_reference(key: str, value: str) -> bool:
    if not _SECRET_SHAPED.match(value):
        return False
    # Absolute container paths are ordinary configuration, not secret refs.
    # Keep slash-shaped values outside these conventional filesystem roots
    # visible to the secret-reference rules, including malformed references.
    if value.startswith(_FILESYSTEM_PATH_PREFIXES):
        return False
    return key.upper() not in {"PATH", "PWD", "HOME"} or value.startswith(
        ("secret:", "secret_ref:")
    )


def _secret_refs(service: dict[str, Any], cadastre: dict[str, Any]) -> tuple[str, ...]:
    refs: list[str] = [str(r) for r in (cadastre.get("secrets") or [])]
    for key, value in _environment_values(service.get("environment")):
        if _looks_like_secret_reference(key, value):
            refs.append(
                value.split(":", 1)[-1] if value.startswith("secret") else value
            )
    for item in service.get("secrets") or []:
        if isinstance(item, str):
            refs.append(item)
        elif isinstance(item, dict) and "source" in item:
            refs.append(str(item["source"]))
    return tuple(sorted(set(refs)))


def parse_compose(path: Path, data: Any) -> Artifact:
    if not isinstance(data, dict):
        raise UsageError(
            f"{path}: expected a Compose file (a mapping at the top level)"
        )
    top = data.get(CADASTRE_KEY) or {}
    if not isinstance(top, dict):
        top = {}
    services: list[ProposedService] = []
    warnings: list[str] = []
    raw_services = data.get("services") or {}
    if not isinstance(raw_services, dict):
        raise UsageError(f"{path}: `services` must be a mapping")
    for name, raw in sorted(raw_services.items()):
        if not isinstance(raw, dict):
            continue
        cadastre = raw.get(CADASTRE_KEY) or {}
        if not isinstance(cadastre, dict):
            cadastre = {}
        host = cadastre.get("host") or top.get("host")
        if host is None:
            warnings.append(
                f"service `{name}` does not say which host it goes on. Add "
                f"`{CADASTRE_KEY}: {{host: <host-id>}}` — placement rules cannot run "
                "without it, and Cadastre will not guess."
            )
        hostnames = tuple(str(h) for h in (cadastre.get("hostnames") or []))
        bindings = _port_bindings(raw.get("ports"))
        services.append(
            ProposedService(
                name=str(cadastre.get("name") or name),
                host=str(host) if host else None,
                expose=str(cadastre["expose"])
                if cadastre.get("expose")
                else top.get("expose"),
                ports=tuple(sorted({binding.port for binding in bindings})),
                bindings=bindings,
                hostnames=hostnames,
                secret_refs=_secret_refs(raw, cadastre),
                image=str(raw.get("image")) if raw.get("image") else None,
                fronted_by=cadastre.get("fronted_by") or top.get("fronted_by"),
                repo=top.get("repo"),
            )
        )
    return Artifact(
        kind="compose",
        path=str(path),
        services=tuple(services),
        hostnames=tuple(
            (hostname, f"service {service.name}")
            for service in services
            for hostname in service.hostnames
        ),
        secret_refs=tuple(
            sorted({ref for service in services for ref in service.secret_refs})
        ),
        repo=top.get("repo"),
        topology=str(top["topology"]) if top.get("topology") else None,
        warnings=tuple(warnings),
    )


def _caddy_hostnames(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Hostnames from a Caddy JSON config. The admin API returns the full
    running config, so no scraping is involved."""
    out: list[tuple[str, str]] = []
    servers = (
        data.get("apps", {}).get("http", {}).get("servers", {})
        if isinstance(data.get("apps"), dict)
        else {}
    )
    if not isinstance(servers, dict):
        return out
    for server_name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            continue
        for index, route in enumerate(server.get("routes") or []):
            if not isinstance(route, dict):
                continue
            for match in route.get("match") or []:
                if not isinstance(match, dict):
                    continue
                for hostname in match.get("host") or []:
                    out.append((str(hostname), f"{server_name}.routes[{index}]"))
    return out


def parse_ingress(path: Path, data: Any) -> Artifact:
    if not isinstance(data, dict):
        raise UsageError(f"{path}: expected an ingress config (a mapping)")
    hostnames = _caddy_hostnames(data)
    # The generic shape, for anything that is not Caddy.
    for index, route in enumerate(data.get("routes") or []):
        if isinstance(route, dict) and route.get("host"):
            hostnames.append((str(route["host"]), f"routes[{index}]"))
    return Artifact(
        kind="ingress",
        path=str(path),
        hostnames=tuple(sorted(set(hostnames))),
        warnings=(
            () if hostnames else ("no hostnames found — is this an ingress config?",)
        ),
    )


def _pipeline_secret_refs(node: Any, found: set[str]) -> None:
    """CI definitions name secrets in several shapes. Walk for all of them."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("from_secret", "secret", "secret_ref") and isinstance(
                value, str
            ):
                found.add(value)
            elif key == "secrets" and isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        found.add(item)
                    elif isinstance(item, dict):
                        for candidate in ("source", "name", "from_secret"):
                            if isinstance(item.get(candidate), str):
                                found.add(item[candidate])
            else:
                _pipeline_secret_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _pipeline_secret_refs(item, found)


#: A CI expression. Never evaluated: deciding it needs the runtime context the
#: CI system has and Cadastre does not.
_EXPRESSION = "${{"


def _selector_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, dict):
        strings: list[str] = []
        for key in ("group", "pool"):
            if isinstance(value.get(key), str):
                strings.append(str(value[key]))
        labels = value.get("labels")
        if isinstance(labels, str):
            strings.append(labels)
        elif isinstance(labels, list):
            strings.extend(item for item in labels if isinstance(item, str))
        return strings
    return []


def _execution_requirement(
    job: str, value: Any, capabilities: tuple[str, ...]
) -> ExecutionRequirement:
    """One selector -> a neutral execution requirement.

    Only routing is read. Steps, scripts, conditions, and names are not parsed
    here and are never followed: a proposed pipeline is a file somebody wrote,
    and `check` reads it as data.
    """
    if value is None:
        return ExecutionRequirement(job=job, kind="absent", capabilities=capabilities)
    strings = _selector_strings(value)
    expressions = tuple(sorted({s for s in strings if _EXPRESSION in s}))
    if expressions:
        return ExecutionRequirement(
            job=job,
            kind="indeterminate",
            expressions=expressions,
            capabilities=capabilities,
        )
    if isinstance(value, dict):
        pool = value.get("group") or value.get("pool")
        named_pool = pool if isinstance(pool, str) else None
        labels = tuple(sorted(item for item in strings if item != named_pool))
        return ExecutionRequirement(
            job=job,
            kind="pool" if isinstance(pool, str) and pool else "labels",
            labels=labels,
            pool=str(pool) if isinstance(pool, str) and pool else None,
            capabilities=capabilities,
        )
    if not strings:
        return ExecutionRequirement(
            job=job, kind="unrecognised", capabilities=capabilities
        )
    return ExecutionRequirement(
        job=job,
        kind="labels",
        labels=tuple(sorted(strings)),
        capabilities=capabilities,
    )


def _executions(
    data: dict[str, Any], cadastre: dict[str, Any]
) -> tuple[ExecutionRequirement, ...]:
    """Every job's execution requirement, from whichever shape the file uses.

    Required capabilities are `x-cadastre.requires`, not guessed. A toolchain
    is not implied by an OS, a custom label, or a job that succeeded once.
    """
    default_capabilities = tuple(
        str(item) for item in (cadastre.get("requires") or ()) if isinstance(item, str)
    )
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return ()
    requirements = []
    for job_id, job in jobs.items():
        if not isinstance(job_id, str):
            continue
        capabilities = default_capabilities
        if isinstance(job, dict):
            job_cadastre = job.get(CADASTRE_KEY)
            if isinstance(job_cadastre, dict):
                capabilities = tuple(
                    str(item)
                    for item in (job_cadastre.get("requires") or ())
                    if isinstance(item, str)
                )
        requirements.append(
            _execution_requirement(
                job_id,
                job.get("runs-on") if isinstance(job, dict) else None,
                tuple(sorted(capabilities)),
            )
        )
    return tuple(sorted(requirements, key=lambda item: item.job))


def parse_pipeline(path: Path, data: Any) -> Artifact:
    if not isinstance(data, dict):
        raise UsageError(f"{path}: expected a pipeline definition (a mapping)")
    cadastre = data.get(CADASTRE_KEY) or {}
    if not isinstance(cadastre, dict):
        cadastre = {}
    refs: set[str] = set()
    _pipeline_secret_refs(data, refs)
    system = cadastre.get("system")
    if not system:
        # Shape, not filename: `jobs:` is one CI's vocabulary, `steps:` another's.
        system = "ci-public" if "jobs" in data else "ci-selfhosted"
    return Artifact(
        kind="pipeline",
        path=str(path),
        secret_refs=tuple(sorted(refs)),
        pipeline_systems=(str(system),),
        repo=cadastre.get("repo"),
        topology=(str(cadastre["topology"]) if cadastre.get("topology") else None),
        executions=_executions(data, cadastre),
        services=tuple(
            ProposedService(name=str(name), repo=cadastre.get("repo"))
            for name in (cadastre.get("deploys") or [])
        ),
    )


def parse_grants(path: Path, data: Any) -> Artifact:
    items = data.get("grants") if isinstance(data, dict) else data
    grants = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        grants.append(
            model.Grant(
                principal=str(item.get("principal", "")),
                role=str(item.get("role", "")),
                targets=tuple(str(t) for t in (item.get("targets") or ())),
                actions=tuple(str(a) for a in (item.get("actions") or ())),
                deny=tuple(str(d) for d in (item.get("deny") or ())),
                ttl=item.get("ttl"),
                id=item.get("id"),
            )
        )
    return Artifact(kind="grants", path=str(path), grants=tuple(grants))


_PARSERS = {
    "compose": parse_compose,
    "ingress": parse_ingress,
    "pipeline": parse_pipeline,
    "grants": parse_grants,
}


def parse(path: Path, kind: str | None = None) -> Artifact:
    resolved = kind or infer_kind(path)
    parser = _PARSERS.get(resolved)
    if parser is None:
        raise UsageError(
            f"unknown artifact kind {resolved!r}; expected one of: "
            + ", ".join(sorted(_PARSERS))
        )
    # Rendered documents cross process and network boundaries.  Keep source
    # paths out of the canonical identity so CLI, HTTP, and MCP answers remain
    # comparable without leaking local filesystem layout.
    artifact = parser(path, _load(path))
    return Artifact(
        kind=artifact.kind,
        path=path.name,
        services=artifact.services,
        hostnames=artifact.hostnames,
        secret_refs=artifact.secret_refs,
        grants=artifact.grants,
        pipeline_systems=artifact.pipeline_systems,
        repo=artifact.repo,
        topology=artifact.topology,
        executions=artifact.executions,
        warnings=artifact.warnings,
    )
