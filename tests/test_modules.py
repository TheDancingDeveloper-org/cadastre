from pathlib import Path

import pytest

from cadastre.core.errors import CatalogError
from cadastre.modules.config import load_modules
from cadastre.plugins.registry import PluginRegistry


def test_missing_modules_file_disables_everything(tmp_path: Path) -> None:
    modules = load_modules(tmp_path)

    assert modules.modules == ()
    assert not modules.enabled("manifest")


def test_modules_are_loaded_from_runtime_or_bundle_location(tmp_path: Path) -> None:
    (tmp_path / "declared").mkdir()
    (tmp_path / "declared" / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n    version: '1'\n",
        encoding="utf-8",
    )

    modules = load_modules(tmp_path)

    assert modules.enabled("manifest")
    config = modules.by_name("manifest")
    assert config is not None
    assert config.version == "1"


@pytest.mark.parametrize(
    "contents, expected",
    [
        ("modules:\n  manifest:\n    enabled: 'yes'\n", "not a boolean"),
        ("modules:\n  unknown:\n    enabled: true\n", "unknown module"),
        ("modules:\n  manifest: true\n", "not a mapping"),
    ],
)
def test_invalid_module_configuration_is_located(
    tmp_path: Path, contents: str, expected: str
) -> None:
    path = tmp_path / "modules.yaml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(CatalogError, match=expected) as caught:
        load_modules(tmp_path)

    assert "modules.yaml:" in str(caught.value)


def test_manifest_collectors_are_registered_only_when_the_module_is_active(
    tmp_path: Path,
) -> None:
    disabled = PluginRegistry.discover(tmp_path)
    assert disabled.get("work-git") is None
    assert disabled.get("work-github") is None
    assert disabled.get("work-markdown") is None

    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )
    enabled = PluginRegistry.discover(tmp_path)
    work_git = enabled.get("work-git")
    assert work_git is not None
    assert work_git.info.entities[0].kind == "repo_checkout"
    assert enabled.get("work-github") is not None
    assert enabled.get("work-markdown") is not None


def test_runtime_configuration_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / "declared").mkdir()
    (tmp_path / "declared" / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: false\n", encoding="utf-8"
    )
    (tmp_path / "modules.yaml").write_text(
        "modules:\n  manifest:\n    enabled: true\n", encoding="utf-8"
    )

    assert load_modules(tmp_path).enabled("manifest")
