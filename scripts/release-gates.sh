#!/bin/sh
# Protected self-hosted release gate. Credentials are supplied by the runner,
# never by this repository or by image build arguments.
set -eu

: "${CADASTRE_REGISTRY:?set protected CADASTRE_REGISTRY}"
: "${CADASTRE_IMAGE:?set immutable image name}"
: "${CADASTRE_GUI_IMAGE:?set immutable GUI image name}"
: "${COSIGN_EXPERIMENTAL:=0}"

# The source tree is the single source of truth for the version; every other
# copy is checked against it rather than restated.
version=${CADASTRE_VERSION:-$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/cadastre/__init__.py)}
test -n "$version"

oci=${CADASTRE_OCI_ARCHIVE:-.ci-artifacts/cadastre.oci.tar}
oci_checksum=${CADASTRE_OCI_CHECKSUM:-.ci-artifacts/cadastre.oci.sha256}
gui_oci=${CADASTRE_GUI_OCI_ARCHIVE:-.ci-artifacts/cadastre-gui.oci.tar}
gui_oci_checksum=${CADASTRE_GUI_OCI_CHECKSUM:-.ci-artifacts/cadastre-gui.oci.sha256}
package_dir=${CADASTRE_PACKAGE_DIR:-.ci-artifacts/package}
gui_artifact=${CADASTRE_GUI_ARTIFACT:-.ci-artifacts/cadastre-gui-$version.tar.gz}
test_report=${CADASTRE_TEST_REPORT:-.ci-artifacts/pytest.txt}
out=${CADASTRE_RELEASE_DIR:-.ci-artifacts/release}
mkdir -p "$out"

# cosign takes either one of its own predicate aliases or a URI, and nothing
# else — a bare `custom.schema-compatibility` is rejected as an invalid
# predicate type. Verifiers must pass this string to `cosign verify-attestation
# --type`, so it is written once here and quoted verbatim in DEPLOYMENT.md.
schema_predicate=https://github.com/TheDancingDeveloper-org/cadastre/schema-compatibility/v1

test -s "$oci"
test -s "$test_report"
test -s "$oci_checksum"
test -s "$gui_oci"
test -s "$gui_oci_checksum"
test -d "$package_dir"
test -s "$gui_artifact"
test "$(find "$package_dir" -maxdepth 1 -type f -name '*.whl' | wc -l)" -ge 1
test "$(find "$package_dir" -maxdepth 1 -type f -name '*.tar.gz' | wc -l)" -ge 1
sha256sum -c "$oci_checksum"
sha256sum -c "$gui_oci_checksum"

case "$CADASTRE_IMAGE" in
  *:sha-*) : ;;
  *) echo 'CADASTRE_IMAGE must use a sha-* release tag' >&2; exit 2 ;;
esac
case "$CADASTRE_GUI_IMAGE" in
  *:sha-*) : ;;
  *) echo 'CADASTRE_GUI_IMAGE must use a sha-* release tag' >&2; exit 2 ;;
esac

# A registry repository name is lowercase-only, so an image built from a
# mixed-case owner is unpushable. crane reports that as `could not parse
# reference`, after the login and the signing setup have already succeeded;
# saying it here names the variable and the rule instead.
case "${CADASTRE_IMAGE%:*}" in
  *[[:upper:]]*) echo 'CADASTRE_IMAGE repository must be lowercase' >&2; exit 2 ;;
esac
case "${CADASTRE_GUI_IMAGE%:*}" in
  *[[:upper:]]*) echo 'CADASTRE_GUI_IMAGE repository must be lowercase' >&2; exit 2 ;;
esac

# A release tag that disagrees with the tree must fail before anything is
# pushed or signed, not produce a mislabelled artifact.
declared=$(sed -n 's/.*"application_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' src/cadastre/release-compatibility.json)
test "$declared" = "$version" || {
  echo "src/cadastre/release-compatibility.json application_version $declared != $version" >&2
  exit 2
}
test "${CI_COMMIT_TAG:?set CI_COMMIT_TAG}" = "v$version" || {
  echo "tag $CI_COMMIT_TAG does not match declared version $version" >&2
  exit 2
}
wheel=$(find "$package_dir" -maxdepth 1 -type f -name '*.whl' -print -quit)
case "$(basename "$wheel")" in
  "cadastre-$version-"*) : ;;
  *) echo "wheel $(basename "$wheel") does not declare version $version" >&2; exit 2 ;;
esac

command -v syft >/dev/null
command -v cosign >/dev/null
command -v crane >/dev/null
command -v uv >/dev/null
: "${CADASTRE_REGISTRY_USER:?set protected registry username}"
# The package publish is skippable because it is the one step another workflow
# may already own: `.github/workflows/release-pypi.yml` publishes the wheel on
# the same `v*` tag. Two publishers of one version is not a redundancy — the
# second fails on an immutable index, after the images have already been signed
# and pushed. Skipping is explicit and never inferred.
: "${CADASTRE_SKIP_PYPI_PUBLISH:=0}"
if [ "$CADASTRE_SKIP_PYPI_PUBLISH" != "1" ]; then
  : "${CADASTRE_PYPI_TOKEN:?set protected PyPI token}"
fi

