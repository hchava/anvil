"""Tests for scope enforcement (Milestone 3).

All tests use a temporary synthetic git repository. No network, no real
API keys, no writes to ~/.anvil.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil.executor.scope import check_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Minimal git repo with one tracked file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_clean_worktree_passes(git_repo: Path) -> None:
    result = check_scope(git_repo, ["src/app.py"], [])
    assert result.passed
    assert result.violations == []
    assert result.touched_files == []


def test_allowed_file_modified_passes(git_repo: Path) -> None:
    (git_repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    result = check_scope(git_repo, ["src/app.py"], [])
    assert result.passed
    assert result.violations == []
    assert "src/app.py" in result.touched_files


def test_multiple_allowed_files_pass(git_repo: Path) -> None:
    (git_repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (git_repo / "src" / "config.py").write_text("DEBUG = True\n", encoding="utf-8")
    result = check_scope(git_repo, ["src/app.py", "src/config.py"], [])
    assert result.passed


# ---------------------------------------------------------------------------
# Forbidden file violations
# ---------------------------------------------------------------------------

def test_forbidden_file_modification_fails(git_repo: Path) -> None:
    (git_repo / "src" / "config.py").write_text("DEBUG = True\n", encoding="utf-8")
    result = check_scope(
        git_repo,
        allowed_files=["src/app.py", "src/config.py"],
        forbidden_files=["src/config.py"],
    )
    assert not result.passed
    assert any("forbidden" in v for v in result.violations)


def test_forbidden_file_beats_allowed(git_repo: Path) -> None:
    """A file in both allowed_files and forbidden_files is still a violation."""
    (git_repo / "src" / "app.py").write_text("x = 99\n", encoding="utf-8")
    result = check_scope(
        git_repo,
        allowed_files=["src/app.py"],
        forbidden_files=["src/app.py"],
    )
    assert not result.passed


# ---------------------------------------------------------------------------
# Out-of-scope file violations
# ---------------------------------------------------------------------------

def test_out_of_scope_modification_fails(git_repo: Path) -> None:
    (git_repo / "src" / "config.py").write_text("DEBUG = True\n", encoding="utf-8")
    result = check_scope(
        git_repo,
        allowed_files=["src/app.py"],
        forbidden_files=[],
    )
    assert not result.passed
    assert any("not in allowed_files" in v for v in result.violations)


def test_new_untracked_file_outside_scope_fails(git_repo: Path) -> None:
    (git_repo / "src" / "secret.py").write_text("KEY = 'xyz'\n", encoding="utf-8")
    result = check_scope(
        git_repo,
        allowed_files=["src/app.py"],
        forbidden_files=[],
    )
    assert not result.passed
    assert any("src/secret.py" in v for v in result.violations)


def test_new_untracked_file_in_scope_passes(git_repo: Path) -> None:
    (git_repo / "src" / "new_module.py").write_text("def f(): pass\n", encoding="utf-8")
    result = check_scope(
        git_repo,
        allowed_files=["src/new_module.py"],
        forbidden_files=[],
    )
    assert result.passed
    assert "src/new_module.py" in result.touched_files


# ---------------------------------------------------------------------------
# Scope result metadata
# ---------------------------------------------------------------------------

def test_touched_files_populated(git_repo: Path) -> None:
    (git_repo / "src" / "app.py").write_text("x = 5\n", encoding="utf-8")
    result = check_scope(git_repo, ["src/app.py"], [])
    assert "src/app.py" in result.touched_files
    assert "src/app.py" in result.modified_tracked


def test_new_untracked_separated_from_modified(git_repo: Path) -> None:
    (git_repo / "src" / "app.py").write_text("x = 5\n", encoding="utf-8")
    (git_repo / "src" / "brand_new.py").write_text("y = 10\n", encoding="utf-8")
    result = check_scope(git_repo, ["src/app.py", "src/brand_new.py"], [])
    assert result.passed
    assert "src/app.py" in result.modified_tracked
    assert "src/brand_new.py" in result.new_untracked
