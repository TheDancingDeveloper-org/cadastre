"""Request-scoped dependencies for application services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cadastre.cli.session import Session
from cadastre.core.storage import RuntimeStore


@dataclass
class RuntimeSession:
    """Request-scoped runtime façade used by application services.

    ``view`` retains the deterministic query model while ``store`` exposes the
    validated two-database boundary for health and transactional operations.
    """

    view: Session
    store: RuntimeStore

    def __getattr__(self, name: str) -> object:
        return getattr(self.view, name)


@dataclass(frozen=True)
class ApplicationContext:
    """The only catalog/session dependency an adapter needs to provide."""

    root: Path
    now: datetime
    runtime: bool = True

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        now: datetime | None = None,
        runtime: bool = True,
    ) -> ApplicationContext:
        return cls(root=root, now=now or datetime.now(tz=UTC), runtime=runtime)

    def session(self) -> Session:
        return Session.open(self.root, now=self.now, runtime=self.runtime)

    def service_session(self) -> Session:
        """Return the validated view used by application services."""
        if self.runtime:
            return self.runtime_session().view
        return self.session()

    def runtime_session(self) -> RuntimeSession:
        return RuntimeSession(
            view=self.session(),
            store=RuntimeStore.open(self.root, read_only=True),
        )
