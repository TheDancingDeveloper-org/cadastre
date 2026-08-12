"""`cadastre fmt` — rewrite `declared/` in canonical form.

Opt-in for a real catalog, where comments are welcome. Required for
`examples/`, whose round-trip test is only meaningful if there is exactly one
way to write a given catalog.
"""

from __future__ import annotations

from pathlib import Path

from cadastre.core import model
from cadastre.core.loader import load_catalog
from cadastre.core.serialize import entities_to_documents
from cadastre.core.yamlio import dump_yaml
from cadastre.render.document import Bullets, Document, Section


def canonical_files(root: Path) -> dict[Path, str]:
    """Path -> canonical text, for every entity file the catalog would write.

    One file per kind, named after the kind's directory. A catalog split across
    several files per kind loads fine; `fmt` consolidates it, which is why it
    is opt-in.
    """
    catalog = load_catalog(root)
    out: dict[Path, str] = {}
    for kind, dirname in model.KIND_DIRS.items():
        entities = catalog.all(kind)
        if not entities:
            continue
        path = root / "declared" / dirname / f"{dirname}.yaml"
        out[path] = dump_yaml(entities_to_documents(entities))
    return out


def fmt(root: Path, *, check_only: bool = False) -> Document:
    wanted = canonical_files(root)
    changed: list[str] = []
    for path, text in sorted(wanted.items()):
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        changed.append(str(path.relative_to(root)))
        if not check_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    verb = "would rewrite" if check_only else "rewrote"
    section = Section(
        "Files",
        (Bullets(tuple(f"{verb} {name}" for name in changed)),),
        note=None if changed else "Already canonical.",
    )
    return Document(
        title="cadastre fmt",
        sections=(section,),
        data={"changed": changed, "check_only": check_only},
        exit_code=1 if (check_only and changed) else 0,
    )
