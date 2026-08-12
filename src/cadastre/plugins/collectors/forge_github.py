"""Public forge collector (GitHub API).

The mirror side of the estate, plus Actions workflows as pipelines. Read-only:
repository listing, workflow listing, and self-hosted runner inventory.

The credential here is the one worth replacing first: an admin-scoped PAT
reachable by an agent process is exactly the risk the Broker generalises, and
PLAN's Phase 4 note says to swap it for a short-lived installation token now,
independently of Cadastre.

`ci.status` is an inert observer. It reads runner and runner-group registrations
and returns them under `result.extra`, where nothing interprets them as model.
It cannot register, remove, relabel, reconfigure, or run anything: the only
paths it can build are the four GET templates in `CI_STATUS_PATHS`, and what
GitHub reports about a runner is never allowed to imply a Cadastre host.
"""

from __future__ import annotations

import base64
import binascii
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import Endpoint, HttpError, get_json
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "forge-github"
VERSION = "1"
CAPABILITIES = ("VCS", "CI")

PAGE_SIZE = 100
MAX_PAGES = 10

#: `ci.status` gets its own, much higher ceiling. Ten pages is a fine limit for
#: a repository list that is allowed to be a sample; a runner inventory that
#: silently stopped at a thousand rows would report absent runners as absent.
#: Reaching this ceiling is a failure, never a truncated answer.
CI_STATUS_MAX_PAGES = 50

#: The whole upstream surface `ci.status` may touch. Not a base URL and a path
#: builder — a closed set of GET templates, so no registration-token, JIT
#: configuration, label, group, or workflow mutation path is reachable even by
#: accident. `tests/test_collectors.py` asserts that.
RUNNERS_PATH = "/orgs/{org}/actions/runners"
RUNNER_GROUPS_PATH = "/orgs/{org}/actions/runner-groups"
GROUP_RUNNERS_PATH = "/orgs/{org}/actions/runner-groups/{group_id}/runners"
GROUP_REPOSITORIES_PATH = "/orgs/{org}/actions/runner-groups/{group_id}/repositories"

CI_STATUS_PATHS = (
    RUNNERS_PATH,
    RUNNER_GROUPS_PATH,
    GROUP_RUNNERS_PATH,
    GROUP_REPOSITORIES_PATH,
)

#: The optional Phase 3 surface, reached only when a source explicitly enables
#: it. Same rule as above: a closed set of GET templates, never a builder.
WORKFLOW_CONTENT_PATH = "/repos/{repo}/contents/{path}"
WORKFLOW_RUNS_PATH = "/repos/{repo}/actions/runs"
RUN_JOBS_PATH = "/repos/{repo}/actions/runs/{run_id}/jobs"

CI_PIPELINES_PATHS = (
    WORKFLOW_CONTENT_PATH,
    WORKFLOW_RUNS_PATH,
    RUN_JOBS_PATH,
)

#: The runner APIs are versioned and still moving; pin the version we recorded
#: fixtures against rather than following whatever the default becomes.
API_HEADERS = {"X-GitHub-Api-Version": "2022-11-28"}

#: Workflow-selector bounds. A workflow file is a text file from an upstream
#: nobody here controls, so its size and count are budgets, not surprises.
MAX_WORKFLOW_BYTES = 128 * 1024
MAX_WORKFLOWS_PER_REPO = 25
MAX_SELECTOR_REPOS = 100

#: Job-history bounds. Repository-by-repository run and job enumeration
#: multiplies API calls fast, so every axis is bounded and the caller must name
#: the repositories explicitly.
DEFAULT_JOB_LOOKBACK_HOURS = 24
MAX_JOB_LOOKBACK_HOURS = 24 * 7
DEFAULT_MAX_RUNS = 50
MAX_RUNS_CEILING = 500

#: A GitHub expression. Its value depends on runtime context Cadastre does not
#: have, so a selector containing one is indeterminate — never evaluated, and
#: never guessed at.
_EXPRESSION = "${{"

