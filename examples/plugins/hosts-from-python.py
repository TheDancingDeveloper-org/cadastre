#!/usr/bin/env python3
"""A self-contained Python plugin backed by a recorded inventory JSON file."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "python-hosts",
    "version": "1",
    "capabilities": ["Inventory"],
    "entities": [
        {
            "kind": "host",
            "authority": "source",
            "reflected": ["id", "role"],
            "intended": [],
            "annotated": ["tags", "notes"],
            "identity": ["id"],
            "attributes": {"type": "object", "additionalProperties": True},
            "on_contest": {"id": "exclude", "role": "exclude"},
            "empty_expected": False,
        }
    ],
}


def _timestamp(epoch: float | None = None) -> str:
    now = (
        datetime.now(tz=UTC) if epoch is None else datetime.fromtimestamp(epoch, tz=UTC)
    ).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def _ok(result: dict[str, Any], *, as_of: str | None = None) -> dict[str, Any]:
    return {
        "v": 1,
        "ok": True,
        "result": result,
        "as_of": as_of or _timestamp(),
        "warnings": [],
    }


def _error(kind: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "v": 1,
        "ok": False,
        "error": {"kind": kind, "message": message, "retryable": retryable},
    }


def _inventory(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("inventory_file")
    if not isinstance(value, str) or not value:
        return _error("invalid_config", "config.inventory_file is required")
    path = Path(value)
    try:
        text = path.read_text(encoding="utf-8")
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return _error("invalid_config", "configured inventory file does not exist")
    except OSError as exc:
        return _error("unreachable", f"cannot read inventory: {exc}", retryable=True)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _error("internal", "configured inventory file is not valid JSON")
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, list) or not all(isinstance(host, dict) for host in hosts):
        return _error("internal", "inventory must contain a list named hosts")
    return _ok({"entities": {"host": hosts}}, as_of=_timestamp(modified))


def _handle(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    if method == "plugin.info":
        return _ok({**PLUGIN, "methods": ["inventory.list"]})
    if method == "inventory.list":
        config = request.get("config")
        if not isinstance(config, dict):
            return _error("invalid_config", "request.config must be an object")
        return _inventory(config)
    return _error("not_found", f"unsupported method: {method}")


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            reply = _error("invalid_config", "request must be an object")
        else:
            reply = _handle(request)
    except (OSError, json.JSONDecodeError):
        reply = _error("invalid_config", "stdin must contain one JSON object")
    json.dump(reply, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
