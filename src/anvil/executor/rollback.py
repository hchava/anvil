"""Structured rollback engine (Milestone 3).

Implements controller-owned rollback primitives. All operations use structured
subprocess calls — no shell strings, no agent-generated commands.

Supported primitives:
  {"op": "restore_file", "target": "<repo-relative path>"}
      → git checkout HEAD -- <target>   (restores tracked file to HEAD state)

  {"op": "delete_file", "target": "<repo-relative path>"}
      → unlink the file                 (removes a new file not in HEAD)

  {"op": "noop"}
      → no-op, recorded for audit trail

Unknown ops are logged as errors but do not abort the rollback sequence
(continue-on-error so we restore as much as possible).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RollbackResult:
    success: bool
    restored_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _validate_target(target: str, worktree: Path) -> str | None:
    """Return an error string if target is unsafe, else None."""
    if not target:
        return "target is empty"
    if target.startswith("/"):
        return f"target must be relative, not absolute: {target!r}"
    resolved = (worktree / target).resolve()
    if not str(resolved).startswith(str(worktree.resolve())):
        return f"target escapes worktree boundary: {target!r}"
    return None


def rollback(worktree: Path, primitives: list[dict]) -> RollbackResult:
    """Execute rollback primitives in order. Continues on per-primitive errors."""
    restored: list[str] = []
    deleted: list[str] = []
    errors: list[str] = []

    for primitive in primitives:
        op = primitive.get("op", "")
        target = primitive.get("target", "")

        if op == "restore_file":
            if not target:
                errors.append("restore_file primitive missing 'target'")
                continue
            containment_error = _validate_target(target, worktree)
            if containment_error:
                errors.append(f"restore_file rejected: {containment_error}")
                continue
            try:
                subprocess.run(
                    ["git", "checkout", "HEAD", "--", target],
                    cwd=str(worktree),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                restored.append(target)
            except subprocess.CalledProcessError as exc:
                errors.append(f"restore_file {target}: {exc.stderr.strip()}")

        elif op == "delete_file":
            if not target:
                errors.append("delete_file primitive missing 'target'")
                continue
            containment_error = _validate_target(target, worktree)
            if containment_error:
                errors.append(f"delete_file rejected: {containment_error}")
                continue
            file_path = worktree / target
            try:
                if file_path.exists():
                    file_path.unlink()
                deleted.append(target)
            except OSError as exc:
                errors.append(f"delete_file {target}: {exc}")

        elif op == "noop":
            pass  # Intentional no-op; recorded for audit trail.

        else:
            errors.append(f"Unknown rollback op: {op!r}")

    return RollbackResult(
        success=not errors,
        restored_files=restored,
        deleted_files=deleted,
        errors=errors,
    )


def build_rollback_primitives(
    modified_tracked: list[str],
    new_untracked: list[str],
) -> list[dict]:
    """Build controller-owned rollback primitives from scope check results.

    Called by the executor when it needs to restore the worktree to its
    pre-work-order state without using agent-generated commands.
    """
    primitives: list[dict] = []
    for path in modified_tracked:
        primitives.append({"op": "restore_file", "target": path})
    for path in new_untracked:
        primitives.append({"op": "delete_file", "target": path})
    return primitives
