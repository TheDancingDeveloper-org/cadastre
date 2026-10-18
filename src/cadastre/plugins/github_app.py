"""GitHub App installation tokens for collector sources (`app_auth`).

A static PAT is long-lived and outlives whoever minted it; an installation
token lasts an hour and is scoped to what the App was granted. The runner
mints one here, before it starts the plugin, and hands it over in the
source's `token_env`. The private key never leaves this process and never
reaches the plugin.

Tokens are cached per (API, app, installation or owner, key file) for the life of the
process, so one `cadastre collect` run mints at most one token per
installation no matter how many sources or methods share it.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cadastre.plugins.config import AppAuth
from cadastre.plugins.protocol import PluginError

DEFAULT_API = "https://api.github.com"
#: GitHub rejects an app JWT that lives longer than ten minutes, and clock
#: skew is real: backdate the issue time and keep the lifetime short.
JWT_BACKDATE = 60
JWT_LIFETIME = 540
#: Re-mint a cached token this long before GitHub says it expires.
REFRESH_MARGIN = 300
TIMEOUT = 20

Fetch = Callable[[urllib.request.Request], Mapping[str, Any]]

_cache: dict[tuple[str, str, str, str], tuple[str, float]] = {}


class AppAuthError(Exception):
    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error = PluginError(kind, message, retryable=retryable)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, private_key_pem: bytes, *, now: float) -> str:
    """An RS256 JWT identifying the App itself (not an installation)."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise AppAuthError(
            "invalid_config",
            "app_auth needs the `cryptography` package: install cadastre[github-app]",
        ) from exc
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise AppAuthError(
            "invalid_config", "app_auth private key is not an unencrypted PEM key"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AppAuthError("invalid_config", "app_auth private key is not an RSA key")
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = {
        "iat": int(now) - JWT_BACKDATE,
        "exp": int(now) + JWT_LIFETIME,
        "iss": app_id,
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def _urlopen(request: urllib.request.Request) -> Mapping[str, Any]:
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("not a JSON object")
    return body


def _parse_expiry(value: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _call(
    fetch: Fetch | None, url: str, jwt: str, *, method: str, what: str
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url, method=method, data=b"" if method == "POST" else None
    )
    request.add_header("Authorization", f"Bearer {jwt}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        return (fetch or _urlopen)(request)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise AppAuthError(
                "unauthorized",
                f"{url}: {exc.code} {what}; check the app id, key and that the "
                "App is installed for this owner",
            ) from exc
        raise AppAuthError(
            "unreachable", f"{url}: {exc.code}", retryable=exc.code >= 500
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AppAuthError("unreachable", f"{url}: {exc}", retryable=True) from exc
    except ValueError as exc:
        raise AppAuthError("internal", f"{url}: response was not JSON") from exc


def _discover(fetch: Fetch | None, api: str, jwt: str, owner: str) -> str:
    """The installation id for `owner`: organisation first, then user."""
    if not _OWNER.match(owner):
        raise AppAuthError(
            "invalid_config", f"app_auth owner is not a GitHub login: {owner!r}"
        )
    last: AppAuthError | None = None
    for scope in ("orgs", "users"):
        try:
            body = _call(
                fetch,
                f"{api}/{scope}/{owner}/installation",
                jwt,
                method="GET",
                what="discovering the installation",
            )
        except AppAuthError as exc:
            if exc.error.kind != "unauthorized":
                raise
            last = exc
            continue
        ident = body.get("id")
        if isinstance(ident, int) and not isinstance(ident, bool):
            return str(ident)
        raise AppAuthError("internal", f"{api}/{scope}/{owner}/installation: no id")
    assert last is not None
    raise last


def installation_token(
    auth: AppAuth,
    environ: Mapping[str, str],
    *,
    owner: str | None = None,
    api_base: str = DEFAULT_API,
    fetch: Fetch | None = None,
    clock: Callable[[], float] = time.time,
) -> str:
    """Return a live installation token, minting one if none is cached.

    The installation comes from `auth.installation_id_env` when set, and is
    otherwise discovered for `auth.owner or owner`. Raises `AppAuthError`
    carrying a protocol error; never includes the key, the JWT or the token
    in a message.
    """
    app_id = environ.get(auth.app_id_env, "").strip()
    if not app_id:
        raise AppAuthError(
            "invalid_config",
            f"app_auth names {auth.app_id_env!r}, which is not set in this environment",
        )
    installation_id: str | None = None
    if auth.installation_id_env:
        installation_id = environ.get(auth.installation_id_env, "").strip()
        if not installation_id.isdigit():
            raise AppAuthError(
                "invalid_config",
                f"app_auth names {auth.installation_id_env!r}, which is not set "
                "to a numeric installation id in this environment",
            )
    target = auth.owner or owner
    if installation_id is None and not target:
        raise AppAuthError(
            "invalid_config",
            "app_auth needs installation_id_env, owner, or a config.org/user "
            "to discover the installation from",
        )
    api = api_base.rstrip("/")
    key = (api, app_id, installation_id or f"owner:{target}", auth.private_key_file)
    now = clock()
    cached = _cache.get(key)
    if cached is not None and cached[1] - REFRESH_MARGIN > now:
        return cached[0]
    try:
        pem = Path(auth.private_key_file).read_bytes()
    except OSError as exc:
        raise AppAuthError(
            "invalid_config",
            f"app_auth private_key_file {auth.private_key_file!r} is unreadable: "
            f"{exc.strerror}",
        ) from exc
    jwt = app_jwt(app_id, pem, now=now)
    if installation_id is None:
        assert target is not None
        installation_id = _discover(fetch, api, jwt, target)
    url = f"{api}/app/installations/{installation_id}/access_tokens"
    body = _call(fetch, url, jwt, method="POST", what="minting an installation token")
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise AppAuthError("internal", f"{url}: response carried no token")
    expires = _parse_expiry(body.get("expires_at")) or now + 3600
    _cache[key] = (token, expires)
    return token


def clear_cache() -> None:
    _cache.clear()
