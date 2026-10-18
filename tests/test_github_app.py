"""`app_auth`: GitHub App installation tokens minted by the runner."""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cadastre.core.errors import CatalogError
from cadastre.plugins import github_app, runner
from cadastre.plugins.collectors import forge_github
from cadastre.plugins.config import AppAuth, SourceConfig, load_plugins
from cadastre.plugins.protocol import Request

ENV = {"GH_APP_ID": "12345", "GH_INSTALLATION_ID": "67890", "PATH": "/bin"}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    github_app.clear_cache()
    yield
    github_app.clear_cache()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_file(tmp_path: Path, rsa_key: rsa.RSAPrivateKey) -> Path:
    path = tmp_path / "app.pem"
    path.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return path


def _auth(key_file: Path) -> AppAuth:
    return AppAuth(
        app_id_env="GH_APP_ID",
        private_key_file=str(key_file),
        installation_id_env="GH_INSTALLATION_ID",
    )


class FakeGitHub:
    def __init__(self, token: str = "ghs_minted") -> None:
        self.token = token
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> Mapping[str, Any]:
        self.requests.append(request)
        url = request.full_url
        if url.endswith("/orgs/example/installation"):
            return {"id": 424242}
        if url.endswith("/installation"):
            raise urllib.error.HTTPError(url, 404, "no", {}, None)  # type: ignore[arg-type]
        return {"token": self.token, "expires_at": "2099-01-01T00:00:00Z"}


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_jwt_is_rs256_signed_by_the_app_key(rsa_key: rsa.RSAPrivateKey) -> None:
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    token = github_app.app_jwt("12345", pem, now=1_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_unb64(payload))
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 600
    rsa_key.public_key().verify(
        _unb64(signature),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_mints_once_per_installation_and_posts_to_the_right_url(
    key_file: Path,
) -> None:
    fake = FakeGitHub()
    first = github_app.installation_token(_auth(key_file), ENV, fetch=fake)
    second = github_app.installation_token(_auth(key_file), ENV, fetch=fake)
    assert first == second == "ghs_minted"
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == (
        "https://api.github.com/app/installations/67890/access_tokens"
    )
    assert str(request.get_header("Authorization")).startswith("Bearer ey")


def test_near_expiry_token_is_reminted(key_file: Path) -> None:
    fake = FakeGitHub()
    github_app.installation_token(_auth(key_file), ENV, fetch=fake)
    github_app.installation_token(
        _auth(key_file), ENV, fetch=fake, clock=lambda: 4_070_908_800 - 60
    )
    assert len(fake.requests) == 2


def test_missing_ids_and_unreadable_key_are_config_errors(key_file: Path) -> None:
    with pytest.raises(github_app.AppAuthError) as missing:
        github_app.installation_token(_auth(key_file), {}, fetch=FakeGitHub())
    assert missing.value.error.kind == "invalid_config"
    assert "GH_APP_ID" in missing.value.error.message
    with pytest.raises(github_app.AppAuthError) as unreadable:
        github_app.installation_token(
            AppAuth(
                app_id_env="GH_APP_ID",
                private_key_file=str(key_file) + ".absent",
                installation_id_env="GH_INSTALLATION_ID",
            ),
            ENV,
            fetch=FakeGitHub(),
        )
    assert unreadable.value.error.kind == "invalid_config"


def test_rejected_mint_is_unauthorized_without_leaking_secrets(
    key_file: Path,
) -> None:
    def refuse(request: urllib.request.Request) -> Mapping[str, Any]:
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)  # type: ignore[arg-type]

    with pytest.raises(github_app.AppAuthError) as exc:
        github_app.installation_token(_auth(key_file), ENV, fetch=refuse)
    assert exc.value.error.kind == "unauthorized"
    assert "PRIVATE KEY" not in exc.value.error.message
    assert "Bearer" not in exc.value.error.message


def _echo_env_source(key_file: Path, **config: Any) -> SourceConfig:
    """A plugin that replies with the token it was handed."""
    script = (
        "import json, os, sys; sys.stdin.read(); "
        "print(json.dumps({'protocol': 1, 'ok': True, 'as_of': "
        "'2026-01-01T00:00:00Z', 'result': "
        "{'token': os.environ.get('CADASTRE_P_GITHUB_TOKEN')}}))"
    )
    return SourceConfig(
        id="gh",
        command=(sys.executable, "-c", script),
        config={"token_env": "CADASTRE_P_GITHUB_TOKEN", **config},
        app_auth=_auth(key_file),
    )


def test_runner_hands_the_minted_token_over_in_token_env(key_file: Path) -> None:
    environ = {**ENV, "CADASTRE_P_GITHUB_TOKEN": "stale-literal-pat"}
    with mock.patch.object(github_app, "_urlopen", FakeGitHub()):
        outcome = runner.call(_echo_env_source(key_file), "vcs.repos", environ=environ)
    assert outcome.ok, outcome.message
    assert outcome.reply is not None
    assert outcome.reply.result["token"] == "ghs_minted"


def test_runner_reports_a_failed_mint_as_a_stale_source(key_file: Path) -> None:
    outcome = runner.call(_echo_env_source(key_file), "vcs.repos", environ={})
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.kind == "invalid_config"


