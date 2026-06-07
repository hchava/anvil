"""Tests for the structured rollback engine (Milestone 3).

All tests use temporary git repos. No network, no real API keys.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil.executor.rollback import RollbackResult, build_rollback_primitives, rollback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ORIGINAL = True\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


# ---------------------------------------------------------------------------
# restore_file (git checkout HEAD --)
# ---------------------------------------------------------------------------

def test_restore_file_reverts_modification(git_repo: Path) -> None:
    (git_repo / "src" / "app.py").write_text("MODIFIED = True\n", encoding="utf-8")
    assert "MODIFIED" in (git_repo / "src" / "app.py").read_text()

    result = rollback(git_repo, [{"op": "restore_file", "target": "src/app.py"}])

    assert result.success
    assert "src/app.py" in result.restored_files
    assert "ORIGINAL" in (git_repo / "src" / "app.py").read_text()


def test_restore_file_missing_target_is_error() -> None:
    result = rollback(Path("/tmp"), [{"op": "restore_file"}])
    assert not result.success
    assert any("missing 'target'" in e for e in result.errors)


# ---------------------------------------------------------------------------
# delete_file (unlink new untracked file)
# ---------------------------------------------------------------------------

def test_delete_file_removes_new_file(git_repo: Path) -> None:
    new_file = git_repo / "src" / "new_module.py"
    new_file.write_text("def f(): pass\n", encoding="utf-8")
    assert new_file.exists()

    result = rollback(git_repo, [{"op": "delete_file", "target": "src/new_module.py"}])

    assert result.success
    assert "src/new_module.py" in result.deleted_files
    assert not new_file.exists()


def test_delete_file_idempotent_when_file_gone(git_repo: Path) -> None:
    # delete_file on a path that doesn't exist should not error.
    result = rollback(git_repo, [{"op": "delete_file", "target": "nonexistent.py"}])
    assert result.success
    assert "nonexistent.py" in result.deleted_files


def test_delete_file_missing_target_is_error() -> None:
    result = rollback(Path("/tmp"), [{"op": "delete_file"}])
    assert not result.success
    assert any("missing 'target'" in e for e in result.errors)


# ---------------------------------------------------------------------------
# noop
# ---------------------------------------------------------------------------

def test_noop_is_success(git_repo: Path) -> None:
    result = rollback(git_repo, [{"op": "noop"}])
    assert result.success
    assert result.errors == []


# ---------------------------------------------------------------------------
# Unknown op
# ---------------------------------------------------------------------------

def test_unknown_op_is_error(git_repo: Path) -> None:
    result = rollback(git_repo, [{"op": "revert_commit_chain"}])
    assert not result.success
    assert any("Unknown rollback op" in e for e in result.errors)


def test_continue_on_error(git_repo: Path) -> None:
    (git_repo / "src" / "app.py").write_text("NEW = 1\n", encoding="utf-8")
    new_file = git_repo / "src" / "extra.py"
    new_file.write_text("EXTRA = 1\n", encoding="utf-8")

    primitives = [
        {"op": "unknown_op"},                             # error — continues
        {"op": "restore_file", "target": "src/app.py"},  # success
        {"op": "delete_file", "target": "src/extra.py"}, # success
    ]
    result = rollback(git_repo, primitives)

    # Errors recorded but other ops still ran.
    assert len(result.errors) == 1
    assert "src/app.py" in result.restored_files
    assert "src/extra.py" in result.deleted_files
    assert "ORIGINAL" in (git_repo / "src" / "app.py").read_text()
    assert not new_file.exists()


# ---------------------------------------------------------------------------
# build_rollback_primitives helper
# ---------------------------------------------------------------------------

def test_build_primitives_from_scope_result(git_repo: Path) -> None:
    primitives = build_rollback_primitives(
        modified_tracked=["src/app.py"],
        new_untracked=["src/brand_new.py"],
    )
    assert {"op": "restore_file", "target": "src/app.py"} in primitives
    assert {"op": "delete_file", "target": "src/brand_new.py"} in primitives


def test_build_primitives_empty(git_repo: Path) -> None:
    primitives = build_rollback_primitives([], [])
    assert primitives == []


# ---------------------------------------------------------------------------
# Integration: rollback after forced failure
# ---------------------------------------------------------------------------

def test_rollback_restores_files_after_validation_failure(git_repo: Path) -> None:
    """Simulate a validation failure and confirm rollback restores the worktree."""
    original_content = (git_repo / "src" / "app.py").read_text()
    (git_repo / "src" / "app.py").write_text("BROKEN = True\n", encoding="utf-8")
    new_file = git_repo / "src" / "added.py"
    new_file.write_text("NEW = 1\n", encoding="utf-8")

    primitives = build_rollback_primitives(
        modified_tracked=["src/app.py"],
        new_untracked=["src/added.py"],
    )
    result = rollback(git_repo, primitives)

    assert result.success
    assert (git_repo / "src" / "app.py").read_text() == original_content
    assert not new_file.exists()