#: The schema version of the `extra.ci_status` envelope. It is plugin evidence,
#: not core truth, so it carries its own version rather than borrowing the
#: protocol's.
CI_STATUS_SCHEMA = 1

_SLUG = re.compile(r"[^a-z0-9]+")

#: GitHub organisation logins. Validated before interpolation so `config.org`
#: cannot smuggle a path segment past the template allowlist.
_ORG_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")

#: `owner/name`, validated for the same reason: it is interpolated into the
#: Phase 3 path templates.
_REPO_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\Z")

#: Where GitHub runs workflows from, and the only place this will fetch from.
#: The path arrives from an upstream listing, so it is upstream text: without
#: this it could name any path at all and the template allowlist would mean
#: nothing.
_WORKFLOW_PATH = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]{1,120}\Z")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")


def _int(value: Any) -> int | None:
    """A GitHub numeric id, or None. `True` is not an id."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def transform_repos(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    forge = str(options.get("forge") or "forge-public")
    mirror_from = options.get("mirror_from") or "forge-selfhosted"
    repos = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or "")
        if not full_name:
            continue
        entity: dict[str, Any] = {
            "id": full_name.replace("/", "-"),
            "remotes": [
                {
                    "forge": forge,
                    "url": str(item.get("clone_url") or ""),
                    # Public-forge copies in this estate are mirrors by default;
                    # a repo whose origin is here declares that in declared/.
                    "role": "mirror",
                }
            ],
            "mirror_from": str(mirror_from),
        }
        if item.get("archived"):
            entity["tags"] = ["archived"]
        repos.append(entity)
    return {"entities": {"repo": sorted(repos, key=lambda r: str(r["id"]))}}


def transform_workflows(
    payload: Any, repo_id: str, options: dict[str, Any]
) -> dict[str, Any]:
    """Actions workflows -> pipeline entities.

    They are recorded as pipelines of system `ci-public`. Whether one of them
    is authoritative for deployment is not knowable from here — that is the
    `pipeline-authority` question, and it is declared, not inferred.
    """
    system = str(options.get("system") or "ci-public")
    workflows = payload.get("workflows") if isinstance(payload, dict) else payload
    pipelines = []
    for item in workflows or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        pipelines.append(
            {
                "id": f"{repo_id}-{_slug(str(item.get('name') or path))}",
                "repo": repo_id,
                "system": system,
                "file": path,
            }
        )
    return {"entities": {"pipeline": pipelines}}


# --------------------------------------------------------------------------
# Self-hosted runners — evidence, not entities
# --------------------------------------------------------------------------


def _labels(raw: Any) -> list[dict[str, str]]:
    """Runner labels, keeping the automatic/custom distinction.

    Which labels GitHub applied itself and which an operator chose is the
    difference between "this runner reports Linux" and "somebody typed linux",
    so `type` is carried rather than flattened away. A label is a routing
    selector; it is never evidence that a toolchain is installed.
    """
    labels: list[dict[str, str]] = []
    for label in raw or []:
        if isinstance(label, dict):
            name = str(label.get("name") or "")
            kind = str(label.get("type") or "")
        elif isinstance(label, str):
            name, kind = label, ""
        else:
            continue
        if not name:
            continue
        entry = {"name": name}
        if kind:
            entry["type"] = kind
        labels.append(entry)
    return sorted(labels, key=lambda item: (item["name"], item.get("type", "")))


def _runner(item: Any) -> dict[str, Any] | None:
    """One runner registration, allowlisted field by field.

    Everything not named here — avatars, owning users, raw bodies, whatever
    GitHub adds next — is dropped rather than carried. `status` is passed
    through as reported: an unknown value stays unknown rather than being
    mapped onto one we recognise.
    """
    if not isinstance(item, dict):
        return None
    runner_id = _int(item.get("id"))
    if runner_id is None:
        return None
    entry: dict[str, Any] = {
        "id": runner_id,
        # A display name, and only that. Renaming a runner must not look like
        # a new one, so identity is the numeric id above.
        "name": str(item.get("name") or ""),
        "os": str(item.get("os") or ""),
        "status": str(item.get("status") or ""),
        # A snapshot of what GitHub knew at collection time. `busy: false` is
        # not spare capacity, queue depth, or a scheduling guarantee.
        "busy": bool(item.get("busy", False)),
        "ephemeral": bool(item.get("ephemeral", False)),
    }
    version = item.get("version")
    if version not in (None, ""):
        entry["version"] = str(version)
    entry["labels"] = _labels(item.get("labels"))
    entry["group_ids"] = []
    return entry


def _group(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    group_id = _int(item.get("id"))
    if group_id is None:
        return None
    return {
        "id": group_id,
        "name": str(item.get("name") or ""),
        "visibility": str(item.get("visibility") or ""),
        "allows_public_repositories": bool(
            item.get("allows_public_repositories", False)
        ),
    }


def _repositories(raw: Any) -> list[dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        repo_id = _int(item.get("id"))
        if repo_id is not None:
            entry["id"] = repo_id
        full_name = str(item.get("full_name") or "")
        if full_name:
            entry["full_name"] = full_name
        if entry:
            repositories.append(entry)
    return sorted(
        repositories,
        key=lambda item: (int(item.get("id", -1)), str(item.get("full_name", ""))),
    )


def transform_ci_status(
    org: str,
    runners: Any,
    groups: Any,
    group_runners: dict[int, Any] | None = None,
    group_repositories: dict[int, Any] | None = None,
) -> dict[str, Any]:
    """Runner and group registrations -> the `extra.ci_status` envelope.

    No entity, deliberately (PLAN §7.1). There is no vendor-neutral core
    representation of a CI execution target yet, and inventing one from a
    single forge's vocabulary is how `runs-on` ends up in the model. Until real
    policy questions justify a neutral entity, this stays observed evidence
    that core does not branch on.

    Ordering is normalised by numeric id and label name so an upstream that
    shuffles its pages does not look like a change.
    """
    memberships: dict[int, set[int]] = {}
    for group_id, listed in (group_runners or {}).items():
        for item in listed or []:
            runner_id = _int(item.get("id")) if isinstance(item, dict) else None
            if runner_id is not None:
                memberships.setdefault(runner_id, set()).add(group_id)

    collected: list[dict[str, Any]] = []
    for item in runners or []:
        entry = _runner(item)
        if entry is None:
            continue
        group_ids = set(memberships.get(entry["id"], set()))
        hinted = _int(item.get("runner_group_id")) if isinstance(item, dict) else None
        if hinted is not None:
            group_ids.add(hinted)
        entry["group_ids"] = sorted(group_ids)
        collected.append(entry)
    collected.sort(key=lambda item: int(item["id"]))

    listed_groups: list[dict[str, Any]] = []
    for item in groups or []:
        entry = _group(item)
        if entry is None:
            continue
        group_id = int(entry["id"])
        members = {
            runner_id
            for runner_id, group_ids in memberships.items()
            if group_id in group_ids
        }
        members |= {r["id"] for r in collected if group_id in r["group_ids"]}
        entry["runner_ids"] = sorted(members)
        # Only a `selected` group has a repository list. Reporting an empty one
        # for an `all` group would read as "no repository may use it", which is
        # the opposite of what that visibility means.
        if entry["visibility"] == "selected":
            entry["selected_repositories"] = _repositories(
                (group_repositories or {}).get(group_id)
            )
        listed_groups.append(entry)
    listed_groups.sort(key=lambda item: int(item["id"]))

    return {
        "extra": {
            "ci_status": {
                "schema": CI_STATUS_SCHEMA,
                "provider": "github",
                "scope": {"kind": "organization", "name": org},
                # A partial collection fails the method outright, so evidence
                # that reaches here is whole. The marker stays explicit: a
                # reader must never have to assume it.
                "complete": True,
                "runners": collected,
                "runner_groups": listed_groups,
                "counts": {
                    "runners": len(collected),
                    "online": sum(1 for r in collected if r["status"] == "online"),
                    "offline": sum(1 for r in collected if r["status"] == "offline"),
                    "busy": sum(1 for r in collected if r["busy"]),
                    "groups": len(listed_groups),
                },
            }
        }
    }


# --------------------------------------------------------------------------
# Workflow selectors — routing facts only, and never an instruction
# --------------------------------------------------------------------------


def _selector_strings(value: Any) -> list[str]:
    """Every string a `runs-on` value can legitimately contain."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, dict):
        strings: list[str] = []
        group = value.get("group")
        if isinstance(group, str):
            strings.append(group)
        labels = value.get("labels")
        if isinstance(labels, str):
            strings.append(labels)
        elif isinstance(labels, list):
            strings.extend(item for item in labels if isinstance(item, str))
        return strings
    return []


