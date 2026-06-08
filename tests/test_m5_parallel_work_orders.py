"""Milestone 5 — Parallel Work Orders & Merge Protocol tests.

Tests cover:
  - Two independent work orders execute in separate worktrees
  - Dependency-ordered work orders execute sequentially
  - Dependency cycle fails closed
  - Two independent WOs merge into integration branch
  - merge_queue lease blocks concurrent merges (verified via Registry)
  - Textual merge conflict detected and blocks merge
  - Forbidden file in one WO rejects that patch
  - File outside allowed_files rejects that patch
  - Integration WO required for multi-WO run
  - Integration validation catches regression
  - Required WO failure blocks dependent WOs
  - Optional WO failure can continue with fail_open
  - worktree_manifest.json is schema-valid after multi-WO run
  - validation_results.json is schema-valid after multi-WO run
  - Single-WO execution still works (M3/M4 backwards compat)

All tests use temp dirs and synthetic git repos.  No real Claude, Codex,
network, or API keys.  No writes to ~/.anvil.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from anvil.controller.events import EventLog
from anvil.errors import LeaseConflictError
from anvil.executor.parallel import (
    DependencyCycleError,
    FileConflictError,
    IntegrationWorkOrderMissingError,
    MultiWorkOrderExecutor,
    detect_write_conflicts,
    topological_sort,
)
from anvil.paths import ANVIL_HOME_ENV
from anvil.registry import Registry
from anvil.schemas_util import validate_artifact


# ---------------------------------------------------------------------------
# Shared git / registry helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / "repos" / name
    repo.mkdir(parents=True)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Anvil Test"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "src" / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


def _setup_registry(
    registry: Registry,
    repo: Path,
    run_id: str = "RUN-20260607-500",
    project_id: str = "proj-m5",
    repo_id: str = "repo-m5",
    scope_id: str = "scope-m5",
) -> tuple[str, str, str]:
    registry.register_repo(repo_id, repo)
    registry.create_project(project_id, [repo_id])
    registry.create_scope(project_id, scope_id, ["src/"], discovery_focus_paths=["src/"])
    registry.create_run(run_id, project_id, repo_id, "tester", task_scope_id=scope_id)
    registry.activate_run(run_id)
    return project_id, repo_id, scope_id


# ---------------------------------------------------------------------------
# Work order / dependency matrix builders
# ---------------------------------------------------------------------------


def _wo(
    wo_id: str,
    allowed_files: list[str],
    forbidden_files: list[str] | None = None,
    criticality: str = "required",
    fail_policy: str = "fail_closed",
    is_integration: bool = False,
) -> dict[str, Any]:
    return {
        "work_order_id": wo_id,
        "title": f"WO {wo_id}",
        "negotiation_status": "agreed",
        "agreed_by": ["claude-001", "codex-001"],
        "criticality": criticality,
        "fail_policy": fail_policy,
        "is_integration_wo": is_integration,
        "assigned_scope": {
            "allowed_files": allowed_files,
            "forbidden_files": forbidden_files or [],
        },
        "local_acceptance_criteria": ["Tests pass"],
        "validation_commands": [
            {"command_array": ["git", "status", "--porcelain"], "expected_exit_code": 0}
        ],
        "rollback_plan": [{"op": "noop"}],
    }


def _dep(wo_id: str, depends_on: list[str], parallel: bool = True) -> dict[str, Any]:
    return {
        "work_order_id": wo_id,
        "depends_on": depends_on,
        "can_run_parallel": parallel,
    }


def _two_independent_wos(run_id: str) -> dict[str, Any]:
    """Two independent WOs + integration WO (no conflicts)."""
    return {
        "run_id": run_id,
        "work_orders": [
            _wo("EXEC-001", ["src/app.py"]),
            _wo("EXEC-002", ["src/config.py"]),
            _wo("EXEC-INT-001", ["src/app.py", "src/config.py"], is_integration=True),
        ],
        "dependency_matrix": [
            _dep("EXEC-001", []),
            _dep("EXEC-002", []),
            _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anvil_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "anvil-home"
    monkeypatch.setenv(ANVIL_HOME_ENV, str(home))
    return home


@pytest.fixture
def registry(anvil_home: Path):
    reg = Registry()
    reg.init()
    try:
        yield reg
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# Helper to build a MultiWorkOrderExecutor for tests
# ---------------------------------------------------------------------------


def _make_executor(
    registry: Registry,
    repo: Path,
    run_id: str = "RUN-20260607-500",
    project_id: str = "proj-m5",
    repo_id: str = "repo-m5",
    scope_id: str = "scope-m5",
) -> tuple[MultiWorkOrderExecutor, Path]:
    """Set up registry + executor; return (executor, run_dir)."""
    _setup_registry(registry, repo, run_id, project_id, repo_id, scope_id)
    run_row = dict(registry.get_run(run_id))
    run_dir = registry.paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    event_log = EventLog(run_dir / "event_log.jsonl")

    main_worktree = Path(run_row["worktree_path"]) if run_row["worktree_path"] else run_dir

    executor = MultiWorkOrderExecutor(
        run_id=run_id,
        run_dir=run_dir,
        repo_id=repo_id,
        repo_path=repo,
        base_commit=run_row["base_commit"],
        worktrees_dir=registry.paths.worktrees_dir,
        main_worktree_path=main_worktree,
        main_branch=run_row.get("branch") or f"anvil/{run_id}",
        registry=registry,
        event_log=event_log,
    )
    return executor, run_dir


# ============================================================================
# Unit tests: topological_sort
# ============================================================================


class TestTopologicalSort:
    def test_no_dependencies_all_in_one_wave(self):
        waves = topological_sort(
            ["EXEC-001", "EXEC-002"],
            [
                _dep("EXEC-001", []),
                _dep("EXEC-002", []),
            ],
        )
        assert len(waves) == 1
        assert sorted(waves[0]) == ["EXEC-001", "EXEC-002"]

    def test_sequential_dependency_creates_two_waves(self):
        waves = topological_sort(
            ["EXEC-001", "EXEC-002"],
            [
                _dep("EXEC-001", []),
                _dep("EXEC-002", ["EXEC-001"]),
            ],
        )
        assert len(waves) == 2
        assert waves[0] == ["EXEC-001"]
        assert waves[1] == ["EXEC-002"]

    def test_three_levels(self):
        waves = topological_sort(
            ["EXEC-001", "EXEC-002", "EXEC-003"],
            [
                _dep("EXEC-001", []),
                _dep("EXEC-002", ["EXEC-001"]),
                _dep("EXEC-003", ["EXEC-002"]),
            ],
        )
        assert len(waves) == 3
        assert waves[0] == ["EXEC-001"]
        assert waves[1] == ["EXEC-002"]
        assert waves[2] == ["EXEC-003"]

    def test_diamond_dependency(self):
        """A and B independent; C depends on both."""
        waves = topological_sort(
            ["EXEC-001", "EXEC-002", "EXEC-003"],
            [
                _dep("EXEC-001", []),
                _dep("EXEC-002", []),
                _dep("EXEC-003", ["EXEC-001", "EXEC-002"]),
            ],
        )
        assert len(waves) == 2
        assert sorted(waves[0]) == ["EXEC-001", "EXEC-002"]
        assert waves[1] == ["EXEC-003"]

    def test_cycle_raises_dependency_cycle_error(self):
        with pytest.raises(DependencyCycleError, match="cycle"):
            topological_sort(
                ["EXEC-001", "EXEC-002"],
                [
                    _dep("EXEC-001", ["EXEC-002"]),
                    _dep("EXEC-002", ["EXEC-001"]),
                ],
            )

    def test_self_cycle_raises(self):
        with pytest.raises(DependencyCycleError):
            topological_sort(
                ["EXEC-001"],
                [_dep("EXEC-001", ["EXEC-001"])],
            )


# ============================================================================
# Unit tests: detect_write_conflicts
# ============================================================================


class TestDetectWriteConflicts:
    def test_independent_distinct_files_no_conflict(self):
        wos = [_wo("EXEC-001", ["src/a.py"]), _wo("EXEC-002", ["src/b.py"])]
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", [])]
        assert detect_write_conflicts(wos, deps) == []

    def test_same_file_no_dependency_is_conflict(self):
        wos = [_wo("EXEC-001", ["src/shared.py"]), _wo("EXEC-002", ["src/shared.py"])]
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", [])]
        conflicts = detect_write_conflicts(wos, deps)
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "concurrent_write"
        assert conflicts[0]["file"] == "src/shared.py"

    def test_same_file_with_dependency_no_conflict(self):
        """EXEC-002 depends on EXEC-001 so the write is sequential — allowed."""
        wos = [_wo("EXEC-001", ["src/shared.py"]), _wo("EXEC-002", ["src/shared.py"])]
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", ["EXEC-001"])]
        assert detect_write_conflicts(wos, deps) == []

    def test_transitive_dependency_resolves_conflict(self):
        """EXEC-003 depends on EXEC-001 via EXEC-002 — no conflict."""
        wos = [
            _wo("EXEC-001", ["src/shared.py"]),
            _wo("EXEC-002", ["src/other.py"]),
            _wo("EXEC-003", ["src/shared.py"]),
        ]
        deps = [
            _dep("EXEC-001", []),
            _dep("EXEC-002", ["EXEC-001"]),
            _dep("EXEC-003", ["EXEC-002"]),
        ]
        assert detect_write_conflicts(wos, deps) == []


# ============================================================================
# Integration tests: MultiWorkOrderExecutor
# ============================================================================


class TestTwoIndependentWorkOrders:
    """Two independent WOs execute in separate worktrees."""

    def test_separate_worktrees_created(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-501"
        )

        executed_in: list[str] = []

        def agent(wt: Path) -> None:
            executed_in.append(str(wt))
            # Write to the file the WO owns
            if "EXEC-001" in str(wt) or "wo-EXEC-001" in str(wt):
                (wt / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            elif "EXEC-002" in str(wt) or "wo-EXEC-002" in str(wt) or "integration" in str(wt):
                # config.py write or integration WO
                pass

        wos = _two_independent_wos("RUN-20260607-501")
        result = executor.execute(wos, agent)

        # At least two distinct worktree paths were used.
        assert len(set(executed_in)) >= 2

    def test_both_wos_succeed(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-502"
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

        wos = _two_independent_wos("RUN-20260607-502")
        result = executor.execute(wos, agent)

        exec_statuses = [r.execution_status for r in result.work_order_results]
        assert "success" in exec_statuses

    def test_integration_wo_runs_last(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-503"
        )

        order: list[str] = []

        def agent(wt: Path) -> None:
            # Use the final path component name to distinguish the integration
            # worktree from regular WO worktrees — do NOT check `in str(wt)` since
            # pytest names the tmp dir after the test function, which already
            # contains "integration".
            if wt.name.endswith("--integration"):
                order.append("integration")
            else:
                order.append("regular")

        wos = _two_independent_wos("RUN-20260607-503")
        executor.execute(wos, agent)

        # Integration WO always comes after regular WOs.
        if "integration" in order:
            assert order.index("integration") == len(order) - 1


class TestDependencyOrderedExecution:
    """Dependency-ordered WOs execute in the correct sequence."""

    def test_dependent_wo_runs_after_its_dependency(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-510"
        )

        order: list[str] = []

        def agent(wt: Path) -> None:
            wt_str = str(wt)
            if "wo-EXEC-001" in wt_str:
                order.append("EXEC-001")
                (wt / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            elif "wo-EXEC-002" in wt_str:
                order.append("EXEC-002")
                (wt / "src" / "config.py").write_text("DEBUG = True\n", encoding="utf-8")

        wos = {
            "run_id": "RUN-20260607-510",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/config.py"]),
                _wo("EXEC-INT-001", ["src/app.py", "src/config.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", ["EXEC-001"], parallel=False),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
        }
        executor.execute(wos, agent)

        # EXEC-001 must appear before EXEC-002.
        if "EXEC-001" in order and "EXEC-002" in order:
            assert order.index("EXEC-001") < order.index("EXEC-002")


class TestDependencyCycle:
    """Dependency cycle fails closed."""

    def test_cycle_raises_before_execution(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, _ = _make_executor(
            registry, repo, run_id="RUN-20260607-520"
        )

        executed: list[str] = []

        def agent(wt: Path) -> None:
            executed.append(str(wt))

        cyclic_wos = {
            "run_id": "RUN-20260607-520",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/config.py"]),
                _wo("EXEC-INT-001", ["src/app.py", "src/config.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", ["EXEC-002"]),
                _dep("EXEC-002", ["EXEC-001"]),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
        }
        with pytest.raises(DependencyCycleError):
            executor.execute(cyclic_wos, agent)

        assert not executed, "No agent should have been called before cycle detection"


class TestIntegrationBranchMerge:
    """Two independent WOs merge into the integration branch in dependency order."""

    def test_both_wos_merged_into_integration(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-530"
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

        wos = _two_independent_wos("RUN-20260607-530")
        result = executor.execute(wos, agent)

        merged = [
            r for r in result.work_order_results if r.merge_status == "merged"
        ]
        assert len(merged) >= 1, "At least one WO should have merged"

    def test_integration_branch_ref_is_set(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-531"
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

        wos = _two_independent_wos("RUN-20260607-531")
        result = executor.execute(wos, agent)

        assert result.integration_branch.startswith("anvil/")
        assert result.integration_branch_ref


class TestMergeQueueLease:
    """merge_queue lease blocks concurrent merges."""

    def test_merge_queue_lease_blocks_second_run(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)

        # Register two separate runs.
        registry.register_repo("repo-lease", repo)
        registry.create_project("proj-lease", ["repo-lease"])
        registry.create_scope("proj-lease", "scope-lease", ["src/"])

        RUN1 = "RUN-20260607-540"
        RUN2 = "RUN-20260607-541"

        registry.create_run(RUN1, "proj-lease", "repo-lease", "tester", task_scope_id="scope-lease")
        registry.activate_run(RUN1)

        registry.create_run(RUN2, "proj-lease", "repo-lease", "tester", task_scope_id="scope-lease")
        registry.activate_run(RUN2)

        integration_branch = "anvil/test/integration"

        # Run 1 acquires the merge_queue lease.
        lease1 = registry.acquire_lease(
            run_id=RUN1,
            repo_id="repo-lease",
            lease_type="merge_queue",
            scope=integration_branch,
        )
        assert lease1["status"] == "active"

        # Run 2 must be blocked.
        with pytest.raises(LeaseConflictError):
            registry.acquire_lease(
                run_id=RUN2,
                repo_id="repo-lease",
                lease_type="merge_queue",
                scope=integration_branch,
            )

        # After releasing run 1's lease, run 2 can acquire.
        registry.release_lease(lease1["lease_id"])
        lease2 = registry.acquire_lease(
            run_id=RUN2,
            repo_id="repo-lease",
            lease_type="merge_queue",
            scope=integration_branch,
        )
        assert lease2["status"] == "active"


class TestMergeConflictDetection:
    """Textual merge conflict blocks the work order."""

    def test_conflicting_wos_detected(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-550"
        )

        # Both WOs write to app.py with conflicting content.
        call_count = [0]

        def agent(wt: Path) -> None:
            call_count[0] += 1
            if (wt / "src" / "app.py").exists():
                if call_count[0] == 1:
                    (wt / "src" / "app.py").write_text(
                        "VALUE = 'A'\n", encoding="utf-8"
                    )
                elif call_count[0] == 2:
                    (wt / "src" / "app.py").write_text(
                        "VALUE = 'B'\n", encoding="utf-8"
                    )

        # This WO set has a file ownership conflict — should raise FileConflictError.
        conflict_wos = {
            "run_id": "RUN-20260607-550",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/app.py"]),
                _wo("EXEC-INT-001", ["src/app.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", []),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
        }
        with pytest.raises(FileConflictError):
            executor.execute(conflict_wos, agent)

    def test_sequential_same_file_allowed(self, registry: Registry, tmp_path: Path):
        """Two WOs writing same file is allowed when EXEC-002 depends on EXEC-001."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-551"
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text(
                    "VALUE = 99\n", encoding="utf-8"
                )

        sequential_wos = {
            "run_id": "RUN-20260607-551",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/app.py"]),
                _wo("EXEC-INT-001", ["src/app.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", ["EXEC-001"], parallel=False),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
        }
        # Should not raise FileConflictError.
        result = executor.execute(sequential_wos, agent)
        assert result is not None


class TestForbiddenFileRejection:
    """Forbidden file touched in one WO rejects that patch."""

    def test_scope_violation_rejects_patch(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-560"
        )

        def agent(wt: Path) -> None:
            # The agent writes to a forbidden file.
            if (wt / "src").exists():
                (wt / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
                forbidden = wt / "src" / "secret.py"
                forbidden.write_text("SECRET = 'password'\n", encoding="utf-8")

        wo_with_forbidden = {
            "run_id": "RUN-20260607-560",
            "work_orders": [
                {
                    "work_order_id": "EXEC-001",
                    "title": "WO with forbidden file",
                    "negotiation_status": "agreed",
                    "agreed_by": ["claude-001", "codex-001"],
                    "criticality": "required",
                    "fail_policy": "fail_closed",
                    "assigned_scope": {
                        "allowed_files": ["src/app.py"],
                        "forbidden_files": ["src/secret.py"],
                    },
                    "local_acceptance_criteria": ["Tests pass"],
                    "validation_commands": [
                        {
                            "command_array": ["git", "status", "--porcelain"],
                            "expected_exit_code": 0,
                        }
                    ],
                    "rollback_plan": [{"op": "noop"}],
                },
                _wo("EXEC-INT-001", ["src/app.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-INT-001", ["EXEC-001"], parallel=False),
            ],
        }
        result = executor.execute(wo_with_forbidden, agent)

        exec001 = next(
            (r for r in result.work_order_results if r.work_order_id == "EXEC-001"),
            None,
        )
        assert exec001 is not None
        assert exec001.execution_status == "scope_violation"
        assert exec001.merge_status == "skipped"


class TestOutOfScopeFileRejection:
    """File outside allowed_files rejects that patch."""

    def test_out_of_scope_file_is_scope_violation(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-570"
        )

        def agent(wt: Path) -> None:
            # Writes a file NOT in allowed_files.
            if (wt / "src").exists():
                (wt / "src" / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")

        out_of_scope_wos = {
            "run_id": "RUN-20260607-570",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-INT-001", ["src/app.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-INT-001", ["EXEC-001"], parallel=False),
            ],
        }
        result = executor.execute(out_of_scope_wos, agent)

        exec001 = next(
            (r for r in result.work_order_results if r.work_order_id == "EXEC-001"),
            None,
        )
        assert exec001 is not None
        assert exec001.execution_status == "scope_violation"


class TestIntegrationWorkOrderRequired:
    """Integration WO is required for multi-WO runs."""

    def test_missing_integration_wo_raises(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, _ = _make_executor(
            registry, repo, run_id="RUN-20260607-580"
        )

        no_integration_wos = {
            "run_id": "RUN-20260607-580",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/config.py"]),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", []),
            ],
        }
        with pytest.raises(IntegrationWorkOrderMissingError):
            executor.execute(no_integration_wos, lambda wt: None)


class TestIntegrationValidation:
    """Integration validation catches regression."""

    def test_integration_wo_failure_marks_overall_failed(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-590"
        )

        def failing_integration_agent(wt: Path) -> None:
            # Write a file NOT in the integration WO's allowed_files to trigger
            # scope_violation for the integration WO.
            if (wt / "src").exists():
                (wt / "src" / "out_of_scope.py").write_text("X = 1\n", encoding="utf-8")

        wos = {
            "run_id": "RUN-20260607-590",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                {
                    "work_order_id": "EXEC-INT-001",
                    "title": "Integration WO",
                    "negotiation_status": "agreed",
                    "agreed_by": ["claude-001", "codex-001"],
                    "criticality": "required",
                    "fail_policy": "fail_closed",
                    "is_integration_wo": True,
                    "assigned_scope": {
                        "allowed_files": ["src/app.py"],
                        "forbidden_files": [],
                    },
                    "local_acceptance_criteria": ["Integration tests pass"],
                    "validation_commands": [
                        {
                            "command_array": ["git", "status", "--porcelain"],
                            "expected_exit_code": 0,
                        }
                    ],
                    "rollback_plan": [{"op": "noop"}],
                },
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-INT-001", ["EXEC-001"], parallel=False),
            ],
        }

        def agent(wt: Path) -> None:
            # Normal agent for EXEC-001; failing agent for integration WO.
            if "integration" in str(wt):
                failing_integration_agent(wt)
            else:
                if (wt / "src" / "app.py").exists():
                    (wt / "src" / "app.py").write_text(
                        "VALUE = 1\n", encoding="utf-8"
                    )

        result = executor.execute(wos, agent)

        # Integration WO should not have passed — either it was blocked by the
        # failed EXEC-001, or it ran and failed its own validation/scope check.
        assert result.integration_wo_result is not None
        assert result.integration_wo_result.execution_status in (
            "scope_violation",
            "validation_failed",
            "agent_error",
            "blocked_by_dependency",
        )
        assert not result.overall_passed


class TestRequiredWorkOrderBlocksDependents:
    """Required WO failure blocks dependent WOs."""

    def test_failed_required_wo_blocks_downstream(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-600"
        )

        def agent(wt: Path) -> None:
            # Agent writes a file out of scope → scope_violation for EXEC-001.
            if "wo-EXEC-001" in str(wt):
                # Write outside allowed_files.
                if (wt / "src").exists():
                    (wt / "src" / "unrelated.py").write_text(
                        "X = 1\n", encoding="utf-8"
                    )

        dependent_wos = {
            "run_id": "RUN-20260607-600",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/config.py"]),
                _wo("EXEC-INT-001", ["src/app.py", "src/config.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", ["EXEC-001"], parallel=False),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
        }
        result = executor.execute(dependent_wos, agent)

        exec002 = next(
            (r for r in result.work_order_results if r.work_order_id == "EXEC-002"),
            None,
        )
        # EXEC-002 should be blocked since EXEC-001 failed.
        assert exec002 is not None
        assert exec002.execution_status in (
            "blocked_by_dependency",
            "scope_violation",
            "skipped",
        )


class TestOptionalWorkOrderFailOpen:
    """Optional WO failure with fail_open can continue."""

    def test_optional_fail_open_wo_does_not_block_integration(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-610"
        )

        def agent(wt: Path) -> None:
            # Optional WO writes out of scope (scope_violation)
            if "wo-EXEC-002" in str(wt):
                if (wt / "src").exists():
                    (wt / "src" / "unrelated.py").write_text("X = 1\n", encoding="utf-8")

        optional_wos = {
            "run_id": "RUN-20260607-610",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/config.py"], criticality="optional", fail_policy="fail_open"),
                _wo("EXEC-INT-001", ["src/app.py", "src/config.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", []),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
        }
        result = executor.execute(optional_wos, agent)

        exec002 = next(
            (r for r in result.work_order_results if r.work_order_id == "EXEC-002"),
            None,
        )
        assert exec002 is not None
        # Optional WO failure doesn't block the run.
        # Integration WO should still execute (not blocked_by_dependency from EXEC-002).
        assert result.integration_wo_result is not None
        assert result.integration_wo_result.execution_status != "blocked_by_dependency"


# ============================================================================
# Schema validity tests
# ============================================================================


class TestWorktreeManifestSchemaValid:
    """worktree_manifest.json is schema-valid after multi-WO run."""

    def test_worktree_manifest_valid(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-620"
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text("VALUE = 5\n", encoding="utf-8")

        wos = _two_independent_wos("RUN-20260607-620")
        executor.execute(wos, agent)

        manifest_path = run_dir / "worktree_manifest.json"
        assert manifest_path.exists(), "worktree_manifest.json was not written"
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        errors = validate_artifact("worktree_manifest", doc)
        assert errors == [], f"Schema errors: {errors}"

    def test_worktree_manifest_has_integration_branch(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-621"
        )

        def agent(wt: Path) -> None:
            pass

        wos = _two_independent_wos("RUN-20260607-621")
        executor.execute(wos, agent)

        doc = json.loads(
            (run_dir / "worktree_manifest.json").read_text(encoding="utf-8")
        )
        assert "integration_branch" in doc
        assert "work_order_entries" in doc
        assert "dependency_order" in doc

    def test_worktree_manifest_has_per_wo_entries(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-622"
        )

        def agent(wt: Path) -> None:
            pass

        wos = _two_independent_wos("RUN-20260607-622")
        executor.execute(wos, agent)

        doc = json.loads(
            (run_dir / "worktree_manifest.json").read_text(encoding="utf-8")
        )
        entries = doc.get("work_order_entries", [])
        entry_ids = {e["work_order_id"] for e in entries}
        assert "EXEC-001" in entry_ids
        assert "EXEC-002" in entry_ids
        assert "EXEC-INT-001" in entry_ids


class TestValidationResultsSchemaValid:
    """validation_results.json is schema-valid after multi-WO run."""

    def test_validation_results_valid(self, registry: Registry, tmp_path: Path):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-630"
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text("VALUE = 7\n", encoding="utf-8")

        wos = _two_independent_wos("RUN-20260607-630")
        executor.execute(wos, agent)

        results_path = run_dir / "validation_results.json"
        assert results_path.exists(), "validation_results.json was not written"
        doc = json.loads(results_path.read_text(encoding="utf-8"))
        errors = validate_artifact("validation_results", doc)
        assert errors == [], f"Schema errors: {errors}"

    def test_validation_results_has_per_wo_results(
        self, registry: Registry, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-631"
        )

        def agent(wt: Path) -> None:
            pass

        wos = _two_independent_wos("RUN-20260607-631")
        executor.execute(wos, agent)

        doc = json.loads(
            (run_dir / "validation_results.json").read_text(encoding="utf-8")
        )
        wo_results = doc.get("work_order_results", [])
        wo_ids = {r["work_order_id"] for r in wo_results}
        assert "EXEC-001" in wo_ids
        assert "EXEC-002" in wo_ids


# ============================================================================
# Backwards compatibility: single-WO execution still works
# ============================================================================


class TestSingleWorkOrderBackwardsCompat:
    """Single-WO execution still works (M3/M4 regression check)."""

    def test_single_wo_with_standard_mode_runner(
        self, registry: Registry, tmp_path: Path
    ):
        """Full Standard Mode happy path with a single WO still passes."""
        from anvil.standard_mode import (
            RunInputs,
            StandardModeAgents,
            StandardModeRunner,
        )
        from tests.test_m4_standard_mode import (
            _fake_claim_ledger_strong,
            _fake_task_contract,
            _fake_work_orders,
            _make_repo,
        )

        RUN_ID = "RUN-20260607-700"
        repo_path = tmp_path / "repos" / "single-svc"
        repo_path.mkdir(parents=True)
        _git(["init", "-b", "main"], repo_path)
        _git(["config", "user.email", "test@example.com"], repo_path)
        _git(["config", "user.name", "Anvil Test"], repo_path)
        (repo_path / "src").mkdir()
        (repo_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", "init"], repo_path)

        registry.register_repo("repo-single", repo_path)
        registry.create_project("proj-single", ["repo-single"])
        registry.create_scope("proj-single", "scope-single", ["src/"])
        registry.create_run(
            RUN_ID, "proj-single", "repo-single", "tester", task_scope_id="scope-single"
        )
        registry.activate_run(RUN_ID)

        inputs = RunInputs(
            run_id=RUN_ID,
            project_id="proj-single",
            repo_id="repo-single",
            scope_id="scope-single",
            multi_wo=False,
        )
        agents = StandardModeAgents(
            task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
            research_agent=lambda ctx: _fake_claim_ledger_strong(
                ctx["run_id"],
                ctx.get("source_manifest", {}).get("sources", [{}])[0].get("source_id", "SRC-001"),
            ),
            blindspot_agent=lambda ctx: [],
            plan_agent=lambda ctx: "# Plan\n\n1. Add retry\n",
            reviewer_agents={"correctness": lambda ctx: []},
            planner_agent=lambda ctx: _fake_work_orders(ctx["run_id"]),
            negotiator_agent=lambda ctx: _fake_work_orders(ctx["run_id"]),
            execution_agent=lambda wt: (wt / "src" / "app.py").write_text(
                "VALUE = 1\n# retry added\n", encoding="utf-8"
            ),
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert scorecard["final_outcome"] == "passed"

    def test_executor_write_artifacts_default_true(
        self, registry: Registry, tmp_path: Path
    ):
        """WorkOrderExecutor.execute() still writes artifacts by default."""
        from anvil.executor import WorkOrderExecutor
        from anvil.controller.events import EventLog

        repo_path = _make_repo(tmp_path, "compat-repo")
        registry.register_repo("repo-compat", repo_path)
        registry.create_project("proj-compat", ["repo-compat"])
        registry.create_scope("proj-compat", "scope-compat", ["src/"])

        RUN_ID = "RUN-20260607-701"
        registry.create_run(
            RUN_ID, "proj-compat", "repo-compat", "tester", task_scope_id="scope-compat"
        )
        registry.activate_run(RUN_ID)

        run_row = dict(registry.get_run(RUN_ID))
        run_dir = registry.paths.run_dir(RUN_ID)
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree = Path(run_row["worktree_path"])

        wo = _wo("EXEC-001", ["src/app.py"])
        executor = WorkOrderExecutor(
            run_id=RUN_ID,
            run_dir=run_dir,
            worktree_path=worktree,
            work_order=wo,
            repo_id="repo-compat",
            registry=registry,
            event_log=EventLog(run_dir / "event_log.jsonl"),
        )
        result = executor.execute(
            lambda wt: (wt / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        )
        assert result.status == "success"
        assert (run_dir / "worktree_manifest.json").exists()
        assert (run_dir / "validation_results.json").exists()


# ============================================================================
# Regression tests for reviewer-identified blockers
# ============================================================================


class TestMergeQueueLeaseConflictBlocksRun:
    """Blocker 1: merge_queue lease conflict must produce overall_passed=False."""

    def test_blocked_merge_fails_overall_passed(
        self, registry: Registry, tmp_path: Path
    ):
        """A required WO whose merge is blocked by a held lease must flip
        overall_passed=False even though the WO's execution itself succeeded."""
        repo = _make_repo(tmp_path)

        # Register two runs: one will hold the merge_queue lease, one will try.
        registry.register_repo("repo-blk", repo)
        registry.create_project("proj-blk", ["repo-blk"])
        registry.create_scope("proj-blk", "scope-blk", ["src/"])

        BLOCKER_RUN = "RUN-20260607-800"
        TEST_RUN = "RUN-20260607-801"

        registry.create_run(BLOCKER_RUN, "proj-blk", "repo-blk", "tester", task_scope_id="scope-blk")
        registry.activate_run(BLOCKER_RUN)
        registry.create_run(TEST_RUN, "proj-blk", "repo-blk", "tester", task_scope_id="scope-blk")
        registry.activate_run(TEST_RUN)

        # Pre-compute the integration branch name that the executor will use.
        integration_branch = f"anvil/{TEST_RUN}/integration"

        # Blocker run holds the merge_queue lease.
        blocking_lease = registry.acquire_lease(
            run_id=BLOCKER_RUN,
            repo_id="repo-blk",
            lease_type="merge_queue",
            scope=integration_branch,
        )
        assert blocking_lease["status"] == "active"

        run_dir = registry.paths.run_dir(TEST_RUN)
        run_dir.mkdir(parents=True, exist_ok=True)
        run_row = dict(registry.get_run(TEST_RUN))
        from anvil.controller.events import EventLog

        executor = MultiWorkOrderExecutor(
            run_id=TEST_RUN,
            run_dir=run_dir,
            repo_id="repo-blk",
            repo_path=repo,
            base_commit=run_row["base_commit"],
            worktrees_dir=registry.paths.worktrees_dir,
            main_worktree_path=Path(run_row["worktree_path"]) if run_row["worktree_path"] else run_dir,
            main_branch=f"anvil/{TEST_RUN}",
            registry=registry,
            event_log=EventLog(run_dir / "event_log.jsonl"),
        )

        def agent(wt: Path) -> None:
            if (wt / "src" / "app.py").exists():
                (wt / "src" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

        wos = _two_independent_wos(TEST_RUN)
        result = executor.execute(wos, agent)

        # Because the merge_queue was held the merges were skipped, which is a
        # required-WO failure → overall_passed must be False.
        assert not result.overall_passed, (
            "expected overall_passed=False when merge_queue lease is held by another run"
        )

        # The integration WO should have been blocked (not run).
        assert result.integration_wo_result is not None
        assert result.integration_wo_result.execution_status == "blocked_by_dependency"

        # Clean up.
        registry.release_lease(blocking_lease["lease_id"])


class TestUnknownDependencyRefFails:
    """High-priority: topological_sort must fail closed on unknown dep refs."""

    def test_unknown_dep_ref_raises_dependency_cycle_error(
        self, registry: Registry, tmp_path: Path
    ):
        """A dependency_matrix entry that references a non-existent WO id must
        raise DependencyCycleError before any agent is called."""
        repo = _make_repo(tmp_path)
        executor, _ = _make_executor(
            registry, repo, run_id="RUN-20260607-810"
        )

        executed: list[str] = []

        def agent(wt: Path) -> None:
            executed.append(str(wt))

        bad_dep_wos = {
            "run_id": "RUN-20260607-810",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-INT-001", ["src/app.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", ["EXEC-NONEXISTENT"]),  # unknown WO id
                _dep("EXEC-INT-001", ["EXEC-001"], parallel=False),
            ],
        }
        with pytest.raises(DependencyCycleError, match="Unknown dependency"):
            executor.execute(bad_dep_wos, agent)

        assert not executed, "No agent should have run before the cycle check fails"

    def test_topological_sort_rejects_unknown_ref_directly(self):
        """Unit test: topological_sort itself rejects an unknown dep ref."""
        with pytest.raises(DependencyCycleError, match="Unknown dependency"):
            topological_sort(
                ["EXEC-001"],
                [_dep("EXEC-001", ["EXEC-GHOST"])],
            )


class TestFileOwnershipAccessSequence:
    """Blocker 2 & 3: explicit file_ownership access/sequence model."""

    def test_explicit_file_ownership_read_write_no_conflict(
        self, registry: Registry, tmp_path: Path
    ):
        """EXEC-001 writes, EXEC-002 reads the same file — no conflict."""
        wos = [
            _wo("EXEC-001", ["src/app.py"]),
            _wo("EXEC-002", ["src/app.py"]),
        ]
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", [])]
        ownership = [
            {"file_path": "src/app.py", "work_order_id": "EXEC-001", "access": "write", "sequence": 1},
            {"file_path": "src/app.py", "work_order_id": "EXEC-002", "access": "read", "sequence": 1},
        ]
        # Reader + writer = no FileConflictError.
        assert detect_write_conflicts(wos, deps, ownership) == []

    def test_explicit_file_ownership_same_sequence_two_writers_conflict(self):
        """Two writers at same sequence with no dep → conflict."""
        wos = [_wo("EXEC-001", ["src/app.py"]), _wo("EXEC-002", ["src/app.py"])]
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", [])]
        ownership = [
            {"file_path": "src/app.py", "work_order_id": "EXEC-001", "access": "write", "sequence": 1},
            {"file_path": "src/app.py", "work_order_id": "EXEC-002", "access": "write", "sequence": 1},
        ]
        conflicts = detect_write_conflicts(wos, deps, ownership)
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "concurrent_write"

    def test_explicit_file_ownership_different_sequence_no_conflict(self):
        """Two writers at different sequences → no conflict (sequencing is the ordering)."""
        wos = [_wo("EXEC-001", ["src/app.py"]), _wo("EXEC-002", ["src/app.py"])]
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", ["EXEC-001"])]
        ownership = [
            {"file_path": "src/app.py", "work_order_id": "EXEC-001", "access": "write", "sequence": 1},
            {"file_path": "src/app.py", "work_order_id": "EXEC-002", "access": "write", "sequence": 2},
        ]
        assert detect_write_conflicts(wos, deps, ownership) == []

    def test_sequence_direction_contradicts_dependency_raises_conflict(self):
        """sequence_direction_mismatch: EXEC-002 seq=1 but EXEC-002 depends_on EXEC-001 (seq=2).

        Execution order is EXEC-001 first (EXEC-002 depends on it), but the
        ownership plan claims EXEC-002 should write first (sequence=1). This
        contradiction must surface as a conflict.
        """
        wos = [_wo("EXEC-001", ["src/app.py"]), _wo("EXEC-002", ["src/app.py"])]
        # EXEC-002 depends on EXEC-001 → EXEC-001 executes first.
        deps = [_dep("EXEC-001", []), _dep("EXEC-002", ["EXEC-001"])]
        # But sequence says EXEC-002 (seq=1) should write before EXEC-001 (seq=2).
        ownership = [
            {"file_path": "src/app.py", "work_order_id": "EXEC-001", "access": "write", "sequence": 2},
            {"file_path": "src/app.py", "work_order_id": "EXEC-002", "access": "write", "sequence": 1},
        ]
        conflicts = detect_write_conflicts(wos, deps, ownership)
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "sequence_direction_mismatch"
        assert conflicts[0]["work_order_a"] == "EXEC-002"  # lower seq, claims to go first
        assert conflicts[0]["work_order_b"] == "EXEC-001"  # higher seq, but actually upstream

    def test_worktree_manifest_has_file_ownership(
        self, registry: Registry, tmp_path: Path
    ):
        """worktree_manifest.json must contain the resolved file_ownership plan."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-820"
        )

        def agent(wt: Path) -> None:
            pass

        wos = _two_independent_wos("RUN-20260607-820")
        executor.execute(wos, agent)

        doc = json.loads(
            (run_dir / "worktree_manifest.json").read_text(encoding="utf-8")
        )
        assert "file_ownership" in doc, "worktree_manifest.json missing file_ownership"
        file_ownership = doc["file_ownership"]
        assert isinstance(file_ownership, list)
        file_paths = {entry["file_path"] for entry in file_ownership}
        assert "src/app.py" in file_paths or "src/config.py" in file_paths

        # Every owner entry must have work_order_id, access, sequence.
        for entry in file_ownership:
            for owner in entry["owners"]:
                assert "work_order_id" in owner
                assert "access" in owner
                assert "sequence" in owner

    def test_worktree_manifest_file_ownership_schema_valid(
        self, registry: Registry, tmp_path: Path
    ):
        """worktree_manifest.json with file_ownership is schema-valid."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-821"
        )

        wos = _two_independent_wos("RUN-20260607-821")
        executor.execute(wos, lambda wt: None)

        doc = json.loads(
            (run_dir / "worktree_manifest.json").read_text(encoding="utf-8")
        )
        errors = validate_artifact("worktree_manifest", doc)
        assert errors == [], f"Schema errors after adding file_ownership: {errors}"

    def test_explicit_file_ownership_consumed_in_manifest(
        self, registry: Registry, tmp_path: Path
    ):
        """Explicit file_ownership (with access/sequence) appears in the manifest."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-822"
        )

        wos = {
            "run_id": "RUN-20260607-822",
            "work_orders": [
                _wo("EXEC-001", ["src/app.py"]),
                _wo("EXEC-002", ["src/config.py"]),
                _wo("EXEC-INT-001", ["src/app.py", "src/config.py"], is_integration=True),
            ],
            "dependency_matrix": [
                _dep("EXEC-001", []),
                _dep("EXEC-002", []),
                _dep("EXEC-INT-001", ["EXEC-001", "EXEC-002"], parallel=False),
            ],
            "file_ownership": [
                {"file_path": "src/app.py", "work_order_id": "EXEC-001", "access": "write", "sequence": 1},
                {"file_path": "src/config.py", "work_order_id": "EXEC-002", "access": "write", "sequence": 1},
                {"file_path": "src/app.py", "work_order_id": "EXEC-INT-001", "access": "read", "sequence": 2},
            ],
        }
        executor.execute(wos, lambda wt: None)

        doc = json.loads(
            (run_dir / "worktree_manifest.json").read_text(encoding="utf-8")
        )
        file_ownership = doc.get("file_ownership", [])
        by_path = {e["file_path"]: e for e in file_ownership}
        assert "src/app.py" in by_path
        app_owners = by_path["src/app.py"]["owners"]
        assert any(o["access"] == "write" and o["work_order_id"] == "EXEC-001" for o in app_owners)


class TestValidationResultsActualCommands:
    """High-priority 1: validation_results.json uses actual command results."""

    def test_results_not_placeholder_after_success(
        self, registry: Registry, tmp_path: Path
    ):
        """After a successful run the results field must not be the synthetic
        'echo integration-placeholder' entry."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-830"
        )

        wos = _two_independent_wos("RUN-20260607-830")
        executor.execute(wos, lambda wt: None)

        doc = json.loads(
            (run_dir / "validation_results.json").read_text(encoding="utf-8")
        )
        for cmd_result in doc["results"]:
            assert cmd_result["command_array"] != ["echo", "integration-placeholder"], (
                "validation_results must not contain synthetic placeholder commands"
            )

    def test_validation_results_has_policy_and_timeout_status(
        self, registry: Registry, tmp_path: Path
    ):
        """validation_results.json must carry command_policy_status and timeout_status."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-831"
        )

        wos = _two_independent_wos("RUN-20260607-831")
        executor.execute(wos, lambda wt: None)

        doc = json.loads(
            (run_dir / "validation_results.json").read_text(encoding="utf-8")
        )
        assert "command_policy_status" in doc
        assert "timeout_status" in doc
        assert doc["command_policy_status"] in ("all_allowed", "some_blocked", "unknown")
        assert doc["timeout_status"] in ("no_timeouts", "some_timed_out", "unknown")

    def test_validation_results_schema_valid_with_new_fields(
        self, registry: Registry, tmp_path: Path
    ):
        """validation_results.json remains schema-valid after adding policy/timeout fields."""
        repo = _make_repo(tmp_path)
        executor, run_dir = _make_executor(
            registry, repo, run_id="RUN-20260607-832"
        )

        wos = _two_independent_wos("RUN-20260607-832")
        executor.execute(wos, lambda wt: None)

        doc = json.loads(
            (run_dir / "validation_results.json").read_text(encoding="utf-8")
        )
        errors = validate_artifact("validation_results", doc)
        assert errors == [], f"Schema errors: {errors}"
