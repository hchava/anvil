"""Shared pytest fixtures for the Milestone 0.5 runtime tests.

Every test runs against a temporary ANVIL_HOME and temporary git repositories
created under pytest's tmp_path. Nothing touches the developer's real
``~/.anvil`` and no network access is performed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from anvil.paths import ANVIL_HOME_ENV
from anvil.registry import Registry


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def anvil_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ANVIL_HOME at a temp dir so the real home is never touched."""
    home = tmp_path / "anvil-home"
    monkeypatch.setenv(ANVIL_HOME_ENV, str(home))
    return home


@pytest.fixture
def registry(anvil_home: Path):
    """An initialized Registry rooted at the temp home."""
    reg = Registry()
    reg.init()
    try:
        yield reg
    finally:
        reg.close()


@pytest.fixture
def git_repo_factory(tmp_path: Path):
    """Factory creating local git repos with a deterministic initial commit.

    Returns a callable ``make(name, default_branch="main") -> Path``.
    """
    created: dict[str, Path] = {}

    def make(name: str, default_branch: str = "main") -> Path:
        repo_path = tmp_path / "repos" / name
        repo_path.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q", "-b", default_branch], cwd=repo_path)
        # Local identity so commits work without global git config.
        _git(["config", "user.email", "test@example.com"], cwd=repo_path)
        _git(["config", "user.name", "Anvil Test"], cwd=repo_path)
        # A worktree add requires at least one commit on the branch.
        (repo_path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        (repo_path / "src").mkdir(exist_ok=True)
        (repo_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(["add", "-A"], cwd=repo_path)
        _git(["commit", "-q", "-m", "initial commit"], cwd=repo_path)
        created[name] = repo_path
        return repo_path

    return make


def commit_change(repo_path: Path, filename: str, content: str, message: str) -> str:
    """Helper used by drift tests: commit a change to the repo's current branch.

    Returns the new HEAD sha.
    """
    (repo_path / filename).write_text(content, encoding="utf-8")
    _git(["add", "-A"], cwd=repo_path)
    _git(["commit", "-q", "-m", message], cwd=repo_path)
    return _git(["rev-parse", "HEAD"], cwd=repo_path)


@dataclass
class ControllerEnv:
    """A registered project/repo/scope with a created, activated run ready for a
    Milestone 1 dry run. ``run_dir`` is where fixture artifacts are dropped."""

    registry: Registry
    run_id: str
    project_id: str
    repo_id: str
    scope_id: str
    repo_path: Path
    run_dir: Path


@pytest.fixture
def controller_env(registry: Registry, git_repo_factory, tmp_path: Path) -> ControllerEnv:
    """Standard Milestone 1 setup: project + repo + scope + activated run."""
    repo_path = git_repo_factory("service")
    # Add a focus path with a couple of files so deterministic discovery finds >=1.
    (repo_path / "src" / "config").mkdir(parents=True, exist_ok=True)
    (repo_path / "src" / "config" / "loader.py").write_text("def load_config():\n    return {}\n", encoding="utf-8")
    (repo_path / "tests").mkdir(exist_ok=True)
    (repo_path / "tests" / "test_loader.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(["add", "-A"], cwd=repo_path)
    _git(["commit", "-q", "-m", "add config + tests"], cwd=repo_path)

    registry.register_repo("repo-service", repo_path)
    registry.create_project("proj-demo", ["repo-service"])
    registry.create_scope(
        "proj-demo",
        "config-scope",
        ["src/config/", "tests/"],
        discovery_focus_paths=["src/config/", "tests/"],
    )
    run_id = "RUN-20260601-001"
    registry.create_run(run_id, "proj-demo", "repo-service", "tester", task_scope_id="config-scope")
    registry.activate_run(run_id)
    return ControllerEnv(
        registry=registry,
        run_id=run_id,
        project_id="proj-demo",
        repo_id="repo-service",
        scope_id="config-scope",
        repo_path=repo_path,
        run_dir=registry.paths.run_dir(run_id),
    )