def transform_selector(value: Any) -> dict[str, Any]:
    """One `runs-on` value -> a neutral routing fact.

    Four honest outcomes and no fifth. A job without `runs-on` delegates to a
    called workflow and is `absent`, not unroutable. A value containing a
    GitHub expression is `indeterminate`, because deciding it needs the runtime
    context GitHub has and Cadastre does not — guessing would turn a review
    prompt into a false answer. A shape we do not recognise stays
    `unrecognised` rather than being flattened into one we do.
    """
    if value is None:
        return {"kind": "absent"}
    strings = _selector_strings(value)
    expressions = sorted({item for item in strings if _EXPRESSION in item})
    if expressions:
        return {"kind": "indeterminate", "expressions": expressions}
    if isinstance(value, dict):
        entry: dict[str, Any] = {}
        group = value.get("group")
        entry["kind"] = "group" if isinstance(group, str) and group else "labels"
        if isinstance(group, str) and group:
            entry["group"] = group
        entry["labels"] = sorted(
            item for item in strings if not isinstance(group, str) or item != group
        )
        return entry
    if not strings:
        return {"kind": "unrecognised"}
    return {"kind": "labels", "labels": sorted(strings)}


def transform_workflow_selectors(
    document: Any, repo: str, workflow: str
) -> list[dict[str, Any]]:
    """A workflow document -> one routing fact per job.

    Only `jobs.<id>.runs-on` is read. Steps, scripts, `if` conditions, names,
    and comments are not parsed, not interpreted, and above all not followed:
    a workflow file is upstream text, and upstream text is data.
    """
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return []
    selectors = []
    for job_id, job in jobs.items():
        if not isinstance(job_id, str):
            continue
        selectors.append(
            {
                "repo": repo,
                "workflow": workflow,
                "job": job_id,
                "selector": transform_selector(
                    job.get("runs-on") if isinstance(job, dict) else None
                ),
            }
        )
    return sorted(selectors, key=lambda item: (str(item["job"]),))


