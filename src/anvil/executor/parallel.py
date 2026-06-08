"""Multi-work-order parallel executor (Milestone 5).

Extends the single-work-order WorkOrderExecutor with:

  1. Topological sort of work orders by dependency_matrix.
  2. Per-work-order isolated git worktrees.
  3. Integration branch management (anvil/{run_id}/integration).
  4. Merge queue lease (exclusive per-repo) guarding merges into the
     integration branch.
  5. Conflict detection: textual git conflicts, scope violations, forbidden
     files — all block the offending work order and record structured issues.
  6. Integration work order: required for multi-WO runs; runs after all regular
     WOs are merged into the integration branch.
  7. Mode-sensitive failure recovery: required WO failure fails closed and
     blocks dependent WOs; optional WO failure respects fail_policy.

Safety boundary: no proprietary code, internal project names, or internal
paths.  All examples and test fixtures must be synthetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import gitutils
from ..controller.events import EventLog
from ..controller.policy import CommandPolicy
from ..errors import AnvilError
from ..timeutil import now_iso
from . import WorkOrderExecutor


class DependencyCycleError(AnvilError):
    """Raised when the dependency_matrix contains a cycle."""


class IntegrationWorkOrderMissingError(AnvilError):
    """Raised when a multi-WO run has no integration work order."""


class FileConflictError(AnvilError):
    """Raised when two work orders conflict on file ownership."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        self.conflicts = conflicts
        super().__init__(f"File ownership conflicts detected: {conflicts}")


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def topological_sort(
    work_order_ids: list[str],
    dependency_matrix: list[dict[str, Any]],
) -> list[list[str]]:
    """Return execution waves: each inner list contains WOs that may run
    independently of each other (no dependency between them in a wave).

    Raises :class:`DependencyCycleError` if a cycle is detected.
    """
    dep_map: dict[str, set[str]] = {wo_id: set() for wo_id in work_order_ids}
    for entry in dependency_matrix:
        wo_id = entry["work_order_id"]
        if wo_id not in dep_map:
            continue
        for dep in entry.get("depends_on", []):
            if dep not in dep_map:
                raise DependencyCycleError(
                    f"Unknown dependency '{dep}' referenced by work order '{wo_id}'. "
                    f"Known work order IDs: {sorted(dep_map)}"
                )
            dep_map[wo_id].add(dep)

    waves: list[list[str]] = []
    completed: set[str] = set()
    remaining = set(work_order_ids)
    guard = 0

    while remaining:
        guard += 1
        if guard > len(work_order_ids) + 1:
            cycle_members = sorted(remaining)
            raise DependencyCycleError(
                f"Dependency cycle detected among work orders: {cycle_members}"
            )
        wave = sorted(
            wo_id
            for wo_id in remaining
            if dep_map[wo_id].issubset(completed)
        )
        if not wave:
            cycle_members = sorted(remaining)
            raise DependencyCycleError(
                f"Dependency cycle detected among work orders: {cycle_members}"
            )
        waves.append(wave)
        completed.update(wave)
        remaining -= set(wave)

    return waves


# ---------------------------------------------------------------------------
# File ownership conflict detection
# ---------------------------------------------------------------------------


