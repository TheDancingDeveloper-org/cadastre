"""The shape of a secret value, never the value (DESIGN §1.3).

A secret that breaks a deployment is usually not wrong, it is mis-shaped: a
service-account JSON pasted with raw newlines into a store that a `.env` line
then truncates, a token with a trailing newline, a value that was meant to be
JSON and no longer parses. Finding that used to need a hand-written script
that read the value and printed only counts — the kind of script that leaks
the value the first time somebody edits it.

`describe_value` is that script, written once and tested. It runs where the
value already is (inside the secrets collector process, which reads values
from the store's API anyway and drops them), and what it returns is counts and
booleans only. Nothing it returns is a substring of the value, and there is a
test that asserts exactly that.

Deliberately absent: a hash. A digest of a short or low-entropy value is an
offline guessing oracle, and nothing asked for comparison yet.
"""

from __future__ import annotations

import json
from typing import Any

#: Every key `describe_value` may return. The attribute schema the secrets
#: collector declares is generated from this, so the two cannot drift.
SHAPE_FIELDS: dict[str, str] = {
    "length": "integer",
    "bytes": "integer",
    "lines": "integer",
    "newlines": "integer",
    "carriage_returns": "integer",
    "tabs": "integer",
    "control_characters": "integer",
    "single_line": "boolean",
    "empty": "boolean",
    "leading_whitespace": "boolean",
    "trailing_whitespace": "boolean",
    "trailing_newline": "boolean",
    "non_ascii": "boolean",
    "parses_as_json": "boolean",
    "json_type": "string",
}


def _json_type(value: str) -> str | None:
    try:
        parsed: Any = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if isinstance(parsed, dict):
        return "object"
    if isinstance(parsed, list):
        return "array"
    if isinstance(parsed, str):
        return "string"
    if isinstance(parsed, bool):
        return "boolean"
    if parsed is None:
        return "null"
    return "number"


def describe_value(value: str) -> dict[str, Any]:
    """Counts and booleans about a secret value. Never the value, never a part.

    `single_line` is the property a `.env` line or a `KEY=VALUE` manifest
    needs: no `\\n` and no `\\r` anywhere. `control_characters` counts other C0
    controls and DEL, which are invisible in every terminal they break.
    """
    newlines = value.count("\n")
    carriage_returns = value.count("\r")
    tabs = value.count("\t")
    control = sum(
        1 for ch in value if (ord(ch) < 32 and ch not in "\n\r\t") or ord(ch) == 127
    )
    json_type = _json_type(value) if value.strip() else None
    return {
        "length": len(value),
        "bytes": len(value.encode("utf-8", errors="surrogatepass")),
        "lines": len(value.splitlines()) if value else 0,
        "newlines": newlines,
        "carriage_returns": carriage_returns,
        "tabs": tabs,
        "control_characters": control,
        "single_line": newlines == 0 and carriage_returns == 0,
        "empty": value == "",
        "leading_whitespace": bool(value) and value[0].isspace(),
        "trailing_whitespace": bool(value) and value[-1].isspace(),
        "trailing_newline": value.endswith(("\n", "\r")),
        "non_ascii": any(ord(ch) > 127 for ch in value),
        "parses_as_json": json_type is not None,
        "json_type": json_type or "none",
    }


def shape_problems(shape: dict[str, Any]) -> list[str]:
    """Plain-language reasons a value may break a line-oriented consumer."""
    problems: list[str] = []
    if shape.get("empty"):
        problems.append("the value is empty")
    if shape.get("newlines"):
        problems.append(
            f"{shape['newlines']} raw newline(s): a .env line or KEY=VALUE "
            "manifest will truncate or split it"
        )
    if shape.get("carriage_returns"):
        problems.append(f"{shape['carriage_returns']} carriage return(s)")
    if shape.get("control_characters"):
        problems.append(f"{shape['control_characters']} other control character(s)")
    if shape.get("leading_whitespace") or (
        shape.get("trailing_whitespace") and not shape.get("trailing_newline")
    ):
        problems.append("leading or trailing whitespace")
    return problems