def transform_jobs(payload: Any, repo: str) -> list[dict[str, Any]]:
    """Workflow jobs -> which executor actually ran them.

    Allowlisted to the routing and identity facts. Logs, step output,
    annotations, environment, and artifacts are not collected: this evidence
    can show that a job used a runner, and cannot show that the machine was
    clean or that the runner is where anyone thinks it is.
    """
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    collected = []
    for item in jobs or []:
        if not isinstance(item, dict):
            continue
        job_id = _int(item.get("id"))
        if job_id is None:
            continue
        entry: dict[str, Any] = {
            "repo": repo,
            "job_id": job_id,
            "run_id": _int(item.get("run_id")),
            "run_attempt": _int(item.get("run_attempt")),
            # No `workflow_name` or job `name`: upstream display text is not in
            # the retained set, and ids are what identity is built from.
            "status": str(item.get("status") or ""),
            "conclusion": str(item.get("conclusion") or ""),
            "started_at": str(item.get("started_at") or ""),
            "completed_at": str(item.get("completed_at") or ""),
            "runner_name": str(item.get("runner_name") or ""),
            "runner_group_name": str(item.get("runner_group_name") or ""),
            "labels": sorted(
                label for label in (item.get("labels") or []) if isinstance(label, str)
            ),
        }
        collected.append(entry)
    return sorted(collected, key=lambda item: int(item["job_id"]))


