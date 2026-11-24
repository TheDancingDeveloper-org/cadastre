"""Kubernetes NetworkPolicy egress evaluation, read from declared manifests.

A pure function of the policy documents and the question: no cluster is
contacted and nothing is probed (DESIGN §1.3). It answers "would a pod in
namespace N be allowed to open a connection to IP:port" the way the
`networking.k8s.io/v1` semantics define it:

* A pod is *egress-isolated* when at least one policy in its namespace selects
  it and lists `Egress` in `policyTypes` (or, with `policyTypes` absent, has an
  `egress` section). A pod nothing isolates may send anywhere.
* An isolated pod may send to a destination when *some* egress rule of *some*
  selecting policy matches both a peer (`to`) and a port (`ports`). Policies
  are additive allow-lists; there is no deny rule and no ordering.

Peers are matched against an IP address only. `ipBlock` is exact (CIDR minus
`except`). `podSelector` / `namespaceSelector` peers name pods, and an IP alone
cannot say which pod it belongs to, so they never match here — reported as a
limitation rather than guessed. Named ports likewise cannot match a number.

The caller's pod labels are optional. With none given, only policies whose
`podSelector` is empty (selects every pod) are certain to apply; a policy with
a non-empty selector is reported under `conditional` instead of being assumed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class Policy:
    """One NetworkPolicy document, with where it came from."""

    name: str
    namespace: str
    spec: dict[str, Any]
    origin: str = ""

    @classmethod
    def from_document(
        cls, document: dict[str, Any], *, namespace: str, origin: str = ""
    ) -> Policy:
        metadata = document.get("metadata") or {}
        spec = document.get("spec") or {}
        return cls(
            name=str(metadata.get("name") or "(unnamed)"),
            namespace=str(metadata.get("namespace") or namespace),
            spec=spec if isinstance(spec, dict) else {},
            origin=origin,
        )

    def policy_types(self) -> tuple[str, ...]:
        declared = self.spec.get("policyTypes")
        if isinstance(declared, list) and declared:
            return tuple(str(item) for item in declared)
        # API default: Ingress always, Egress when an egress section exists.
        types = ["Ingress"]
        if "egress" in self.spec:
            types.append("Egress")
        return tuple(types)

    def isolates_egress(self) -> bool:
        return "Egress" in self.policy_types()

    def egress_rules(self) -> list[dict[str, Any]]:
        rules = self.spec.get("egress") or []
        return [rule for rule in rules if isinstance(rule, dict)]


@dataclass(frozen=True)
class Match:
    """The policy and rule that allowed a connection."""

    policy: str
    rule_index: int
    peer: dict[str, Any]
    port: dict[str, Any] | None
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "rule_index": self.rule_index,
            "peer": self.peer,
            "port": self.port,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class Verdict:
    decision: str  # allow | deny
    reason: str
    matches: tuple[Match, ...] = ()
    isolating: tuple[str, ...] = ()
    conditional: tuple[str, ...] = ()
    limitations: tuple[str, ...] = field(default_factory=tuple)


def selector_matches(selector: Any, labels: dict[str, str] | None) -> bool | None:
    """Whether a label selector selects a pod with `labels`.

    Returns None when the answer depends on labels the caller did not give.
    """
    if not isinstance(selector, dict):
        return True
    match_labels = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not match_labels and not expressions:
        return True
    if labels is None:
        return None
    for key, value in match_labels.items():
        if labels.get(str(key)) != str(value):
            return False
    for expression in expressions:
        key = str(expression.get("key"))
        operator = str(expression.get("operator"))
        values = {str(v) for v in expression.get("values") or []}
        present = key in labels
        if operator == "In" and not (present and labels[key] in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def _ip_block_matches(block: Any, address: IPAddress) -> bool:
    if not isinstance(block, dict):
        return False
    try:
        network = ipaddress.ip_network(str(block.get("cidr")), strict=False)
    except ValueError:
        return False
    if address.version != network.version or address not in network:
        return False
    for excluded in block.get("except") or []:
        try:
            if address in ipaddress.ip_network(str(excluded), strict=False):
                return False
        except ValueError:
            continue
    return True


def _peer_matches(rule: dict[str, Any], address: IPAddress) -> dict[str, Any] | None:
    """The first peer of `rule` that admits `address`, or None.

    An absent or empty `to` admits every destination.
    """
    peers = rule.get("to")
    if not peers:
        return {}
    for peer in peers:
        if (
            isinstance(peer, dict)
            and "ipBlock" in peer
            and _ip_block_matches(peer["ipBlock"], address)
        ):
            return peer
    return None


def _port_matches(
    rule: dict[str, Any], port: int, protocol: str
) -> tuple[bool, dict[str, Any] | None]:
    ports = rule.get("ports")
    if not ports:
        return True, None
    for entry in ports:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("protocol") or "TCP").upper() != protocol:
            continue
        if "port" not in entry or entry.get("port") is None:
            return True, entry
        declared = entry["port"]
        if isinstance(declared, str) and not declared.isdigit():
            continue  # named port: cannot be matched against a number
        low = int(declared)
        high = int(entry.get("endPort") or low)
        if low <= port <= high:
            return True, entry
    return False, None


LIMITATION_SELECTORS = (
    "podSelector/namespaceSelector peers name pods, not addresses; an IP "
    "destination is matched against ipBlock peers only."
)
LIMITATION_SCOPE = (
    "Declared manifests only: a policy applied to the cluster by hand, or "
    "a CNI that does not enforce NetworkPolicy, is invisible here."
)


def evaluate_egress(
    policies: list[Policy],
    *,
    namespace: str,
    address: IPAddress,
    port: int,
    protocol: str = "TCP",
    pod_labels: dict[str, str] | None = None,
) -> Verdict:
    protocol = protocol.upper()
    limitations = (LIMITATION_SELECTORS, LIMITATION_SCOPE)
    selecting: list[Policy] = []
    conditional: list[str] = []
    for policy in policies:
        if policy.namespace != namespace or not policy.isolates_egress():
            continue
        selected = selector_matches(policy.spec.get("podSelector"), pod_labels)
        if selected is None:
            conditional.append(policy.name)
        elif selected:
            selecting.append(policy)
    isolating = tuple(policy.name for policy in selecting)
    if not selecting:
        return Verdict(
            "allow",
            f"no egress policy in namespace {namespace} selects the caller, so "
            "its egress is not isolated",
            conditional=tuple(conditional),
            limitations=limitations,
        )
    matches: list[Match] = []
    for policy in selecting:
        for index, rule in enumerate(policy.egress_rules()):
            peer = _peer_matches(rule, address)
            if peer is None:
                continue
            port_ok, port_entry = _port_matches(rule, port, protocol)
            if port_ok:
                matches.append(
                    Match(policy.name, index, peer, port_entry, policy.origin)
                )
    if matches:
        first = matches[0]
        return Verdict(
            "allow",
            f"allowed by {first.policy} egress rule #{first.rule_index}",
            tuple(matches),
            isolating,
            tuple(conditional),
            limitations,
        )
    return Verdict(
        "deny",
        f"egress is isolated by {', '.join(isolating)} and no egress rule "
        f"admits {address}:{port}/{protocol}",
        (),
        isolating,
        tuple(conditional),
        limitations,
    )
