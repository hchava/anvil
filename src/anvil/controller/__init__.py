"""Deterministic controller dry run (Milestone 1).

The :class:`Controller` walks a per-mode state sequence over a run directory,
producing the deterministic artifacts itself (baseline_validation,
source_manifest, risk_assessment, validation_results, worktree_manifest,
run_scorecard, controller_state, event_log) and validating the LLM-owned
artifacts that are supplied as fixtures (task_contract, gap_matrix,
claim_ledger, issue_ledger, execution_work_orders, guardrail_matrix).

There is NO LLM, NO agent launcher, NO code writes to the target repo. Every
transition runs a deterministic gate; on failure the controller fails closed
(Standard/Critical) by raising, after logging a gate_failed event. State is
persisted after each transition so a run can resume from controller_state.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import gitutils
from ..config import ProjectConfig, TaskScope
from ..discovery import discover_sources
from ..errors import AnvilError, ValidationError
from ..paths import AnvilPaths
from ..registry import Registry
from ..schemas_util import assert_valid
from ..timeutil import now_iso
from . import gates, states
from .baseline import (
    baseline_validation_dict,
    run_command,
    validation_results_dict,
    write_baseline_tests,
)
from .command_policy import check_read_only
from .events import EventLog
from .risk import FloorRules, RiskEngine
from .scorecard import build_scorecard


class ControllerError(AnvilError):
    """Raised when the controller cannot proceed (fail-closed)."""


# Fixture artifacts the controller consumes (LLM-owned in later milestones).
# Mapping: state at which the artifact is validated -> (filename, schema).
_FIXTURE_ARTIFACTS: dict[str, tuple[str, str]] = {
    states.TASK_CONTRACT_PROPOSED: ("task_contract.json", "task_contract"),
    states.SOURCE_GAPS_RESOLVED: ("gap_matrix.json", "gap_matrix"),
    states.CLAIMS_RESEARCHED: ("claim_ledger.json", "claim_ledger"),
    states.BLINDSPOT_SCAN_COMPLETE: ("issue_ledger.json", "issue_ledger"),
    states.WORK_ORDERS_AGREED: ("execution_work_orders.json", "execution_work_orders"),
    states.READY_FOR_COMMIT_REVIEW: ("guardrail_matrix.json", "guardrail_matrix"),
}


@dataclass
class RunInputs:
    """Everything the controller needs to drive one run (all local, no network)."""

    run_id: str
    project_id: str
    repo_id: str
    scope_id: str | None = None
    initial_factor_ids: list[str] = field(default_factory=list)
    post_discovery_factor_ids: list[str] | None = None
    post_plan_factor_ids: list[str] | None = None
    post_execution_factor_ids: list[str] | None = None
    floor: FloorRules | None = None
    multi_wo: bool = False
    keyword: str | None = None

    def factors_for(self, stage: str) -> list[str]:
        """Stage-specific factor ids, each falling back to the most recent prior
        stage that was specified."""
        if stage == "initial":
            return self.initial_factor_ids
        if stage == "post_discovery":
            return self.post_discovery_factor_ids if self.post_discovery_factor_ids is not None else self.initial_factor_ids
        if stage == "post_plan":
            if self.post_plan_factor_ids is not None:
                return self.post_plan_factor_ids
            return self.factors_for("post_discovery")
        if stage == "post_execution":
            if self.post_execution_factor_ids is not None:
                return self.post_execution_factor_ids
            return self.factors_for("post_plan")
        raise ValueError(f"unknown stage: {stage}")


class Controller:
    """Drives a single deterministic dry run over fixture artifacts."""

    def __init__(self, registry: Registry, run_id: str) -> None:
        self.registry = registry
        self.run_id = run_id
        self.paths: AnvilPaths = registry.paths
        self.run_dir: Path = self.paths.run_dir(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "event_log.jsonl")
        self.risk = RiskEngine()
        self._mode: str = "pending_risk_assessment"
        self._state: str = states.INIT
        self._history: list[dict[str, str]] = []
        self._mode_escalated = False

    # ------------------------------------------------------------------ #
    # artifact IO
    # ------------------------------------------------------------------ #
    def _artifact_path(self, filename: str) -> Path:
        return self.run_dir / filename

    def _read_artifact(self, filename: str) -> dict[str, Any]:
        path = self._artifact_path(filename)
        if not path.exists():
            raise ControllerError(f"required fixture artifact missing: {filename}")
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _write_artifact(self, filename: str, payload: dict[str, Any], schema: str | None = None) -> None:
        if schema is not None:
            assert_valid(schema, payload)
        path = self._artifact_path(filename)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        self.events.append("artifact_written", artifact_ref=filename)

    def _maybe_read(self, filename: str) -> dict[str, Any] | None:
        path = self._artifact_path(filename)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    # ------------------------------------------------------------------ #
    # state persistence + resume
    # ------------------------------------------------------------------ #
    def _persist_state(self) -> None:
        state_doc: dict[str, Any] = {
            "run_id": self.run_id,
            "current_state": self._state,
            "mode": self._mode,
            "risk_scores": {
                "initial": None,
                "post_discovery": None,
                "post_plan": None,
                "post_execution": None,
            },
            "state_history": self._history,
            "pending_human_decisions": [],
        }
        for assessment in self.risk._assessments:  # noqa: SLF001 - same package
            state_doc["risk_scores"][assessment.stage] = assessment.score
        assert_valid("controller_state", state_doc)
        path = self.run_dir / "controller_state.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(state_doc, handle, indent=2)
            handle.write("\n")
        # Mirror the operational pipeline_state into the registry for `status`.
        self.registry.set_pipeline_state(self.run_id, self._state)

    def load_state(self) -> dict[str, Any] | None:
        """Load controller_state.json if present (for resume)."""
        path = self.run_dir / "controller_state.json"
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            doc = json.load(handle)
        self._state = doc["current_state"]
        self._mode = doc["mode"]
        self._history = list(doc.get("state_history", []))
        return doc

    # ------------------------------------------------------------------ #
    # transition helper
    # ------------------------------------------------------------------ #
    def _enter(self, state: str, gate: Callable[[], None] | None = None) -> None:
        before = self._state
        ts = now_iso()
        try:
            if gate is not None:
                gate()
        except gates.GateError as exc:
            self.events.append(
                "gate_failed", state_before=before, state_after=state, error=str(exc)
            )
            raise ControllerError(f"gate failed entering {state}: {exc}") from exc
        # Record exit of previous state, enter new state.
        if self._history and "exited_at" not in self._history[-1]:
            self._history[-1]["exited_at"] = ts
        self._history.append({"state": state, "entered_at": ts})
        self._state = state
        if gate is not None:
            self.events.append("gate_passed", state_before=before, state_after=state)
        self.events.append("state_transition", state_before=before, state_after=state)
        self._persist_state()

    # ------------------------------------------------------------------ #
    # the dry run
    # ------------------------------------------------------------------ #
    def run(self, inputs: RunInputs) -> dict[str, Any]:
        """Execute (or resume) the deterministic dry run; return the scorecard.

        The run is driven by an ordered step list. On entry we load the persisted
        controller_state.json: a FINALIZED run is idempotent (returns the existing
        scorecard); a mid-run state resumes from the next step without re-running
        completed phases or duplicating events/artifacts.
        """
        run_row = self._validate_inputs(inputs)
        scope = self._resolve_scope(inputs)
        self._inputs = inputs
        self._scope = scope
        self._run_row = run_row
        self._base_commit = run_row["base_commit"]
        self._worktree = (
            Path(run_row["worktree_path"]) if run_row["worktree_path"] else Path(run_row["base_commit"])
        )

        persisted = self.load_state()
        resume_state = persisted["current_state"] if persisted else states.INIT
        if resume_state == states.FINALIZED:
            return self._read_artifact("run_scorecard.json")
        fresh = resume_state == states.INIT
        if not fresh:
            self._resume_from_disk()

        started = fresh
        if fresh:
            self._record_init(run_row)

        # HEAD steps are mode-independent (present in every sequence).
        head = [
            (states.BASELINE_CAPTURED, self._step_baseline),
            (states.TASK_CONTRACT_PROPOSED, self._step_task_contract),
            (states.TASK_CONTRACT_ACCEPTED, self._step_accept),
            (states.SOURCES_DISCOVERED, self._step_discover),
            (states.SOURCE_GAPS_RESOLVED, self._step_gaps),
        ]
        self._drive(head, resume_state, started)

        # Drive the mode-specific tail dynamically. The live sequence is consulted
        # after EVERY step, so a late mode escalation (e.g. fast -> critical at
        # post_plan) extends the remaining path with newly required future states
        # such as PLAN_REVIEWED. Resume is implicit: the loop walks forward from
        # the persisted current state without re-running it.
        self._drive_tail()

        return self._finalize(inputs)

    def _drive(self, steps: list[tuple[str, Callable[[], None]]], resume_state: str, started: bool) -> bool:
        """Run each step whose target state comes after ``resume_state``."""
        for target, fn in steps:
            if not started:
                if target == resume_state:
                    started = True  # already completed; resume after it.
                continue
            fn()
        return started

    # ------------------------------------------------------------------ #
    # input validation + resume reconstruction
    # ------------------------------------------------------------------ #
    def _validate_inputs(self, inputs: RunInputs) -> Any:
        """Ensure RunInputs identify the SAME run/project/repo/scope as the row."""
        if inputs.run_id != self.run_id:
            raise ControllerError(
                f"RunInputs.run_id '{inputs.run_id}' != controller run '{self.run_id}'"
            )
        run_row = self.registry.get_run(self.run_id)
        if inputs.project_id != run_row["project_id"]:
            raise ControllerError(
                f"run '{self.run_id}' belongs to project '{run_row['project_id']}', "
                f"not '{inputs.project_id}'"
            )
        if inputs.repo_id != run_row["repo_id"]:
            raise ControllerError(
                f"run '{self.run_id}' is bound to repo '{run_row['repo_id']}', not '{inputs.repo_id}'"
            )
        if inputs.scope_id != run_row["task_scope_id"]:
            raise ControllerError(
                f"run '{self.run_id}' has scope '{run_row['task_scope_id']}', not '{inputs.scope_id}'"
            )
        return run_row

    def _resolve_scope(self, inputs: RunInputs) -> TaskScope | None:
        if inputs.scope_id is None:
            return None
        project_cfg: ProjectConfig = self.registry.load_project_config(inputs.project_id)
        scope = project_cfg.task_scopes.get(inputs.scope_id)
        if scope is None:
            raise ControllerError(f"scope '{inputs.scope_id}' not found in project")
        return scope

    def _resume_from_disk(self) -> None:
        """Rebuild in-memory risk state from the persisted artifacts."""
        risk_doc = self._maybe_read("risk_assessment.json")
        if risk_doc is not None:
            self.risk.load_from_dict(risk_doc)
            self._mode = self.risk.current_mode
            self._mode_escalated = self.risk.mode_escalated
        self._baseline_green = (self._maybe_read("baseline_validation.json") or {}).get("baseline_green", True)

    def _record_init(self, run_row: Any) -> None:
        self._history.append({"state": states.INIT, "entered_at": now_iso()})
        self.events.append("state_transition", state_after=states.INIT)
        self._write_worktree_manifest(run_row, status="active")
        self._persist_state()

    # ------------------------------------------------------------------ #
    # HEAD steps
    # ------------------------------------------------------------------ #
    def _step_baseline(self) -> None:
        commands: list[list[str]] = []
        if self._scope is not None:
            for entry in self._scope.baseline_commands:
                commands.append(list(entry["command_array"]))
        if not commands:
            # A harmless read-only default so the baseline is well-formed.
            commands = [["git", "status", "--porcelain"]]

        # Enforce the read-only argv policy BEFORE executing anything (dry run:
        # never run an arbitrary, worktree-mutating command).
        for cmd in commands:
            ok, reason = check_read_only(cmd)
            if not ok:
                self.events.append("gate_failed", error=f"baseline command rejected: {reason}")
                raise ControllerError(f"baseline command not permitted in a dry run: {reason}")

        # Backstop: a pre/post worktree cleanliness check around EVERY command.
        # If a command mutates a tracked file (anything the argv policy missed),
        # the change is reverted and the run fails closed.
        check_clean = gitutils.is_git_repo(self._worktree)
        dirty_before = gitutils.dirty_tracked_files(self._worktree) if check_clean else set()

        outcomes = []
        for cmd in commands:
            outcome = run_command(cmd, self._worktree)
            self.events.append(
                "command_executed",
                details={"command_array": outcome.command_array, "exit_code": outcome.exit_code},
            )
            if check_clean:
                new_dirty = gitutils.dirty_tracked_files(self._worktree) - dirty_before
                if new_dirty:
                    gitutils.restore_tracked(self._worktree, new_dirty)
                    self.events.append(
                        "gate_failed",
                        error=f"baseline command mutated the worktree (reverted): {sorted(new_dirty)}",
                    )
                    raise ControllerError(
                        f"baseline command mutated the worktree and was reverted: {sorted(new_dirty)}"
                    )
            outcomes.append(outcome)
        baseline = baseline_validation_dict(self.run_id, self._base_commit, outcomes)
        self._write_artifact("baseline_validation.json", baseline, schema="baseline_validation")
        write_baseline_tests(self.run_dir / "baseline_tests.json", outcomes)
        validation = validation_results_dict(self.run_id, "EXEC-000", outcomes)
        self._write_artifact("validation_results.json", validation, schema="validation_results")
        self._baseline_green = baseline["baseline_green"]
        self._enter(states.BASELINE_CAPTURED)

    def _step_task_contract(self) -> None:
        self._enter(states.TASK_CONTRACT_PROPOSED, gate=self._gate_task_contract)

    def _step_accept(self) -> None:
        self._score_stage("initial")
        self._enter(states.TASK_CONTRACT_ACCEPTED)

    def _step_discover(self) -> None:
        manifest = discover_sources(self.run_id, self._worktree, self._scope, keyword=self._inputs.keyword)
        self._write_artifact("source_manifest.json", manifest, schema="source_manifest")
        self._enter(states.SOURCES_DISCOVERED)

    def _step_gaps(self) -> None:
        self._enter(states.SOURCE_GAPS_RESOLVED, gate=self._gate_gap_matrix)
        self._score_stage("post_discovery")

    # ------------------------------------------------------------------ #
    # TAIL driver (mode-specific, re-evaluated after every step)
    # ------------------------------------------------------------------ #
    def _drive_tail(self) -> None:
        """Walk from the current state to COMMIT_REVIEWED, re-reading the live
        per-mode sequence each iteration so escalations extend the path."""
        guard = 0
        while self._state != states.COMMIT_REVIEWED:
            guard += 1
            if guard > 64:  # pragma: no cover - safety against an unexpected loop
                raise ControllerError("tail driver did not converge")
            seq = states.sequence(self._mode, multi_wo=self._inputs.multi_wo)
            try:
                idx = seq.index(self._state)
            except ValueError as exc:  # pragma: no cover - defensive
                raise ControllerError(
                    f"current state '{self._state}' not in {self._mode} sequence"
                ) from exc
            nxt = seq[idx + 1]
            if nxt == states.FINALIZED:
                break
            self._run_tail_step(nxt)

    def _run_tail_step(self, state: str) -> None:
        if state == states.PLAN_CREATED:
            self._enter(states.PLAN_CREATED, gate=self._gate_for(states.PLAN_CREATED))
            self._score_stage("post_plan")
        elif state == states.WORK_ORDERS_AGREED:
            self._enter(states.WORK_ORDERS_AGREED, gate=self._gate_work_orders)
            self._check_drift("pre_execution")
        elif state == states.EXECUTION_COMPLETE:
            self._enter(states.EXECUTION_COMPLETE)
            self._score_stage("post_execution")
        elif state == states.READY_FOR_COMMIT_REVIEW:
            self._check_drift("pre_final_validation")
            self._enter(states.READY_FOR_COMMIT_REVIEW, gate=self._gate_ready_for_commit)
        elif state == states.COMMIT_REVIEWED:
            self._enter(states.COMMIT_REVIEWED, gate=self._gate_commit_review)
        else:
            # research / integration states: enter with their (possibly None) gate.
            self._enter(state, gate=self._gate_for(state))

    # ------------------------------------------------------------------ #
    # staged scoring with mode propagation
    # ------------------------------------------------------------------ #
    def _score_stage(self, stage: str) -> None:
        before_mode = self._mode
        assessment = self.risk.assess(stage, self._inputs.factors_for(stage), floor=self._inputs.floor)
        self._mode = self.risk.current_mode
        if assessment.mode_changed:
            self._mode_escalated = True
            self.events.append(
                "mode_escalated",
                details={"stage": stage, "from": before_mode, "to": self._mode, "reason": assessment.escalation_reason},
            )
        self.events.append(
            "risk_rescored", details={"stage": stage, "score": assessment.score, "mode": self._mode}
        )
        self._write_risk()

    def _finalize(self, inputs: RunInputs) -> dict[str, Any]:
        issue_ledger = self._maybe_read("issue_ledger.json")
        work_orders = self._maybe_read("execution_work_orders.json")
        scorecard = build_scorecard(
            self.run_id,
            self._mode,
            risk_initial=self.risk.initial_score,
            risk_final=self.risk.final_score,
            mode_escalated=self._mode_escalated,
            issue_ledger=issue_ledger,
            work_orders=work_orders,
            baseline_green=getattr(self, "_baseline_green", True),
            final_outcome="passed",
        )
        self._write_artifact("run_scorecard.json", scorecard, schema="run_scorecard")
        self._enter(states.FINALIZED)
        # Dry run performs NO merge; the worktree exists but is never merged.
        self._write_worktree_manifest(self.registry.get_run(self.run_id), status="active")
        # Finalize the operational lifecycle (release leases) but keep the
        # worktree + artifacts on disk for inspection.
        try:
            self.registry.finalize_run(self.run_id, remove_worktree=False)
        except AnvilError:
            pass
        self.events.append("run_finalized")
        return scorecard

    # ------------------------------------------------------------------ #
    # drift + worktree manifest
    # ------------------------------------------------------------------ #
    def _check_drift(self, label: str) -> None:
        try:
            drift = self.registry.check_drift(self.run_id)
        except AnvilError:
            return
        self.events.append(
            "command_executed",
            details={"drift_check": label, **drift.to_dict()},
        )
        if drift.base_is_stale:
            self.events.append(
                "gate_failed",
                error=f"base-commit drift detected at {label}: target moved "
                f"{drift.target_head_at_start} -> {drift.target_head_current}",
            )
            # Detection only in Milestone 1: record drift; do not rebase.
            (self.run_dir / "drift.json").write_text(
                json.dumps({"label": label, **drift.to_dict()}, indent=2) + "\n",
                encoding="utf-8",
            )

    def _write_worktree_manifest(self, run_row: Any, status: str) -> None:
        if not run_row["worktree_path"]:
            return
        manifest = {
            "run_id": self.run_id,
            "schema_version": "0.1.0",
            "created_at": run_row["created_at"],
            "worktree_id": f"wt-{self.run_id}",
            "base_repo": run_row["repo_id"],
            "base_commit": run_row["base_commit"],
            "branch": run_row["branch"] or f"anvil/{self.run_id}",
            "path": run_row["worktree_path"],
            "status": status,
        }
        self._write_artifact("worktree_manifest.json", manifest, schema="worktree_manifest")

    def _write_risk(self) -> None:
        doc = self.risk.to_dict(self.run_id)
        self._write_artifact("risk_assessment.json", doc, schema="risk_assessment")

    # ------------------------------------------------------------------ #
    # gates
    # ------------------------------------------------------------------ #
    def _gate_for(self, state: str) -> Callable[[], None] | None:
        mapping: dict[str, Callable[[], None]] = {
            states.CLAIMS_RESEARCHED: self._gate_claims,
            states.BLINDSPOT_SCAN_COMPLETE: self._gate_blindspot,
            states.CROSS_VALIDATION_PENDING: self._gate_cross_validation_pending,
            states.CROSS_VALIDATION_COMPLETE: self._gate_cross_validation_complete,
            states.PLAN_REVIEWED: self._gate_plan_reviewed,
        }
        return mapping.get(state)

    def _gate_task_contract(self) -> None:
        contract = self._read_artifact("task_contract.json")
        assert_valid("task_contract", contract)
        if not contract.get("goals"):
            raise gates.GateError("task contract has no goals")
        if not contract.get("acceptance_criteria"):
            raise gates.GateError("task contract has no acceptance_criteria")

    def _gate_gap_matrix(self) -> None:
        gap = self._read_artifact("gap_matrix.json")
        assert_valid("gap_matrix", gap)
        if not gap.get("overall_sufficient", False):
            raise gates.GateError("gap matrix reports sources not sufficient")
        for area in gap.get("coverage_areas", []):
            if area.get("required_level") == "required" and area.get("gap_status") == "gap" and area.get("blocking"):
                raise gates.GateError(f"required coverage area '{area['area']}' has a blocking gap")

    def _gate_claims(self) -> None:
        claim_ledger = self._read_artifact("claim_ledger.json")
        assert_valid("claim_ledger", claim_ledger)
        task_contract = self._read_artifact("task_contract.json")
        source_manifest = self._read_artifact("source_manifest.json")
        gates.validate_task_contract_refs(claim_ledger, task_contract)
        gates.validate_source_refs(claim_ledger, source_manifest)

    def _gate_blindspot(self) -> None:
        issue_ledger = self._read_artifact("issue_ledger.json")
        assert_valid("issue_ledger", issue_ledger)
        claim_ledger = self._read_artifact("claim_ledger.json")
        gates.validate_claim_refs(issue_ledger, claim_ledger)
        # No critical blindspot issue may already be open-and-unsafe here is
        # checked at cross-validation; blindspot only requires a valid ledger.

    def _gate_cross_validation_pending(self) -> None:
        # The pending state always precedes COMPLETE; run the closure gate here so
        # Standard Mode cannot skip it.
        issue_ledger = self._read_artifact("issue_ledger.json")
        gates.cross_validation_gate(issue_ledger)
        gates.issue_closure_gate(issue_ledger)

    def _gate_cross_validation_complete(self) -> None:
        issue_ledger = self._read_artifact("issue_ledger.json")
        gates.issue_closure_gate(issue_ledger)

    def _gate_plan_reviewed(self) -> None:
        # Standard/Critical get a targeted review; in Milestone 1 this is
        # fixture-validated when present, otherwise a no-op (review is fixtureable).
        raw = self._maybe_read("review_findings_raw.json")
        consolidated = self._maybe_read("review_findings_consolidated.json")
        if raw is not None:
            assert_valid("review_findings_raw", raw)
        if consolidated is not None:
            assert_valid("review_findings_consolidated", consolidated)

    def _gate_work_orders(self) -> None:
        work_orders = self._read_artifact("execution_work_orders.json")
        assert_valid("execution_work_orders", work_orders)
        task_contract = self._read_artifact("task_contract.json")
        gates.validate_work_order_dependencies(work_orders)
        gates.validate_file_scope_against_contract(work_orders, task_contract)
        issue_ledger = self._maybe_read("issue_ledger.json")
        if issue_ledger is not None:
            gates.validate_issue_refs(issue_ledger, work_orders)

    def _gate_ready_for_commit(self) -> None:
        guardrail_matrix = self._read_artifact("guardrail_matrix.json")
        assert_valid("guardrail_matrix", guardrail_matrix)
        gates.validate_guardrail_refs(guardrail_matrix)
        gates.guardrail_gate(guardrail_matrix, self._mode)

    def _gate_commit_review(self) -> None:
        issue_ledger = self._maybe_read("issue_ledger.json")
        if issue_ledger is not None:
            gates.issue_closure_gate(issue_ledger)
        guardrail_matrix = self._read_artifact("guardrail_matrix.json")
        gates.guardrail_gate(guardrail_matrix, self._mode)


__all__ = ["Controller", "ControllerError", "RunInputs"]