# --------------------------------------------------------------------------
# The neutral view: the same registrations as core entities
# --------------------------------------------------------------------------

#: Statuses core models. Anything else stays `unknown` rather than being mapped
#: onto one of these — the raw value survives in `extra.ci_status`.
_CORE_STATUS = ("online", "offline")


def _executor_id(org: str, runner_id: int) -> str:
    """Identity from GitHub's stable numeric id, never from the display name.

    A renamed runner has to remain the same executor, or every rename would
    read as one registration disappearing and another arriving.
    """
    return f"{_slug(org)}-executor-{runner_id}"


def _pool_id(org: str, group_id: int) -> str:
    return f"{_slug(org)}-pool-{group_id}"


def transform_execution_targets(
    evidence: dict[str, Any], options: dict[str, Any]
) -> dict[str, Any]:
    """The `ci_status` envelope -> `ci_executor` and `ci_pool` entities.

    The neutral half of the same observation. Vendor nouns stop here: what
    leaves is an executor and a pool, not a runner and a runner group.

    Two fields are deliberately never set. `runs_on` is catalog intent — a
    registration's name, OS, labels, or IP cannot establish which host it is —
    and `capabilities` is too: a label routes a job, it does not install a
    toolchain. Both are declared in the catalog and compared here by drift.
    """
    system = str(options.get("system") or "ci-public")
    scope = evidence.get("scope") if isinstance(evidence, dict) else None
    org = str((scope or {}).get("name") or "")
    scope_label = f"organization:{org}" if org else ""

    executors = []
    for runner in evidence.get("runners") or []:
        runner_id = _int(runner.get("id"))
        if runner_id is None:
            continue
        status = str(runner.get("status") or "")
        entity: dict[str, Any] = {
            "id": _executor_id(org, runner_id),
            "system": system,
            "scope": scope_label,
            "status": status if status in _CORE_STATUS else "unknown",
            "busy": bool(runner.get("busy", False)),
            "ephemeral": bool(runner.get("ephemeral", False)),
            "selectors": sorted(
                label["name"]
                for label in runner.get("labels") or []
                if label.get("name")
            ),
        }
        if runner.get("os"):
            entity["os"] = str(runner["os"])
        if runner.get("version"):
            entity["version"] = str(runner["version"])
        group_ids = [gid for gid in runner.get("group_ids") or [] if _int(gid)]
        if group_ids:
            entity["pool"] = _pool_id(org, int(sorted(group_ids)[0]))
        executors.append(entity)

    pools = []
    for group in evidence.get("runner_groups") or []:
        group_id = _int(group.get("id"))
        if group_id is None:
            continue
        entity = {
            "id": _pool_id(org, group_id),
            "system": system,
            "scope": scope_label,
            "visibility": str(group.get("visibility") or "private"),
            "public_repositories": bool(group.get("allows_public_repositories", False)),
        }
        repositories = [
            str(item.get("full_name") or "").replace("/", "-")
            for item in group.get("selected_repositories") or []
            if item.get("full_name")
        ]
        if repositories:
            entity["repositories"] = sorted(repositories)
        pools.append(entity)

    return {
        "entities": {
            "ci_executor": sorted(executors, key=lambda item: str(item["id"])),
            "ci_pool": sorted(pools, key=lambda item: str(item["id"])),
        }
    }


def _paged(endpoint: Endpoint, path: str) -> list[Any]:
    out: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        items = get_json(endpoint, path, {"per_page": PAGE_SIZE, "page": page})
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        if len(items) < PAGE_SIZE:
            break
    return out


