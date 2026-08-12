"""Bounded, read-only Markdown work-finding collector."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "work-markdown"
VERSION = "1"
CAPABILITIES = ("Work",)

_TASK = re.compile(r"^(?P<indent>\s*)[-*+] \[(?P<checked>[ xX])\] (?P<text>.+?)\s*$")
_MARKER = re.compile(r"<!--\s*cadastre-work\s+((?:item|forge)=[^\s>]+\s*)+-->")
_ATTRIBUTE = re.compile(r"(?P<key>item|forge)=(?P<value>[^\s>]+)")


def _marker(text: str) -> dict[str, str]:
    match = _MARKER.search(text)
    if not match:
        return {}
    return {
        item.group("key"): item.group("value")
        for item in _ATTRIBUTE.finditer(match.group(0))
    }


def scan_file(path: Path, *, repo: str, max_file_bytes: int) -> list[dict[str, Any]]:
    """Extract task-list findings without retaining file bodies."""
    raw = path.read_bytes()
    if len(raw) > max_file_bytes:
        raise ValueError(f"file exceeds max_file_bytes: {path}")
    text = raw.decode("utf-8")
    findings: list[dict[str, Any]] = []
    heading = ""
    in_fence = False
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        task = _TASK.match(line)
        if not task:
            continue
        marker = _marker(line)
        clean = re.sub(_MARKER, "", task.group("text")).strip()
        finding: dict[str, Any] = {
            "id": marker.get("item")
            or f"{repo}:{path.as_posix()}:{line_number}:{clean}",
            "repo": repo,
            "path": path.as_posix(),
            "line": line_number,
            "text": clean,
            "checked": task.group("checked").lower() == "x",
        }
        if heading:
            finding["heading"] = heading
        if marker.get("item"):
            finding["work_item"] = marker["item"]
        if marker.get("forge"):
            finding["forge_ref"] = marker["forge"]
        findings.append(finding)
    return findings


def transform(config: dict[str, Any]) -> dict[str, Any]:
    repo = str(config.get("repo") or "")
    root = Path(str(config.get("root") or ".")).resolve()
    files = config.get("files") or []
    max_files = int(config.get("max_files", 100))
    max_file_bytes = int(config.get("max_file_bytes", 256 * 1024))
    max_total_bytes = int(config.get("max_total_bytes", 4 * 1024 * 1024))
    if not repo or not isinstance(files, list) or not files:
        raise ValueError("config.repo and a non-empty config.files list are required")
    if len(files) > max_files:
        raise ValueError("configured Markdown file list exceeds max_files")
    findings: list[dict[str, Any]] = []
    total = 0
    for relative in files:
        # Check the un-resolved path for a symlink first: `.resolve()` follows
        # symlinks, so testing `is_symlink()` on its result can never be true
        # and would silently let a symlinked file through.
        raw = root / str(relative)
        if raw.is_symlink():
            raise ValueError(f"configured file is a symlink: {relative}")
        candidate = raw.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"configured file escapes root: {relative}")
        size = candidate.stat().st_size
        total += size
        if total > max_total_bytes:
            raise ValueError("configured Markdown files exceed max_total_bytes")
        findings.extend(scan_file(candidate, repo=repo, max_file_bytes=max_file_bytes))
    return {"entities": {"markdown_finding": findings}}


def _collect(request: Request) -> Reply:
    return ok(transform(request.config), format_timestamp(datetime.now(tz=UTC)))


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={"work.findings": _collect},
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
