"""Scope enforcement for single work order execution (Milestone 3).

After a fake agent writes files to the worktree, this module compares the
git diff against the work order's assigned_scope (allowed_files / forbidden_files).

Rules enforced:
  - Any file touched outside allowed_files is a violation.
  - Any file in forbidden_files is a violation even if it is also in allowed_files.
  - Scope violations fail closed: the work order is rejected and rolled back.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class ScopeViolationError(Exception):
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(f"Scope violation(s): {violations}")


@dataclass
class ScopeResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    # Separated so the executor can issue the right rollback primitive.
    modified_tracked: list[str] = field(default_factory=list)
    new_untracked: list[str] = field(default_factory=list)


def _norm(path: str) -> str:
    """Normalize a repo-relative path for comparison."""
    return str(PurePosixPath(path)).lstrip("./")


def _git_modified(worktree: Path) -> list[str]:
    """Files that differ from HEAD (tracked files modified in place)."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    return [p for p in result.stdout.splitlines() if p.strip()]


def _git_untracked(worktree: Path) -> list[str]:
    """New files not yet in the index and not gitignored."""
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    return [p for p in result.stdout.splitlines() if p.strip()]


def check_scope(
    worktree: Path,
    allowed_files: list[str],
    forbidden_files: list[str],
) -> ScopeResult:
    """Return a ScopeResult describing whether the worktree diff is within scope."""
    modified = _git_modified(worktree)
    untracked = _git_untracked(worktree)

    # Deduplicated, order-preserving union of all touched paths.
    seen: dict[str, None] = {}
    for p in modified + untracked:
        seen[p] = None
    touched = list(seen)

    allowed_norm = {_norm(f) for f in allowed_files}
    forbidden_norm = {_norm(f) for f in forbidden_files}

    violations: list[str] = []
    for path in touched:
        n = _norm(path)
        if n in forbidden_norm:
            violations.append(f"{path}: in forbidden_files")
        elif n not in allowed_norm:
            violations.append(f"{path}: not in allowed_files")

    return ScopeResult(
        passed=not violations,
        violations=violations,
        touched_files=touched,
        modified_tracked=modified,
        new_untracked=untracked,
    )
