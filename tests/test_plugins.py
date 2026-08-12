"""M10 — plugin declarations, identity, and discovery."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cadastre.cli.plugins import plugins
from cadastre.cli.session import Session
from cadastre.plugins import (
    PluginRegistry,
    default_entity_declaration,
    matches,
    parse_plugin_info,
)
from cadastre.plugins.contract import normalize_remote_url
from tests.conftest import DECLARED_AS_OF, NOW


def info(*, entities: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "fixture-plugin",
        "version": "1",
        "capabilities": ["Inventory"],
        "entities": entities,
    }


def test_source_declaration_separates_reflected_and_annotated_fields() -> None:
    declaration = default_entity_declaration("host")
    assert declaration.authority == "source"
    assert "role" in declaration.reflected
    assert declaration.field_classes["tags"] == "annotated"


def test_catalog_declaration_makes_non_annotations_intended() -> None:
    declaration = default_entity_declaration("host", authority="catalog")
    assert declaration.intended
    assert not declaration.reflected


def test_identity_matches_across_renames_but_not_different_records() -> None:
    declaration = default_entity_declaration("host")
    first = {"id": "node-1", "role": "server", "notes": "old"}
    renamed = {"id": "node-1", "role": "server", "notes": "new"}
    other = {"id": "node-2", "role": "server", "notes": "old"}
    assert matches(first, renamed, declaration)
    assert not matches(first, other, declaration)


def test_secret_and_pipeline_identity_correlate_on_the_field_that_matches() -> None:
    """No collector's `id` convention matches the catalog's, so identity
    defaulting to ("id",) everywhere correlated
    nothing for secrets/pipelines even when the entities were the same
    upstream thing under a different catalog-chosen id."""
    from cadastre.core import model

    secret_decl = default_entity_declaration("secret")
    assert secret_decl.identity == ("ref",)
    declared_secret = model.Secret(id="forgejo-token", ref="forgejo-token")
    observed_secret = model.Secret(id="infisical-forgejo-token", ref="forgejo-token")
    assert matches(observed_secret, declared_secret, secret_decl)

    pipeline_decl = default_entity_declaration("pipeline")
    assert pipeline_decl.identity == ("repo", "system")
    declared_pipeline = model.Pipeline(id="devbox-ci", repo="devbox", system="ci")
    observed_pipeline = model.Pipeline(
        id="acme-DevBox-ci-selfhosted", repo="devbox", system="ci"
    )
    assert matches(observed_pipeline, declared_pipeline, pipeline_decl)


def test_normalize_remote_url_treats_ssh_https_and_dotgit_as_equivalent() -> None:
    forms = [
        "git@github.com:dancingdeveloper/cadastre.git",
        "ssh://git@github.com/dancingdeveloper/cadastre.git",
        "ssh://git@github.com/dancingdeveloper/cadastre",
        "https://github.com/dancingdeveloper/cadastre.git",
        "https://github.com/dancingdeveloper/cadastre",
        "https://x-access-token@github.com/dancingdeveloper/cadastre",
        "https://GitHub.com/dancingdeveloper/cadastre",
    ]
    normalized = {normalize_remote_url(url) for url in forms}
    assert normalized == {"github.com/dancingdeveloper/cadastre"}

    assert normalize_remote_url(
        "git@forgejo.example.invalid:dancingdeveloper/cadastre.git"
    ) != normalize_remote_url("git@github.com:dancingdeveloper/cadastre.git")


def test_repo_match_overlaps_on_any_shared_remote() -> None:
    """§2d: a repo mid-migration carries two remotes; set overlap on
    normalized URLs correlates it against an observed record that only
    knows one of them, without picking a canonical remote."""
    from cadastre.core import model
    from cadastre.plugins.contract import MATCH_OVERRIDES

    repo_decl = default_entity_declaration("repo")
    assert repo_decl.kind in MATCH_OVERRIDES

    declared = model.Repo(
        id="cadastre",
        remotes=(
            model.Remote(
                forge="forgejo",
                url="https://forgejo.internal.invalid/dancingdeveloper/cadastre.git",
            ),
            model.Remote(
                forge="github",
                url="git@github.com:dancingdeveloper/cadastre.git",
            ),
        ),
    )
    observed_github_only = model.Repo(
        id="dancingdeveloper-cadastre",
        remotes=(
            model.Remote(
                forge="github",
                url="https://github.com/dancingdeveloper/cadastre",
            ),
        ),
    )
    assert matches(observed_github_only, declared, repo_decl)

    disjoint = model.Repo(
        id="unrelated",
        remotes=(
            model.Remote(forge="github", url="https://github.com/someone-else/other"),
        ),
    )
    assert not matches(disjoint, declared, repo_decl)


def test_repo_match_falls_back_to_id_when_remotes_are_absent() -> None:
    from cadastre.core import model

    repo_decl = default_entity_declaration("repo")
    declared = model.Repo(id="cadastre")
    same_id_no_remotes = model.Repo(id="cadastre")
    other_id_no_remotes = model.Repo(id="other")
    assert matches(same_id_no_remotes, declared, repo_decl)
    assert not matches(other_id_no_remotes, declared, repo_decl)


def test_plugin_info_rejects_overlapping_field_classes() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        parse_plugin_info(
            info(
                entities=[
                    {
                        "kind": "host",
                        "authority": "source",
                        "reflected": ["id"],
                        "annotated": ["id"],
                    }
                ]
            )
        )


def test_single_file_plugin_is_registered_inactive(tmp_path: Path) -> None:
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "demo.py").write_text(
        "PLUGIN = {"
        "'name': 'demo', 'version': '1', 'capabilities': [], 'entities': []"
        "}\n",
        encoding="utf-8",
    )
    registry = PluginRegistry.discover(tmp_path)
    assert registry.get("demo") is not None
    assert registry.get("demo").active is False  # type: ignore[union-attr]


def test_plugins_command_reports_registered_inactive_plugin(catalog_copy: Path) -> None:
    plugin_dir = catalog_copy / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "demo.py").write_text(
        "PLUGIN = {"
        "'name': 'demo', 'version': '1', 'capabilities': [], 'entities': []"
        "}\n",
        encoding="utf-8",
    )
    session = Session.open(catalog_copy, now=NOW, as_of=DECLARED_AS_OF)
    document = plugins(session)
    demo = next(item for item in document.data["plugins"] if item["name"] == "demo")
    assert demo == {
        "name": "demo",
        "state": "registered-inactive",
        "version": "1",
        "capabilities": [],
        "entities": [],
        "origin": str(plugin_dir / "demo.py"),
    }
    static = next(item for item in document.data["plugins"] if item["name"] == "static")
    assert static["state"] == "registered-inactive"


def test_every_shipped_plugin_has_an_operator_guide_section() -> None:
    guide = (Path(__file__).parents[1] / "BUILTIN_PLUGINS.md").read_text(
        encoding="utf-8"
    )
    registered = PluginRegistry.discover().plugins
    headings = set(re.findall(r"^## `([^`]+)`$", guide, flags=re.MULTILINE))
    assert {plugin.name for plugin in registered} <= headings


def test_operator_guide_claims_match_registered_builtin_contract() -> None:
    """The comparison table is useful only while it reflects shipped metadata."""
    guide = (Path(__file__).parents[1] / "BUILTIN_PLUGINS.md").read_text(
        encoding="utf-8"
    )
    rows = {
        cells[0].strip("`"): cells
        for line in guide.splitlines()
        if line.startswith("| `")
        if len(cells := [cell.strip() for cell in line.strip("|").split("|")]) == 6
    }
    for plugin in PluginRegistry.discover().plugins:
        assert plugin.name in rows
        for entity in plugin.info.entities:
            assert entity.kind in rows[plugin.name][3]
