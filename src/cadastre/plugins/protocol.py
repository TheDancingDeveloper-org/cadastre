"""The plugin wire protocol (DESIGN §4.3).

One JSON object in on stdin, one JSON object out on stdout, one exec per call.
Simple, trivially correct, language-agnostic — a plugin can be a shell script.

Two rules are enforced here rather than documented:

* Nothing but the JSON object on stdout. Diagnostics go to stderr, and stdout
  pollution is a protocol error, not a parse that happens to succeed.
* Credentials arrive by environment variable named in `config`, never in
  `params` — `params` may be logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

#: The closed set from DESIGN §4.3. A plugin returning anything else is
#: itself broken, and is reported as `internal`.
ERROR_KINDS = (
    "unreachable",
    "unauthorized",
    "not_found",
    "invalid_config",
    "rate_limited",
    "internal",
)

#: Capability -> methods (DESIGN §4.2). A plugin implements any subset and
#: declares which via the `plugin.info` handshake.
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "Inventory": ("inventory.list",),
    "Network": ("network.list", "network.members"),
    "DNS": ("dns.zones", "dns.records"),
    "SecretRef": ("secret.list", "secret.stat"),
    "VCS": ("vcs.repos", "vcs.open_pr"),
    "CI": ("ci.pipelines", "ci.status", "ci.trigger"),
    "Topology": ("topology.list",),
    "Broker": ("broker.mint", "broker.exec"),
    # Not in the DESIGN table: endpoints are what an ingress collector returns,
    # and calling that "Inventory" would lose the distinction drift needs.
    "Endpoint": ("endpoint.list",),
    "Work": ("work.items", "work.findings", "work.repo-state", "work.revision-checks"),
}

METHOD_CAPABILITY: dict[str, str] = {
    method: capability
    for capability, methods in CAPABILITIES.items()
    for method in methods
}

METHOD_ENTITY_KINDS: dict[str, tuple[str, ...]] = {
    "inventory.list": ("host", "service"),
    "network.list": ("network",),
    "network.members": ("host",),
    "endpoint.list": ("endpoint",),
    "dns.records": ("domain",),
    "secret.list": ("secret",),
    "vcs.repos": ("repo",),
    "ci.pipelines": ("pipeline",),
    "ci.status": ("ci_executor", "ci_pool"),
    "work.items": ("forge_item",),
    "work.findings": ("markdown_finding",),
    "work.repo-state": ("repo_checkout",),
    "work.revision-checks": ("revision_check",),
}

#: Methods that change something. The runner refuses them so a misconfigured
#: source cannot become a write path.
WRITE_METHODS = frozenset({"vcs.open_pr", "ci.trigger", "broker.mint", "broker.exec"})


@dataclass(frozen=True)
class Request:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    v: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "method": self.method,
                "params": self.params,
                "config": self.config,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class PluginError:
    kind: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind if self.kind in ERROR_KINDS else "internal",
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class Reply:
    """A plugin's answer. `ok=False` is a normal outcome, not a crash."""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    as_of: str | None = None
    warnings: tuple[str, ...] = ()
    error: PluginError | None = None
    v: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"v": self.v, "ok": self.ok}
        if self.ok:
            payload["result"] = self.result
            payload["as_of"] = self.as_of
            payload["warnings"] = list(self.warnings)
        else:
            payload["error"] = (
                self.error.to_dict()
                if self.error
                else PluginError("internal", "unspecified").to_dict()
            )
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=False)


def ok(result: dict[str, Any], as_of: str, warnings: tuple[str, ...] = ()) -> Reply:
    return Reply(ok=True, result=result, as_of=as_of, warnings=warnings)


def fail(kind: str, message: str, *, retryable: bool = False) -> Reply:
    return Reply(ok=False, error=PluginError(kind, message, retryable))


def parse_request(text: str) -> Request:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    method = payload.get("method")
    if not isinstance(method, str):
        raise ValueError("request.method must be a string")
    return Request(
        method=method,
        params=dict(payload.get("params") or {}),
        config=dict(payload.get("config") or {}),
        v=int(payload.get("v", PROTOCOL_VERSION)),
    )


def parse_reply(text: str) -> Reply:
    """Parse a plugin's stdout. Raises ValueError on anything unclean —
    the runner turns that into a stale source rather than a crash."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("plugin wrote nothing to stdout")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdout is not a single JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("stdout is not a JSON object")
    if payload.get("ok") is True:
        as_of = payload.get("as_of")
        if not isinstance(as_of, str) or not as_of:
            raise ValueError("a successful reply must carry as_of")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("reply.result must be an object")
        warnings = payload.get("warnings") or []
        return Reply(
            ok=True,
            result=result,
            as_of=as_of,
            warnings=tuple(str(w) for w in warnings),
            v=int(payload.get("v", PROTOCOL_VERSION)),
        )
    error = payload.get("error") or {}
    if not isinstance(error, dict):
        raise ValueError("reply.error must be an object")
    kind = str(error.get("kind", "internal"))
    return Reply(
        ok=False,
        error=PluginError(
            kind=kind if kind in ERROR_KINDS else "internal",
            message=str(error.get("message", "")),
            retryable=bool(error.get("retryable", False)),
        ),
        v=int(payload.get("v", PROTOCOL_VERSION)),
    )
