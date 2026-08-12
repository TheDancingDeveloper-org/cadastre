#!/usr/bin/env python3
"""Infisical universal-auth login, then exec the real command.

Mints a short-lived access token from a machine identity's client-id/secret
and exports it into the environment variable(s) the configured
`secrets-infisical` plugin source(s) read their credential from
(`token_env` in `declared/plugins.yaml`) — then replaces this process with
the real command, so the token exists only in that command's environment
for the run's lifetime. It is never written to disk and never logged.

The client-id/secret themselves preferably arrive as files (the
Docker/Compose secrets convention: a mounted file, not an environment
variable), because unlike an ordinary read-only collector token these are
long-lived and mint credentials of their own. Where the deployment
mechanism only exposes secrets as plain environment variables (no secret
mount available — e.g. a Komodo-managed stack environment), the
non-`_FILE` variable is read instead; it is still never written to disk or
logged, and the mounted-file form should be preferred wherever the
orchestrator supports it.

Usage, as a container entrypoint:

    infisical-entrypoint.py cadastre --data-dir /var/lib/cadastre collect

Environment (one of each pair is required):
    INFISICAL_CLIENT_ID_FILE       path to the mounted client-id secret
    INFISICAL_CLIENT_ID            the client-id value directly
    INFISICAL_CLIENT_SECRET_FILE   path to the mounted client-secret
    INFISICAL_CLIENT_SECRET        the client-secret value directly
    INFISICAL_LOGIN_URL            default: https://app.infisical.com/api/v1/auth/universal-auth/login
    INFISICAL_TOKEN_ENV            default: CADASTRE_P_SECRETS_TOKEN
                                    comma-separated if more than one configured
                                    `secrets-infisical` source shares this login
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_LOGIN_URL = "https://app.infisical.com/api/v1/auth/universal-auth/login"
DEFAULT_TOKEN_ENV = "CADASTRE_P_SECRETS_TOKEN"


def _read_credential(name: str) -> str:
    """`<NAME>_FILE` (a mounted secret file) if set, else the plain `<NAME>`
    variable. The file form is preferred; the plain form exists only for
    deployment mechanisms with no secret-file mount to offer."""
    file_var = f"{name}_FILE"
    path = os.environ.get(file_var, "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError as exc:
            raise SystemExit(
                f"infisical-entrypoint: cannot read {file_var} ({path}): {exc}"
            ) from None
        if not value:
            raise SystemExit(f"infisical-entrypoint: {file_var} ({path}) is empty")
        return value
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"infisical-entrypoint: set {file_var} to a mounted secret file, "
            f"or {name} directly"
        )
    return value


def login(login_url: str, client_id: str, client_secret: str) -> str:
    """Universal-auth login. Returns the short-lived access token."""
    body = json.dumps({"clientId": client_id, "clientSecret": client_secret}).encode()
    request = urllib.request.Request(
        login_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Never echo the response body: a login failure can echo the request
        # back, and the request carries the client secret.
        raise SystemExit(
            f"infisical-entrypoint: universal-auth login failed: HTTP {exc.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(
            f"infisical-entrypoint: could not reach {login_url}: {exc}"
        ) from None
    except json.JSONDecodeError:
        raise SystemExit(
            "infisical-entrypoint: universal-auth login returned invalid JSON"
        ) from None
    token = payload.get("accessToken") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise SystemExit(
            "infisical-entrypoint: universal-auth login response had no accessToken"
        )
    return token


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(
            "infisical-entrypoint: usage: infisical-entrypoint.py <command> [args...]"
        )
    client_id = _read_credential("INFISICAL_CLIENT_ID")
    client_secret = _read_credential("INFISICAL_CLIENT_SECRET")
    login_url = os.environ.get("INFISICAL_LOGIN_URL", "").strip() or DEFAULT_LOGIN_URL
    token_env = os.environ.get("INFISICAL_TOKEN_ENV", "").strip() or DEFAULT_TOKEN_ENV
    token = login(login_url, client_id, client_secret)
    for name in (item.strip() for item in token_env.split(",")):
        if name:
            os.environ[name] = token
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
