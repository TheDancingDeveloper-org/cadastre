"""M3 — the plugin protocol, and every way a plugin can misbehave.

Each malformed case must degrade to a stale source and never a crash. That is
the property that lets a collector run unattended.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cadastre.plugins import PluginRegistry, runner
from cadastre.plugins.config import SourceConfig, load_plugins
from cadastre.plugins.protocol import Request, parse_reply
from tests.conftest import FIXTURES, REPO_ROOT

FIXTURE = [sys.executable, str(FIXTURES / "plugin_fixture.py")]
PYTHON_EXAMPLE = REPO_ROOT / "examples" / "plugins" / "hosts-from-python.py"


def source(mode: str = "ok", timeout_seconds: int = 10) -> SourceConfig:
    return SourceConfig(
        id="fixture",
        command=tuple(FIXTURE),
        config={"mode": mode},
        timeout_seconds=timeout_seconds,
    )


# -- the happy path ---------------------------------------------------------


def test_handshake_reports_capabilities() -> None:
    outcome = runner.info(source())
    assert outcome.ok
    assert outcome.reply is not None
    assert "Inventory" in outcome.reply.result["capabilities"]


def test_a_successful_call_carries_as_of() -> None:
    outcome = runner.call(source(), "inventory.list")
    assert outcome.ok
    assert outcome.reply is not None
    assert outcome.reply.as_of == "2026-08-07T09:00:00Z"


# -- malformed plugins ----------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_kind"),
    [
        ("crash", "internal"),  # non-zero exit
        ("garbage", "internal"),  # not JSON
        ("chatty", "internal"),  # stdout pollution
        ("no_as_of", "internal"),  # a success without provenance
        ("unauthorized", "unauthorized"),  # a well-formed refusal
    ],
)
def test_misbehaviour_becomes_an_error_not_an_exception(
    mode: str, expected_kind: str
) -> None:
    outcome = runner.call(source(mode), "inventory.list")
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.kind == expected_kind


def test_a_hanging_plugin_times_out_and_is_retryable() -> None:
    outcome = runner.call(source("hang", timeout_seconds=1), "inventory.list")
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.kind == "unreachable"
    assert outcome.error.retryable


def test_a_missing_command_is_a_config_error() -> None:
    config = SourceConfig(id="nope", command=("definitely-not-a-real-binary-xyz",))
    outcome = runner.call(config, "inventory.list")
    assert outcome.error is not None
    assert outcome.error.kind == "invalid_config"


def test_an_unimplemented_method_is_not_found() -> None:
    outcome = runner.call(source(), "dns.zones")
    assert outcome.error is not None
    assert outcome.error.kind == "not_found"


def test_an_unknown_error_kind_is_normalised_to_internal() -> None:
    reply = parse_reply(
        json.dumps({"v": 1, "ok": False, "error": {"kind": "banana", "message": ""}})
    )
    assert reply.error is not None
    assert reply.error.kind == "internal"


# -- the rules that are not about failure -----------------------------------


def test_write_methods_are_refused_before_the_process_starts() -> None:
    outcome = runner.call(source(), "ci.trigger")
    assert outcome.error is not None
    assert "write method" in outcome.error.message


def test_credentials_reach_a_plugin_by_environment_not_argv() -> None:
    config = SourceConfig(
        id="s",
        command=("true",),
        config={"endpoint": "https://x", "token_env": "CADASTRE_P_FOO_TOKEN"},
    )
    environ = {"PATH": "/usr/bin", "CADASTRE_P_FOO_TOKEN": "sekrit", "OTHER": "no"}
    env = runner.build_env(config, environ)
    assert env["CADASTRE_P_FOO_TOKEN"] == "sekrit"
    assert "OTHER" not in env
    assert "sekrit" not in Request("inventory.list", config=config.config).to_json()


def test_the_environment_is_not_inherited_wholesale() -> None:
    config = SourceConfig(id="s", command=("true",), env=("WANTED",))
    env = runner.build_env(config, {"PATH": "/bin", "WANTED": "y", "SECRET": "n"})
    assert set(env) == {"PATH", "WANTED"}


# -- the in-tree plugins --------------------------------------------------


def _call_module(module: str, request: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_static_plugin_reads_declared(example_catalog: Path) -> None:
    reply = _call_module(
        "cadastre.plugins.static",
        {
            "v": 1,
            "method": "inventory.list",
            "params": {},
            "config": {"catalog": str(example_catalog)},
        },
    )
    assert reply["ok"] is True
    hosts = reply["result"]["entities"]["host"]  # type: ignore[index]
    assert {h["id"] for h in hosts} >= {"app-01", "edge-01"}


def test_static_plugin_as_of_is_the_catalogs_age_not_now(
    example_catalog: Path,
) -> None:
    reply = _call_module(
        "cadastre.plugins.static",
        {
            "v": 1,
            "method": "plugin.info",
            "config": {"catalog": str(example_catalog)},
        },
    )
    from datetime import UTC, datetime

    from cadastre.core.provenance import parse_timestamp

    assert parse_timestamp(str(reply["as_of"])) <= datetime.now(tz=UTC)


def test_exec_plugin_runs_a_configured_command() -> None:
    payload = json.dumps({"entities": {"network": [{"id": "n1", "class": "private"}]}})
    reply = _call_module(
        "cadastre.plugins.exec_plugin",
        {
            "v": 1,
            "method": "network.list",
            "config": {
                "commands": {
                    "network.list": [sys.executable, "-c", f"print({payload!r})"]
                }
            },
        },
    )
    assert reply["ok"] is True
    assert reply["result"]["entities"]["network"][0]["id"] == "n1"  # type: ignore[index]


def test_exec_plugin_reports_an_unconfigured_method() -> None:
    reply = _call_module(
        "cadastre.plugins.exec_plugin",
        {"v": 1, "method": "dns.zones", "config": {"commands": {}}},
    )
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "not_found"  # type: ignore[index]


def test_a_plugin_that_raises_still_exits_zero_with_a_json_error() -> None:
    """Exit code 0 on a well-formed response, including `ok:false`."""
    reply = _call_module(
        "cadastre.plugins.static",
        {"v": 1, "method": "inventory.list", "config": {"catalog": "/nonexistent"}},
    )
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "invalid_config"  # type: ignore[index]


# -- configuration ----------------------------------------------------------


def test_a_shell_string_command_is_rejected(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    (declared / "plugins.yaml").write_text(
        "sources:\n- id: s\n  command: 'curl https://x | jq'\n", encoding="utf-8"
    )
    from cadastre.core.errors import CatalogError

    with pytest.raises(CatalogError) as caught:
        load_plugins(tmp_path)
    assert "never runs a shell" in caught.value.render()


def test_absent_plugins_file_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "declared").mkdir()
    assert load_plugins(tmp_path).sources == ()


def test_source_coverage_must_be_a_kind_mapping(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    (declared / "plugins.yaml").write_text(
        "sources:\n- id: s\n  command: [collector]\n  coverage: nope\n",
        encoding="utf-8",
    )
    from cadastre.core.errors import CatalogError

    with pytest.raises(CatalogError) as caught:
        load_plugins(tmp_path)
    assert "kind -> coverage mapping" in caught.value.render()


def test_source_coverage_accepts_a_manifest_kind_when_the_module_is_active(
    tmp_path: Path,
) -> None:
    """Regression: coverage validation checked the base-only ENTITY_SPECS,
    so a Manifest collector's coverage narrowing (e.g. forge_item) was
    rejected as an "unknown entity kind" even with the module enabled."""
    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    (tmp_path / "plugins.yaml").write_text(
        "sources:\n"
        "- id: s\n"
        "  command: [collector]\n"
        "  coverage:\n"
        "    forge_item:\n"
        "      where:\n"
        "        repo: org/repo\n",
        encoding="utf-8",
    )

    plugins = load_plugins(tmp_path)

    assert plugins.sources[0].coverage["forge_item"]["where"]["repo"] == "org/repo"


def test_the_shell_example_plugin_speaks_the_protocol() -> None:
    """The adoption claim, tested: a plugin in 20 lines of shell works."""
    script = REPO_ROOT / "examples" / "plugins" / "hosts-from-ssh.sh"
    config = SourceConfig(id="shell", command=("sh", str(script)))
    outcome = runner.call(config, "inventory.list")
    assert outcome.ok
    assert outcome.reply is not None
    hosts = outcome.reply.result["entities"]["host"]
    assert hosts[0]["id"] == "app-01"


def test_one_python_file_provides_declaration_and_collection(tmp_path: Path) -> None:
    plugin_directory = tmp_path / "plugins"
    plugin_directory.mkdir()
    plugin = plugin_directory / PYTHON_EXAMPLE.name
    plugin.write_bytes(PYTHON_EXAMPLE.read_bytes())
    inventory = tmp_path / "hosts.json"
    inventory.write_text(
        json.dumps({"hosts": [{"id": "worker-01", "role": "container-host"}]}),
        encoding="utf-8",
    )

    registered = PluginRegistry.discover(tmp_path).get("python-hosts")
    assert registered is not None
    assert registered.info.entity("host") is not None

    config = SourceConfig(
        id="python-hosts",
        plugin="python-hosts",
        command=(sys.executable, str(plugin)),
        methods=("inventory.list",),
        config={"inventory_file": str(inventory)},
    )
    handshake = runner.info(config)
    assert handshake.ok
    outcome = runner.call(config, "inventory.list")
    assert outcome.ok
    assert outcome.reply is not None
    assert outcome.reply.result["entities"]["host"] == [
        {"id": "worker-01", "role": "container-host"}
    ]
