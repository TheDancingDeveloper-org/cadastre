"""The packaged release compatibility document.

The document ships inside the wheel so a running server can answer what it is
compatible with, rather than restating those facts as constants that drift.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

DOCUMENT = "release-compatibility.json"


@lru_cache(maxsize=1)
def compatibility() -> dict[str, Any]:
    """Return the release compatibility document shipped with this package."""
    text = resources.files("cadastre").joinpath(DOCUMENT).read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(text)
    return document
