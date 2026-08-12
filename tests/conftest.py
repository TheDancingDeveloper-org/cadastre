"""Shared fixtures.

Two rules the whole suite obeys:

* **No test writes to `declared/`.** Anything that mutates works on a copy in
  tmp_path.
* **No test touches a live service.** Plugins are exercised as fixture
  processes, so the suite runs on a laptop with no network and no credentials.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cadastre.cli.session import Session
from tests.fixtures.workspace import WorkspaceBuilder

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CATALOG = REPO_ROOT / "examples" / "catalog"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def console_script(name: str) -> str | None:
    """Resolve a console script belonging to the interpreter under test.

    NOT `shutil.which` alone. The tests that spawn a real binary as a
    subprocess would otherwise take whatever PATH names first, and a
    system-wide or pipx install alongside a dev checkout is the normal
    developer setup. The substitution is silent and the failure baffling:
    write tools reported "missing" from a build that has them, because an
    older package was the thing actually launched. Prefer the script beside
    this interpreter, so the subprocess is the code the suite imported.
    """
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
GOLDEN = Path(__file__).resolve().parent / "golden"

#: Fixed clock. Staleness is a function of `now`, so every golden file and
#: every staleness assertion pins it.
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)

#: The catalog's own age, pinned for interchange fixtures.
DECLARED_AS_OF = "2026-08-07T08:00:00Z"


def _loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # Unix-domain and other non-IP sockets are in-process/local.
    host = address[0]
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail closed on non-loopback connections, including Python children."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock: socket.socket, address: object) -> object:
        if not _loopback(address):
            raise AssertionError(
                f"test attempted a non-loopback connection: {address!r}"
            )
        return original_connect(sock, address)  # type: ignore[arg-type]

    def guarded_connect_ex(sock: socket.socket, address: object) -> int:
        if not _loopback(address):
            raise AssertionError(
                f"test attempted a non-loopback connection: {address!r}"
            )
        return original_connect_ex(sock, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    guard_path = str(FIXTURES / "no_network")
    prior = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("CADASTRE_TEST_NO_NETWORK", "1")
    monkeypatch.setenv(
        "PYTHONPATH", guard_path if not prior else guard_path + os.pathsep + prior
    )
    yield


@pytest.fixture
def mock_workspace(tmp_path: Path) -> WorkspaceBuilder:
    return WorkspaceBuilder(tmp_path / "workspace")


@pytest.fixture
def example_catalog() -> Path:
    return EXAMPLE_CATALOG


@pytest.fixture
def catalog_copy(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """A writable copy of the example catalog."""
    destination = tmp_path / "catalog"
    shutil.copytree(EXAMPLE_CATALOG, destination)
    runtime_modules = {
        "tests.test_adapters",
        "tests.test_application_services",
        "tests.test_mcp",
        "tests.test_mcp_stdio_sdk",
        "tests.test_observations",
        "tests.test_remote_bridge",
        "tests.test_security",
        "tests.test_streamable",
    }
    if request.node.module.__name__ in runtime_modules:
        from cadastre.core.storage import import_legacy, initialize

        initialize(destination)
        import_legacy(destination, destination)
    return destination


@pytest.fixture
def session(example_catalog: Path) -> Session:
    return Session.open(example_catalog, now=NOW, as_of=DECLARED_AS_OF)


@pytest.fixture
def fixture_plugin() -> Iterator[list[str]]:
    """argv for the fixture plugin. It answers from files, never a network."""
    yield [sys.executable, str(FIXTURES / "plugin_fixture.py")]


def read_golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def write_golden(name: str, text: str) -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / name).write_text(text, encoding="utf-8")


def assert_golden(name: str, actual: str) -> None:
    """Compare against a golden file, or create it when `--update-golden`.

    Determinism is a stated property, so it needs a test that fails when it
    breaks — not a test that quietly re-records.
    """
    path = GOLDEN / name
    if not path.exists():
        write_golden(name, actual)
        pytest.fail(f"created missing golden file {name}; re-run to verify")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, f"{name} changed"
