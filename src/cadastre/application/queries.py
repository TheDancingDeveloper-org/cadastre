"""Canonical query use cases.

Adapters translate inputs and render the returned Document; they do not select
commands or open a second catalog themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cadastre.application.context import ApplicationContext
from cadastre.cli import brief, context_for, drift, lookup, question, trust
from cadastre.cli.session import Session
from cadastre.render.document import Document


@dataclass(frozen=True)
class QueryService:
    context: ApplicationContext

    def _session(self) -> Session:
        return self.context.service_session()

    def brief(self) -> Document:
        return brief.brief(self._session())

    def context_for(self, intent: str) -> Document:
        return context_for.context_for(self._session(), intent)

    def question(
        self,
        question_id: str,
        *,
        subject: str | None = None,
        value: str | None = None,
    ) -> Document:
        return question.question(
            self._session(), question_id, subject=subject, value=value
        )

    def lookup(self, entity_id: str, *, kind: str | None = None) -> Document:
        return lookup.lookup(self._session(), entity_id, kind=kind)

    def search(
        self, query: str, *, kind: str | None = None, limit: int | None = None
    ) -> Document:
        from cadastre.cli import search

        return search.search(self._session(), query, kind=kind, limit=limit)

    def lookup_or_search(
        self,
        entity_id: str | None,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int | None = None,
    ) -> Document:
        """`lookup` addressed by id, or searched by words — whichever was given.

        An id wins when both are present: it is the more specific request.
        """
        if entity_id:
            return self.lookup(entity_id, kind=kind)
        if query:
            return self.search(query, kind=kind, limit=limit)
        from cadastre.core.errors import UsageError

        raise UsageError(
            "lookup needs `entity_id` (an exact id, e.g. 'node-b') or `query` "
            "(free text, e.g. 'komodo api key')"
        )

    def credential_for(self, service: str, action: str | None = None) -> Document:
        from cadastre.cli import search

        return search.credential_for(self._session(), service, action)

    def dns_chain(self, hostname: str) -> Document:
        from cadastre.cli import dns_chain

        return dns_chain.dns_chain(self._session(), hostname)

    def secret_describe(self, ref: str) -> Document:
        from cadastre.cli import secret_describe

        return secret_describe.secret_describe(self._session(), ref)

    def drift(self, **filters: Any) -> Document:
        return drift.drift(self._session(), **filters)

    def stale(self) -> Document:
        return trust.stale(self._session())

    def sources(self) -> Document:
        from cadastre.cli import sources

        return sources.sources(self._session())

    def observations(
        self,
        *,
        source: str | None = None,
        method: str | None = None,
        key: str | None = None,
        limit: int | None = None,
        summary_only: bool = False,
    ) -> Document:
        from cadastre.cli import observations

        return observations.observations(
            self._session(),
            source=source,
            method=method,
            key=key,
            limit=limit,
            summary_only=summary_only,
        )

    def plugins(self) -> Document:
        from cadastre.cli import plugins

        return plugins.plugins(self._session())

    def version(self) -> Document:
        from cadastre.application.health import HealthService

        return Document(
            title="cadastre version", data=HealthService(self.context.root).version()
        )

    def manifest_brief(self) -> Document:
        from cadastre.cli import manifest

        return manifest.brief(self._session())

    def manifest_projects(self) -> Document:
        from cadastre.cli import manifest

        return manifest.projects(self._session())

    def manifest_backlog(self, **filters: Any) -> Document:
        from cadastre.cli import manifest

        return manifest.backlog(self._session(), **filters)

    def manifest_next(self, *, limit: int = 10) -> Document:
        from cadastre.cli import manifest

        return manifest.next_(self._session(), limit=limit)

    def manifest_drift(self, *, repo: str | None = None) -> Document:
        from cadastre.cli import manifest

        return manifest.drift(self._session(), repo=repo)

    def manifest_repo(self, repo: str) -> Document:
        from cadastre.cli import manifest

        return manifest.repo(self._session(), repo)

    def manifest_why(self, entity_id: str) -> Document:
        from cadastre.cli import manifest

        return manifest.why(self._session(), entity_id)

    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> Document:
        """Dispatch a registry operation with one canonical service boundary."""
        values = arguments or {}
        methods = {
            "brief": lambda: self.brief(),
            "context_for": lambda: self.context_for(str(values.get("intent", ""))),
            "question": lambda: self.question(
                str(values.get("question_id", "")),
                subject=values.get("subject"),
                value=values.get("value"),
            ),
            "lookup": lambda: self.lookup_or_search(
                values.get("entity_id"),
                kind=values.get("kind"),
                query=values.get("query"),
                limit=values.get("limit"),
            ),
            "search": lambda: self.search(
                str(values.get("query") or ""),
                kind=values.get("kind"),
                limit=values.get("limit"),
            ),
            "credential_for": lambda: self.credential_for(
                str(values.get("service") or ""), values.get("action")
            ),
            "dns_chain": lambda: self.dns_chain(str(values.get("hostname") or "")),
            "secret_describe": lambda: self.secret_describe(
                str(values.get("ref") or "")
            ),
            "drift": lambda: self.drift(
                **{
                    key: values[key]
                    for key in (
                        "category",
                        "kind",
                        "source",
                        "entity_id",
                        "limit",
                        "cursor",
                        "summary_only",
                    )
                    if key in values
                }
            ),
            "stale": lambda: self.stale(),
            "sources": lambda: self.sources(),
            "plugins": lambda: self.plugins(),
            "version": lambda: self.version(),
            "observations": lambda: self.observations(
                source=values.get("source"),
                method=values.get("method"),
                key=values.get("key"),
                limit=values.get("limit"),
                summary_only=bool(values.get("summary_only", False)),
            ),
            "manifest_brief": lambda: self.manifest_brief(),
            "manifest_projects": lambda: self.manifest_projects(),
            "manifest_backlog": lambda: self.manifest_backlog(
                state=values.get("state"),
                initiative=values.get("initiative"),
                repo=values.get("repo"),
                limit=int(values.get("limit", 10)),
            ),
            "manifest_next": lambda: self.manifest_next(
                limit=int(values.get("limit", 10))
            ),
            "manifest_drift": lambda: self.manifest_drift(repo=values.get("repo")),
            "manifest_repo": lambda: self.manifest_repo(str(values.get("repo", ""))),
            "manifest_why": lambda: self.manifest_why(str(values.get("entity_id", ""))),
        }
        try:
            return methods[name]()
        except KeyError as exc:
            from cadastre.core.errors import UsageError

            raise UsageError(f"unknown query operation `{name}`") from exc