syft "oci-archive:$oci" -o spdx-json="$out/sbom.spdx.json"
syft "oci-archive:$gui_oci" -o spdx-json="$out/gui-sbom.spdx.json"
# No image vulnerability scan runs here; the SBOM above is what the release
# ships for downstream analysis. Fixed-version floors this image does need are
# pinned in security-constraints.txt.
sha256sum "$oci" > "$out/oci-archive.sha256"
sha256sum "$gui_oci" > "$out/gui-oci-archive.sha256"
cp "$test_report" "$out/release-test-report.txt"
cp "$package_dir"/*.whl "$out/"
cp "$package_dir"/*.tar.gz "$out/"
cp "$gui_artifact" "$out/"
cp "$gui_oci_checksum" "$out/"
cp src/cadastre/release-compatibility.json "$out/schema-compatibility.json"
CADASTRE_PACKAGE_ARTIFACT=$(find "$package_dir" -maxdepth 1 -type f -name '*.whl' -print -quit) \
CADASTRE_GUI_ARTIFACT="$gui_artifact" \
CADASTRE_OCI_ARCHIVE="$oci" \
CADASTRE_GUI_OCI_ARCHIVE="$gui_oci" \
CADASTRE_SOURCE_REVISION="${CI_COMMIT_SHA:-unknown}" \
CADASTRE_VERSION="$version" \
  scripts/release-metadata.sh "$out/release-metadata.json"
cat > "$out/provenance.json" <<EOF
{"buildType":"https://slsa.dev/provenance/v1","invocation":{"configSource":{"uri":"${CI_REPO_URL:-unknown}","digest":{"sha1":"${CI_COMMIT_SHA:-unknown}"}}},"builder":{"id":"cadastre-self-hosted-rootless"}}
EOF

# The registry login and signing identity are runner-managed. cosign reads its
# key/workload identity from the protected environment or OIDC provider.
printf '%s' "$CADASTRE_REGISTRY_TOKEN" | crane auth login "$CADASTRE_REGISTRY" \
  --username "$CADASTRE_REGISTRY_USER" --password-stdin
# `crane push` reads a docker-archive tarball or an OCI layout DIRECTORY. The
# release artifact is an OCI archive, so unpack the layout and push that; the
# bytes pushed are the ones that were checksummed above.
layout_dir=$(mktemp -d)
trap 'rm -rf "$layout_dir"' EXIT
mkdir -p "$layout_dir/app" "$layout_dir/gui"
tar -xf "$oci" -C "$layout_dir/app"
tar -xf "$gui_oci" -C "$layout_dir/gui"
crane push "$layout_dir/app" "$CADASTRE_IMAGE"
digest=$(crane digest "$CADASTRE_IMAGE")
printf '%s\n' "$digest" > "$out/image.digest"
cosign sign --yes "$CADASTRE_IMAGE@$digest"
cosign attest --yes --predicate "$out/sbom.spdx.json" \
  --type spdxjson "$CADASTRE_IMAGE@$digest"
cosign attest --yes --predicate "$out/schema-compatibility.json" \
  --type "$schema_predicate" "$CADASTRE_IMAGE@$digest"
cosign attest --yes --predicate "$out/provenance.json" \
  --type slsaprovenance "$CADASTRE_IMAGE@$digest"
# Renovate, Watchtower, and Komodo track registry tags and can do nothing with
# `sha-<sha>`. These aliases point at the digest that was just signed and
# attested, so the artifact is unchanged and `sha-*` stays the immutable tag.
for alias in "$version" "${version%.*}" latest; do
  crane tag "${CADASTRE_IMAGE%:*}@$digest" "$alias"
done
crane push "$layout_dir/gui" "$CADASTRE_GUI_IMAGE"
gui_digest=$(crane digest "$CADASTRE_GUI_IMAGE")
printf '%s\n' "$gui_digest" > "$out/gui-image.digest"
cosign sign --yes "$CADASTRE_GUI_IMAGE@$gui_digest"
cosign attest --yes --predicate "$out/gui-sbom.spdx.json" \
  --type spdxjson "$CADASTRE_GUI_IMAGE@$gui_digest"
cosign attest --yes --predicate "$out/schema-compatibility.json" \
  --type "$schema_predicate" "$CADASTRE_GUI_IMAGE@$gui_digest"
cosign attest --yes --predicate "$out/provenance.json" \
  --type slsaprovenance "$CADASTRE_GUI_IMAGE@$gui_digest"
for alias in "$version" "${version%.*}" latest; do
  crane tag "${CADASTRE_GUI_IMAGE%:*}@$gui_digest" "$alias"
done
# Last, because it is the only step that cannot be undone: a version yanked
# from PyPI still cannot be reused. Everything above must have succeeded first.
# The token is read from the protected environment, never passed in argv.
if [ "$CADASTRE_SKIP_PYPI_PUBLISH" = "1" ]; then
  printf '%s\n' "skipping PyPI publish: CADASTRE_SKIP_PYPI_PUBLISH=1" >&2
else
  UV_PUBLISH_TOKEN="$CADASTRE_PYPI_TOKEN" \
    uv publish "$package_dir"/*.whl "$package_dir"/*.tar.gz
fi
printf '%s\n' "verified backend digest: $digest" "verified GUI digest: $gui_digest" \
  > "$out/release-report.txt"
