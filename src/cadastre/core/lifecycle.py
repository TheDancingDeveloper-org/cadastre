"""Runtime readiness and lifecycle reporting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class StartupState(StrEnum):
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class Health:
    state: StartupState
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "checks": self.checks}


def failed(error: Exception) -> Health:
    return Health(StartupState.FAILED, {"error": type(error).__name__})


def ready(data_dir: Path, checks: dict[str, Any]) -> Health:
    # Do not expose data_dir: health is safe to return to remote callers.
    return Health(StartupState.READY, {**checks, "runtime_store": "sqlite"})


def degraded(checks: dict[str, Any]) -> Health:
    """Return a live-but-imperfect health state for source failures."""
    return Health(StartupState.DEGRADED, {**checks, "runtime_store": "sqlite"})
