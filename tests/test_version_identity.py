"""One true version: every published copy is derived from `cadastre.__version__`.

Bumping the literal in `src/cadastre/__init__.py` and nothing else must fail
here with a precise list of the files that still disagree.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from cadastre import __version__

ROOT = Path(__file__).parents[1]
# A three-part dotted number that is not part of a longer one, so that IP
# addresses such as 127.0.0.1 are not mistaken for version strings.
SEMVER = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")

# Semver-shaped literals in `src/` that are not the application version. Each
# entry is an explicit, reviewed exemption; anything new must fail the guard.
ALLOWED_SOURCE_LITERALS = {
    ("adapters/http.py", "3.1.0"),  # the OpenAPI specification version
}


def test_version_is_a_valid_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pyproject_declares_the_same_version() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


def test_compatibility_document_declares_the_same_version() -> None:
    document = json.loads(
        (ROOT / "src" / "cadastre" / "release-compatibility.json").read_text()
    )
    assert document["application_version"] == __version__


def test_gui_package_declares_the_same_version() -> None:
    package = json.loads((ROOT / "ui" / "package.json").read_text())
    assert package["version"] == __version__


def test_no_version_literals_outside_the_source_of_truth() -> None:
    """`streamable.py` reporting a stale hardcoded `0.1.0` must not recur."""
    offenders = []
    for path in sorted((ROOT / "src" / "cadastre").rglob("*.py")):
        relative = path.relative_to(ROOT / "src" / "cadastre").as_posix()
        if relative == "__init__.py":
            continue
        for literal in SEMVER.findall(path.read_text()):
            if (relative, literal) in ALLOWED_SOURCE_LITERALS:
                continue
            offenders.append(f"{relative}: {literal}")
    assert offenders == []


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.gui"])
def test_image_labels_are_parameterised(name: str) -> None:
    """Base-image tags are pinned literals; label versions are build args."""
    lines = [
        line
        for line in (ROOT / name).read_text().splitlines()
        if not line.startswith("FROM")
    ]
    assert [line for line in lines if SEMVER.search(line)] == []
    text = (ROOT / name).read_text()
    assert "ARG CADASTRE_VERSION" in text
    assert 'org.opencontainers.image.version="$CADASTRE_VERSION"' in text


def test_release_metadata_takes_the_version_as_input() -> None:
    text = (ROOT / "scripts" / "release-metadata.sh").read_text()
    assert SEMVER.search(text) is None
    assert "CADASTRE_VERSION:?" in text


def test_gui_artifact_name_is_derived() -> None:
    text = (ROOT / ".github" / "workflows" / "release-images.yml").read_text()
    assert re.search(r"cadastre-gui-\d", text) is None


def test_release_gate_checks_the_tag_against_the_tree() -> None:
    text = (ROOT / "scripts" / "release-gates.sh").read_text()
    assert SEMVER.search(text) is None
    assert "CI_COMMIT_TAG:?" in text
    assert '"v$version"' in text
