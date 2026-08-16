"""Regression checks for the authoritative and mirror release boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import yaml


def _oci_archive(path: Path, layer_files: tuple[str, ...]) -> None:
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        for name in layer_files:
            payload = b"fixture"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            layer.addfile(info, io.BytesIO(payload))
    layer_blob = layer_buffer.getvalue()
    layer_digest = "sha256:" + hashlib.sha256(layer_blob).hexdigest()
    manifest_value = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": layer_digest,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": layer_digest,
            }
        ],
    }
    manifest_blob = json.dumps(manifest_value, separators=(",", ":")).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_blob).hexdigest()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
            }
        ],
    }
    with tarfile.open(path, "w") as archive:
        for name, payload in (
            ("index.json", json.dumps(index).encode()),
            (f"blobs/sha256/{manifest_digest[7:]}", manifest_blob),
            (f"blobs/sha256/{layer_digest[7:]}", layer_blob),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _workflows() -> dict[str, Any]:
    directory = Path(__file__).parents[1] / ".github" / "workflows"
    return {
        path.name: yaml.safe_load(path.read_text())
        for path in sorted(directory.iterdir())
    }


def test_authoritative_release_is_tag_gated() -> None:
    """The image release path may only ever be reached by a version tag.

    These gates lived in `.woodpecker/production.yaml` until 2026-08-11 and had
    never run — Woodpecker had no `cadastre` repository — so every image the
    estate deployed was hand-built outside them. They now live where CI actually
    executes, and the properties asserted here are the ones that made them
    worth having.
    """
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "release-images.yml").read_text()
    )
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert list(triggers["push"]) == ["tags"]
    assert triggers["push"]["tags"] == ["v*"]
    assert "branches" not in triggers["push"]

    assert workflow["permissions"]["id-token"] == "write", "keyless signing needs OIDC"
    assert workflow["permissions"]["packages"] == "write"
    assert workflow["permissions"]["contents"] == "read"

    steps = workflow["jobs"]["release-images"]["steps"]
    runs = " ".join(step.get("run", "") for step in steps)
    uses = [step.get("uses", "") for step in steps]

    # The gate is one implementation, invoked — never reimplemented inline.
    assert "./scripts/release-gates.sh" in runs
    assert any("cosign-installer" in u for u in uses)
    assert any("syft" in u for u in uses)
    assert any("crane" in u for u in uses)

    # It must publish artifacts it actually built and tested.
    assert "verify-oci-archive.py" in runs
    assert "container-smoke.sh" in runs
    assert "package-install-smoke.sh" in runs
    assert "pytest" in runs

    gate = next(s for s in steps if s.get("run") == "./scripts/release-gates.sh")
    assert gate["env"]["CADASTRE_SKIP_PYPI_PUBLISH"] == "1", (
        "release-pypi.yml owns the wheel on this same tag; two publishers means "
        "the second fails after the images are already signed and pushed"
    )
    assert gate["env"]["CI_COMMIT_TAG"]


def test_only_a_tag_gated_workflow_may_publish() -> None:
    """No push-to-main path may reach a registry or a deployment.

    Previously this asserted no workflow anywhere held `packages: write`,
    because GitHub was the mirror and publishing belonged elsewhere. GitHub is
    now the authoritative side, so the property is narrower and sharper: the
    permission is allowed, but only where a version tag is the only trigger.
    """
    for name, workflow in _workflows().items():
        text = (Path(__file__).parents[1] / ".github" / "workflows" / name).read_text()
        assert "deploy-komodo" not in text, name
        assert "komodo" not in text.lower(), name

        permissions = workflow.get("permissions") or {}
        publishes = permissions.get("packages") == "write"
        if not publishes:
            assert "docker/build-push-action" not in text, name
            continue

        triggers = workflow.get("on") or workflow.get(True) or {}
        push = triggers.get("push") or {}
        assert list(push) == ["tags"], name
        assert push["tags"] == ["v*"], name


def test_release_images_are_pushed_to_a_lowercase_repository() -> None:
    """A registry repository name is lowercase-only; the owner is not.

    `github.repository_owner` is `TheDancingDeveloper-org`, and workflow
    expressions have no lower-casing function, so interpolating it into the
    job's `env:` produced a reference nothing could push. It cost a whole
    release run, which failed at `crane push` with `could not parse reference`
    long after the registry login had succeeded. The lowercased spelling is
    also the only one that reaches the package the estate already pulls.
    """
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "release-images.yml").read_text()
    )
    job = workflow["jobs"]["release-images"]
    job_env = job.get("env") or {}
    assert "CADASTRE_IMAGE" not in job_env, "would be interpolated verbatim"
    assert "CADASTRE_GUI_IMAGE" not in job_env, "would be interpolated verbatim"

    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "tr '[:upper:]' '[:lower:]'" in runs
    assert 'echo "CADASTRE_IMAGE=ghcr.io/$owner/cadastre:sha-$GITHUB_SHA"' in runs
    gui = 'echo "CADASTRE_GUI_IMAGE=ghcr.io/$owner/cadastre-gui:sha-$GITHUB_SHA"'
    assert gui in runs

    # And the gate names the rule itself, rather than leaving a downstream tool
    # to fail on the reference after the irreversible steps have begun.
    release = (root / "scripts" / "release-gates.sh").read_text()
    assert "CADASTRE_IMAGE repository must be lowercase" in release
    assert "CADASTRE_GUI_IMAGE repository must be lowercase" in release
    assert release.index("must be lowercase") < release.index("crane push")


def test_the_schema_compatibility_predicate_type_is_a_uri() -> None:
    """cosign accepts one of its own aliases or a URI, and nothing else.

    `custom.schema-compatibility` is neither, so every image release died on
    `invalid predicate type` — after the image had been pushed, signed, and
    given its SBOM attestation, which is the most expensive place to fail. An
    operator passes this same string to `cosign verify-attestation --type`, so
    the gate and DEPLOYMENT.md have to agree on it character for character.
    """
    root = Path(__file__).parents[1]
    release = (root / "scripts" / "release-gates.sh").read_text()
    declaration = re.search(r"^schema_predicate=(\S+)$", release, re.MULTILINE)
    assert declaration, "the predicate type is declared once, as a variable"
    predicate = declaration.group(1)
    assert predicate.startswith("https://"), predicate
    # Both images carry it, and neither restates the literal.
    assert release.count('--type "$schema_predicate"') == 2
    assert (root / "DEPLOYMENT.md").read_text().count(predicate) == 1


# Only a tag-gated workflow may take the trusted self-hosted pool. Everything
# else is reachable from a fork's pull request.
SELF_HOSTED_WORKFLOWS = frozenset({"release-images.yml", "release-pypi.yml"})


def test_untrusted_triggers_never_reach_the_self_hosted_pool() -> None:
    """This is a public repository, so `pull_request` builds fork code.

    A self-hosted runner on a pull-request trigger is arbitrary code execution
    inside the estate by anyone who can open a PR. The rule is therefore the
    inverse of what it was while the repository was private: only the two
    tag-gated release workflows may name `self-hosted`, and they are reachable
    only from a `refs/tags/v*` push, which requires write access.

    Dynamic `runs-on: ${{ ... }}` is rejected as well: the audit cannot
    statically prove where such a job lands.
    """
    for name, workflow in _workflows().items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            where = f"{name}:{job_name}"
            runner = job.get("runs-on")
            assert isinstance(runner, str | list), f"{where}: {runner!r}"
            labels = [runner] if isinstance(runner, str) else runner
            assert not any("${{" in label for label in labels), where
            if name in SELF_HOSTED_WORKFLOWS:
                assert "self-hosted" in labels, f"{where}: {runner!r}"
            else:
                assert "self-hosted" not in labels, f"{where}: {runner!r}"


def test_self_hosted_workflows_are_all_tag_gated() -> None:
    """The allowlist above is only safe while every entry on it is tag-gated.

    `workflow_dispatch` is permitted alongside the tag push because it is
    restricted to accounts with write access; no fork-controlled event may
    appear.
    """
    for name in SELF_HOSTED_WORKFLOWS:
        workflow = _workflows()[name]
        triggers = workflow.get("on", workflow.get(True))
        assert set(triggers) <= {"push", "workflow_dispatch"}, name
        assert list(triggers["push"]) == ["tags"], name
        assert triggers["push"]["tags"] == ["v*"], name


def test_pull_request_ci_is_github_hosted_and_runs_full_stack_e2e() -> None:
    root = Path(__file__).parents[1]
    ci = yaml.safe_load((root / ".github" / "workflows" / "ci.yaml").read_text())
    for name, job in ci["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", name
    e2e_steps = ci["jobs"]["e2e"]["steps"]
    assert any(step.get("run") == "sh scripts/run-e2e.sh" for step in e2e_steps)


def test_ci_enforces_and_records_python_coverage() -> None:
    root = Path(__file__).parents[1]
    ci = (root / ".github" / "workflows" / "ci.yaml").read_text()
    assert "--cov=src/cadastre" in ci
    assert "python-coverage-${{ matrix.python-version }}" in ci


def test_every_ci_job_installs_its_own_dependencies() -> None:
    """No job may inherit an environment another job happened to leave behind."""
    root = Path(__file__).parents[1]
    ci = yaml.safe_load((root / ".github" / "workflows" / "ci.yaml").read_text())
    for name, job in ci["jobs"].items():
        steps = job["steps"]
        assert any("astral-sh/setup-uv" in s.get("uses", "") for s in steps), name
        # `uv sync` for the jobs that test the tree, `uv tool install` for the
        # one that tests the packaged install path. Either provisions the job;
        # what is not allowed is a job that provisions nothing.
        runs = " ".join(s.get("run", "") for s in steps)
        assert "uv sync" in runs or "uv tool install" in runs, name


def test_release_workflow_has_no_implicit_deploy_step() -> None:
    text = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release-images.yml"
    ).read_text()
    assert "komodo" not in text.lower()


def test_release_scripts_require_the_tested_artifacts() -> None:
    root = Path(__file__).parents[1]
    release = (root / "scripts" / "release-gates.sh").read_text()
    smoke = (root / "scripts" / "container-smoke.sh").read_text()
    stack_smoke = (root / "scripts" / "stack-smoke.sh").read_text()
    archive = (root / "scripts" / "verify-oci-archive.py").read_text()
    assert "CADASTRE_OCI_ARCHIVE" in release
    assert "CADASTRE_PACKAGE_DIR" in release
    assert "CADASTRE_GUI_ARTIFACT" in release
    assert "CADASTRE_GUI_OCI_ARCHIVE" in release
    assert "CADASTRE_GUI_OCI_CHECKSUM" in release
    assert "gui-sbom.spdx.json" in release
    assert "release-metadata.sh" in release
    assert "CADASTRE_TEST_REPORT" in release
    assert "sha256sum -c" in release
    assert 'crane push "$layout_dir/app" "$CADASTRE_IMAGE"' in release
    assert 'crane push "$layout_dir/gui" "$CADASTRE_GUI_IMAGE"' in release
    assert "CADASTRE_GUI_IMAGE must use a sha-* release tag" in release
    assert "CADASTRE_IMAGE must use a sha-* release tag" in release
    # Semver aliases are added against the signed digest, never re-pushed.
    assert 'crane tag "${CADASTRE_IMAGE%:*}@$digest"' in release
    assert 'crane tag "${CADASTRE_GUI_IMAGE%:*}@$gui_digest"' in release
    assert release.index("cosign sign") < release.index("crane tag")
    # A tag that disagrees with the tree fails before anything is pushed.
    assert release.index('test "${CI_COMMIT_TAG:?') < release.index("crane push")
    # PyPI publication is irreversible, so it runs after every other gate.
    assert "uv publish" in release
    assert "CADASTRE_PYPI_TOKEN" in release
    assert release.index("cosign attest") < release.index("uv publish")
    assert "release-compatibility.json" in release
    dockerfile = (root / "Dockerfile").read_text()
    assert "org.opencontainers.image.title" in dockerfile
    assert "org.opencontainers.image.licenses" in dockerfile
    # `image.source` is what links the published package back to this
    # repository. Without it GHCR keeps the package unattached, which costs
    # the repository's Packages listing, Renovate's source lookup, and the
    # provenance a signed image is published for in the first place. Both
    # images carry it, because both are published.
    source = 'org.opencontainers.image.source="https://github.com/TheDancingDeveloper-org/cadastre"'
    assert source in dockerfile
    assert source in (root / "Dockerfile.gui").read_text()
    assert '"PyYAML==6.0.3"' in (root / "pyproject.toml").read_text()
    ci_install = (root / "scripts" / "ci-install.sh").read_text()
    assert "uv export --frozen" in ci_install
    assert "--requirement /tmp/cadastre-requirements.txt" in ci_install
    assert "docker volume create" in smoke
    assert "serverInfo" in smoke
    assert "network smoke annotation" in smoke
    assert "docker compose" in stack_smoke
    assert "serverInfo" in stack_smoke
    assert "stack smoke annotation" in stack_smoke
    assert "CADASTRE_IMAGE" in stack_smoke
    assert "openssl req -x509" in stack_smoke
    assert "openssl rand" in stack_smoke
    assert "--wait --wait-timeout" in stack_smoke
    assert "--allow-write" in stack_smoke
    assert '"$image" backup' in smoke
    assert '"$image" restore' in smoke
    assert "tests/fixtures/container-smoke-bundle" in smoke
    assert "--user 10001:10001" in smoke
    assert "index.json" in archive
    assert "docker.sock" in archive
    assert "mcp-server" in ci_install
    package_smoke = (root / "scripts" / "package-install-smoke.sh").read_text()
    assert "python3 -m venv" in package_smoke
    assert "[mcp-client]" in package_smoke
    assert "cadastre-mcp-remote" in package_smoke
    assert "CADASTRE_MCP_URL is required" in package_smoke
    metadata = (root / "scripts" / "release-metadata.sh").read_text()
    assert "source_revision" in metadata
    assert "backend_oci" in metadata
    assert "gui_oci" in metadata


def test_oci_archive_verifier_rejects_forbidden_runtime_content(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "verify-oci-archive.py"
    clean = tmp_path / "clean.oci.tar"
    _oci_archive(clean, ("app/cadastre",))
    passed = subprocess.run(
        [sys.executable, str(script), str(clean)], capture_output=True, text=True
    )
    assert passed.returncode == 0

    forbidden = tmp_path / "forbidden.oci.tar"
    _oci_archive(forbidden, ("usr/bin/git",))
    failed = subprocess.run(
        [sys.executable, str(script), str(forbidden)], capture_output=True, text=True
    )
    assert failed.returncode != 0
    assert "forbidden runtime paths" in failed.stderr
