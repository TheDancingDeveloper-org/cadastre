"""`cadastre reachable` — may a caller's network context reach host:port?

Evaluates the Kubernetes NetworkPolicies declared in the GitOps source (the
ops repository checkout the `orchestrator-gitops` source reads) for a caller
named `k8s:<namespace>`, and returns allow or deny with the policy and rule
that decided it. Nothing is probed (DESIGN §1.3); the evaluator is
`cadastre.core.netpol`.

Only the Kubernetes layer is evaluated. A tailnet ACL or a host firewall can
still refuse a connection this calls allowed; that is stated in every answer.

The checkout is found, in order: the `CADASTRE_GITOPS_PATH` environment
variable, then the `path` of the first enabled `orchestrator-gitops` source in
`plugins.yaml` (relative paths resolve against the catalog root).
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

import yaml

from cadastre.cli.session import Session
from cadastre.core.catalog import Catalog
from cadastre.core.errors import AmbiguousEntityError, MissingEntityError, UsageError
from cadastre.core.model import Host
from cadastre.core.netpol import Policy, Verdict, evaluate_egress
from cadastre.render.document import Bullets, Document, Fields, Para, Section, Table

GITOPS_PLUGIN = "orchestrator-gitops"
GITOPS_ENV = "CADASTRE_GITOPS_PATH"
YAML_SUFFIXES = (".yaml", ".yml")
LAYER_NOTE = (
    "Kubernetes NetworkPolicy only. A tailnet ACL, host firewall or the "
    "destination service itself may still refuse a connection allowed here."
)


def parse_caller(raw: str) -> str:
    text = (raw or "").strip()
    scheme, sep, rest = text.partition(":")
    if not sep:
        raise UsageError(
            "reachable needs `from_context` as k8s:<namespace>, e.g. "
            "'k8s:vogt-sessions'"
        )
    if scheme.lower() != "k8s" or not rest.strip():
        raise UsageError(
            f"unsupported caller context {text!r}: only k8s:<namespace> is "
            "evaluated (tailnet ACLs are not yet supported)"
        )
    return rest.strip()


def parse_target(raw: str) -> tuple[str, int]:
    """Split `to` into (host, port). The host is an IP or a catalog name."""
    text = (raw or "").strip()
    if text.startswith("["):
        host, _, port_text = text[1:].partition("]:")
    else:
        host, _, port_text = text.rpartition(":")
    host = host.strip()
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    if not host or not port_text.strip().isdigit():
        raise UsageError(
            f"reachable needs `to` as <host>:<port>, e.g. 'winrarhost:22' or "
            f"'100.92.54.45:22'; got {text!r}"
        )
    if not 0 < port < 65536:
        raise UsageError(f"port {port} is out of range")
    return host, port


Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _as_ip(text: str) -> Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def resolve_host(catalog: Catalog | None, host: str) -> list[Address]:
    """The addresses `host` stands for: itself if an IP, else a catalog host's
    IP aliases (matched by host id, then by alias).

    An unknown name is a MissingEntityError (not-found), not a usage error.
    """
    literal = _as_ip(host)
    if literal is not None:
        return [literal]
    if catalog is None:
        raise UsageError(
            f"cannot resolve {host!r} without a catalog; give `to` as <ip>:<port>"
        )
    hosts = [e for kind, e in catalog.find(host) if isinstance(e, Host)]
    if not hosts:
        hosts = catalog.find_by_alias(host)
    if not hosts:
        raise MissingEntityError(
            f"no host {host!r} in the catalog (looked up by id and alias); "
            "use `lookup` to find the host or give an <ip>:<port>"
        )
    if len(hosts) > 1:
        raise AmbiguousEntityError(
            f"{host!r} is an alias of more than one host "
            f"({', '.join(h.id for h in hosts)}); use the host id"
        )
    addresses: list[Address] = []
    for alias in hosts[0].aliases:
        ip = _as_ip(alias)
        if ip is not None and ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise MissingEntityError(
            f"host {hosts[0].id!r} has no IP address among its aliases, so its "
            "network reachability cannot be evaluated; add the address as an alias"
        )
    return addresses


def gitops_checkout(session: Session) -> Path:
    override = os.environ.get(GITOPS_ENV)
    if override:
        return Path(override)
    for source in session.plugins.sources:
        if source.enabled and source.plugin == GITOPS_PLUGIN:
            path = source.config.get("path")
            if path:
                candidate = Path(str(path))
                return (
                    candidate if candidate.is_absolute() else session.root / candidate
                )
    raise UsageError(
        f"no GitOps checkout configured: set {GITOPS_ENV} or give the "
        f"`{GITOPS_PLUGIN}` source a `path` in plugins.yaml"
    )


def _kustomize_namespace(directory: Path) -> str | None:
    for name in ("kustomization.yaml", "kustomization.yml"):
        path = directory / name
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                return None
            if isinstance(data, dict) and data.get("namespace"):
                return str(data["namespace"])
    return None


def load_policies(checkout: Path) -> list[Policy]:
    """Every NetworkPolicy document under `checkout`.

    A policy without `metadata.namespace` takes its directory's kustomization
    `namespace`, else the directory name (the ops repo's `apps/<namespace>/`
    layout).
    """
    if not checkout.is_dir():
        raise UsageError(f"GitOps checkout {checkout} is not a directory")
    policies: list[Policy] = []
    for path in sorted(checkout.rglob("*")):
        if path.suffix not in YAML_SUFFIXES or not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(checkout).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "NetworkPolicy" not in text:
            continue
        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        default_namespace = _kustomize_namespace(path.parent) or path.parent.name
        rel = path.relative_to(checkout).as_posix()
        for document in documents:
            if (
                isinstance(document, dict)
                and document.get("kind") == "NetworkPolicy"
                and str(document.get("apiVersion", "")).startswith("networking.k8s.io/")
            ):
                policies.append(
                    Policy.from_document(
                        document, namespace=default_namespace, origin=rel
                    )
                )
    return policies


def reachable_from_policies(
    policies: list[Policy],
    from_context: str,
    to: str,
    *,
    source: str = "",
    catalog: Catalog | None = None,
) -> Document:
    namespace = parse_caller(from_context)
    host, port = parse_target(to)
    addresses = resolve_host(catalog, host)
    verdicts = [
        (a, evaluate_egress(policies, namespace=namespace, address=a, port=port))
        for a in addresses
    ]
    # Reachable if any address is allowed; that address is the one reported.
    used, verdict = next(
        ((a, v) for a, v in verdicts if v.decision == "allow"), verdicts[0]
    )
    named = _as_ip(host) is None
    target = f"{host}:{port}" + (f" (via {used})" if named else "")
    document = _render(verdict, namespace, target, source)
    if named:
        document.data["host"] = host
        document.data["address_used"] = str(used)
        document.data["addresses"] = [
            {"address": str(a), "decision": v.decision} for a, v in verdicts
        ]
    return document


def reachable(session: Session, from_context: str, to: str) -> Document:
    parse_caller(from_context)
    host, _ = parse_target(to)
    resolve_host(session.catalog, host)
    checkout = gitops_checkout(session)
    document = reachable_from_policies(
        load_policies(checkout),
        from_context,
        to,
        source=str(checkout),
        catalog=session.catalog,
    )
    return Document(
        title=document.title,
        sections=document.sections,
        provenance=session.provenance(),
        data=document.data,
        exit_code=document.exit_code,
    )


def _render(verdict: Verdict, namespace: str, target: str, source: str) -> Document:
    matches = [match.to_dict() for match in verdict.matches]
    rows = tuple(
        (
            match.policy,
            f"#{match.rule_index}",
            _describe(match.peer) or "any destination",
            _describe(match.port) if match.port else "any port",
            match.origin,
        )
        for match in verdict.matches
    )
    blocks: list[Any] = [
        Fields(
            (
                ("decision", verdict.decision),
                ("from", f"k8s:{namespace}"),
                ("to", target),
                ("reason", verdict.reason),
            )
        ),
    ]
    if rows:
        blocks.append(Table(("policy", "rule", "peer", "port", "file"), rows))
    if verdict.isolating:
        blocks.append(Para("Egress isolated by: " + ", ".join(verdict.isolating)))
    if verdict.conditional:
        blocks.append(
            Para(
                "Not evaluated (selector depends on pod labels): "
                + ", ".join(verdict.conditional)
            )
        )
    blocks.append(Bullets((LAYER_NOTE, *verdict.limitations)))
    return Document(
        title=f"cadastre reachable k8s:{namespace} -> {target}",
        sections=(Section(f"reachable: k8s:{namespace} -> {target}", tuple(blocks)),),
        data={
            "from": f"k8s:{namespace}",
            "to": target,
            "decision": verdict.decision,
            "reason": verdict.reason,
            "matches": matches,
            "isolating_policies": list(verdict.isolating),
            "conditional_policies": list(verdict.conditional),
            "source": source,
            "limitations": [LAYER_NOTE, *verdict.limitations],
        },
        exit_code=1 if verdict.decision == "deny" else 0,
    )


def _describe(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return yaml.safe_dump(value, default_flow_style=True, sort_keys=True).strip()