def _paged_items(endpoint: Endpoint, path: str, key: str) -> list[Any]:
    """Every page of a `{"total_count": N, "<key>": [...]}` list.

    The repository paginator above stops at ten pages and returns what it has.
    That is wrong for an inventory: unobserved records past the limit would be
    indistinguishable from absent ones, and absence is a claim Cadastre makes
    carefully. So this one either sees a short page — the end — or refuses.
    """
    items: list[Any] = []
    for page in range(1, CI_STATUS_MAX_PAGES + 1):
        payload = get_json(
            endpoint,
            path,
            {"per_page": PAGE_SIZE, "page": page},
            headers=API_HEADERS,
        )
        listed = payload.get(key) if isinstance(payload, dict) else payload
        if not isinstance(listed, list):
            raise HttpError("internal", f"{path}: no {key!r} list in the response")
        items.extend(listed)
        if len(listed) < PAGE_SIZE:
            return items
    raise HttpError(
        "internal",
        f"{path}: more than {CI_STATUS_MAX_PAGES} pages of {key}; refusing to "
        "publish a truncated inventory as if it were complete",
    )


def _require_org(config: dict[str, Any]) -> str:
    """The organisation whose runners to read.

    User scope is refused rather than approximated: runner groups — and with
    them the access boundary that makes this evidence worth having — only
    exist at organisation scope.
    """
    org = str(config.get("org") or "")
    if not org:
        if config.get("user"):
            raise HttpError(
                "invalid_config",
                "ci.status is organization-scoped: runner groups do not exist "
                "for a user account. Set config.org.",
            )
        raise HttpError("invalid_config", "config.org is required for ci.status")
    if not _ORG_NAME.match(org):
        raise HttpError("invalid_config", f"config.org is not a GitHub login: {org!r}")
    return org


def _ci_status(request: Request) -> Reply:
    """Self-hosted runner and runner-group inventory for one organisation.

    One organisation per source, never merged: separate provenance and separate
    authorisation scope are the operationally significant part.

    Any failure fails the whole method. A half-collected organisation published
    as a new snapshot would make missing group membership look authoritative;
    failing instead keeps the previous evidence and marks it stale, which is
    the honest reading.
    """
    org = _require_org(request.config)
    endpoint = Endpoint.from_config(request.config)
    runners = _paged_items(endpoint, RUNNERS_PATH.format(org=org), "runners")
    groups = _paged_items(endpoint, RUNNER_GROUPS_PATH.format(org=org), "runner_groups")
    group_runners: dict[int, Any] = {}
    group_repositories: dict[int, Any] = {}
    for group in groups:
        group_id = _int(group.get("id")) if isinstance(group, dict) else None
        if group_id is None:
            continue
        group_runners[group_id] = _paged_items(
            endpoint,
            GROUP_RUNNERS_PATH.format(org=org, group_id=group_id),
            "runners",
        )
        # The group response carries visibility itself; the repository list is
        # only fetched when `selected` means there is one to complete.
        if str(group.get("visibility") or "") == "selected":
            group_repositories[group_id] = _paged_items(
                endpoint,
                GROUP_REPOSITORIES_PATH.format(org=org, group_id=group_id),
                "repositories",
            )
    evidence = transform_ci_status(
        org, runners, groups, group_runners, group_repositories
    )
    # Both halves of one observation: the neutral entities policy may read, and
    # the vendor evidence it must not.
    result = transform_execution_targets(evidence["extra"]["ci_status"], request.config)
    result["extra"] = evidence["extra"]
    return ok(result, format_timestamp(datetime.now(tz=UTC)))


def _repo_path(config: dict[str, Any]) -> str:
    if config.get("org"):
        return f"/orgs/{config['org']}/repos"
    if config.get("user"):
        return f"/users/{config['user']}/repos"
    raise HttpError("invalid_config", "config.org or config.user is required")


def _repos(request: Request) -> Reply:
    endpoint = Endpoint.from_config(request.config)
    items = _paged(endpoint, _repo_path(request.config))
    return ok(
        transform_repos(items, request.config),
        format_timestamp(datetime.now(tz=UTC)),
    )


