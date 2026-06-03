"""Anvil home directory layout.

The installation root defaults to ``~/.anvil`` but is overridable via the
``ANVIL_HOME`` environment variable. Tests MUST set ``ANVIL_HOME`` to a
temporary directory so nothing ever touches a developer's real home.

Note on naming: the roadmap draft uses ``~/.edge-harness`` and ``edge/...``
branch prefixes, but that predates the public "Anvil" rename (see README and
docs/public-safety-boundary.md). This implementation uses ``~/.anvil`` and the
``anvil/`` branch prefix to stay consistent with the shipped brand.
"""

from __future__ import annotations

import os
from pathlib import Path

ANVIL_HOME_ENV = "ANVIL_HOME"


def default_home() -> Path:
    """Resolve the Anvil home directory, honoring ``ANVIL_HOME``."""
    override = os.environ.get(ANVIL_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".anvil").resolve()


class AnvilPaths:
    """Computes the on-disk layout under a given Anvil home."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or default_home()).resolve()

    # -- top-level files ---------------------------------------------------
    @property
    def installation_json(self) -> Path:
        return self.home / "installation.json"

    @property
    def registry_db(self) -> Path:
        return self.home / "registry.sqlite"

    # -- directories -------------------------------------------------------
    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    @property
    def repos_dir(self) -> Path:
        return self.home / "repos"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def shared_dir(self) -> Path:
        return self.home / "shared"

    # -- per-entity paths --------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def project_json(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def repo_dir(self, repo_id: str) -> Path:
        return self.repos_dir / repo_id

    def repo_json(self, repo_id: str) -> Path:
        return self.repo_dir(repo_id) / "repo.json"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def worktree_path(self, repo_id: str, run_id: str) -> Path:
        """Per-run worktree: ``{home}/worktrees/{repo_id}/{run_id}``."""
        return self.worktrees_dir / repo_id / run_id

    def exists(self) -> bool:
        return self.installation_json.exists() and self.registry_db.exists()
