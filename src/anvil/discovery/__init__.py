"""Deterministic, scope-aware source discovery (Milestone 1).

No LLM. A simple first implementation per the roadmap: a file inventory scoped
to the task scope's ``discovery_focus_paths`` (falling back to ``root_paths``),
a test-file finder, an optional ripgrep keyword pass when ``rg`` is available,
and a git-history summary. Produces a source_manifest dict that validates
against source_manifest.schema (code/test sources carry the worktree's commit
SHA; doc sources carry last_modified).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import TaskScope
from ..errors import ValidationError
from ..timeutil import now_iso

# Extension -> source_type mapping for the schema's source_type enum.
_CODE_EXT = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h"}
_DOC_EXT = {".md", ".rst", ".txt"}
_CONFIG_EXT = {".toml", ".ini", ".cfg", ".yaml", ".yml", ".json"}
_LOCKFILES = {"poetry.lock", "package-lock.json", "Cargo.lock", "requirements.txt", "uv.lock"}


def _source_type(rel_path: str) -> str:
    """Classify by the REPO-RELATIVE path (never the absolute path, whose temp
    directory may incidentally contain words like 'test')."""
    rel = rel_path.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    parent = rel[: rel.rfind("/")] if "/" in rel else ""
    if name in _LOCKFILES:
        return "lockfile"
    if name.startswith("test_") or name.endswith("_test.py") or _has_test_segment(parent):
        return "test"
    if suffix in _DOC_EXT:
        return "doc"
    if suffix in _CONFIG_EXT:
        return "config"
    if suffix in _CODE_EXT:
        return "code"
    return "other"


def _has_test_segment(parent: str) -> bool:
    return any(seg in ("test", "tests") for seg in parent.split("/") if seg)


def _validate_focus_paths(focus: list[str], worktree: Path) -> list[str]:
    """Reject absolute / traversal focus paths; return the validated list.

    Discovery must never read outside the run's worktree, so an absolute path or
    one that resolves outside the worktree is a hard error (raised before any
    filesystem walk or ripgrep invocation).
    """
    validated: list[str] = []
    for raw in focus:
        candidate = raw.replace("\\", "/")
        if PurePosixPath(candidate).is_absolute():
            raise ValidationError(f"discovery focus path must be repo-relative, not absolute: {raw!r}")
        resolved = (worktree / candidate).resolve()
        try:
            resolved.relative_to(worktree)
        except ValueError as exc:
            raise ValidationError(
                f"discovery focus path escapes the worktree: {raw!r}"
            ) from exc
        validated.append(candidate)
    return validated


def _git_commit(worktree: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except FileNotFoundError:  # pragma: no cover - git always present in tests
        pass
    return None


def _git_history_summary(worktree: Path, rel_paths: list[str], limit: int = 5) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "log", f"-{limit}", "--oneline", "--", *rel_paths],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()
    except FileNotFoundError:  # pragma: no cover
        pass
    return []


def _ripgrep_hits(worktree: Path, keyword: str, focus: list[str]) -> set[str]:
    if not shutil.which("rg") or not keyword:
        return set()
    try:
        out = subprocess.run(
            ["rg", "-l", "--", keyword, *focus],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0:
            return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except FileNotFoundError:  # pragma: no cover
        pass
    return set()


def discover_sources(
    run_id: str,
    worktree: Path,
    scope: TaskScope | None,
    *,
    keyword: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic source_manifest for the run's worktree.

    Discovery is scoped to the task scope's ``discovery_focus_paths`` (or
    ``root_paths`` when focus is unset). Results are sorted for determinism.
    """
    focus: list[str] = []
    if scope is not None:
        focus = list(scope.discovery_focus_paths or scope.root_paths)
    if not focus:
        focus = ["."]

    # Constrain every focus path to the worktree BEFORE invoking rg or walking.
    worktree = worktree.resolve()
    focus = _validate_focus_paths(focus, worktree)

    commit = _git_commit(worktree)
    rg_hits = _ripgrep_hits(worktree, keyword or "", focus)

    discovered: dict[str, dict[str, Any]] = {}
    for focus_path in focus:
        base = (worktree / focus_path).resolve()
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for file_path in candidates:
            if not file_path.is_file():
                continue
            if ".git" in file_path.parts:
                continue
            rel = file_path.relative_to(worktree).as_posix()
            if rel in discovered:
                continue
            discovered[rel] = _build_source_entry(file_path, rel, worktree, commit, rg_hits)

    sources = [discovered[rel] for rel in sorted(discovered)]
    # Re-number source_ids deterministically after sorting.
    for index, entry in enumerate(sources, start=1):
        entry["source_id"] = f"SRC-{index:03d}"

    return {"run_id": run_id, "sources": sources}


def git_history_summary(worktree: Path, scope: TaskScope | None, limit: int = 5) -> list[str]:
    """Public helper: recent commit one-liners touching the scope's focus paths.

    Focus paths are constrained to the worktree (same containment as
    discover_sources) before being passed to git.
    """
    focus: list[str] = []
    if scope is not None:
        focus = list(scope.discovery_focus_paths or scope.root_paths)
    if not focus:
        focus = ["."]
    worktree = worktree.resolve()
    focus = _validate_focus_paths(focus, worktree)
    return _git_history_summary(worktree, focus, limit=limit)


def _build_source_entry(
    file_path: Path, rel: str, worktree: Path, commit: str | None, rg_hits: set[str]
) -> dict[str, Any]:
    source_type = _source_type(rel)
    discovered_by = ["file_inventory"]
    if source_type == "test":
        discovered_by.append("test_finder")
    if rel in rg_hits:
        discovered_by.append("ripgrep")

    freshness: dict[str, Any] = {"checked_at": now_iso()}
    if source_type in ("code", "test", "config", "migration", "lockfile"):
        # Schema requires commit_sha for these types.
        freshness["commit_sha"] = commit or "0" * 40
    if source_type in ("doc", "runbook", "post"):
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
        freshness["last_modified"] = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "source_id": "SRC-000",  # renumbered after sort
        "path": rel,
        "source_type": source_type,
        "discovered_by": discovered_by,
        "reason_for_inclusion": f"In task scope focus path; type={source_type}",
        "freshness": freshness,
    }