def _workflow_document(endpoint: Endpoint, repo: str, path: str) -> Any:
    """Fetch and parse one workflow file, or say why not.

    Returns `None` when the file is too large or does not parse. Both are
    reported as `unparsed` rather than as "this workflow selects nothing" —
    the second is a fact, and it is not one we established.
    """
    payload = get_json(
        endpoint,
        WORKFLOW_CONTENT_PATH.format(repo=repo, path=path),
        headers=API_HEADERS,
    )
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    size = _int(payload.get("size"))
    if size is not None and size > MAX_WORKFLOW_BYTES:
        return None
    try:
        raw = base64.b64decode(str(payload.get("content") or ""), validate=False)
    except (ValueError, binascii.Error):
        return None
    if len(raw) > MAX_WORKFLOW_BYTES:
        return None
    try:
        return yaml.safe_load(raw.decode("utf-8", errors="replace"))
    except yaml.YAMLError:
        return None


def _selectors(
    endpoint: Endpoint, repos: list[str], workflows: dict[str, list[str]]
) -> dict[str, Any]:
    """Static routing facts for every workflow in the listed repositories."""
    selectors: list[dict[str, Any]] = []
    complete = True
    reasons: list[str] = []
    if len(repos) > MAX_SELECTOR_REPOS:
        complete = False
        reasons.append(f"more than {MAX_SELECTOR_REPOS} repositories")
        repos = repos[:MAX_SELECTOR_REPOS]
    for repo in repos:
        if not _REPO_NAME.match(repo):
            complete = False
            reasons.append("a repository name was not owner/name and was skipped")
            continue
        paths = workflows.get(repo, [])
        if len(paths) > MAX_WORKFLOWS_PER_REPO:
            complete = False
            reasons.append(f"{repo}: more than {MAX_WORKFLOWS_PER_REPO} workflows")
            paths = paths[:MAX_WORKFLOWS_PER_REPO]
        for path in paths:
            if not _WORKFLOW_PATH.match(path):
                complete = False
                reasons.append(f"{repo}: a workflow path outside .github/workflows/")
                continue
            document = _workflow_document(endpoint, repo, path)
            if document is None:
                complete = False
                reasons.append(f"{repo}: {path} was too large or did not parse")
                selectors.append(
                    {
                        "repo": repo,
                        "workflow": path,
                        "job": None,
                        "selector": {"kind": "unparsed"},
                    }
                )
                continue
            selectors.extend(transform_workflow_selectors(document, repo, path))
    ordered = sorted(
        selectors,
        key=lambda item: (
            str(item["repo"]),
            str(item["workflow"]),
            str(item["job"] or ""),
        ),
    )
    kinds: dict[str, int] = {}
    for item in ordered:
        kind = str(item["selector"]["kind"])
        kinds[kind] = kinds.get(kind, 0) + 1
    evidence: dict[str, Any] = {
        "schema": CI_STATUS_SCHEMA,
        "provider": "github",
        "complete": complete,
        "selectors": ordered,
        "counts": {
            "jobs": len(ordered),
            **{f"kind_{k}": v for k, v in sorted(kinds.items())},
        },
    }
    if reasons:
        evidence["incomplete_reasons"] = sorted(set(reasons))
    return evidence


