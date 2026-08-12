"""The little bit of HTTP every collector needs.

`urllib` rather than `requests`: every dependency is a deployment problem on
the collector host (DESIGN §7), and this is a GET with a header.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from cadastre.plugins.protocol import Reply, fail

DEFAULT_TIMEOUT = 20


class HttpError(Exception):
    """A request that failed in a way the protocol has a name for."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable

    def as_reply(self) -> Reply:
        return fail(self.kind, self.message, retryable=self.retryable)


@dataclass(frozen=True)
class Endpoint:
    """Where to call, and which environment variable holds the credential."""

    base_url: str
    token: str | None = None
    header: str = "Authorization"
    scheme: str = "Bearer"
    timeout: int = DEFAULT_TIMEOUT
    verify_tls: bool = True

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        default_header: str = "Authorization",
        default_scheme: str = "Bearer",
        required: bool = True,
        environ: dict[str, str] | None = None,
    ) -> Endpoint:
        env = environ if environ is not None else dict(os.environ)
        base = str(config.get("endpoint") or "").rstrip("/")
        if not base:
            raise HttpError("invalid_config", "config.endpoint is required")
        token_env = config.get("token_env")
        token = env.get(str(token_env)) if token_env else None
        if required and not token:
            raise HttpError(
                "invalid_config",
                f"no credential: config.token_env names {token_env!r}, which is "
                "not set in this process's environment. Credentials arrive by "
                "environment variable, never in params.",
            )
        return cls(
            base_url=base,
            token=token,
            header=str(config.get("auth_header") or default_header),
            scheme=str(config.get("auth_scheme", default_scheme)),
            timeout=int(config.get("timeout_seconds", DEFAULT_TIMEOUT)),
            verify_tls=bool(config.get("verify_tls", True)),
        )


def authorization_value(scheme: str, token: str) -> str:
    """Join credential scheme and token the way the upstream expects.

    RFC 7235 spells an authorization header `<scheme> <credentials>`, and for
    `Bearer` or `Basic` the space is part of the grammar. Not every API follows
    it: Proxmox wants `PVEAPIToken=root@pam!name=<uuid>`, where the prefix is
    complete in itself and a space makes the header unparseable — a 401 that
    looks exactly like a bad credential.

    So the separator is inferred from the scheme rather than assumed: a scheme
    that already ends in its own separator is used verbatim, and a bare scheme
    name gets the RFC's space. That reads both `PVEAPIToken=` and `Bearer` from
    configuration correctly without a per-collector special case.
    """
    if not scheme:
        return token
    if scheme.endswith(("=", " ", ":")):
        return f"{scheme}{token}"
    return f"{scheme} {token}"


def get_json(
    endpoint: Endpoint,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET, parse JSON, and translate every failure into an `error.kind`."""
    url = endpoint.base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method="GET")
    if not url.startswith(("http://", "https://")):
        raise HttpError("invalid_config", f"not an http(s) endpoint: {url!r}")
    request.add_header("Accept", "application/json")
    if endpoint.token:
        request.add_header(
            endpoint.header, authorization_value(endpoint.scheme, endpoint.token)
        )
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    context = None if endpoint.verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            request, timeout=endpoint.timeout, context=context
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise _from_status(exc.code, url) from exc
    except urllib.error.URLError as exc:
        raise HttpError("unreachable", f"{url}: {exc.reason}", retryable=True) from exc
    except TimeoutError as exc:
        raise HttpError("unreachable", f"{url}: timed out", retryable=True) from exc
    except OSError as exc:
        raise HttpError("unreachable", f"{url}: {exc}", retryable=True) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError("internal", f"{url}: response was not JSON: {exc}") from exc


def _from_status(status: int, url: str) -> HttpError:
    if status in (401, 403):
        return HttpError(
            "unauthorized",
            f"{url}: {status}. The collector credential is read-only by design; "
            "check its scope rather than widening it.",
        )
    if status == 404:
        return HttpError("not_found", f"{url}: 404")
    if status == 429:
        return HttpError("rate_limited", f"{url}: 429", retryable=True)
    if status >= 500:
        return HttpError("unreachable", f"{url}: {status}", retryable=True)
    return HttpError("internal", f"{url}: {status}")
