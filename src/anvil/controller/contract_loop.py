"""Claude/Codex contract loop (Milestone 2).

Orchestrates the two-phase contract workflow:
  Phase 1 — Claude-style worker generates task_contract.json
  Phase 2 — Codex-style reviewer reviews and co-signs (or requests changes)

Schema validation is the gate: an invalid output triggers a retry up to
MAX_RETRIES times. Every attempt is recorded in agent_attempts.jsonl.
Events are written to the run event_log.jsonl.

Codex disagreement on risk score:
  - "disagree_higher": risk is re-scored upward (log + return; caller should escalate)
  - "disagree_lower": raises ContractBlockedError (upward-only risk rule enforced)
  - "agree": proceed

Contract review decisions:
  - "co-sign": returned to caller
  - "request_changes": raises ContractBlockedError (review.requested_changes available)
  - "reject": raises ContractBlockedError

Secret redaction:
  - task_description and context string values are redacted via SecretRedactor
    before being written into agent_task.json

Design: this module is standalone — it does not subclass Controller. It
accepts an EventLog, a run_dir, and launchers, and produces the two artifact
files (task_contract.json, contract_review.json) plus the event records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents.io import AgentTask, AgentWorkspace
from ..agents.launcher import AgentLauncher
from ..agents.monitor import AgentMonitor
from ..agents.redact import SecretRedactor
from ..errors import AnvilError
from ..schemas_util import assert_valid, validate_artifact
from ..timeutil import now_iso
from .events import EventLog

MAX_RETRIES = 2

_CONTRACT_GEN_AGENT = "claude-contract-gen"
_CONTRACT_REV_AGENT = "codex-contract-rev"


class ContractLoopError(AnvilError):
    """Raised when the contract loop cannot produce a valid contract."""


class ContractBlockedError(ContractLoopError):
    """Raised when a contract review or risk gate blocks forward progress.

    Carries the blocking decision and the review dict so callers can inspect
    the full Codex output (e.g. requested_changes) without re-reading disk.
    """

    def __init__(self, message: str, decision: str, review: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.decision = decision
        self.review: dict[str, Any] = review or {}


class ContractLoopRunner:
    """Drives contract generation + review using pluggable launchers."""

    def __init__(
        self,
        run_id: str,
        run_dir: Path,
        event_log: EventLog,
        claude_launcher: AgentLauncher,
        codex_launcher: AgentLauncher,
    ) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.event_log = event_log
        self.claude = claude_launcher
        self.codex = codex_launcher
        self.monitor = AgentMonitor()
        self._agents_dir = run_dir / "agents"
        self._redactor = SecretRedactor()

    # ------------------------------------------------------------------ #
    # Phase 1: contract generation
    # ------------------------------------------------------------------ #

    def generate_task_contract(
        self, task_description: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask a Claude-style worker to produce a valid task_contract.json.

        task_description and context string values are redacted before the
        agent task is written to disk. Retries up to MAX_RETRIES times on
        schema validation failure. Raises ContractLoopError if all attempts fail.
        """
        # Redact secrets before they reach the agent prompt.
        clean_desc, n_desc = self._redactor.redact(task_description)
        clean_context, n_ctx = self._redact_context(context or {})
        total_redactions = n_desc + n_ctx

        workspace = AgentWorkspace(self._agents_dir, _CONTRACT_GEN_AGENT)

        for attempt_num in range(MAX_RETRIES + 1):
            attempt_id = f"ATT-{attempt_num + 1:03d}"
            task = AgentTask(
                agent_id=_CONTRACT_GEN_AGENT,
                attempt_id=attempt_id,
                task_type="task_contract_generation",
                run_id=self.run_id,
                prompt=(
                    f"Generate a task_contract.json for the following task: {clean_desc}. "
                    "Output must conform to the task_contract schema."
                ),
                output_schema="task_contract",
                created_at=now_iso(),
                context=clean_context,
                redaction_count=total_redactions,
            )
            workspace.write_task(task)
            self.event_log.append(
                "agent_launched",
                actor=_CONTRACT_GEN_AGENT,
                details={"attempt_id": attempt_id, "task_type": "task_contract_generation"},
            )

            self.claude.launch(workspace, task)

            result = self.monitor.check(workspace, task)
            if result.complete:
                output = workspace.read_output()
                contract = output["output"]  # type: ignore[index]
                workspace.record_attempt(attempt_id, "success")
                self.event_log.append(
                    "agent_completed",
                    actor=_CONTRACT_GEN_AGENT,
                    details={"attempt_id": attempt_id},
                )
                # Write task_contract.json to the run dir.
                self._write_json("task_contract.json", contract)
                self.event_log.append("artifact_written", artifact_ref="task_contract.json")
                return contract

            # Attempt failed — record and retry (unless exhausted).
            schema_errors = result.schema_errors or result.reasons
            workspace.record_attempt(attempt_id, "schema_failure", schema_errors=schema_errors)
            self.event_log.append(
                "agent_retried" if attempt_num < MAX_RETRIES else "agent_failed",
                actor=_CONTRACT_GEN_AGENT,
                details={"attempt_id": attempt_id, "reasons": result.reasons},
            )
            if attempt_num == MAX_RETRIES:
                raise ContractLoopError(
                    f"Contract generation failed after {attempt_num + 1} attempt(s): "
                    + "; ".join(result.reasons)
                )

        raise ContractLoopError("Should not reach here")  # defensive

    # ------------------------------------------------------------------ #
    # Phase 2: contract review
    # ------------------------------------------------------------------ #

    def review_task_contract(
        self, contract: dict[str, Any], risk_mode: str = "fast"
    ) -> dict[str, Any]:
        """Ask a Codex-style reviewer to review task_contract.json.

        Retries up to MAX_RETRIES times on invalid output.

        Returns the contract_review dict only when decision == "co-sign"
        and risk_score_agreement != "disagree_lower".

        Raises:
          ContractBlockedError(decision="reject")           — Codex rejected
          ContractBlockedError(decision="request_changes")  — changes requested
          ContractBlockedError(decision="risk_blocked_disagree_lower") — risk downgrade blocked
          ContractLoopError                                 — schema failure exhausted retries
        """
        # Redact string context values before writing the review task.
        clean_contract_ctx, _ = self._redact_context(contract)
        workspace = AgentWorkspace(self._agents_dir, _CONTRACT_REV_AGENT)

        for attempt_num in range(MAX_RETRIES + 1):
            attempt_id = f"ATT-{attempt_num + 1:03d}"
            task = AgentTask(
                agent_id=_CONTRACT_REV_AGENT,
                attempt_id=attempt_id,
                task_type="task_contract_review",
                run_id=self.run_id,
                prompt=(
                    "Review the following task contract for clarity, completeness, and risk. "
                    "Return a contract_review with decision: co-sign | request_changes | reject."
                ),
                output_schema="contract_review",
                created_at=now_iso(),
                context={"task_contract": clean_contract_ctx, "current_mode": risk_mode},
            )
            workspace.write_task(task)
            self.event_log.append(
                "agent_launched",
                actor=_CONTRACT_REV_AGENT,
                details={"attempt_id": attempt_id, "task_type": "task_contract_review"},
            )

            self.codex.launch(workspace, task)

            result = self.monitor.check(workspace, task)
            if result.complete:
                output = workspace.read_output()
                review = output["output"]  # type: ignore[index]
                workspace.record_attempt(attempt_id, "success")
                self.event_log.append(
                    "agent_completed",
                    actor=_CONTRACT_REV_AGENT,
                    details={"attempt_id": attempt_id, "decision": review.get("decision")},
                )
                # Write contract_review.json to run dir.
                self._write_json("contract_review.json", review)
                self.event_log.append("artifact_written", artifact_ref="contract_review.json")

                # Handle risk disagreement (checked before decision gate).
                agreement = review.get("risk_score_agreement")
                if agreement == "disagree_lower":
                    self.event_log.append(
                        "gate_failed",
                        actor=_CONTRACT_REV_AGENT,
                        error=(
                            "Codex disagrees with risk score (lower) — mode downgrade blocked. "
                            "Upward-only risk escalation rule enforced."
                        ),
                    )
                    raise ContractBlockedError(
                        "Codex risk disagree_lower: mode downgrade blocked by upward-only rule.",
                        decision="risk_blocked_disagree_lower",
                        review=review,
                    )
                elif agreement == "disagree_higher":
                    self.event_log.append(
                        "risk_rescored",
                        actor=_CONTRACT_REV_AGENT,
                        details={"codex_flags": "disagree_higher", "action": "escalation_recommended"},
                    )

                # Gate on review decision — only "co-sign" is a success path.
                review_decision = review.get("decision")
                if review_decision in ("request_changes", "reject"):
                    self.event_log.append(
                        "gate_failed",
                        actor=_CONTRACT_REV_AGENT,
                        error=f"Contract review blocked: decision={review_decision}",
                    )
                    raise ContractBlockedError(
                        f"Contract review decision '{review_decision}' blocks forward progress.",
                        decision=review_decision,
                        review=review,
                    )

                return review

            schema_errors = result.schema_errors or result.reasons
            workspace.record_attempt(attempt_id, "schema_failure", schema_errors=schema_errors)
            self.event_log.append(
                "agent_retried" if attempt_num < MAX_RETRIES else "agent_failed",
                actor=_CONTRACT_REV_AGENT,
                details={"attempt_id": attempt_id, "reasons": result.reasons},
            )
            if attempt_num == MAX_RETRIES:
                raise ContractLoopError(
                    f"Contract review failed after {attempt_num + 1} attempt(s): "
                    + "; ".join(result.reasons)
                )

        raise ContractLoopError("Should not reach here")  # defensive

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _redact_context(self, ctx: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Return (clean_ctx, total_redaction_count) for a context dict.

        Only top-level string values are redacted. Nested objects (like a
        task_contract dict) have their own string-leaf values redacted one
        level deep.
        """
        clean: dict[str, Any] = {}
        count = 0
        for k, v in ctx.items():
            if isinstance(v, str):
                clean_v, n = self._redactor.redact(v)
                clean[k] = clean_v
                count += n
            elif isinstance(v, dict):
                clean_sub: dict[str, Any] = {}
                for sk, sv in v.items():
                    if isinstance(sv, str):
                        clean_sv, n = self._redactor.redact(sv)
                        clean_sub[sk] = clean_sv
                        count += n
                    else:
                        clean_sub[sk] = sv
                clean[k] = clean_sub
            else:
                clean[k] = v
        return clean, count

    def _write_json(self, filename: str, payload: dict[str, Any]) -> None:
        import json

        path = self.run_dir / filename
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
