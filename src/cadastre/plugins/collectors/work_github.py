"""Bounded GitHub issue and pull-request evidence collector."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "work-github"
VERSION = "1"
CAPABILITIES = ("Work",)
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 10
_REPO = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z"
)


def _identity(forge: str, repo: str, kind: str, ref: str) -> str:
    return f"{forge}:{repo}:{kind}:{ref}"


def transform_item(
    item: dict[str, Any], *, forge: str, repo: str, kind: str
) -> dict[str, Any] | None:
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    pull = kind == "pull_request"
    merged = item.get("merged_at") if pull else None
    state = "merged" if merged else str(item.get("state") or "unknown")
    result: dict[str, Any] = {
        "id": _identity(forge, repo, kind, str(number)),
        "forge": forge,
        "repo": repo,
        "kind": kind,
        "ref": str(number),
        "title": str(item.get("title") or ""),
        "state": state,
        "draft": bool(item.get("draft", False)) if pull else False,
    }
    for key in ("created_at", "updated_at"):
        if isinstance(item.get(key), str):
            result[key] = item[key]
    if isinstance(item.get("html_url"), str):
        result["url"] = item["html_url"]
    head = item.get("head")
    base = item.get("merge_commit_sha")
    if isinstance(head, dict) and isinstance(head.get("sha"), str):
        result["head_revision"] = head["sha"]
    if isinstance(base, str) and base:
        result["merge_revision"] = base
    return result


def transform_pages(
    pages: list[Any], *, forge: str, repo: str, kind: str
) -> list[dict[str, Any]]:
    result = []
    for item in pages:
        if isinstance(item, dict):
            if kind == "issue" and isinstance(item.get("pull_request"), dict):
                continue
            transformed = transform_item(item, forge=forge, repo=repo, kind=kind)
            if transformed is not None:
                result.append(transformed)
    return sorted(result, key=lambda item: (item["repo"], item["kind"], item["ref"]))


def _paged(endpoint: Endpoint, path: str, *, max_pages: int) -> list[Any]:
    result: list[Any] = []
    for page in range(1, max_pages + 1):
        payload = get_json(
            endpoint,
            path,
            {"state": "all", "per_page": PAGE_SIZE, "page": page},
        )
        if not isinstance(payload, list):
            raise HttpError("internal", f"{path}: expected a list")
        result.extend(payload)
        if len(payload) < PAGE_SIZE:
            return result
    raise HttpError(
        "internal",
        f"{path}: exceeded max_pages={max_pages}; refusing incomplete evidence",
    )


def transform_config(endpoint: Endpoint, config: dict[str, Any]) -> dict[str, Any]:
    forge = str(config.get("forge") or "github")
    repos = config.get("repos")
    max_pages = int(config.get("max_pages", DEFAULT_MAX_PAGES))
    max_repos = int(config.get("max_repos", 100))
    if not isinstance(repos, list) or not repos:
        raise HttpError("invalid_config", "config.repos must be a non-empty list")
    if len(repos) > max_repos:
        raise HttpError("invalid_config", "config.repos exceeds max_repos")
    items: list[dict[str, Any]] = []
    for value in repos:
        repo = str(value)
        if not _REPO.fullmatch(repo):
            raise HttpError("invalid_config", f"invalid GitHub repository: {repo!r}")
        issues = _paged(endpoint, f"/repos/{repo}/issues", max_pages=max_pages)
        pulls = _paged(endpoint, f"/repos/{repo}/pulls", max_pages=max_pages)
        items.extend(transform_pages(issues, forge=forge, repo=repo, kind="issue"))
        items.extend(
            transform_pages(pulls, forge=forge, repo=repo, kind="pull_request")
        )
    return {"entities": {"forge_item": sorted(items, key=lambda item: item["id"])}}


def _collect(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config)
    return ok(
        transform_config(endpoint, request.config),
        format_timestamp(datetime.now(tz=UTC)),
    )


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={"work.items": _collect},
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