def _write_plugins(root: Path, body: str) -> None:
    (root / "plugins.yaml").write_text(body)


def test_load_plugins_parses_app_auth(tmp_path: Path) -> None:
    _write_plugins(
        tmp_path,
        """
sources:
  - id: forge-public
    plugin: forge-github
    command: [cadastre-plugin-forge-github]
    app_auth:
      app_id_env: GH_APP_ID
      installation_id_env: GH_INSTALLATION_ID
      private_key_file: /run/cadastre/github-app/private-key.pem
    config:
      endpoint: https://api.github.com
      token_env: CADASTRE_P_GITHUB_TOKEN
      org: example
""",
    )
    (source,) = load_plugins(tmp_path).sources
    assert source.app_auth == AppAuth(
        app_id_env="GH_APP_ID",
        private_key_file="/run/cadastre/github-app/private-key.pem",
        installation_id_env="GH_INSTALLATION_ID",
    )
    assert "app_auth" not in source.config


@pytest.mark.parametrize(
    ("app_auth", "config", "field"),
    [
        ("{app_id_env: A}", "{token_env: T}", "app_auth"),
        (
            "{app_id_env: A, installation_id_env: I, private_key_file: /k}",
            "{org: x}",
            "config.token_env",
        ),
    ],
)
def test_load_plugins_rejects_incomplete_app_auth(
    tmp_path: Path, app_auth: str, config: str, field: str
) -> None:
    _write_plugins(
        tmp_path,
        f"""
sources:
  - id: s
    command: [x]
    app_auth: {app_auth}
    config: {config}
""",
    )
    with pytest.raises(CatalogError) as exc:
        load_plugins(tmp_path)
    assert f"sources[s].{field}" in str(exc.value)


def test_personal_repo_listing_is_unauthenticated() -> None:
    seen: list[str | None] = []

    def fake_get_json(endpoint: Any, path: str, query: Any = None) -> Any:
        seen.append(endpoint.token)
        return []

    request = Request(
        method="vcs.repos",
        config={
            "endpoint": "https://api.github.com",
            "token_env": "CADASTRE_P_GITHUB_TOKEN",
            "user": "someone",
        },
    )
    with (
        mock.patch.dict("os.environ", {"CADASTRE_P_GITHUB_TOKEN": "t"}),
        mock.patch.object(forge_github, "get_json", fake_get_json),
    ):
        assert forge_github._repos(request).ok
    assert seen == [None]


def _discovering(key_file: Path, owner: str | None = None) -> AppAuth:
    return AppAuth(app_id_env="GH_APP_ID", private_key_file=str(key_file), owner=owner)


def test_installation_is_discovered_for_the_owner(key_file: Path) -> None:
    fake = FakeGitHub()
    token = github_app.installation_token(
        _discovering(key_file), ENV, owner="example", fetch=fake
    )
    assert token == "ghs_minted"
    assert [r.full_url for r in fake.requests] == [
        "https://api.github.com/orgs/example/installation",
        "https://api.github.com/app/installations/424242/access_tokens",
    ]


def test_configured_owner_wins_and_falls_back_to_a_user_install(
    key_file: Path,
) -> None:
    fake = FakeGitHub()
    with pytest.raises(github_app.AppAuthError) as exc:
        github_app.installation_token(
            _discovering(key_file, owner="someone"), ENV, owner="example", fetch=fake
        )
    assert exc.value.error.kind == "unauthorized"
    assert [r.full_url for r in fake.requests] == [
        "https://api.github.com/orgs/someone/installation",
        "https://api.github.com/users/someone/installation",
    ]


def test_discovery_without_any_owner_is_a_config_error(key_file: Path) -> None:
    with pytest.raises(github_app.AppAuthError) as exc:
        github_app.installation_token(_discovering(key_file), ENV, fetch=FakeGitHub())
    assert exc.value.error.kind == "invalid_config"


def test_runner_discovers_from_config_org(key_file: Path) -> None:
    source = _echo_env_source(key_file, org="example")
    source = SourceConfig(
        id=source.id,
        command=source.command,
        config=source.config,
        app_auth=_discovering(key_file),
    )
    with mock.patch.object(github_app, "_urlopen", FakeGitHub()):
        outcome = runner.call(source, "vcs.repos", environ={"GH_APP_ID": "12345"})
    assert outcome.ok, outcome.message
    assert outcome.reply is not None
    assert outcome.reply.result["token"] == "ghs_minted"


def test_token_env_alone_still_works_without_app_auth() -> None:
    script = (
        "import json, os, sys; sys.stdin.read(); "
        "print(json.dumps({'protocol': 1, 'ok': True, 'as_of': "
        "'2026-01-01T00:00:00Z', 'result': "
        "{'token': os.environ.get('CADASTRE_P_GITHUB_TOKEN')}}))"
    )
    source = SourceConfig(
        id="gh",
        command=(sys.executable, "-c", script),
        config={"token_env": "CADASTRE_P_GITHUB_TOKEN"},
    )
    outcome = runner.call(
        source, "vcs.repos", environ={"CADASTRE_P_GITHUB_TOKEN": "static"}
    )
    assert outcome.reply is not None
    assert outcome.reply.result["token"] == "static"
