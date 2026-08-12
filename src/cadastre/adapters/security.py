"""Security policy for network adapters.

The catalog remains credential-free.  This module only handles credentials
supplied to an optional server process and deliberately keeps them out of
request documents, logs, and error messages.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import ssl
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre.core.errors import UsageError
from cadastre.core.provenance import parse_timestamp

READ_SCOPE = "catalog.read"
CHECK_SCOPE = "catalog.check"
WRITE_SCOPE = "catalog.write"
MCP_SCOPE = "mcp"
ALL_SCOPES = frozenset({READ_SCOPE, CHECK_SCOPE, WRITE_SCOPE, MCP_SCOPE})


@dataclass(frozen=True)
class Identity:
    """Authenticated identity; it carries no estate or Broker capability."""

    principal: str
    method: str
    scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AuthConfig:
    require_auth: bool = True
    audience: str = "cadastre"
    allow_legacy_tokens: bool = False


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    principal: str | None
    scope: str
    reason: str


@dataclass(frozen=True)
class RequestContext:
    identity: Identity | None
    operation: str
    target: str


class Authorizer:
    """Single default-deny application authorization evaluator."""

    def __init__(
        self,
        config: AuthConfig,
        *,
        tokens: Mapping[str, TokenCredential] | None = None,
        proxy_scopes: Mapping[str, frozenset[str]] | None = None,
        mtls_scopes: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self.config = config
        self.tokens = tokens or {}
        self.proxy_scopes = proxy_scopes or {}
        self.mtls_scopes = mtls_scopes or {}

    def decide(
        self, context: RequestContext, scope: str, *, now: datetime | None = None
    ) -> AuthorizationDecision:
        principal = context.identity.principal if context.identity else None
        allowed = authorized(
            principal,
            scope,
            require_auth=self.config.require_auth,
            tokens=self.tokens,
            proxy_scopes=self.proxy_scopes,
            mtls_scopes=self.mtls_scopes,
            audience=self.config.audience,
            now=now,
        )
        return AuthorizationDecision(
            allowed,
            principal,
            scope,
            "allowed" if allowed else "missing identity or scope",
        )


@dataclass(frozen=True)
class TokenCredential:
    """A live token definition; token values are never serialised."""

    principal: str
    scopes: frozenset[str] = frozenset()
    expires_at: datetime | None = None
    audience: str | None = None
    revoked: bool = False

    def valid(self, *, now: datetime, audience: str | None = None) -> bool:
        return (
            not self.revoked
            and (self.expires_at is None or self.expires_at > now)
            and (audience is None or self.audience is None or self.audience == audience)
        )

    def permits(
        self,
        scope: str,
        *,
        now: datetime,
        audience: str | None = None,
    ) -> bool:
        return self.valid(now=now, audience=audience) and scope in self.scopes


def certificate_common_name(connection: Any) -> str | None:
    """Return an mTLS certificate CN without exposing certificate material."""
    try:
        subject = connection.getpeercert().get("subject", ())
        for relative_name in subject:
            for attribute, value in relative_name:
                if attribute == "commonName":
                    return str(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def credential(
    principal: str,
    *,
    scopes: set[str] | frozenset[str] | None = None,
    expires_at: str | datetime | None = None,
    audience: str | None = None,
    revoked: bool = False,
) -> TokenCredential:
    if principal == "*":
        raise UsageError("wildcard principals are not allowed")
    expiry = parse_timestamp(expires_at) if isinstance(expires_at, str) else expires_at
    return TokenCredential(
        principal=principal,
        scopes=frozenset(scopes or ()),
        expires_at=expiry,
        audience=audience,
        revoked=revoked,
    )


def authorized(
    principal: str | None,
    scope: str,
    *,
    require_auth: bool,
    tokens: Mapping[str, TokenCredential],
    proxy_scopes: Mapping[str, frozenset[str]],
    mtls_scopes: Mapping[str, frozenset[str]],
    audience: str,
    now: datetime | None = None,
) -> bool:
    """Evaluate one application-scope decision with default deny.

    This is deliberately separate from Broker grants: a successful result is
    permission to query Cadastre, never an estate-execution capability.
    """
    if not require_auth:
        return True
    if principal is None or principal == "*":
        return False
    if scope in proxy_scopes.get(principal, frozenset()):
        return True
    if scope in mtls_scopes.get(principal, frozenset()):
        return True
    moment = now or datetime.now(tz=UTC)
    return any(
        item.principal == principal
        and item.permits(scope, now=moment, audience=audience)
        for item in tokens.values()
    )


def parse_token_file(
    path: Path, *, allow_legacy: bool = False
) -> dict[str, TokenCredential]:
    """Load a protected token file without ever returning its token values.

    JSON is the canonical format.  The old ``principal=token`` line format is
    retained for local compatibility and receives all scopes only when used by
    an explicitly configured test/local server; production examples use JSON.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read token file: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        if not allow_legacy:
            raise UsageError(
                "legacy principal=token files are disabled; use scoped JSON tokens"
            ) from None
        return _parse_legacy_tokens(raw_text)
    items = raw.get("tokens", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise UsageError("token file must contain a `tokens` list")
    result: dict[str, TokenCredential] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise UsageError(f"token file tokens[{index}] must be an object")
        token = item.get("token")
        principal = item.get("principal")
        scopes = item.get("scopes")
        if not isinstance(token, str) or not token:
            raise UsageError(f"token file tokens[{index}].token is required")
        if not isinstance(principal, str) or not principal or principal == "*":
            raise UsageError(
                f"token file tokens[{index}].principal must be a named principal"
            )
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise UsageError(f"token file tokens[{index}].scopes must be a list")
        unknown = set(scopes) - ALL_SCOPES
        if unknown:
            raise UsageError(
                "token file tokens[{}] has unknown scopes: {}".format(
                    index, ", ".join(sorted(unknown))
                )
            )
        result[token] = credential(
            principal,
            scopes=set(scopes),
            expires_at=item.get("expires_at"),
            audience=item.get("audience"),
            revoked=bool(item.get("revoked", False)),
        )
    return result


def parse_scope_bindings(values: list[str]) -> dict[str, frozenset[str]]:
    """Parse explicit ``principal=scope,scope`` bindings from CLI config."""
    result: dict[str, frozenset[str]] = {}
    for value in values:
        principal, separator, raw_scopes = value.partition("=")
        scopes = frozenset(raw_scopes.split(",")) if separator else frozenset()
        if not principal or not separator or not scopes or principal == "*":
            raise UsageError("scope bindings must be principal=scope[,scope]")
        unknown = scopes - ALL_SCOPES
        if unknown:
            raise UsageError(f"unknown security scope(s): {', '.join(sorted(unknown))}")
        result[principal] = scopes
    return result


def proxy_from_file(
    networks: list[str], secret_file: Path | None
) -> ProxyConfig | None:
    """Build an explicit trusted-proxy policy from operator-owned files."""
    if not networks and secret_file is None:
        return None
    if not networks or secret_file is None:
        raise UsageError("trusted proxy needs --proxy-network and --proxy-secret-file")
    try:
        secret = secret_file.read_bytes().strip()
    except OSError as exc:
        raise UsageError(f"cannot read proxy identity secret: {exc}") from exc
    if not secret:
        raise UsageError("proxy identity secret must not be empty")
    return ProxyConfig(networks=tuple(networks), identity_secret=secret)


def _parse_legacy_tokens(raw: str) -> dict[str, TokenCredential]:
    result: dict[str, TokenCredential] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        principal, separator, token = line.partition("=")
        if not separator or not principal.strip() or not token.strip():
            raise UsageError("legacy token file lines must be principal=token")
        result[token.strip()] = credential(principal.strip(), scopes=set(ALL_SCOPES))
    return result


@dataclass(frozen=True)
class ProxyConfig:
    """Explicit trust for a proxy-forwarded principal."""

    networks: tuple[str, ...] = ()
    identity_secret: bytes | None = None

    def permits(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(network) for network in self.networks)

    def principal(self, headers: dict[str, str], address: str) -> str | None:
        if not self.identity_secret or not self.permits(address):
            return None
        name = headers.get("X-Cadastre-Principal", "")
        signature = headers.get("X-Cadastre-Identity-Signature", "")
        expected = hmac.new(
            self.identity_secret, name.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if name and hmac.compare_digest(signature, expected):
            return name
        return None


@dataclass
class RateLimiter:
    limit: int = 120
    window_seconds: int = 60
    _events: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = [
                t for t in self._events.get(key, []) if now - t < self.window_seconds
            ]
            if len(events) >= self.limit:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


class AuditLog:
    """Append security metadata before a request result is returned."""

    def __init__(self, path: Path | None, *, required: bool = False) -> None:
        self.path = path
        self.required = required
        self._lock = threading.Lock()

    def record(
        self,
        *,
        principal: str | None,
        operation: str,
        target: str,
        decision: str,
        request_material: bytes = b"",
        catalog_revision: int | None = None,
        result: str | None = None,
    ) -> None:
        if self.path is None:
            if self.required:
                raise PermissionError("required security audit sink is not configured")
            return
        row: dict[str, Any] = {
            "ts": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "principal": principal or "anonymous",
            "operation": operation,
            "target": target,
            "decision": decision,
            "request_hash": "sha256:" + hashlib.sha256(request_material).hexdigest(),
        }
        if catalog_revision is not None:
            row["catalog_revision"] = catalog_revision
        if result is not None:
            row["result"] = result
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
        except OSError as exc:
            if self.required:
                raise PermissionError("required security audit sink failed") from exc
            raise


def tls_context(
    certfile: Path,
    keyfile: Path,
    *,
    ca_file: Path | None = None,
    require_client_cert: bool = False,
    minimum: str = "TLSv1.2",
) -> ssl.SSLContext:
    """Build a platform TLS context and surface actionable setup errors."""
    if not certfile.is_file() or not keyfile.is_file():
        raise UsageError("TLS certificate and private-key files must both exist")
    if stat.S_IMODE(keyfile.stat().st_mode) & 0o077:
        raise UsageError("TLS private key must not be readable by group or other")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    versions = {"TLSv1.2": ssl.TLSVersion.TLSv1_2, "TLSv1.3": ssl.TLSVersion.TLSv1_3}
    if minimum not in versions:
        raise UsageError("minimum TLS version must be TLSv1.2 or TLSv1.3")
    context.minimum_version = versions[minimum]
    context.options |= ssl.OP_NO_COMPRESSION
    try:
        context.load_cert_chain(str(certfile), str(keyfile))
    except (OSError, ssl.SSLError) as exc:
        raise UsageError(f"could not load TLS certificate/key: {exc}") from exc
    if ca_file is not None:
        if not ca_file.is_file():
            raise UsageError("TLS client CA file does not exist")
        try:
            context.load_verify_locations(cafile=str(ca_file))
        except (OSError, ssl.SSLError) as exc:
            raise UsageError(f"could not load TLS client CA: {exc}") from exc
        context.verify_mode = (
            ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_OPTIONAL
        )
    elif require_client_cert:
        raise UsageError("--require-client-cert needs --tls-ca")
    return context


def security_report(
    *,
    bind: str,
    tls: bool,
    profile: str,
    require_auth: bool,
    scopes: set[str] | frozenset[str],
    certfile: Path | None = None,
    keyfile: Path | None = None,
    ca_file: Path | None = None,
    proxy: ProxyConfig | None = None,
) -> dict[str, Any]:
    """Return deterministic operator-facing readiness diagnostics."""
    host, _, port = bind.rpartition(":")
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    checks: list[dict[str, Any]] = []
    known_profiles = {
        "loopback-development",
        "direct-https",
        "trusted-proxy",
        "mtls",
        "development-plaintext",
    }
    checks.append(
        {"name": "profile", "ok": profile in known_profiles, "detail": profile}
    )
    checks.append(
        {
            "name": "loopback-or-tls",
            "ok": loopback or tls,
            "detail": (
                "loopback"
                if loopback
                else ("TLS enabled" if tls else "non-loopback plaintext")
            ),
        }
    )
    checks.append(
        {
            "name": "authentication",
            "ok": loopback or require_auth or profile == "development-plaintext",
            "detail": "required" if require_auth else "not required",
        }
    )
    checks.append(
        {
            "name": "scopes",
            "ok": bool(scopes) if require_auth else True,
            "detail": sorted(scopes),
        }
    )
    if certfile is not None:
        checks.append(
            {"name": "certificate", "ok": certfile.is_file(), "detail": str(certfile)}
        )
    if keyfile is not None:
        checks.append(
            {
                "name": "private-key",
                "ok": keyfile.is_file()
                and not bool(stat.S_IMODE(keyfile.stat().st_mode) & 0o077),
                "detail": str(keyfile),
            }
        )
    if ca_file is not None:
        checks.append(
            {"name": "client-ca", "ok": ca_file.is_file(), "detail": str(ca_file)}
        )
    if profile in {"direct-https", "mtls"}:
        checks.append(
            {
                "name": "remote-tls",
                "ok": not loopback and tls,
                "detail": "non-loopback TLS",
            }
        )
    if profile == "mtls":
        checks.append(
            {
                "name": "mtls-ca",
                "ok": ca_file is not None and ca_file.is_file(),
                "detail": "client CA required",
            }
        )
    if profile == "trusted-proxy":
        checks.append(
            {
                "name": "proxy-auth",
                "ok": proxy is not None
                and bool(proxy.networks and proxy.identity_secret),
                "detail": "trusted proxy identity required",
            }
        )
    checks.append(
        {
            "name": "proxy-boundary",
            "ok": proxy is None or bool(proxy.networks and proxy.identity_secret),
            "detail": "direct" if proxy is None else list(proxy.networks),
        }
    )
    return {
        "bind": {"host": host, "port": int(port) if port.isdigit() else port},
        "profile": profile,
        "tls": tls,
        "authentication": {"required": require_auth, "scopes": sorted(scopes)},
        "checks": checks,
        "ready": all(bool(item["ok"]) for item in checks),
    }


def random_token() -> str:
    """Generate a token for an operator to place in a protected file."""
    return secrets.token_urlsafe(32)
