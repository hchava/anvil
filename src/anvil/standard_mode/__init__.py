"""Standard Mode MVP orchestrator (Milestone 4).

Connects existing components (registry, event log, risk engine,
WorkOrderExecutor) into a full Standard Mode pipeline:

  task_contract → source_discovery → gap_analysis → claim_ledger
  → blindspot_scan → cross_validation → plan_creation → plan_review
  → review_consolidation → work_order_negotiation → guardrail_check
  → execution → commit_review → scorecard

Agent callables are injected via :class:`StandardModeAgents` so tests use
synthetic agents without real Claude/Codex/tmux/network.

Safety boundary: no proprietary code, internal project names, or internal
paths appear here.  All examples and fixtures must be synthetic.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..controller import gates, states
from ..controller.baseline import (
    baseline_validation_dict,
    run_command,
    validation_results_dict,
    write_baseline_tests,
)
from ..controller.command_policy import check_read_only
from ..controller.events import EventLog
from ..controller.policy import CommandPolicy
from ..controller.risk import FloorRules, RiskEngine
from ..controller.scorecard import build_scorecard
from ..discovery import discover_sources
from ..errors import AnvilError
from ..executor import WorkOrderExecutor
from ..executor.parallel import (
    IntegrationWorkOrderMissingError,
    MultiWorkOrderExecutor,
)
from ..registry import Registry
from ..schemas_util import assert_valid, validate_artifact
from ..timeutil import now_iso
from . import blindspot as blindspot_mod
from . import consolidation as consol_mod
from . import discovery as disc_mod
from . import gap_analysis as gap_mod
from . import guardrails as gr_mod
from . import negotiation as neg_mod
from . import research as res_mod
from . import review as rev_mod

# Expose the re-exported RunInputs from the controller so callers don't need
# to import it from two places.
from ..controller import RunInputs  # noqa: F401  (re-export)


class StandardModeError(AnvilError):
    """Raised when Standard Mode cannot proceed (fail-closed)."""


# ---------------------------------------------------------------------------
# Agent container
# ---------------------------------------------------------------------------


@dataclass
class StandardModeAgents:
    """Callable agents for one Standard Mode run.

    All callables are optional; the orchestrator raises StandardModeError if a
    required agent is absent when it is needed.

    Context dicts passed to agents contain only serialisable data so fake
    agents in tests can be simple lambdas without registry access.
    """

    # Layer 0 — task contract (required for Standard Mode)
    task_contract_agent: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    # Layers 0A/0B/0C — semantic source expansion (optional)
    # Each entry is (label, callable[[context], list[source_dict]])
    discovery_agents: list[tuple[str, Callable[[dict[str, Any]], list[dict[str, Any]]]]] = (
        field(default_factory=list)
    )

    # Gap analysis agent (optional — default matrix used when None)
    gap_analysis_agent: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    # Layer 1 — research / claim ledger (required for Standard Mode)
    research_agent: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    # Layer 1.5 — Codex blind-spot scan (optional)
    # Returns list of finding dicts; empty list == no findings.
    blindspot_agent: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None

    # Layer 4 — implementation plan text (required for Standard Mode)
    plan_agent: Callable[[dict[str, Any]], str] | None = None

    # Layer 5 — plan review (keyed by reviewer type)
    # Each callable returns list of raw finding dicts.
    reviewer_agents: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = (
        field(default_factory=dict)
    )

    # Layer 5.5 — work-order negotiation (both required for execution)
    planner_agent: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    negotiator_agent: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    # Guardrail matrix (optional — default matrix used when None)
    guardrail_agent: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    # Execution (optional — execution is skipped when None)
    execution_agent: Callable[[Path], None] | None = None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class StandardModeRunner:
    """Orchestrates a single Standard Mode run end-to-end."""

    def __init__(
        self,
        registry: Registry,
        run_id: str,
        agents: StandardModeAgents,
        policy: CommandPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.run_id = run_id
        self._agents = agents
        self._policy = policy or CommandPolicy.from_dict({})
        self._run_dir = registry.paths.run_dir(run_id)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._events = EventLog(self._run_dir / "event_log.jsonl")
        self._risk = RiskEngine()
        self._state = states.INIT
        self._mode = "standard"
        self._history: list[dict[str, str]] = []
        self._agents_launched = 0
        self._baseline_green = True
        self._multi_wo = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, inputs: "RunInputs") -> dict[str, Any]:
        """Execute Standard Mode end-to-end; return the run scorecard dict."""
        _start = time.monotonic()

        run_row = self._validate_inputs(inputs)
        scope = self._resolve_scope(inputs)
        worktree_raw = run_row.get("worktree_path")
        worktree = Path(worktree_raw) if worktree_raw else self._run_dir
        self._multi_wo = inputs.multi_wo

        # — INIT —
        self._transition(states.INIT)
        self._write_worktree_manifest(run_row, "active")

        # — BASELINE —
        baseline = self._run_baseline(worktree, scope, run_row)
        self._transition(states.BASELINE_CAPTURED)

        # — TASK CONTRACT —
        ctx: dict[str, Any] = self._base_context(run_row, scope, inputs)
        task_contract = self._step_task_contract(ctx, inputs)
        self._transition(states.TASK_CONTRACT_PROPOSED)

        # — INITIAL RISK SCORE —
        self._score("initial", inputs.initial_factor_ids, inputs.floor)
        self._transition(states.TASK_CONTRACT_ACCEPTED)

        # — SOURCE DISCOVERY —
        source_manifest = self._step_discovery(worktree, scope, inputs, ctx)
        ctx = {**ctx, "source_manifest": source_manifest}
        self._transition(states.SOURCES_DISCOVERED)

        # — GAP ANALYSIS —
        gap_matrix = self._step_gap_analysis(source_manifest, task_contract, ctx, inputs)
        ctx = {**ctx, "gap_matrix": gap_matrix}
        self._score("post_discovery", inputs.factors_for("post_discovery"), inputs.floor)
        self._transition(states.SOURCE_GAPS_RESOLVED)

        # — RESEARCH / CLAIM LEDGER —
        claim_ledger, weak_ev_issues = self._step_research(source_manifest, task_contract, ctx)
        ctx = {**ctx, "claim_ledger": claim_ledger}
        self._transition(states.CLAIMS_RESEARCHED)

        # — BLINDSPOT SCAN —
        blindspot_issues = self._step_blindspot(claim_ledger, source_manifest, task_contract, ctx)
        all_issues = weak_ev_issues + blindspot_issues
        issue_ledger = self._write_issue_ledger(all_issues, inputs.run_id)
        ctx = {**ctx, "issue_ledger": issue_ledger}
        self._transition(states.BLINDSPOT_SCAN_COMPLETE)

        # — CROSS VALIDATION —
        self._gate_cross_validation(issue_ledger)
        self._transition(states.CROSS_VALIDATION_PENDING)
        self._gate_cross_validation(issue_ledger)  # re-checked at COMPLETE
        self._transition(states.CROSS_VALIDATION_COMPLETE)

        # — PLAN CREATION —
        plan_text = self._step_plan(task_contract, claim_ledger, issue_ledger, ctx)
        ctx = {**ctx, "implementation_plan": plan_text}
        self._score("post_plan", inputs.factors_for("post_plan"), inputs.floor)
        self._transition(states.PLAN_CREATED)

        # — PLAN REVIEW —
        raw_findings, consolidated, review_issues = self._step_plan_review(
            task_contract, plan_text, ctx, inputs
        )
        if review_issues:
            all_issues = all_issues + review_issues
            issue_ledger = self._write_issue_ledger(all_issues, inputs.run_id)
            ctx = {**ctx, "issue_ledger": issue_ledger}
            self._gate_cross_validation(issue_ledger)
        self._transition(states.PLAN_REVIEWED)

        # — WORK-ORDER NEGOTIATION —
        work_orders = self._step_negotiation(task_contract, plan_text, issue_ledger, ctx)
        self._gate_work_orders(work_orders, task_contract, issue_ledger)
        self._check_drift()
        self._transition(states.WORK_ORDERS_AGREED)

        # — EXECUTION —
        exec_result = self._step_execution(work_orders, worktree, run_row)
        self._score("post_execution", inputs.factors_for("post_execution"), inputs.floor)
        self._transition(states.EXECUTION_COMPLETE)

        # — GUARDRAIL MATRIX + COMMIT REVIEW —
        guardrail_matrix = self._step_guardrails(task_contract, work_orders, ctx, inputs)
        self._transition(states.READY_FOR_COMMIT_REVIEW)

        self._gate_commit_review(issue_ledger, guardrail_matrix)
        self._transition(states.COMMIT_REVIEWED)

        # — FINALIZE —
        duration = time.monotonic() - _start
        return self._finalize(
            run_row, issue_ledger, work_orders,
            baseline_green=self._baseline_green,
            exec_result=exec_result,
            duration=duration,
        )

    # ------------------------------------------------------------------
    # Input / scope resolution
    # ------------------------------------------------------------------

    def _validate_inputs(self, inputs: "RunInputs") -> dict[str, Any]:
        if inputs.run_id != self.run_id:
            raise StandardModeError(
                f"RunInputs.run_id '{inputs.run_id}' != runner run_id '{self.run_id}'"
            )
        run_row = dict(self.registry.get_run(self.run_id))
        if inputs.project_id != run_row["project_id"]:
            raise StandardModeError(
                f"inputs.project_id '{inputs.project_id}' != run project '{run_row['project_id']}'"
            )
        if inputs.repo_id != run_row["repo_id"]:
            raise StandardModeError(
                f"inputs.repo_id '{inputs.repo_id}' != run repo '{run_row['repo_id']}'"
            )
        if inputs.scope_id != run_row["task_scope_id"]:
            raise StandardModeError(
                f"inputs.scope_id '{inputs.scope_id}' != run scope '{run_row['task_scope_id']}'"
            )
        return run_row

    def _resolve_scope(self, inputs: "RunInputs") -> Any:
        if inputs.scope_id is None:
            return None
        project_cfg = self.registry.load_project_config(inputs.project_id)
        scope = project_cfg.task_scopes.get(inputs.scope_id)
        if scope is None:
            raise StandardModeError(
                f"scope '{inputs.scope_id}' not found in project '{inputs.project_id}'"
            )
        return scope

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _base_context(
        self, run_row: dict[str, Any], scope: Any, inputs: "RunInputs"
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": inputs.project_id,
            "repo_id": inputs.repo_id,
            "task_summary": run_row.get("task_summary", ""),
            "base_commit": run_row.get("base_commit", ""),
            "scope_paths": list(scope.root_paths) if scope is not None else [],
            "risk_factor_ids": list(inputs.initial_factor_ids),
        }

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _transition(self, state: str) -> None:
        before = self._state
        ts = now_iso()
        if self._history and "exited_at" not in self._history[-1]:
            self._history[-1]["exited_at"] = ts
        self._history.append({"state": state, "entered_at": ts})
        self._state = state
        self._events.append(
            "state_transition", state_before=before, state_after=state
        )
        self._persist_state()

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
        for assessment in self._risk._assessments:  # noqa: SLF001
            state_doc["risk_scores"][assessment.stage] = assessment.score
        assert_valid("controller_state", state_doc)
        (self._run_dir / "controller_state.json").write_text(
            json.dumps(state_doc, indent=2) + "\n", encoding="utf-8"
        )
        self.registry.set_pipeline_state(self.run_id, self._state)

    def _score(
        self,
        stage: str,
        factor_ids: list[str],
        floor: FloorRules | None,
    ) -> None:
        before_mode = self._mode
        assessment = self._risk.assess(stage, factor_ids, floor=floor)
        self._mode = self._risk.current_mode
        if assessment.mode_changed:
            self._events.append(
                "mode_escalated",
                details={
                    "stage": stage,
                    "from": before_mode,
                    "to": self._mode,
                    "reason": assessment.escalation_reason,
                },
            )
        self._events.append(
            "risk_rescored",
            details={"stage": stage, "score": assessment.score, "mode": self._mode},
        )
        self._write_artifact("risk_assessment.json", self._risk.to_dict(self.run_id), "risk_assessment")

    # ------------------------------------------------------------------
    # Baseline step
    # ------------------------------------------------------------------

    def _run_baseline(
        self, worktree: Path, scope: Any, run_row: dict[str, Any]
    ) -> dict[str, Any]:
        commands: list[list[str]] = []
        if scope is not None:
            for entry in scope.baseline_commands:
                commands.append(list(entry["command_array"]))
        if not commands:
            commands = [["git", "status", "--porcelain"]]

        for cmd in commands:
            ok, reason = check_read_only(cmd)
            if not ok:
                raise StandardModeError(f"baseline command not permitted: {reason}")

        outcomes = []
        for cmd in commands:
            outcome = run_command(cmd, worktree)
            self._events.append(
                "command_executed",
                details={"command_array": outcome.command_array, "exit_code": outcome.exit_code},
            )
            outcomes.append(outcome)

        baseline = baseline_validation_dict(
            self.run_id, run_row.get("base_commit", "HEAD"), outcomes
        )
        self._write_artifact("baseline_validation.json", baseline, "baseline_validation")
        write_baseline_tests(self._run_dir / "baseline_tests.json", outcomes)
        validation = validation_results_dict(self.run_id, "EXEC-000", outcomes)
        self._write_artifact("validation_results.json", validation, "validation_results")
        self._baseline_green = baseline.get("baseline_green", True)
        return baseline

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    def _step_task_contract(
        self, ctx: dict[str, Any], inputs: "RunInputs"
    ) -> dict[str, Any]:
        if self._agents.task_contract_agent is None:
            raise StandardModeError("task_contract_agent is required for Standard Mode")
        self._agents_launched += 1
        contract = self._agents.task_contract_agent(ctx)
        errors = validate_artifact("task_contract", contract)
        if errors:
            raise StandardModeError(
                f"task_contract failed schema validation: {errors[0]}"
            )
        if not contract.get("goals"):
            raise StandardModeError("task contract has no goals")
        if not contract.get("acceptance_criteria"):
            raise StandardModeError("task contract has no acceptance_criteria")
        self._write_artifact("task_contract.json", contract)
        return contract

    def _step_discovery(
        self,
        worktree: Path,
        scope: Any,
        inputs: "RunInputs",
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        # Deterministic discovery first.
        manifest = discover_sources(
            self.run_id, worktree, scope, keyword=inputs.keyword
        )
        if not manifest.get("sources"):
            raise StandardModeError("source discovery found zero sources")

        # LLM semantic expansion (agents 0A/0B/0C).
        if self._agents.discovery_agents:
            manifest, launched, _neg = disc_mod.augment_source_manifest(
                manifest, self._agents.discovery_agents, ctx
            )
            self._agents_launched += launched

        errors = validate_artifact("source_manifest", manifest)
        if errors:
            raise StandardModeError(f"source_manifest invalid: {errors[0]}")
        self._write_artifact("source_manifest.json", manifest)
        return manifest

    def _step_gap_analysis(
        self,
        source_manifest: dict[str, Any],
        task_contract: dict[str, Any],
        ctx: dict[str, Any],
        inputs: "RunInputs",
    ) -> dict[str, Any]:
        if self._agents.gap_analysis_agent is not None:
            self._agents_launched += 1
            gap_matrix = self._agents.gap_analysis_agent(
                {**ctx, "source_manifest": source_manifest, "task_contract": task_contract}
            )
        else:
            gap_matrix = gap_mod.build_default_gap_matrix(self.run_id, source_manifest)

        errors = validate_artifact("gap_matrix", gap_matrix)
        if errors:
            raise StandardModeError(f"gap_matrix invalid: {errors[0]}")
        try:
            gap_mod.gap_gate(gap_matrix)
        except gap_mod.GapError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"gap gate failed: {exc}") from exc

        self._write_artifact("gap_matrix.json", gap_matrix)
        return gap_matrix

    def _step_research(
        self,
        source_manifest: dict[str, Any],
        task_contract: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self._agents.research_agent is None:
            raise StandardModeError("research_agent is required for Standard Mode")
        self._agents_launched += 1
        claim_ledger = self._agents.research_agent(
            {**ctx, "source_manifest": source_manifest, "task_contract": task_contract}
        )

        # Check weak evidence BEFORE schema validation so we can create issues.
        existing_issue_count = 0
        weak_ev_issues = res_mod.check_weak_evidence(claim_ledger, existing_issue_count)

        # Schema validate; emit errors as blocking issues rather than raising
        # immediately, so the cross-validation gate surfaces them cleanly.
        errors = validate_artifact("claim_ledger", claim_ledger)
        if errors:
            # Only raise immediately for structural errors unrelated to evidence.
            evidence_keywords = ("direct evidence", "requires", "minItems")
            non_ev_errors = [
                e for e in errors
                if not any(kw in e for kw in evidence_keywords)
            ]
            if non_ev_errors:
                raise StandardModeError(
                    f"claim_ledger has structural errors: {non_ev_errors[0]}"
                )
            # Evidence-quality errors become blocking issues if not already captured.
            if not weak_ev_issues:
                existing_issue_count += 1
                weak_ev_issues.append(
                    {
                        "issue_id": f"ISSUE-{existing_issue_count:03d}",
                        "title": f"Claim ledger evidence quality: {errors[0]}",
                        "severity": "high",
                        "raised_by": "standard-mode-research-gate",
                        "layer": "research",
                        "related_claims": [],
                        "related_sources": [],
                        "resolution": "open",
                        "safe_to_continue_without_resolution": False,
                        "blocks_work_orders": [],
                        "blocks_layers": ["execution"],
                    }
                )

        gates.validate_task_contract_refs(claim_ledger, task_contract)
        gates.validate_source_refs(claim_ledger, source_manifest)

        self._write_artifact("claim_ledger.json", claim_ledger)
        return claim_ledger, weak_ev_issues

    def _step_blindspot(
        self,
        claim_ledger: dict[str, Any],
        source_manifest: dict[str, Any],
        task_contract: dict[str, Any],
        ctx: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._agents.blindspot_agent is None:
            return []
        self._agents_launched += 1
        findings = self._agents.blindspot_agent(
            {
                **ctx,
                "claim_ledger": claim_ledger,
                "source_manifest": source_manifest,
                "task_contract": task_contract,
            }
        ) or []
        issues = blindspot_mod.findings_to_issues(
            self.run_id, findings, existing_issue_count=0
        )
        return issues

    def _step_plan(
        self,
        task_contract: dict[str, Any],
        claim_ledger: dict[str, Any],
        issue_ledger: dict[str, Any],
        ctx: dict[str, Any],
    ) -> str:
        if self._agents.plan_agent is None:
            raise StandardModeError("plan_agent is required for Standard Mode")
        self._agents_launched += 1
        plan_text = self._agents.plan_agent(
            {**ctx, "task_contract": task_contract, "claim_ledger": claim_ledger}
        )

        plan_path = self._run_dir / "implementation_plan.md"
        plan_path.write_text(plan_text, encoding="utf-8")
        self._events.append("artifact_written", artifact_ref="implementation_plan.md")
        return plan_text

    def _step_plan_review(
        self,
        task_contract: dict[str, Any],
        plan_text: str,
        ctx: dict[str, Any],
        inputs: "RunInputs",
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        # Reviewer selection based on active risk factors.
        try:
            reviewer_types = rev_mod.select_reviewers(
                inputs.initial_factor_ids, self._agents.reviewer_agents
            )
        except rev_mod.ReviewError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"plan review gate failed: {exc}") from exc
        raw_ctx = {**ctx, "task_contract": task_contract, "implementation_plan": plan_text}
        try:
            raw_findings = rev_mod.run_review(
                self.run_id,
                reviewer_types,
                self._agents.reviewer_agents,
                raw_ctx,
            )
        except rev_mod.ReviewError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"plan review gate failed: {exc}") from exc
        self._agents_launched += len(reviewer_types)

        errors = validate_artifact("review_findings_raw", raw_findings)
        if errors:
            raise StandardModeError(f"review_findings_raw invalid: {errors[0]}")
        self._write_artifact("review_findings_raw.json", raw_findings)

        # Consolidation: build default pass-through if no consolidation agent.
        consolidated = consol_mod.build_default_consolidated(self.run_id, raw_findings)
        try:
            consol_mod.verify_consolidation(raw_findings, consolidated)
        except consol_mod.ConsolidationError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"review consolidation gate failed: {exc}") from exc

        errors = validate_artifact("review_findings_consolidated", consolidated)
        if errors:
            raise StandardModeError(f"review_findings_consolidated invalid: {errors[0]}")
        self._write_artifact("review_findings_consolidated.json", consolidated)

        existing_count = 0
        review_issues = consol_mod.preserved_issues_from_consolidation(
            consolidated, raw_findings, existing_issue_count=existing_count
        )
        return raw_findings, consolidated, review_issues

    def _step_negotiation(
        self,
        task_contract: dict[str, Any],
        plan_text: str,
        issue_ledger: dict[str, Any],
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        if self._agents.planner_agent is None:
            raise StandardModeError("planner_agent is required for Standard Mode")
        if self._agents.negotiator_agent is None:
            raise StandardModeError("negotiator_agent is required for Standard Mode")

        self._agents_launched += 1
        plan_ctx = neg_mod.build_planner_context(
            self.run_id,
            task_contract,
            ctx.get("claim_ledger", {}),
            plan_text,
            issue_ledger,
        )
        planner_output = self._agents.planner_agent(plan_ctx)
        assert_valid("execution_work_orders", planner_output)
        self._write_artifact("execution_work_orders_proposed.json", planner_output)

        self._agents_launched += 1
        neg_ctx = neg_mod.build_negotiator_context(
            self.run_id, planner_output, task_contract, issue_ledger
        )
        negotiator_output = self._agents.negotiator_agent(neg_ctx)

        try:
            work_orders = neg_mod.negotiate(self.run_id, planner_output, negotiator_output)
        except neg_mod.NegotiationError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"work-order negotiation failed: {exc}") from exc

        assert_valid("execution_work_orders", work_orders)
        self._write_artifact("execution_work_orders.json", work_orders)
        return work_orders

    def _step_guardrails(
        self,
        task_contract: dict[str, Any],
        work_orders: dict[str, Any],
        ctx: dict[str, Any],
        inputs: "RunInputs",
    ) -> dict[str, Any]:
        if self._agents.guardrail_agent is not None:
            self._agents_launched += 1
            guardrail_matrix = self._agents.guardrail_agent(
                {**ctx, "task_contract": task_contract, "work_orders": work_orders}
            )
        else:
            guardrail_matrix = gr_mod.build_default_guardrail_matrix(
                self.run_id, list(inputs.initial_factor_ids)
            )

        errors = validate_artifact("guardrail_matrix", guardrail_matrix)
        if errors:
            raise StandardModeError(f"guardrail_matrix invalid: {errors[0]}")
        self._write_artifact("guardrail_matrix.json", guardrail_matrix)
        return guardrail_matrix

    def _step_execution(
        self,
        work_orders: dict[str, Any],
        worktree: Path,
        run_row: dict[str, Any],
    ) -> Any:
        """Execute work orders via WorkOrderExecutor (single-WO) or
        MultiWorkOrderExecutor (multi-WO).
        """
        if self._agents.execution_agent is None:
            raise StandardModeError("execution_agent is required for Standard Mode")

        agreed_required = [
            wo
            for wo in work_orders.get("work_orders", [])
            if wo.get("negotiation_status") == "agreed"
            and wo.get("criticality") == "required"
        ]
        if not agreed_required:
            raise StandardModeError(
                "Standard Mode requires at least one agreed required work order "
                "before execution; none found"
            )

        baseline_tests_path = self._run_dir / "baseline_tests.json"
        baseline_tests: list[dict[str, Any]] = []
        if baseline_tests_path.exists():
            raw = json.loads(baseline_tests_path.read_text(encoding="utf-8"))
            baseline_tests = raw.get("tests", []) if isinstance(raw, dict) else raw

        # --- Multi-WO path ---
        if self._multi_wo:
            repo = self.registry.get_repo(run_row["repo_id"])
            try:
                multi_executor = MultiWorkOrderExecutor(
                    run_id=self.run_id,
                    run_dir=self._run_dir,
                    repo_id=run_row["repo_id"],
                    repo_path=Path(repo["path"]),
                    base_commit=run_row["base_commit"],
                    worktrees_dir=self.registry.paths.worktrees_dir,
                    main_worktree_path=worktree,
                    main_branch=run_row.get("branch") or f"anvil/{self.run_id}",
                    registry=self.registry,
                    event_log=self._events,
                    policy=self._policy,
                    baseline_tests=baseline_tests,
                )
                multi_result = multi_executor.execute(
                    work_orders, self._agents.execution_agent
                )
            except IntegrationWorkOrderMissingError as exc:
                raise StandardModeError(str(exc)) from exc
            except Exception as exc:
                raise StandardModeError(
                    f"Multi-WO execution failed: {exc}"
                ) from exc

            if not multi_result.overall_passed:
                raise StandardModeError(
                    f"Multi-WO execution did not pass: "
                    f"error={multi_result.error or 'see work_order_entries'}"
                )
            return multi_result

        # --- Single-WO path (M3/M4 behaviour) ---
        work_order = agreed_required[0]
        executor = WorkOrderExecutor(
            run_id=self.run_id,
            run_dir=self._run_dir,
            worktree_path=worktree,
            work_order=work_order,
            repo_id=run_row["repo_id"],
            registry=self.registry,
            event_log=self._events,
            policy=self._policy,
            baseline_tests=baseline_tests,
        )
        result = executor.execute(self._agents.execution_agent)
        if result.status not in ("success", "pending"):
            raise StandardModeError(
                f"work order execution failed: status={result.status} error={result.error}"
            )
        return result

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _write_issue_ledger(
        self, issues: list[dict[str, Any]], run_id: str
    ) -> dict[str, Any]:
        ledger = {"run_id": run_id, "issues": issues}
        errors = validate_artifact("issue_ledger", ledger)
        if errors:
            raise StandardModeError(f"issue_ledger schema invalid: {errors[0]}")
        self._write_artifact("issue_ledger.json", ledger)
        return ledger

    def _gate_cross_validation(self, issue_ledger: dict[str, Any]) -> None:
        try:
            gates.cross_validation_gate(issue_ledger)
            gates.issue_closure_gate(issue_ledger)
        except gates.GateError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"cross-validation gate failed: {exc}") from exc

    def _gate_work_orders(
        self,
        work_orders: dict[str, Any],
        task_contract: dict[str, Any],
        issue_ledger: dict[str, Any],
    ) -> None:
        try:
            gates.validate_work_order_dependencies(work_orders)
            gates.validate_file_scope_against_contract(work_orders, task_contract)
            gates.validate_issue_refs(issue_ledger, work_orders)
        except gates.GateError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"work-order gate failed: {exc}") from exc

    def _gate_commit_review(
        self,
        issue_ledger: dict[str, Any],
        guardrail_matrix: dict[str, Any],
    ) -> None:
        try:
            gates.issue_closure_gate(issue_ledger)
            gates.validate_guardrail_refs(guardrail_matrix)
            gates.guardrail_gate(guardrail_matrix, self._mode)
        except gates.GateError as exc:
            self._events.append("gate_failed", error=str(exc))
            raise StandardModeError(f"commit-review gate failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Drift check
    # ------------------------------------------------------------------

    def _check_drift(self) -> None:
        try:
            drift = self.registry.check_drift(self.run_id)
        except Exception:
            return
        self._events.append(
            "command_executed",
            details={"drift_check": "pre_execution", **drift.to_dict()},
        )
        if drift.base_is_stale:
            self._events.append(
                "gate_failed",
                error=f"base-commit drift: target moved "
                f"{drift.target_head_at_start} -> {drift.target_head_current}",
            )
            (self._run_dir / "drift.json").write_text(
                json.dumps({"label": "pre_execution", **drift.to_dict()}, indent=2) + "\n",
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # Artifact I/O
    # ------------------------------------------------------------------

    def _write_artifact(
        self, filename: str, payload: dict[str, Any], schema: str | None = None
    ) -> None:
        if schema is not None:
            assert_valid(schema, payload)
        path = self._run_dir / filename
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self._events.append("artifact_written", artifact_ref=filename)

    def _write_worktree_manifest(
        self, run_row: dict[str, Any], status: str
    ) -> None:
        if not run_row.get("worktree_path"):
            return
        manifest = {
            "run_id": self.run_id,
            "schema_version": "0.1.0",
            "created_at": run_row.get("created_at", now_iso()),
            "worktree_id": f"wt-{self.run_id}",
            "base_repo": run_row["repo_id"],
            "base_commit": run_row.get("base_commit", ""),
            "branch": run_row.get("branch") or f"anvil/{self.run_id}",
            "path": run_row["worktree_path"],
            "status": status,
        }
        self._write_artifact("worktree_manifest.json", manifest, "worktree_manifest")

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def _finalize(
        self,
        run_row: dict[str, Any],
        issue_ledger: dict[str, Any],
        work_orders: dict[str, Any],
        *,
        baseline_green: bool,
        exec_result: Any,
        duration: float,
    ) -> dict[str, Any]:
        scorecard = build_scorecard(
            self.run_id,
            "standard",
            risk_initial=self._risk.initial_score,
            risk_final=self._risk.final_score,
            mode_escalated=self._risk.mode_escalated,
            issue_ledger=issue_ledger,
            work_orders=work_orders,
            baseline_green=baseline_green,
            final_outcome="passed",
        )
        scorecard["agents_launched"] = self._agents_launched
        scorecard["duration_seconds"] = round(duration, 3)
        self._write_artifact("run_scorecard.json", scorecard, "run_scorecard")

        self._transition(states.FINALIZED)
        self._write_worktree_manifest(run_row, "active")

        try:
            self.registry.finalize_run(self.run_id, remove_worktree=False)
        except Exception:
            pass
        self._events.append("run_finalized")
        return scorecard


__all__ = ["StandardModeRunner", "StandardModeAgents", "StandardModeError", "RunInputs"]
