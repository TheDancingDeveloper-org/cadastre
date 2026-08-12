#!/usr/bin/env python3
"""Inspect an OCI archive and its layers for forbidden runtime content."""

from __future__ import annotations

import io
import json
import sys
import tarfile


def read_json(bundle: tarfile.TarFile, name: str) -> dict[str, object]:
    member = bundle.extractfile(name)
    if member is None:
        raise SystemExit(f"OCI archive is missing {name}")
    value = json.load(member)
    if not isinstance(value, dict):
        raise SystemExit(f"OCI archive {name} is not a JSON object")
    return value


def blob_name(digest: str) -> str:
    algorithm, value = digest.split(":", 1)
    if algorithm != "sha256" or not value:
        raise SystemExit(f"unsupported OCI digest: {digest}")
    return f"blobs/{algorithm}/{value}"


def main() -> int:
    archive = sys.argv[1] if len(sys.argv) == 2 else ""
    if not archive:
        print("usage: verify-oci-archive.py OCI_ARCHIVE", file=sys.stderr)
        return 2
    bad: list[str] = []
    with tarfile.open(archive, "r:*") as bundle:
        index = read_json(bundle, "index.json")
        manifests = index.get("manifests")
        if not isinstance(manifests, list) or not manifests:
            raise SystemExit("OCI archive has no image manifest")
        descriptor = manifests[0]
        if not isinstance(descriptor, dict) or not isinstance(
            descriptor.get("digest"), str
        ):
            raise SystemExit("OCI archive has an invalid manifest descriptor")
        manifest = read_json(bundle, blob_name(descriptor["digest"]))
        layers = manifest.get("layers")
        if not isinstance(layers, list):
            raise SystemExit("OCI image manifest has no layers")
        for layer in layers:
            if not isinstance(layer, dict) or not isinstance(layer.get("digest"), str):
                raise SystemExit("OCI image manifest has an invalid layer descriptor")
            layer_file = bundle.extractfile(blob_name(layer["digest"]))
            if layer_file is None:
                raise SystemExit("OCI image manifest references a missing layer")
            with tarfile.open(
                fileobj=io.BytesIO(layer_file.read()), mode="r:*"
            ) as files:
                for member in files.getmembers():
                    name = member.name.lstrip("./")
                    parts = name.split("/")
                    if name.endswith("docker.sock") or ".git" in parts:
                        bad.append(name)
                    if (
                        member.isfile()
                        and member.mode & 0o111
                        and parts[-1]
                        in {
                            "git",
                            "docker",
                        }
                    ):
                        bad.append(name)
    if bad:
        raise SystemExit(
            "forbidden runtime paths in OCI layers: " + ", ".join(sorted(set(bad)))
        )
    print(
        "OCI archive verified: no Git executable, Docker executable, repository, "
        "or socket path"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
