"""The `--json` form.

Same answer, machine shape. Keys are emitted in insertion order and the whole
document is one object, so `cadastre ... --json | diff` is meaningful.
"""

from __future__ import annotations

import json
from typing import Any

from cadastre.render.document import Document


def to_dict(document: Document) -> dict[str, Any]:
    return {
        "command": document.title,
        "result": document.data,
        "provenance": [p.to_dict() for p in document.provenance],
        "stale": [p.source for p in document.stale],
    }


def render(document: Document) -> str:
    return json.dumps(to_dict(document), indent=2, sort_keys=False) + "\n"
