"""Thin, explicit wrappers around the ``git`` CLI.

Everything goes through :func:`_run`, which raises :class:`GitError` on a
non-zero exit. No network operations are performed by any function here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitError


def _run(args: list[str], cwd: Path) -> str:
    """Run a git command in ``cwd`` and return stripped stdout."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git missing
        raise GitError("git executable not found on PATH") from exc
    if result.returncode != 0:
        cmd = " ".join(["git", *args])
        raise GitError(
            f"`{cmd}` failed in {cwd} (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def is_git_repo(path: Path) -> bool:
    """True if ``path`` is inside a git work tree."""
    try:
        out = _run(["rev-parse", "--is-inside-work-tree"], cwd=path)
    except GitError:
        return False
    return out == "true"


def repo_toplevel(path: Path) -> Path:
    """Absolute path of the repository's top level."""
    return Path(_run(["rev-parse", "--show-toplevel"], cwd=path)).resolve()


def current_branch(path: Path) -> str:
    """Name of the currently checked-out branch."""
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)


def rev_parse(path: Path, ref: str) -> str:
    """Resolve ``ref`` to a full commit SHA."""
    return _run(["rev-parse", ref], cwd=path)


def branch_head(path: Path, branch: str) -> str:
    """Commit SHA at the tip of ``branch``."""
    return _run(["rev-parse", branch], cwd=path)


def add_worktree(repo_path: Path, worktree_path: Path, branch: str, base_commit: str) -> None:
    """Create a new worktree with a fresh branch ``branch`` at ``base_commit``."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["worktree", "add", "-b", branch, str(worktree_path), base_commit],
        cwd=repo_path,
    )


def remove_worktree(repo_path: Path, worktree_path: Path, force: bool = True) -> None:
    """Remove a previously-added worktree.

    Tolerates a worktree that is already gone (prunes stale metadata), but does
    NOT silently report success if the directory still exists after the attempt —
    callers rely on abort/finalize genuinely removing the checkout.
    """
    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")
    try:
        _run(args, cwd=repo_path)
    except GitError:
        # The worktree may already be gone; prune stale metadata and re-check.
        _run(["worktree", "prune"], cwd=repo_path)
    if worktree_path.exists():
        raise GitError(f"worktree still present after removal: {worktree_path}")


def delete_branch(repo_path: Path, branch: str) -> None:
    """Delete a local branch if it exists. Best-effort (no error if absent)."""
    try:
        _run(["branch", "-D", branch], cwd=repo_path)
    except GitError:
        pass
