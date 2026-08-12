"""Health and version use cases shared by service adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cadastre import __version__
from cadastre.compatibility import compatibility
from cadastre.core.storage import startup_check

# Advertised to clients so a bridge can tell whether it is merely old or
# actually unsupported. Additive only: `name` and `version` are the contract.
_ADVERTISED = (
    "application_version",
    "catalog_format_version",
    "observed_format_version",
    "minimum_client_version",
    "release_url",
)


@dataclass(frozen=True)
class HealthService:
    root: Path

    def live(self) -> dict[str, Any]:
        return {"status": "ok", "liveness": True, "version": __version__}

    def ready(self) -> dict[str, Any]:
        from cadastre.modules.config import SUPPORTED_MODULES, load_modules

        modules = load_modules(self.root)
        return {
            "status": "ok",
            "version": __version__,
            # Additive capability flags a GUI or bridge can use to show/hide
            # module-specific navigation without duplicating activation logic
            # (MANIFEST.md R09). Never grown into an authorization surface —
            # routes and CLI subcommands independently enforce activation.
            "modules": {
                name: modules.enabled(name) for name in sorted(SUPPORTED_MODULES)
            },
            **startup_check(self.root),
        }

    def version(self) -> dict[str, Any]:
        document = compatibility()
        return {
            "name": "cadastre",
            "version": __version__,
            **{key: document[key] for key in _ADVERTISED if key in document},
        }
