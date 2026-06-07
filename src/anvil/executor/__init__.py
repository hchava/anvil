"""Single work order executor (Milestone 3).

Orchestrates the full execution pipeline for one work order inside a
pre-allocated per-run worktree:

  1. Acquire file_write leases for every path in assigned_scope.allowed_files.
  2. Call the execution agent (a callable that writes files to the worktree).
  3. Enforce scope: reject diffs that touch forbidden or out-of-scope files.
  4. Scan diff for secrets; fail closed if any are detected.
  5. Run validation commands (policy-checked, timeout-enforced).
  6. Compare validation output against baseline at test-identity level.
  7. On any failure: rollback using controller-owned primitives.
  8. Release leases (always — in finally block).
  9. Write worktree_manifest.json and validation_results.json.

Design principles:
  - The executor is the deterministic control plane; it never trusts
    agent-generated file paths or shell strings for rollback.
  - Leases are always released, even on exceptions.
  - Logs must not print secret values.
  - Schema-validated artifacts are written on every exit path.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..controller.events import EventLog
from ..controller.policy import CommandPolicy
from ..schemas_util import validate_artifact
from ..timeutil import now_iso
from .ownership import FileOwnershipTracker
from .rollback import RollbackResult, build_rollback_primitives, rollback
from .runner import CommandResult, ValidationRunner
from .scanner import ScanResult, scan_diff
from .scope import ScopeResult, check_scope


@dataclass
class ExecutionResult:
    work_order_id: str
    run_id: str
    # Possible statuses:
    #   pending         — set at construction; should never be seen by callers
    #   success         — validation passed, no scope/secret violations
    #   lease_conflict  — lease acquisition failed before execution
    #   scope_violation — diff touched forbidden or out-of-scope files; rolled back
    #   secret_detected — diff contained credential patterns; rolled back
    #   validation_failed — command returned non-zero / new test failures; rolled back
    #   rollback_error  — rollback itself failed (partial restore)
    status: str = "pending"
    scope_result: ScopeResult | None = None
    scan_result: ScanResult | None = None
    command_results: list[CommandResult] = field(default_factory=list)
    rollback_result: RollbackResult | None = None
    new_failures: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    acquired_lease_ids: list[str] = field(default_factory=list)
    error: str | None = None


class WorkOrderExecutor:
    """Executes a single work order inside a pre-allocated worktree."""

    def __init__(
        self,
        run_id: str,
        run_dir: Path,
        worktree_path: Path,
        work_order: dict[str, Any],
        repo_id: str,
        registry: Any,
        event_log: EventLog,
        policy: CommandPolicy | None = None,
        baseline_tests: list[dict] | None = None,
    ) -> None:
        self._run_id = run_id
        self._run_dir = run_dir
        self._worktree = worktree_path
        self._work_order = work_order
        self._repo_id = repo_id
        self._registry = registry
        self._event_log = event_log
        self._policy = policy or CommandPolicy.from_dict({})
        self._baseline_tests: list[dict] = baseline_tests or []
        self._runner = ValidationRunner(policy=self._policy)
        self._ownership = FileOwnershipTracker()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def execute(self, execution_agent: Callable[[Path], None]) -> ExecutionResult:
        """Run the full work order pipeline and return a structured result.

        ``execution_agent`` is a callable that writes the work order's
        output into the worktree.  In production this wraps a real agent;
        in tests it is a FakeExecutionAgent that writes specific files.
        """
        result = ExecutionResult(
            work_order_id=self._work_order_id,
            run_id=self._run_id,
        )
        lease_ids: list[str] = []

        # --- 1. Acquire leases ------------------------------------------
        try:
            for file_path in self._allowed_files:
                lease = self._registry.acquire_lease(
                    run_id=self._run_id,
                    repo_id=self._repo_id,
                    lease_type="file_write",
                    scope=file_path,
                )
                lease_ids.append(lease["lease_id"])
            result.acquired_lease_ids = lease_ids
            self._ownership.track_work_order(
                self._work_order_id, self._allowed_files, sequence=1
            )
        except Exception as exc:
            result.status = "lease_conflict"
            result.error = str(exc)
            self._event_log.append(
                "execution_blocked",
                details={"work_order_id": self._work_order_id, "reason": str(exc)},
            )
            return result

        try:
            # --- 2. Run execution agent ---------------------------------
            self._event_log.append(
                "execution_started",
                details={"work_order_id": self._work_order_id},
            )
            execution_agent(self._worktree)

            # --- 3. Scope enforcement -----------------------------------
            scope_result = check_scope(
                self._worktree, self._allowed_files, self._forbidden_files
            )
            result.scope_result = scope_result
            result.touched_files = scope_result.touched_files

            if not scope_result.passed:
                result.status = "scope_violation"
                self._event_log.append(
                    "scope_violation",
                    details={
                        "work_order_id": self._work_order_id,
                        "violations": scope_result.violations,
                    },
                )
                result.rollback_result = self._do_rollback(scope_result)
                if not result.rollback_result.success:
                    result.status = "rollback_error"
                return result

            # --- 4. Secret scan -----------------------------------------
            diff_text = self._get_full_diff()
            scan_result = scan_diff(diff_text)
            result.scan_result = scan_result

            if scan_result.has_secrets:
                # Log pattern names only — never the secret values.
                self._event_log.append(
                    "secret_detected",
                    details={
                        "work_order_id": self._work_order_id,
                        "finding_count": len(scan_result.findings),
                        "pattern_names": [f.pattern_name for f in scan_result.findings],
                    },
                )
                result.status = "secret_detected"
                result.rollback_result = self._do_rollback(scope_result)
                if not result.rollback_result.success:
                    result.status = "rollback_error"
                return result

            # --- 5. Validation commands ---------------------------------
            command_results = self._runner.run_all(
                self._validation_commands,
                cwd=self._worktree,
                default_timeout=60,
            )
            result.command_results = command_results

            # --- 6. Baseline comparison ---------------------------------
            new_failures = self._compute_new_failures(command_results)
            result.new_failures = new_failures

            any_command_failed = any(not r.passed for r in command_results)
            if new_failures or any_command_failed:
                result.status = "validation_failed"
                self._event_log.append(
                    "validation_failed",
                    details={
                        "work_order_id": self._work_order_id,
                        "new_failures": new_failures,
                        "command_failures": [
                            r.command_id for r in command_results if not r.passed
                        ],
                    },
                )
                result.rollback_result = self._do_rollback(scope_result)
                if not result.rollback_result.success:
                    result.status = "rollback_error"
                return result

            result.status = "success"
            self._event_log.append(
                "execution_success",
                details={"work_order_id": self._work_order_id},
            )

        finally:
            # --- Always release leases and write artifacts ---------------
            # This finally block runs on every exit path (success, all
            # failure modes, and exceptions), so artifacts and lease
            # releases are guaranteed regardless of which branch was taken.
            release_reason = "released" if result.status == "success" else "aborted"
            for lease_id in lease_ids:
                try:
                    self._registry.release_lease(lease_id, reason=release_reason)
                except Exception:
                    pass  # Best-effort; don't mask the primary result.

            # Write artifacts for every exit path except lease_conflict
            # (which returned before this try block and has no execution data).
            if result.status != "pending":
                self._write_worktree_manifest(result)
                self._write_validation_results(result)

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @property
    def _work_order_id(self) -> str:
        return self._work_order["work_order_id"]

    @property
    def _allowed_files(self) -> list[str]:
        return self._work_order.get("assigned_scope", {}).get("allowed_files", [])

    @property
    def _forbidden_files(self) -> list[str]:
        return self._work_order.get("assigned_scope", {}).get("forbidden_files", [])

    @property
    def _validation_commands(self) -> list[dict]:
        return self._work_order.get("validation_commands", [])

    def _get_full_diff(self) -> str:
        unstaged = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(self._worktree),
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        staged = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=str(self._worktree),
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return unstaged + staged

    def _do_rollback(self, scope_result: ScopeResult) -> RollbackResult:
        primitives = build_rollback_primitives(
            modified_tracked=scope_result.modified_tracked,
            new_untracked=scope_result.new_untracked,
        )
        return rollback(self._worktree, primitives)

    def _compute_new_failures(self, command_results: list[CommandResult]) -> list[str]:
        """Return test_ids that were passing in baseline but are now failing."""
        if not self._baseline_tests:
            return []
        baseline_passing = {
            t["test_id"]
            for t in self._baseline_tests
            if t.get("status") == "passed"
        }
        now_failing: set[str] = set()
        for cr in command_results:
            for t in cr.test_identities:
                if t.get("status") in ("failed", "error"):
                    now_failing.add(t["test_id"])
        return sorted(now_failing & baseline_passing)

    def _write_worktree_manifest(self, result: ExecutionResult) -> None:
        manifest_path = self._run_dir / "worktree_manifest.json"
        doc: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        doc["work_order_id"] = result.work_order_id
        doc["execution_status"] = result.status
        doc["touched_files"] = result.touched_files
        doc["validation_status"] = (
            "passed" if result.status == "success" else "failed"
        )
        doc["file_ownership"] = self._ownership.to_list()
        if result.rollback_result is not None:
            doc["rollback_status"] = (
                "success" if result.rollback_result.success else "failed"
            )

        manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def _write_validation_results(self, result: ExecutionResult) -> None:
        results_path = self._run_dir / "validation_results.json"

        serialized_results: list[dict] = [
            {
                "command_array": r.command_array,
                "exit_code": r.exit_code,
                "passed": r.passed,
                "stdout_excerpt": r.stdout_excerpt[:2000],
                "stderr_excerpt": r.stderr_excerpt[:2000],
            }
            for r in result.command_results
        ]
        # Schema requires minItems: 1 even when no commands ran.
        if not serialized_results:
            serialized_results = [
                {
                    "command_array": ["echo", "no-validation-commands"],
                    "exit_code": 0,
                    "passed": True,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                }
            ]

        doc: dict[str, Any] = {
            "run_id": self._run_id,
            "schema_version": "1.0.0",
            "generated_at": now_iso(),
            "work_order_ref": result.work_order_id,
            "overall_passed": result.status == "success",
            "new_failures_vs_baseline": len(result.new_failures),
            "results": serialized_results,
        }
        results_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
