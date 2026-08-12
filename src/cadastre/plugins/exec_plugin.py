"""The `exec` plugin — runs a configured command, parses JSON from stdout.

The adoption unlock (DESIGN §4.4): anyone integrates anything with a short
shell script, and nobody writes a real plugin to try the tool. It is also the
discovery mechanism for which plugins deserve to be real ones — a script that
keeps growing is a plugin asking to be written.

Configuration, in `declared/plugins.yaml`:

```yaml
sources:
- id: router
  command: [cadastre-plugin-exec]
  config:
    commands:
      network.list: [ssh, rtr-01, "vtysh -c 'show interface json'"]
    shape: entities        # or: raw
```

The remote side runs its own native command and returns JSON. Nothing is
installed on the observed host.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.harness import serve
from cadastre.plugins.protocol import Reply, Request, fail, ok

NAME = "exec"
VERSION = "1"

DEFAULT_TIMEOUT = 60


def _commands(request: Request) -> dict[str, list[str]]:
    raw = request.config.get("commands") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for method, command in raw.items():
        if isinstance(command, list) and command:
            out[str(method)] = [str(word) for word in command]
    return out


def _info(request: Request) -> Reply:
    commands = _commands(request)
    return ok(
        {
            "name": NAME,
            "version": VERSION,
            # An exec source's capabilities are whatever the operator wired up.
            "capabilities": sorted({method.split(".")[0] for method in commands}),
            "methods": sorted(commands),
            "entities": request.config.get("entities") or [],
        },
        format_timestamp(datetime.now(tz=UTC)),
    )


def _run(request: Request) -> Reply:
    commands = _commands(request)
    command = commands.get(request.method)
    if command is None:
        return fail(
            "not_found",
            f"no command configured for {request.method!r}; configured: "
            + (", ".join(sorted(commands)) or "(none)"),
        )
    timeout = int(request.config.get("timeout_seconds", DEFAULT_TIMEOUT))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return fail("invalid_config", f"no such command: {command[0]}")
    except subprocess.TimeoutExpired:
        return fail("unreachable", f"timed out after {timeout}s", retryable=True)
    except OSError as exc:
        return fail("internal", str(exc))

    if completed.returncode != 0:
        tail = " ".join(completed.stderr.split())[-400:]
        return fail(
            "unreachable",
            f"command exited {completed.returncode}: {tail}",
            retryable=True,
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return fail("internal", f"command stdout is not JSON: {exc}")

    shape = str(request.config.get("shape", "entities"))
    as_of = format_timestamp(datetime.now(tz=UTC))
    if shape == "raw":
        # Kept verbatim under `extra`, where nothing interprets it as model
        # data. Useful before you know what the shape should be.
        return ok({"extra": {request.method: payload}}, as_of)
    if not isinstance(payload, dict) or "entities" not in payload:
        return fail(
            "internal",
            "expected an object with an `entities` key; set `shape: raw` in "
            "config to pass the output through untouched instead",
        )
    result: dict[str, Any] = {"entities": payload["entities"]}
    if isinstance(payload.get("extra"), dict):
        result["extra"] = payload["extra"]
    return ok(result, str(payload.get("as_of") or as_of))


def main() -> int:
    # Method dispatch is configuration here, not code: anything the operator
    # wired a command for is a method this plugin implements.
    return serve({}, info=_info, fallback=_run)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
