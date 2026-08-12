#!/bin/sh
# Emit deterministic local release metadata from already-built artifacts.
set -eu

out=${1:?usage: release-metadata.sh OUTPUT.json}
package=${CADASTRE_PACKAGE_ARTIFACT:?set CADASTRE_PACKAGE_ARTIFACT}
gui=${CADASTRE_GUI_ARTIFACT:?set CADASTRE_GUI_ARTIFACT}
backend_oci=${CADASTRE_OCI_ARCHIVE:?set CADASTRE_OCI_ARCHIVE}
gui_oci=${CADASTRE_GUI_OCI_ARCHIVE:?set CADASTRE_GUI_OCI_ARCHIVE}
revision=${CADASTRE_SOURCE_REVISION:-unknown}
version=${CADASTRE_VERSION:?set CADASTRE_VERSION}

for artifact in "$package" "$gui" "$backend_oci" "$gui_oci"; do
  test -s "$artifact"
done

python3 - "$out" "$package" "$gui" "$backend_oci" "$gui_oci" "$revision" "$version" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

out, *values = sys.argv[1:]
package, gui, backend_oci, gui_oci, revision, version = values
def digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()

document = {
    "application_version": version,
    "schema_version": 1,
    "source_revision": revision,
    "artifacts": {
        "python_package": {"path": package, "digest": digest(package)},
        "gui_static": {"path": gui, "digest": digest(gui)},
        "backend_oci": {"path": backend_oci, "digest": digest(backend_oci)},
        "gui_oci": {"path": gui_oci, "digest": digest(gui_oci)},
    },
}
Path(out).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
