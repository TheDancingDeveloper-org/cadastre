"""M4 — the scheduled job that populates observed/.

The job itself cannot be tested here: it talks to a real estate, which is the
whole point of it. What can be tested is the part that rots silently — the
shipped script and the credential file drifting apart from the sample plugin
configuration they exist to serve.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import yaml

from tests.conftest import REPO_ROOT

COLLECTOR = REPO_ROOT / "examples" / "collector"
INFISICAL_ENTRYPOINT = REPO_ROOT / "scripts" / "infisical-entrypoint.py"


def test_the_scheduled_job_is_valid_posix_sh() -> None:
    """It runs unattended on a host nobody is watching. It has to parse."""
    result = subprocess.run(
        ["sh", "-n", str(COLLECTOR / "collect.sh")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_every_sample_credential_variable_is_documented() -> None:
    """A token_env with no entry in collect.env.sample is a plugin that will
    fail its handshake on first run, for a reason nobody can see."""
    sample = yaml.safe_load(
        (REPO_ROOT / "examples" / "plugins.sample.yaml").read_text()
    )
    wanted = {
        source["config"]["token_env"]
        for source in sample["sources"]
        if "token_env" in source.get("config", {})
    }
    documented = {
        line.split("=", 1)[0]
        for line in (COLLECTOR / "collect.env.sample").read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert wanted <= documented


def test_the_unit_runs_the_installed_script() -> None:
    unit = (COLLECTOR / "cadastre-collect.service").read_text()
    readme = (COLLECTOR / "README.md").read_text()
    assert "ExecStart=/usr/local/bin/cadastre-collect" in unit
    assert "collect.sh /usr/local/bin/cadastre-collect" in readme
    # The runtime data directory is the one path the job writes. Anything else
    # is a bug in the sandboxing, not a bug in Cadastre.
    assert "ReadWritePaths=/var/lib/cadastre" in unit


# --------------------------------------------------------------------------
# Infisical universal-auth entrypoint (§3 remainder, F5)
# --------------------------------------------------------------------------


class _StubLoginHandler(BaseHTTPRequestHandler):
    """A minimal stand-in for Infisical's universal-auth login endpoint."""

    received: ClassVar[dict[str, object]] = {}

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _StubLoginHandler.received = json.loads(self.rfile.read(length))
        payload = json.dumps({"accessToken": "minted-token-xyz"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _run_entrypoint(
    tmp_path: Path, *, login_url: str, token_env: str, command: list[str]
) -> subprocess.CompletedProcess[str]:
    client_id_file = tmp_path / "client-id"
    client_secret_file = tmp_path / "client-secret"
    client_id_file.write_text("id123\n", encoding="utf-8")
    client_secret_file.write_text("secret456\n", encoding="utf-8")
    env = {
        **os.environ,
        "INFISICAL_CLIENT_ID_FILE": str(client_id_file),
        "INFISICAL_CLIENT_SECRET_FILE": str(client_secret_file),
        "INFISICAL_LOGIN_URL": login_url,
        "INFISICAL_TOKEN_ENV": token_env,
    }
    return subprocess.run(
        [sys.executable, str(INFISICAL_ENTRYPOINT), *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_infisical_entrypoint_accepts_plain_env_vars_when_no_file_is_mounted(
    tmp_path: Path,
) -> None:
    """Some deployment mechanisms (a Komodo-managed stack environment, for
    one) only expose secrets as plain environment variables, with no secret
    file to mount. The file form is still preferred when available (the
    sibling tests cover that); this is the fallback, not the default."""
    server = HTTPServer(("127.0.0.1", 0), _StubLoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **os.environ,
            "INFISICAL_CLIENT_ID": "id123",
            "INFISICAL_CLIENT_SECRET": "secret456",
            "INFISICAL_LOGIN_URL": f"http://127.0.0.1:{server.server_port}/login",
            "INFISICAL_TOKEN_ENV": "CADASTRE_P_SECRETS_TOKEN",
        }
        result = subprocess.run(
            [
                sys.executable,
                str(INFISICAL_ENTRYPOINT),
                sys.executable,
                "-c",
                "import os; print(os.environ['CADASTRE_P_SECRETS_TOKEN'])",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "minted-token-xyz"
    assert _StubLoginHandler.received == {
        "clientId": "id123",
        "clientSecret": "secret456",
    }


def test_infisical_entrypoint_is_valid_python() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(INFISICAL_ENTRYPOINT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_infisical_entrypoint_mints_a_token_and_execs_the_real_command(
    tmp_path: Path,
) -> None:
    """The minted token must reach the wrapped process's environment under
    every configured `token_env` name — and never anywhere else (stdout,
    argv, a file)."""
    server = HTTPServer(("127.0.0.1", 0), _StubLoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_entrypoint(
            tmp_path,
            login_url=f"http://127.0.0.1:{server.server_port}/login",
            token_env="CADASTRE_P_SECRETS_TOKEN,CADASTRE_P_OTHER_TOKEN",
            command=[
                sys.executable,
                "-c",
                "import os; print(os.environ['CADASTRE_P_SECRETS_TOKEN']); "
                "print(os.environ['CADASTRE_P_OTHER_TOKEN'])",
            ],
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["minted-token-xyz", "minted-token-xyz"]
    # The login request itself carried the credential, in the body only.
    assert _StubLoginHandler.received == {
        "clientId": "id123",
        "clientSecret": "secret456",
    }


def test_infisical_entrypoint_never_persists_the_token_to_disk(
    tmp_path: Path,
) -> None:
    server = HTTPServer(("127.0.0.1", 0), _StubLoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_entrypoint(
            tmp_path,
            login_url=f"http://127.0.0.1:{server.server_port}/login",
            token_env="CADASTRE_P_SECRETS_TOKEN",
            command=[sys.executable, "-c", "pass"],
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
    assert result.returncode == 0, result.stderr
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "minted-token-xyz" not in path.read_text(encoding="utf-8")


def test_infisical_entrypoint_fails_closed_on_a_login_error(tmp_path: Path) -> None:
    """No fallback to running the collector without a credential — a
    handshake failure a plugin reports itself is legible; a silent empty
    result is the #52 failure mode this whole design exists to avoid."""

    class _Rejecting(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            self.send_response(401)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), _Rejecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_entrypoint(
            tmp_path,
            login_url=f"http://127.0.0.1:{server.server_port}/login",
            token_env="CADASTRE_P_SECRETS_TOKEN",
            command=[sys.executable, "-c", "print('should not run')"],
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
    assert result.returncode != 0
    assert "should not run" not in result.stdout
    assert "login failed" in result.stderr
    assert "secret456" not in result.stderr


def test_infisical_entrypoint_documents_its_token_env_in_the_compose_file() -> None:
    """`compose.production.yaml`'s `cadastre-collector` service is the shipped
    caller of this wrapper; the two must not drift apart silently."""
    compose = (REPO_ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    assert "infisical-entrypoint.py" in compose
    assert "INFISICAL_CLIENT_ID_FILE" in compose
    assert "INFISICAL_CLIENT_SECRET_FILE" in compose
