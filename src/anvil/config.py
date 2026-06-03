"""Config models and loaders for installation.json, repo.json, project.json.

These are the human-readable, on-disk counterparts to the operational SQLite
state. SQLite is the source of truth for *operational* queries (status, leases);
the JSON configs are the durable, reviewable description of each entity.

project.json / repo.json are validated against the Milestone 0 schemas in
``schemas/`` so the two milestones stay consistent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ValidationError
from .timeutil import now_iso

SCHEMA_VERSION = "0.1.0"
_FORMAT_CHECKER = FormatChecker()

# schemas/ lives at the repo root: src/anvil/config.py -> parents[2]/schemas
_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    schema_path = _SCHEMAS_DIR / f"{schema_name}.schema.json"
    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    return Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def _validate(schema_name: str, payload: dict[str, Any]) -> None:
    errors = sorted(_validator(schema_name).iter_errors(payload), key=str)
    if errors:
        detail = "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
        raise ValidationError(f"{schema_name} config is invalid: {detail}")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


# ---------------------------------------------------------------------------
# installation.json
# ---------------------------------------------------------------------------

@dataclass
class InstallationConfig:
    """Top-level marker describing an Anvil installation."""

    schema_version: str = SCHEMA_VERSION
    created_at: str = field(default_factory=now_iso)
    anvil_version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "anvil_version": self.anvil_version,
        }

    def save(self, path: Path) -> None:
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> "InstallationConfig":
        data = _read_json(path)
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            created_at=data.get("created_at", now_iso()),
            anvil_version=data.get("anvil_version", "0.1.0"),
        )


# ---------------------------------------------------------------------------
# repo.json
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    repo_id: str
    name: str
    path: str
    default_branch: str
    created_at: str = field(default_factory=now_iso)
    vcs: str = "git"
    url: str | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "repo_id": self.repo_id,
            "schema_version": self.schema_version,
            "name": self.name,
            "path": self.path,
            "default_branch": self.default_branch,
            "vcs": self.vcs,
            "created_at": self.created_at,
        }
        if self.url is not None:
            data["url"] = self.url
        return data

    def validate(self) -> None:
        _validate("repo", self.to_dict())

    def save(self, path: Path) -> None:
        self.validate()
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> "RepoConfig":
        data = _read_json(path)
        _validate("repo", data)
        return cls(
            repo_id=data["repo_id"],
            name=data["name"],
            path=data["path"],
            default_branch=data["default_branch"],
            created_at=data["created_at"],
            vcs=data.get("vcs", "git"),
            url=data.get("url"),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


# ---------------------------------------------------------------------------
# task scopes + project.json
# ---------------------------------------------------------------------------

@dataclass
class TaskScope:
    """A bounded operating area inside a project's repo."""

    scope_id: str
    root_paths: list[str]
    discovery_focus_paths: list[str] = field(default_factory=list)
    default_forbidden_changes: list[str] = field(default_factory=list)
    baseline_commands: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"root_paths": list(self.root_paths)}
        if self.discovery_focus_paths:
            data["discovery_focus_paths"] = list(self.discovery_focus_paths)
        if self.default_forbidden_changes:
            data["default_forbidden_changes"] = list(self.default_forbidden_changes)
        if self.baseline_commands:
            data["baseline_commands"] = list(self.baseline_commands)
        return data


@dataclass
class ProjectConfig:
    project_id: str
    name: str
    repos: list[str]
    created_at: str = field(default_factory=now_iso)
    description: str | None = None
    task_scopes: dict[str, TaskScope] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "project_id": self.project_id,
            "schema_version": self.schema_version,
            "name": self.name,
            "created_at": self.created_at,
            "repos": list(self.repos),
        }
        if self.description is not None:
            data["description"] = self.description
        if self.task_scopes:
            data["task_scopes"] = {
                scope_id: scope.to_dict() for scope_id, scope in self.task_scopes.items()
            }
        return data

    def validate(self) -> None:
        _validate("project", self.to_dict())

    def save(self, path: Path) -> None:
        self.validate()
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> "ProjectConfig":
        data = _read_json(path)
        _validate("project", data)
        scopes: dict[str, TaskScope] = {}
        for scope_id, raw in (data.get("task_scopes") or {}).items():
            scopes[scope_id] = TaskScope(
                scope_id=scope_id,
                root_paths=list(raw["root_paths"]),
                discovery_focus_paths=list(raw.get("discovery_focus_paths", [])),
                default_forbidden_changes=list(raw.get("default_forbidden_changes", [])),
                baseline_commands=list(raw.get("baseline_commands", [])),
            )
        return cls(
            project_id=data["project_id"],
            name=data["name"],
            repos=list(data["repos"]),
            created_at=data["created_at"],
            description=data.get("description"),
            task_scopes=scopes,
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
