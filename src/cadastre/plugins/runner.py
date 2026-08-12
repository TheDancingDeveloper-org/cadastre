"""Running an out-of-process plugin.

Everything that can go wrong with an external process is caught and turned into
a *stale source*, never a crash and never a silent success: bad JSON, non-zero
exit, timeout, stdout pollution, a well-formed `ok:false`. A collector that
cannot reach its plugin must be visible, not absent (DESIGN §2.2).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any

from cadastre.plugins.config import SourceConfig
from cadastre.plugins.protocol import (
    WRITE_METHODS,
    PluginError,
    Reply,
    Request,
    parse_reply,
)

#: Passed through to every plugin. Anything else must be named in the
#: source's `env` list, so what a plugin can read is declared in git.
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE")

STDERR_TAIL = 400


@dataclass(frozen=True)
class Outcome:
    """What one plugin call produced. `reply` is set only on a clean answer."""

    source: str
    method: str
    reply: Reply | None
    error: PluginError | None = None

    @property
    def ok(self) -> bool:
        return self.reply is not None and self.reply.ok

    @property
    def message(self) -> str | None:
        if self.error is None:
            return None
        return f"{self.error.kind}: {self.error.message}".strip().rstrip(":")


def build_env(
    source: SourceConfig, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """The plugin's environment: a small base, plus exactly what was declared.

    Credentials reach a plugin this way and no other. They are never in argv
    — argv is visible in `ps`, in shell history, and in agent transcripts.
    """
    base = dict(environ if environ is not None else os.environ)
    env = {key: base[key] for key in _BASE_ENV_KEYS if key in base}
    for name in source.env:
        if name in base:
            env[name] = base[name]
    # A token_env naming convention means the config can say which variable
    # carries the credential without the operator listing it twice.
    for key, value in source.config.items():
        if key.endswith("_env") and isinstance(value, str) and value in base:
            env[value] = base[value]
    return env


def _tail(text: str) -> str:
    flat = " ".join(text.split())
    return flat[-STDERR_TAIL:] if len(flat) > STDERR_TAIL else flat


def call(
    source: SourceConfig,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> Outcome:
    """Invoke one method on one plugin. Never raises for plugin misbehaviour."""
    if method in WRITE_METHODS:
        return Outcome(
            source.id,
            method,
            None,
            PluginError(
                "invalid_config",
                f"{method} is a write method; Cadastre has no write path (DESIGN §1.3)",
            ),
        )
    request = Request(
        method=method,
        params={**source.params, **(params or {})},
        config=source.config,
    )
    try:
        completed = subprocess.run(
            list(source.command),
            input=request.to_json(),
            capture_output=True,
            text=True,
            timeout=source.timeout_seconds,
            env=build_env(source, environ),
            check=False,
        )
    except FileNotFoundError:
        return Outcome(
            source.id,
            method,
            None,
            PluginError("invalid_config", f"no such command: {source.command[0]}"),
        )
    except subprocess.TimeoutExpired:
        return Outcome(
            source.id,
            method,
            None,
            PluginError(
                "unreachable",
                f"timed out after {source.timeout_seconds}s",
                retryable=True,
            ),
        )
    except OSError as exc:
        return Outcome(source.id, method, None, PluginError("internal", str(exc)))

    if completed.returncode != 0:
        return Outcome(
            source.id,
            method,
            None,
            PluginError(
                "internal",
                f"exit {completed.returncode}: {_tail(completed.stderr)}",
            ),
        )
    try:
        reply = parse_reply(completed.stdout)
    except ValueError as exc:
        return Outcome(source.id, method, None, PluginError("internal", str(exc)))
    if not reply.ok:
        return Outcome(source.id, method, reply, reply.error)
    return Outcome(source.id, method, reply)


def info(source: SourceConfig, *, environ: dict[str, str] | None = None) -> Outcome:
    """The `plugin.info` handshake and declaration validation."""
    outcome = call(source, "plugin.info", environ=environ)
    if outcome.ok and outcome.reply is not None:
        from cadastre.plugins.contract import parse_plugin_info

        try:
            parse_plugin_info(outcome.reply.result)
        except ValueError as exc:
            return Outcome(
                source.id,
                "plugin.info",
                None,
                PluginError("internal", f"invalid plugin.info: {exc}"),
            )
    return outcome
