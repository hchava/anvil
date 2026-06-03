"""Project / repo registration and the shared-repo invariant."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from anvil.config import ProjectConfig, RepoConfig
from anvil.errors import (
    AlreadyExistsError,
    NotFoundError,
    NotInitializedError,
    ValidationError,
)
from anvil.registry import Registry


def test_init_creates_layout(anvil_home: Path):
    reg = Registry()
    reg.init()
    assert reg.paths.installation_json.exists()
    assert reg.paths.registry_db.exists()
    for d in (reg.paths.projects_dir, reg.paths.repos_dir, reg.paths.runs_dir, reg.paths.worktrees_dir):
        assert d.is_dir()
    reg.close()


def test_init_is_idempotent(anvil_home: Path):
    Registry().init()
    # Second init must not error or wipe state.
    reg = Registry()
    reg.init()
    assert reg.paths.exists()
    reg.close()


def test_registry_without_init_raises(anvil_home: Path):
    reg = Registry()
    with pytest.raises(NotInitializedError):
        reg.list_projects()


def test_register_repo_and_config(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    cfg = registry.register_repo("repo-svc", repo_path)
    assert cfg.repo_id == "repo-svc"
    assert Path(cfg.path) == repo_path.resolve()
    assert cfg.default_branch == "main"
    # repo.json on disk validates against the M0 schema.
    loaded = RepoConfig.load(registry.paths.repo_json("repo-svc"))
    assert loaded.repo_id == "repo-svc"
    # row present in SQLite
    row = registry.get_repo("repo-svc")
    assert row["default_branch"] == "main"


def test_register_repo_rejects_bad_id(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    with pytest.raises(ValidationError):
        registry.register_repo("SVC", repo_path)  # missing repo- prefix


def test_register_repo_rejects_non_git(registry: Registry, tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(ValidationError):
        registry.register_repo("repo-plain", plain)


def test_register_repo_duplicate(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    with pytest.raises(AlreadyExistsError):
        registry.register_repo("repo-svc", repo_path)


def test_create_project_requires_registered_repo(registry: Registry):
    with pytest.raises(NotFoundError):
        registry.create_project("proj-a", ["repo-missing"])


def test_two_projects_share_one_repo(registry: Registry, git_repo_factory):
    """Acceptance: two projects registered, one shared repo, no binding conflict."""
    repo_path = git_repo_factory("data-pipelines")
    registry.register_repo("repo-data-pipelines", repo_path)

    registry.create_project("proj-ingestion", ["repo-data-pipelines"])
    registry.create_project("proj-export", ["repo-data-pipelines"])

    projects = {row["project_id"] for row in registry.list_projects()}
    assert projects == {"proj-ingestion", "proj-export"}

    # Both project configs reference the same shared repo.
    ing = ProjectConfig.load(registry.paths.project_json("proj-ingestion"))
    exp = ProjectConfig.load(registry.paths.project_json("proj-export"))
    assert ing.repos == ["repo-data-pipelines"]
    assert exp.repos == ["repo-data-pipelines"]

    # The repo row is unchanged / not "owned" by either project.
    assert registry.get_repo("repo-data-pipelines")["repo_id"] == "repo-data-pipelines"


def test_create_project_rejects_bad_id(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    with pytest.raises(ValidationError):
        registry.create_project("ingestion", ["repo-svc"])  # missing proj- prefix


def test_create_scope_persists_to_project_json(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])
    registry.create_scope("proj-svc", "ingestion", ["src/ingestion/", "tests/ingestion/"])

    cfg = ProjectConfig.load(registry.paths.project_json("proj-svc"))
    assert "ingestion" in cfg.task_scopes
    assert cfg.task_scopes["ingestion"].root_paths == ["src/ingestion/", "tests/ingestion/"]


def test_create_scope_duplicate(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])
    registry.create_scope("proj-svc", "ingestion", ["src/ingestion/"])
    with pytest.raises(AlreadyExistsError):
        registry.create_scope("proj-svc", "ingestion", ["src/ingestion/"])


def test_create_scope_requires_root_paths(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])
    with pytest.raises(ValidationError):
        registry.create_scope("proj-svc", "ingestion", [])