def detect_write_conflicts(
    work_orders: list[dict[str, Any]],
    dependency_matrix: list[dict[str, Any]],
    file_ownership: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detect file ownership conflicts across work orders.

    Rules:
      - Two WOs that both *write* the same file at the same sequence number
        and neither depends on the other → conflict (``concurrent_write``).
      - Two WOs that both *write* the same file at different sequence numbers
        with no dependency ordering → conflict (``sequence_order_mismatch``).

    Args:
        work_orders: List of work order dicts (used for fallback if no
            explicit ``file_ownership`` is provided).
        dependency_matrix: Dependency edges.
        file_ownership: Optional explicit ownership plan from the top-level
            work orders dict.  Each entry: ``{file_path, work_order_id,
            access?, sequence?}``.  When provided, this is the authoritative
            source; each ``allowed_files`` entry in the WO's ``assigned_scope``
            is **ignored**.  When ``None``, every ``allowed_files`` entry is
            treated as ``access="write", sequence=1``.

    Returns a list of conflict descriptors (empty if no conflicts).
    """
    dep_map: dict[str, set[str]] = {}
    for entry in dependency_matrix:
        wo_id = entry["work_order_id"]
        dep_map[wo_id] = set(entry.get("depends_on", []))

    def depends_on_transitively(a: str, b: str) -> bool:
        """True if work order *a* has *b* in its transitive dependency set."""
        visited: set[str] = set()
        queue = list(dep_map.get(a, set()))
        while queue:
            node = queue.pop()
            if node == b:
                return True
            if node not in visited:
                visited.add(node)
                queue.extend(dep_map.get(node, set()))
        return False

    # Build file → owner entries from explicit file_ownership when provided,
    # otherwise fall back to treating every allowed_file as write/sequence=1.
    file_to_owners: dict[str, list[dict[str, Any]]] = {}
    if file_ownership:
        for entry in file_ownership:
            fp = entry["file_path"]
            file_to_owners.setdefault(fp, [])
            file_to_owners[fp].append({
                "work_order_id": entry["work_order_id"],
                "access": entry.get("access", "write"),
                "sequence": entry.get("sequence", 1),
            })
    else:
        for wo in work_orders:
            wo_id = wo["work_order_id"]
            scope = wo.get("assigned_scope", {})
            for f in scope.get("allowed_files", []):
                file_to_owners.setdefault(f, [])
                file_to_owners[f].append({"work_order_id": wo_id, "access": "write", "sequence": 1})

    conflicts: list[dict[str, Any]] = []
    for file_path, owners in file_to_owners.items():
        writers = [o for o in owners if o["access"] == "write"]
        for i in range(len(writers)):
            for j in range(i + 1, len(writers)):
                a = writers[i]
                b = writers[j]
                a_id, b_id = a["work_order_id"], b["work_order_id"]
                has_ordering = (
                    depends_on_transitively(a_id, b_id)
                    or depends_on_transitively(b_id, a_id)
                )
                if a["sequence"] == b["sequence"]:
                    # Same sequence + same file + both write + no ordering → conflict.
                    if not has_ordering:
                        conflicts.append({
                            "file": file_path,
                            "work_order_a": a_id,
                            "work_order_b": b_id,
                            "type": "concurrent_write",
                        })
                else:
                    # Different sequences — ordering must exist; if not it is a
                    # sequence/dependency mismatch.
                    first_id = a_id if a["sequence"] < b["sequence"] else b_id
                    second_id = b_id if a["sequence"] < b["sequence"] else a_id
                    if not has_ordering:
                        conflicts.append({
                            "file": file_path,
                            "work_order_a": first_id,
                            "work_order_b": second_id,
                            "type": "sequence_order_mismatch",
                        })
                    elif not depends_on_transitively(second_id, first_id):
                        # Dependency ordering contradicts ownership sequence:
                        # the lower-sequence WO must be the upstream dependency.
                        conflicts.append({
                            "file": file_path,
                            "work_order_a": first_id,
                            "work_order_b": second_id,
                            "type": "sequence_direction_mismatch",
                        })
    return conflicts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_command_results(command_results: list[Any]) -> list[dict[str, Any]]:
    """Serialize CommandResult objects to plain dicts for storage."""
    out: list[dict[str, Any]] = []
    for r in command_results:
        out.append({
            "command_array": r.command_array,
            "exit_code": r.exit_code,
            "passed": r.passed,
            "stdout_excerpt": r.stdout_excerpt[:2000],
            "stderr_excerpt": r.stderr_excerpt[:2000],
            "timed_out": r.timed_out,
            "policy_allowed": r.policy_allowed,
        })
    return out


# ---------------------------------------------------------------------------
# Per-WO and aggregate result types
# ---------------------------------------------------------------------------


@dataclass
class WorkOrderMergeResult:
    work_order_id: str
    execution_status: str = "pending"
    merge_status: str = "pending"
    validation_status: str = "pending"
    rollback_status: str = "not_needed"
    touched_files: list[str] = field(default_factory=list)
    branch: str = ""
    worktree_path: str = ""
    merge_conflict_files: list[str] = field(default_factory=list)
    error: str | None = None
    new_failures: int = 0
    # Serialised CommandResult dicts from the WO's validation run.
    command_results: list[dict[str, Any]] = field(default_factory=list)

    def to_entry_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "work_order_id": self.work_order_id,
            "execution_status": self.execution_status,
            "merge_status": self.merge_status,
            "validation_status": self.validation_status,
            "rollback_status": self.rollback_status,
            "touched_files": self.touched_files,
        }
        if self.branch:
            d["branch"] = self.branch
        if self.worktree_path:
            d["worktree_path"] = self.worktree_path
        if self.merge_conflict_files:
            d["merge_conflict_detail"] = f"Conflicts in: {self.merge_conflict_files}"
        return d


@dataclass
class MultiExecutionResult:
    run_id: str
    integration_branch: str = ""
    integration_branch_ref: str = ""
    work_order_results: list[WorkOrderMergeResult] = field(default_factory=list)
    integration_wo_result: WorkOrderMergeResult | None = None
    overall_passed: bool = False
    error: str | None = None
    dependency_order: list[str] = field(default_factory=list)
    merge_conflict_details: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Multi-work-order executor
# ---------------------------------------------------------------------------


class MultiWorkOrderExecutor:
    """Orchestrates parallel work-order execution for Milestone 5.

    For each run:
      1. Validate: integration WO required for multi-WO runs.
      2. Detect file ownership conflicts (fail closed).
      3. Topologically sort work orders into dependency-ordered waves.
      4. Create an integration branch and worktree from base_commit.
      5. For each wave (in order), for each WO in the wave:
           a. Create a per-WO worktree/branch.
           b. Execute via WorkOrderExecutor.
           c. If success: commit changes, acquire merge_queue lease, merge to
              integration branch, release lease.
           d. If failure: block dependent WOs, record structured status.
      6. After all regular WOs: run integration WO in the integration worktree.
      7. Write extended worktree_manifest.json and validation_results.json.
    """

    def __init__(
        self,
        run_id: str,
        run_dir: Path,
        repo_id: str,
        repo_path: Path,
        base_commit: str,
        worktrees_dir: Path,
        main_worktree_path: Path,
        main_branch: str,
        registry: Any,
        event_log: EventLog,
        policy: CommandPolicy | None = None,
        baseline_tests: list[dict[str, Any]] | None = None,
    ) -> None:
        self._run_id = run_id
        self._run_dir = run_dir
        self._repo_id = repo_id
        self._repo_path = repo_path
        self._base_commit = base_commit
        self._worktrees_dir = worktrees_dir
        self._main_worktree = main_worktree_path
        self._main_branch = main_branch
        self._registry = registry
        self._event_log = event_log
        self._policy = policy or CommandPolicy.from_dict({})
        self._baseline_tests: list[dict[str, Any]] = baseline_tests or []

        self._integration_branch = f"anvil/{run_id}/integration"
        self._integration_wt_path = worktrees_dir / repo_id / f"{run_id}--integration"

        self._wo_branches: dict[str, str] = {}
        self._wo_worktree_paths: dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute(
        self,
        work_orders: dict[str, Any],
        execution_agent: Callable[[Path], None],
    ) -> MultiExecutionResult:
        """Execute all work orders; return a MultiExecutionResult."""
        result = MultiExecutionResult(run_id=self._run_id)
        wo_list = work_orders.get("work_orders", [])
        dep_matrix = work_orders.get("dependency_matrix", [])

        # Separate integration WO from regular WOs.
        integration_wos = [wo for wo in wo_list if wo.get("is_integration_wo")]
        regular_wos = [wo for wo in wo_list if not wo.get("is_integration_wo")]

        # Require an integration WO for multi-WO runs.
        if not integration_wos:
            raise IntegrationWorkOrderMissingError(
                "Multi-work-order runs require an integration work order "
                "(is_integration_wo: true). None found."
            )

        all_regular_ids = [wo["work_order_id"] for wo in regular_wos]
        integration_wo = integration_wos[0]
        int_wo_id = integration_wo["work_order_id"]

        # Detect file ownership conflicts among regular WOs.
        # Use explicit file_ownership plan when provided (M5 access/sequence model).
        conflicts = detect_write_conflicts(
            regular_wos, dep_matrix, work_orders.get("file_ownership")
        )
        if conflicts:
            raise FileConflictError(conflicts)

        # Topological sort (regular WOs only; integration WO runs last).
        try:
            waves = topological_sort(all_regular_ids, dep_matrix)
        except DependencyCycleError:
            raise

        result.dependency_order = [wo_id for wave in waves for wo_id in wave] + [int_wo_id]

        # Create integration branch + worktree.
        try:
            self._setup_integration_branch()
        except Exception as exc:
            result.error = f"Failed to create integration branch: {exc}"
            result.overall_passed = False
            return result

        result.integration_branch = self._integration_branch

        wo_map = {wo["work_order_id"]: wo for wo in wo_list}
        failed_required: set[str] = set()
        merge_conflicts: list[dict[str, Any]] = []

        # Execute each wave.
        for wave in waves:
            for wo_id in wave:
                wo = wo_map[wo_id]
                is_required = wo.get("criticality") == "required"
                fail_policy = wo.get("fail_policy", "fail_closed")

                # Block if a required dependency failed.
                deps = {
                    e["work_order_id"]: set(e.get("depends_on", []))
                    for e in dep_matrix
                }
                unmet_failures = failed_required & deps.get(wo_id, set())
                if unmet_failures:
                    wo_result = WorkOrderMergeResult(
                        work_order_id=wo_id,
                        execution_status="blocked_by_dependency",
                        merge_status="skipped",
                        validation_status="pending",
                        error=f"Blocked by failed required WO(s): {sorted(unmet_failures)}",
                    )
                    result.work_order_results.append(wo_result)
                    if is_required:
                        failed_required.add(wo_id)
                    self._event_log.append(
                        "execution_blocked",
                        details={"work_order_id": wo_id, "blocked_by": sorted(unmet_failures)},
                    )
                    continue

                wo_result = self._execute_single_wo(wo, execution_agent)
                result.work_order_results.append(wo_result)

                if wo_result.execution_status not in ("success",):
                    # Execution failed.
                    if is_required and fail_policy != "fail_open":
                        failed_required.add(wo_id)
                    continue

                # Merge into integration branch.
                self._do_merge(wo_result, merge_conflicts)

                if wo_result.merge_status == "conflict":
                    merge_conflicts.append({
                        "work_order_id": wo_id,
                        "conflict_files": wo_result.merge_conflict_files,
                    })
                    if is_required and fail_policy != "fail_open":
                        failed_required.add(wo_id)
                elif wo_result.merge_status in ("skipped", "rejected"):
                    # merge_queue lease conflict or hard merge failure is a
                    # required-WO failure — block the integration WO and ensure
                    # overall_passed is False even if execution itself succeeded.
                    if is_required and fail_policy != "fail_open":
                        failed_required.add(wo_id)

        result.merge_conflict_details = merge_conflicts

        # Run integration WO if no required failures block it.
        int_result = self._execute_integration_wo(
            integration_wo, execution_agent, failed_required
        )
        result.integration_wo_result = int_result

        # Update integration branch ref.
        try:
            result.integration_branch_ref = gitutils.rev_parse(
                self._integration_wt_path, "HEAD"
            )
        except Exception:
            result.integration_branch_ref = "0" * 40

        result.overall_passed = (
            not failed_required
            and not merge_conflicts
            and int_result.execution_status in ("success", "not_required")
        )

        # Write extended artifacts.
        self._write_worktree_manifest(result, work_orders)
        self._write_validation_results(result, work_orders)

        return result

    # ------------------------------------------------------------------
    # Integration branch setup
    # ------------------------------------------------------------------

    def _setup_integration_branch(self) -> None:
        """Create the integration branch and worktree from base_commit."""
        gitutils.add_worktree(
            self._repo_path,
            self._integration_wt_path,
            self._integration_branch,
            self._base_commit,
        )

    # ------------------------------------------------------------------
    # Single WO execution
    # ------------------------------------------------------------------

    def _execute_single_wo(
        self,
        work_order: dict[str, Any],
        execution_agent: Callable[[Path], None],
    ) -> WorkOrderMergeResult:
        wo_id = work_order["work_order_id"]
        safe_id = wo_id.replace("/", "-").replace(":", "-")
        wo_wt_path = self._worktrees_dir / self._repo_id / f"{self._run_id}--wo-{safe_id}"
        wo_branch = f"anvil/{self._run_id}/wo-{safe_id}"

        self._event_log.append(
            "execution_started",
            details={"work_order_id": wo_id, "branch": wo_branch},
        )

        wo_result = WorkOrderMergeResult(
            work_order_id=wo_id,
            branch=wo_branch,
            worktree_path=str(wo_wt_path),
        )

        # Create per-WO worktree from base_commit.
        try:
            gitutils.add_worktree(
                self._repo_path,
                wo_wt_path,
                wo_branch,
                self._base_commit,
            )
        except Exception as exc:
            wo_result.execution_status = "agent_error"
            wo_result.merge_status = "skipped"
            wo_result.error = f"Failed to create worktree: {exc}"
            return wo_result

        self._wo_branches[wo_id] = wo_branch
        self._wo_worktree_paths[wo_id] = wo_wt_path

        # Execute via the single-WO executor with artifact writing disabled.
        executor = WorkOrderExecutor(
            run_id=self._run_id,
            run_dir=self._run_dir,
            worktree_path=wo_wt_path,
            work_order=work_order,
            repo_id=self._repo_id,
            registry=self._registry,
            event_log=self._event_log,
            policy=self._policy,
            baseline_tests=self._baseline_tests,
        )
        exec_result = executor.execute(execution_agent, write_artifacts=False)

        wo_result.execution_status = exec_result.status
        wo_result.touched_files = exec_result.touched_files
        wo_result.new_failures = len(exec_result.new_failures)
        if exec_result.rollback_result is not None:
            wo_result.rollback_status = (
                "success" if exec_result.rollback_result.success else "failed"
            )
        wo_result.validation_status = (
            "passed" if exec_result.status == "success" else "failed"
        )
        if exec_result.error:
            wo_result.error = exec_result.error
        wo_result.command_results = _serialize_command_results(exec_result.command_results)

        if exec_result.status != "success":
            wo_result.merge_status = "skipped"
            return wo_result

        # Commit changes in WO worktree for merging.
        try:
            gitutils.commit_all(wo_wt_path, f"WO: {wo_id} — harness-generated")
        except Exception as exc:
            wo_result.execution_status = "agent_error"
            wo_result.merge_status = "skipped"
            wo_result.error = f"Post-execution commit failed: {exc}"

        return wo_result

    # ------------------------------------------------------------------
    # Merge step
    # ------------------------------------------------------------------

    def _do_merge(
        self,
        wo_result: WorkOrderMergeResult,
        existing_conflicts: list[dict[str, Any]],
    ) -> None:
        """Merge a successfully-executed WO into the integration branch."""
        wo_id = wo_result.work_order_id
        wo_branch = wo_result.branch

        # Acquire exclusive merge_queue lease before touching the integration branch.
        lease_id: str | None = None
        try:
            lease = self._registry.acquire_lease(
                run_id=self._run_id,
                repo_id=self._repo_id,
                lease_type="merge_queue",
                scope=self._integration_branch,
            )
            lease_id = lease["lease_id"]
        except Exception as exc:
            wo_result.merge_status = "skipped"
            wo_result.error = f"merge_queue lease conflict: {exc}"
            self._event_log.append(
                "execution_blocked",
                details={"work_order_id": wo_id, "reason": str(exc)},
            )
            return

        try:
            merge_result = gitutils.merge_branch_into(
                self._integration_wt_path,
                wo_branch,
                message=f"Merge WO {wo_id} into integration",
            )
        except Exception as exc:
            wo_result.merge_status = "rejected"
            wo_result.error = f"Merge attempt failed: {exc}"
            self._event_log.append(
                "execution_blocked",
                details={"work_order_id": wo_id, "error": str(exc)},
            )
            return
        finally:
            # Always release the lease — even if the merge raised.
            if lease_id is not None:
                try:
                    self._registry.release_lease(lease_id)
                except Exception:
                    pass

        if merge_result.success:
            wo_result.merge_status = "merged"
            self._event_log.append(
                "execution_success",
                details={"work_order_id": wo_id, "new_head": merge_result.new_head},
            )
        else:
            wo_result.merge_status = "conflict"
            wo_result.merge_conflict_files = merge_result.conflict_files
            self._event_log.append(
                "checkout_contaminated",
                details={
                    "work_order_id": wo_id,
                    "conflict_files": merge_result.conflict_files,
                },
            )

    # ------------------------------------------------------------------
    # Integration WO
    # ------------------------------------------------------------------

    def _execute_integration_wo(
        self,
        integration_wo: dict[str, Any],
        execution_agent: Callable[[Path], None],
        failed_required: set[str],
    ) -> WorkOrderMergeResult:
        int_wo_id = integration_wo["work_order_id"]
        result = WorkOrderMergeResult(
            work_order_id=int_wo_id,
            branch=self._integration_branch,
            worktree_path=str(self._integration_wt_path),
            merge_status="not_applicable",
        )

        if failed_required:
            result.execution_status = "blocked_by_dependency"
            result.error = f"Integration WO blocked by failed WOs: {sorted(failed_required)}"
            self._event_log.append(
                "execution_blocked",
                details={"work_order_id": int_wo_id, "reason": result.error},
            )
            return result

        self._event_log.append(
            "execution_started",
            details={"work_order_id": int_wo_id},
        )

        executor = WorkOrderExecutor(
            run_id=self._run_id,
            run_dir=self._run_dir,
            worktree_path=self._integration_wt_path,
            work_order=integration_wo,
            repo_id=self._repo_id,
            registry=self._registry,
            event_log=self._event_log,
            policy=self._policy,
            baseline_tests=self._baseline_tests,
        )
        exec_result = executor.execute(execution_agent, write_artifacts=False)

        result.execution_status = exec_result.status
        result.touched_files = exec_result.touched_files
        result.new_failures = len(exec_result.new_failures)
        if exec_result.rollback_result is not None:
            result.rollback_status = (
                "success" if exec_result.rollback_result.success else "failed"
            )
        result.validation_status = (
            "passed" if exec_result.status == "success" else "failed"
        )
        if exec_result.error:
            result.error = exec_result.error
        result.command_results = _serialize_command_results(exec_result.command_results)

        if exec_result.status == "success":
            self._event_log.append(
                "execution_success",
                details={"work_order_id": int_wo_id},
            )
        else:
            self._event_log.append(
                "execution_blocked",
                details={"work_order_id": int_wo_id, "status": exec_result.status},
            )

        return result

    # ------------------------------------------------------------------
    # Artifact writing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_manifest_file_ownership(work_orders: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert the work orders' file ownership plan to worktree_manifest format.

        The manifest ``file_ownership`` format groups owners per file:
        ``[{"file_path": "src/a.py", "owners": [{"work_order_id": "...",
           "access": "write", "sequence": 1}]}]``.

        Source priority:
        1. Explicit ``work_orders["file_ownership"]`` flat list (M5 model).
        2. Derived from each WO's ``assigned_scope.allowed_files`` (M3 fallback),
           treating every entry as ``access="write", sequence=1``.
        """
        from collections import defaultdict

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

        flat_ownership = work_orders.get("file_ownership", [])
        if flat_ownership:
            for entry in flat_ownership:
                grouped[entry["file_path"]].append({
                    "work_order_id": entry["work_order_id"],
                    "access": entry.get("access", "write"),
                    "sequence": entry.get("sequence", 1),
                })
        else:
            for wo in work_orders.get("work_orders", []):
                wo_id = wo["work_order_id"]
                for f in wo.get("assigned_scope", {}).get("allowed_files", []):
                    grouped[f].append({
                        "work_order_id": wo_id,
                        "access": "write",
                        "sequence": 1,
                    })

        return [{"file_path": fp, "owners": owners} for fp, owners in grouped.items()]

    def _write_worktree_manifest(
        self, result: MultiExecutionResult, work_orders: dict[str, Any]
    ) -> None:
        manifest_path = self._run_dir / "worktree_manifest.json"
        doc: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                doc = {}

        # Ensure required fields are present.
        doc.setdefault("run_id", self._run_id)
        doc.setdefault("schema_version", "1.0.0")
        doc.setdefault("created_at", now_iso())
        doc.setdefault("worktree_id", f"wt-{self._run_id}")
        doc.setdefault("base_repo", self._repo_id)
        doc.setdefault("base_commit", self._base_commit)
        doc.setdefault("branch", self._main_branch)
        doc.setdefault("path", str(self._main_worktree))
        doc.setdefault("status", "active")

        # M5 fields.
        doc["repo_id"] = self._repo_id
        doc["integration_branch"] = result.integration_branch
        if result.integration_branch_ref and len(result.integration_branch_ref) >= 7:
            doc["integration_branch_ref"] = result.integration_branch_ref
        doc["work_order_entries"] = [
            r.to_entry_dict() for r in result.work_order_results
        ]
        if result.integration_wo_result is not None:
            doc["work_order_entries"].append(result.integration_wo_result.to_entry_dict())
        doc["dependency_order"] = result.dependency_order
        doc["merge_conflict_details"] = result.merge_conflict_details
        # Populate resolved file ownership plan (M5 access/sequence model).
        doc["file_ownership"] = self._build_manifest_file_ownership(work_orders)

        manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def _write_validation_results(
        self, result: MultiExecutionResult, work_orders: dict[str, Any]
    ) -> None:
        results_path = self._run_dir / "validation_results.json"

        int_wo = result.integration_wo_result
        ref_id = int_wo.work_order_id if int_wo is not None else "EXEC-INT-001"

        # Use actual integration WO validation command results.  Fall back to a
        # minimal status entry only when no commands ran (e.g. blocked WO).
        if int_wo is not None and int_wo.command_results:
            results_list: list[dict[str, Any]] = int_wo.command_results
        elif int_wo is not None and int_wo.execution_status == "success":
            results_list = [{
                "command_array": ["git", "status", "--porcelain"],
                "exit_code": 0,
                "passed": True,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
            }]
        else:
            results_list = [{
                "command_array": ["git", "status", "--porcelain"],
                "exit_code": 0 if result.overall_passed else 1,
                "passed": result.overall_passed,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
            }]

        # Aggregate command_policy_status and timeout_status across all WOs.
        all_cmd: list[dict[str, Any]] = []
        for wo_r in result.work_order_results:
            all_cmd.extend(wo_r.command_results)
        if int_wo is not None:
            all_cmd.extend(int_wo.command_results)

        if not all_cmd:
            command_policy_status = "unknown"
            timeout_status = "unknown"
        else:
            command_policy_status = (
                "some_blocked"
                if any(not c.get("policy_allowed", True) for c in all_cmd)
                else "all_allowed"
            )
            timeout_status = (
                "some_timed_out"
                if any(c.get("timed_out", False) for c in all_cmd)
                else "no_timeouts"
            )

        doc: dict[str, Any] = {
            "run_id": self._run_id,
            "schema_version": "1.0.0",
            "generated_at": now_iso(),
            "work_order_ref": ref_id,
            "overall_passed": result.overall_passed,
            "new_failures_vs_baseline": (
                int_wo.new_failures if int_wo is not None else 0
            ),
            "results": results_list,
            "work_order_results": [
                {
                    "work_order_id": r.work_order_id,
                    "execution_status": r.execution_status,
                    "merge_status": r.merge_status,
                    "validation_passed": r.validation_status == "passed",
                    "new_failures": r.new_failures,
                }
                for r in result.work_order_results
            ],
            "command_policy_status": command_policy_status,
            "timeout_status": timeout_status,
        }
        if int_wo is not None:
            doc["integration_result"] = {
                "work_order_id": int_wo.work_order_id,
                "overall_passed": int_wo.execution_status == "success",
                "new_failures_vs_baseline": int_wo.new_failures,
            }

        results_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


__all__ = [
    "MultiWorkOrderExecutor",
    "MultiExecutionResult",
    "WorkOrderMergeResult",
    "topological_sort",
    "detect_write_conflicts",
    "DependencyCycleError",
    "IntegrationWorkOrderMissingError",
    "FileConflictError",
]
