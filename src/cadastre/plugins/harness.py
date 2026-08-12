"""A minimal harness for writing a plugin in Python.

Plugins are external processes and may be written in anything — this exists
because the two in-tree plugins are Python and should not each re-implement
"read one JSON object, write one JSON object, exit 0".

The exit-code rule is the subtle one: a well-formed `ok:false` is still exit 0.
Non-zero means *the plugin itself* is broken, which is a different finding.
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from typing import TextIO

from cadastre.plugins.protocol import Reply, Request, fail, parse_request

Handler = Callable[[Request], Reply]


def serve(
    handlers: dict[str, Handler],
    *,
    info: Handler | None = None,
    fallback: Handler | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Read one request, answer it, return the process exit code."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    try:
        text = source.read()
    except OSError as exc:  # pragma: no cover - stdin closed
        print(f"cannot read stdin: {exc}", file=sys.stderr)
        return 1
    try:
        request = parse_request(text)
    except (ValueError, TypeError) as exc:
        _write(sink, fail("invalid_config", f"bad request: {exc}"))
        return 0
    # `fallback` exists for plugins whose method set is configuration rather
    # than code — the `exec` plugin answers whatever the operator wired up.
    if request.method == "plugin.info" and info is not None:
        handler: Handler | None = info
    else:
        handler = handlers.get(request.method, fallback)
    if handler is None:
        _write(
            sink,
            fail(
                "not_found",
                f"unsupported method {request.method!r}; this plugin implements: "
                + ", ".join(sorted(handlers)),
            ),
        )
        return 0
    try:
        reply = handler(request)
    except Exception as exc:
        # Diagnostics to stderr; stdout stays a single JSON object.
        traceback.print_exc(file=sys.stderr)
        _write(sink, fail("internal", f"{type(exc).__name__}: {exc}"))
        return 0
    _write(sink, reply)
    return 0


def _write(sink: TextIO, reply: Reply) -> None:
    sink.write(reply.to_json() + "\n")