def _job_history(
    endpoint: Endpoint, config: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Which executor ran which job, bounded on every axis.

    The repository allowlist is required rather than defaulted: enumerating an
    organisation's runs is a large amount of upstream traffic to start doing
    because a boolean was set.
    """
    settings = config.get("job_history")
    if not isinstance(settings, dict):
        raise HttpError("invalid_config", "config.job_history must be an object")
    repositories = settings.get("repositories")
    if not isinstance(repositories, list) or not all(
        isinstance(item, str) and _REPO_NAME.match(item) for item in repositories
    ):
        raise HttpError(
            "invalid_config",
            "config.job_history.repositories must be a non-empty list of "
            "owner/name repositories: job history is opt-in per repository, "
            "never organisation-wide by default",
        )
    if not repositories:
        raise HttpError(
            "invalid_config", "config.job_history.repositories must not be empty"
        )
    lookback = int(settings.get("lookback_hours", DEFAULT_JOB_LOOKBACK_HOURS))
    max_runs = int(settings.get("max_runs", DEFAULT_MAX_RUNS))
    if lookback < 1 or lookback > MAX_JOB_LOOKBACK_HOURS:
        raise HttpError(
            "invalid_config",
            f"config.job_history.lookback_hours must be 1..{MAX_JOB_LOOKBACK_HOURS}",
        )
    if max_runs < 1 or max_runs > MAX_RUNS_CEILING:
        raise HttpError(
            "invalid_config",
            f"config.job_history.max_runs must be 1..{MAX_RUNS_CEILING}",
        )

    since = format_timestamp(now - timedelta(hours=lookback))
    jobs: list[dict[str, Any]] = []
    complete = True
    reasons: list[str] = []
    budget = max_runs
    for repo in sorted(set(repositories)):
        if budget <= 0:
            complete = False
            reasons.append(f"run budget of {max_runs} was exhausted before {repo}")
            continue
        payload = get_json(
            endpoint,
            WORKFLOW_RUNS_PATH.format(repo=repo),
            {"per_page": min(PAGE_SIZE, budget), "created": f">={since}"},
            headers=API_HEADERS,
        )
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise HttpError(
                "internal", f"{WORKFLOW_RUNS_PATH.format(repo=repo)}: no run list"
            )
        total = _int(payload.get("total_count")) if isinstance(payload, dict) else None
        if total is not None and total > len(runs):
            complete = False
            reasons.append(f"{repo}: {total} runs in window, {len(runs)} examined")
        for run in runs[:budget]:
            run_id = _int(run.get("id")) if isinstance(run, dict) else None
            if run_id is None:
                continue
            budget -= 1
            jobs.extend(
                transform_jobs(
                    get_json(
                        endpoint,
                        RUN_JOBS_PATH.format(repo=repo, run_id=run_id),
                        {"per_page": PAGE_SIZE},
                        headers=API_HEADERS,
                    ),
                    repo,
                )
            )
    evidence: dict[str, Any] = {
        "schema": CI_STATUS_SCHEMA,
        "provider": "github",
        "complete": complete,
        "since": since,
        "lookback_hours": lookback,
        "repositories": sorted(set(repositories)),
        "jobs": sorted(jobs, key=lambda item: (str(item["repo"]), int(item["job_id"]))),
        "counts": {"jobs": len(jobs)},
    }
    if reasons:
        evidence["incomplete_reasons"] = sorted(set(reasons))
    return evidence


def _pipelines(request: Request) -> Reply:
    """Workflow definitions, and optionally where they say they will run.

    The two optional sections are separately configured and off by default.
    They need repository permissions the runner-inventory source does not have
    (`Contents: read` for selectors, `Actions: read` for job history), which is
    why they belong to this source and not to `ci.status`.
    """
    endpoint = Endpoint.from_config(request.config)
    items = _paged(endpoint, _repo_path(request.config))
    pipelines: list[Any] = []
    workflow_paths: dict[str, list[str]] = {}
    repos: list[str] = []
    for item in items:
        full_name = str(item.get("full_name") or "")
        if not full_name:
            continue
        repos.append(full_name)
        payload = get_json(endpoint, f"/repos/{full_name}/actions/workflows")
        transformed = transform_workflows(
            payload, full_name.replace("/", "-"), request.config
        )
        pipelines.extend(transformed["entities"]["pipeline"])
        listed = payload.get("workflows") if isinstance(payload, dict) else payload
        workflow_paths[full_name] = [
            str(entry.get("path") or "")
            for entry in (listed or [])
            if isinstance(entry, dict) and entry.get("path")
        ]

    result: dict[str, Any] = {"entities": {"pipeline": pipelines}}
    extra: dict[str, Any] = {}
    if bool(request.config.get("workflow_selectors", False)):
        extra["ci_selectors"] = _selectors(endpoint, repos, workflow_paths)
    if request.config.get("job_history") is not None:
        extra["ci_job_history"] = _job_history(
            endpoint, request.config, datetime.now(tz=UTC)
        )
    if extra:
        result["extra"] = extra
    return ok(result, format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={
            "vcs.repos": _repos,
            "ci.pipelines": _pipelines,
            "ci.status": _ci_status,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
