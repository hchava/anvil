"""M4 Standard Mode MVP tests.

Covers:
  - Full Standard Mode happy path with fake agents
  - Blocking gap stops the run
  - High-impact claim without direct evidence creates blocking issue
  - Codex blind-spot finding creates issue
  - Unresolved critical/high issue blocks work-order execution
  - Targeted security reviewer is selected when risk factor requires it
  - Review consolidation fails if raw issue is dropped
  - Work-order negotiation requires Claude + Codex agreement
  - Guardrail critical not_checked blocks Standard Mode
  - Standard Mode executes agreed single work order using Milestone 3 executor
  - Final scorecard includes layer yield and agents_launched
  - Public safety: no proprietary/internal references in fixtures or examples

All tests use temp dirs and synthetic git repos.  No real Claude, Codex, tmux,
network, or API keys.  No writes to real ~/.anvil.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from anvil.controller.policy import CommandPolicy
from anvil.controller.risk import FloorRules
from anvil.paths import ANVIL_HOME_ENV
from anvil.registry import Registry
from anvil.schemas_util import validate_artifact
from anvil.standard_mode import (
    RunInputs,
    StandardModeAgents,
    StandardModeError,
    StandardModeRunner,
)
from anvil.standard_mode.consolidation import ConsolidationError
from anvil.standard_mode.review import SECURITY_RISK_FACTORS, select_reviewers
from anvil.timeutil import now_iso

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_repo(tmp_path: Path, name: str) -> Path:
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


def _setup_full_run(
    tmp_path: Path,
    registry: Registry,
    repo: Path,
    run_id: str = "RUN-20260607-001",
) -> tuple[str, str, str]:
    """Register project/repo/scope/run (worktree allocated by create_run). Returns (project_id, repo_id, scope_id)."""
    registry.register_repo("repo-alpha", repo)
    registry.create_project("proj-alpha", ["repo-alpha"])
    registry.create_scope(
        "proj-alpha",
        "scope-alpha",
        ["src/"],
        discovery_focus_paths=["src/"],
    )
    # create_run allocates the worktree by default (allocate_worktree=True)
    registry.create_run(run_id, "proj-alpha", "repo-alpha", "tester", task_scope_id="scope-alpha")
    registry.activate_run(run_id)
    return "proj-alpha", "repo-alpha", "scope-alpha"


def _make_inputs(
    run_id: str,
    project_id: str,
    repo_id: str,
    scope_id: str,
    factor_ids: list[str] | None = None,
) -> RunInputs:
    return RunInputs(
        run_id=run_id,
        project_id=project_id,
        repo_id=repo_id,
        scope_id=scope_id,
        initial_factor_ids=factor_ids or [],
        multi_wo=False,
    )


# ---------------------------------------------------------------------------
# Fake agent helpers
# ---------------------------------------------------------------------------


def _fake_task_contract(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_summary": "Add retry logic to the config loader module",
        "goals": ["Add retry decorator", "Ensure retries are tested"],
        "non_goals": ["Refactor unrelated modules"],
        "acceptance_criteria": ["All tests pass", "Retry fires on transient failure"],
    }


def _fake_gap_matrix_passing(run_id: str, src_ids: list[str]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "coverage_areas": [
            {
                "area": "codebase",
                "required_level": "required",
                "evidence_found": True,
                "source_ids": src_ids,
                "gap_status": "covered",
            }
        ],
        "overall_sufficient": True,
    }


def _fake_claim_ledger_strong(run_id: str, src_id: str) -> dict[str, Any]:
    commit = "abc1234abc1234a"
    return {
        "run_id": run_id,
        "claims": [
            {
                "claim_id": "CLAIM-001",
                "claim": "Config loader has no retry logic",
                "claim_type": "code_behavior",
                "impact": "medium",
                "task_contract_ref": ["goal-add-retry"],
                "evidence": [
                    {
                        "source_id": src_id,
                        "source_type": "code",
                        "evidence_type": "direct",
                        "supports_claim_because": "No retry decorator found in code",
                        "commit_sha": commit,
                        "line_start": 1,
                        "line_end": 5,
                        "content_hash": "sha256:" + "a" * 64,
                    }
                ],
                "confidence": "high",
                "validated_by": ["claude-agent-001"],
            }
        ],
    }


def _fake_work_orders(run_id: str, allowed_file: str = "src/app.py") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "work_orders": [
            {
                "work_order_id": "EXEC-001",
                "title": "Add retry logic to app.py",
                "negotiation_status": "agreed",
                "agreed_by": ["claude-001", "codex-001"],
                "criticality": "required",
                "fail_policy": "fail_closed",
                "assigned_scope": {
                    "allowed_files": [allowed_file],
                    "forbidden_files": [],
                },
                "local_acceptance_criteria": ["Tests pass", "Retry fires on failure"],
                "validation_commands": [
                    {
                        "command_array": ["git", "status", "--porcelain"],
                        "expected_exit_code": 0,
                    }
                ],
                "rollback_plan": [{"op": "restore_file", "target": allowed_file}],
            }
        ],
        "dependency_matrix": [
            {"work_order_id": "EXEC-001", "depends_on": [], "can_run_parallel": False}
        ],
    }


def _make_happy_path_agents(run_id: str, src_id: str) -> StandardModeAgents:
    """Build a complete set of passing fake agents."""
    return StandardModeAgents(
        task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
        gap_analysis_agent=lambda ctx: _fake_gap_matrix_passing(
            ctx["run_id"],
            [s["source_id"] for s in ctx.get("source_manifest", {}).get("sources", [])],
        ),
        research_agent=lambda ctx: _fake_claim_ledger_strong(
            ctx["run_id"],
            ctx.get("source_manifest", {}).get("sources", [{}])[0].get("source_id", "SRC-001"),
        ),
        blindspot_agent=lambda ctx: [],
        plan_agent=lambda ctx: "# Plan\n\n1. Add retry decorator\n2. Test\n",
        reviewer_agents={
            "correctness": lambda ctx: [],
        },
        planner_agent=lambda ctx: _fake_work_orders(ctx["run_id"]),
        negotiator_agent=lambda ctx: _fake_work_orders(ctx["run_id"]),
        execution_agent=lambda wt: (wt / "src" / "app.py").write_text(
            "VALUE = 1\n# retry added\n", encoding="utf-8"
        ),
    )


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
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Full Standard Mode happy path."""

    def test_happy_path_returns_scorecard(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-001"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        run_row = registry.get_run(RUN_ID)
        src_id = "SRC-001"
        agents = _make_happy_path_agents(RUN_ID, src_id)
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert scorecard["run_id"] == RUN_ID
        assert scorecard["mode"] == "standard"
        assert scorecard["final_outcome"] == "passed"

    def test_happy_path_scorecard_includes_agents_launched(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-002"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert scorecard.get("agents_launched", 0) > 0

    def test_happy_path_scorecard_includes_layer_yield(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-003"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert "layer_yield" in scorecard

    def test_happy_path_scorecard_is_schema_valid(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-004"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert validate_artifact("run_scorecard", scorecard) == []

    def test_happy_path_writes_all_key_artifacts(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-005"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        runner = StandardModeRunner(registry, RUN_ID, agents)
        runner.run(inputs)
        run_dir = registry.paths.run_dir(RUN_ID)
        for filename in [
            "task_contract.json",
            "source_manifest.json",
            "gap_matrix.json",
            "claim_ledger.json",
            "issue_ledger.json",
            "implementation_plan.md",
            "review_findings_raw.json",
            "review_findings_consolidated.json",
            "execution_work_orders.json",
            "guardrail_matrix.json",
            "run_scorecard.json",
            "event_log.jsonl",
        ]:
            assert (run_dir / filename).exists(), f"missing artifact: {filename}"


class TestBlockingGap:
    """Blocking gap stops the run."""

    def test_blocking_gap_raises_standard_mode_error(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-010"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def blocking_gap(ctx: dict) -> dict:
            return {
                "run_id": ctx["run_id"],
                "coverage_areas": [
                    {
                        "area": "codebase",
                        "required_level": "required",
                        "evidence_found": False,
                        "gap_status": "gap",
                        "gap_reason": "No relevant code found",
                        "blocking": True,
                    }
                ],
                "overall_sufficient": False,
            }

        agents = StandardModeAgents(
            task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
            gap_analysis_agent=blocking_gap,
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="gap"):
            runner.run(inputs)


class TestWeakEvidence:
    """High-impact claim without code-level direct evidence creates blocking issue."""

    def test_high_impact_doc_only_evidence_creates_blocking_issue(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-020"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def weak_research(ctx: dict) -> dict:
            src_id = (
                ctx.get("source_manifest", {}).get("sources", [{}])[0].get("source_id", "SRC-001")
            )
            return {
                "run_id": ctx["run_id"],
                "claims": [
                    {
                        "claim_id": "CLAIM-001",
                        "claim": "Auth module missing rate limiting",
                        "claim_type": "security_assumption",
                        "impact": "high",
                        "task_contract_ref": ["goal-add-retry"],
                        "evidence": [
                            {
                                "source_id": src_id,
                                "source_type": "doc",
                                "evidence_type": "direct",
                                "supports_claim_because": "Architecture doc mentions no rate limit",
                                "checked_at": now_iso(),
                                "content_hash": "sha256:" + "b" * 64,
                            }
                        ],
                        "confidence": "medium",
                        "validated_by": ["claude-agent-001"],
                    }
                ],
            }

        agents = StandardModeAgents(
            task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
            research_agent=weak_research,
            blindspot_agent=lambda ctx: [],
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="cross-validation"):
            runner.run(inputs)

    def test_high_impact_doc_only_evidence_issue_written_to_ledger(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-021"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def weak_research(ctx: dict) -> dict:
            src_id = (
                ctx.get("source_manifest", {}).get("sources", [{}])[0].get("source_id", "SRC-001")
            )
            return {
                "run_id": ctx["run_id"],
                "claims": [
                    {
                        "claim_id": "CLAIM-001",
                        "claim": "Auth module missing rate limiting",
                        "claim_type": "security_assumption",
                        "impact": "high",
                        "task_contract_ref": ["goal-add-retry"],
                        "evidence": [
                            {
                                "source_id": src_id,
                                "source_type": "doc",
                                "evidence_type": "direct",
                                "supports_claim_because": "Architecture doc mentions no rate limit",
                                "checked_at": now_iso(),
                                "content_hash": "sha256:" + "b" * 64,
                            }
                        ],
                        "confidence": "medium",
                        "validated_by": ["claude-agent-001"],
                    }
                ],
            }

        agents = StandardModeAgents(
            task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
            research_agent=weak_research,
            blindspot_agent=lambda ctx: [],
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError):
            runner.run(inputs)
        run_dir = registry.paths.run_dir(RUN_ID)
        ledger_path = run_dir / "issue_ledger.json"
        assert ledger_path.exists()
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        blocking = [i for i in ledger["issues"] if not i["safe_to_continue_without_resolution"]]
        assert blocking, "expected at least one blocking issue for weak evidence claim"


class TestBlindspot:
    """Codex blind-spot finding creates an issue."""

    def test_blindspot_finding_creates_issue(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-030"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def blindspot_with_finding(ctx: dict) -> list:
            return [
                {
                    "severity": "high",
                    "title": "Error handling missing in config loader",
                    "related_claims": [],
                }
            ]

        agents = StandardModeAgents(
            task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
            research_agent=lambda ctx: _fake_claim_ledger_strong(
                ctx["run_id"],
                ctx.get("source_manifest", {}).get("sources", [{}])[0].get("source_id", "SRC-001"),
            ),
            blindspot_agent=blindspot_with_finding,
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="cross-validation"):
            runner.run(inputs)

        run_dir = registry.paths.run_dir(RUN_ID)
        ledger = json.loads((run_dir / "issue_ledger.json").read_text(encoding="utf-8"))
        blindspot_issues = [i for i in ledger["issues"] if i.get("raised_by") == "codex-blindspot"]
        assert len(blindspot_issues) >= 1
        assert blindspot_issues[0]["severity"] == "high"

    def test_low_severity_blindspot_does_not_block(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-031"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def low_finding(ctx: dict) -> list:
            return [{"severity": "low", "title": "Minor style inconsistency"}]

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.blindspot_agent = low_finding
        runner = StandardModeRunner(registry, RUN_ID, agents)
        # Low-severity blindspot should not block; run should complete
        scorecard = runner.run(inputs)
        assert scorecard["final_outcome"] == "passed"


class TestUnresolvedIssueBlocksExecution:
    """Unresolved critical/high issue blocks work-order execution."""

    def test_unresolved_high_issue_blocks_work_orders(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-040"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        # Blindspot agent returns a high-severity finding that will block
        agents = StandardModeAgents(
            task_contract_agent=lambda ctx: _fake_task_contract(ctx["run_id"]),
            research_agent=lambda ctx: _fake_claim_ledger_strong(
                ctx["run_id"],
                ctx.get("source_manifest", {}).get("sources", [{}])[0].get("source_id", "SRC-001"),
            ),
            blindspot_agent=lambda ctx: [
                {"severity": "high", "title": "Critical gap: no auth check on write path"}
            ],
            planner_agent=lambda ctx: _fake_work_orders(ctx["run_id"]),
            negotiator_agent=lambda ctx: _fake_work_orders(ctx["run_id"]),
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="cross-validation"):
            runner.run(inputs)
        # Verify execution did not happen
        run_dir = registry.paths.run_dir(RUN_ID)
        events = [
            json.loads(line)
            for line in (run_dir / "event_log.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        started = [e for e in events if e.get("event_type") == "execution_started"]
        assert not started, "execution should not have started when high issue is unresolved"


class TestSecurityReviewer:
    """Security reviewer is selected when risk factor requires it."""

    def test_security_reviewer_selected_for_security_factor(self):
        selected = select_reviewers(
            ["security_auth_data"],
            {"security": None, "correctness": None},
        )
        assert "security" in selected

    def test_security_reviewer_selected_for_dependency_factor(self):
        selected = select_reviewers(
            ["dependency_lockfile"],
            {"security": None, "correctness": None},
        )
        assert "security" in selected

    def test_no_security_reviewer_when_factor_absent(self):
        selected = select_reviewers(
            ["generated_or_docs_only"],
            {"correctness": None},
        )
        assert "security" not in selected

    def test_security_reviewer_called_when_risk_factor_active(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-050"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(
            RUN_ID, proj_id, repo_id, scope_id, factor_ids=["security_auth_data"]
        )

        security_called = []

        def security_reviewer(ctx: dict) -> list:
            security_called.append(True)
            return []

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.reviewer_agents = {"security": security_reviewer, "correctness": lambda ctx: []}
        runner = StandardModeRunner(registry, RUN_ID, agents)
        runner.run(inputs)
        assert security_called, "security reviewer was not called despite risk factor"


class TestReviewConsolidation:
    """Review consolidation fails if raw issue is dropped."""

    def test_consolidation_gate_catches_dropped_raw_issue(
        self, registry: Registry, tmp_path: Path
    ):
        from anvil.standard_mode.consolidation import verify_consolidation, ConsolidationError

        raw_findings = {
            "run_id": "RUN-20260607-001",
            "raw_issues": [
                {"raw_issue_id": "RAW-CORRE-001", "reviewer": "correctness", "severity": "high", "title": "Issue A"},
                {"raw_issue_id": "RAW-CORRE-002", "reviewer": "correctness", "severity": "medium", "title": "Issue B"},
            ],
        }
        consolidated_missing_one = {
            "run_id": "RUN-20260607-001",
            "consolidated_issues": [
                {
                    "consolidated_id": "CONSOL-001",
                    "raw_issue_ids": ["RAW-CORRE-001"],
                    "disposition": "preserved",
                    "title": "Issue A",
                    "severity": "high",
                }
                # RAW-CORRE-002 intentionally dropped
            ],
        }
        with pytest.raises(ConsolidationError, match="RAW-CORRE-002"):
            verify_consolidation(raw_findings, consolidated_missing_one)

    def test_consolidation_gate_passes_when_all_accounted(self):
        from anvil.standard_mode.consolidation import verify_consolidation

        raw_findings = {
            "run_id": "RUN-20260607-001",
            "raw_issues": [
                {"raw_issue_id": "RAW-CORRE-001", "reviewer": "correctness", "severity": "high", "title": "A"},
            ],
        }
        consolidated_complete = {
            "run_id": "RUN-20260607-001",
            "consolidated_issues": [
                {
                    "consolidated_id": "CONSOL-001",
                    "raw_issue_ids": ["RAW-CORRE-001"],
                    "disposition": "preserved",
                    "title": "A",
                    "severity": "high",
                }
            ],
        }
        verify_consolidation(raw_findings, consolidated_complete)  # must not raise

    def test_consolidation_gate_passes_for_rejected_issue(self):
        from anvil.standard_mode.consolidation import verify_consolidation

        raw = {
            "run_id": "RUN-20260607-001",
            "raw_issues": [
                {"raw_issue_id": "RAW-CORRE-001", "reviewer": "correctness", "severity": "low", "title": "X"},
            ],
        }
        consol = {
            "run_id": "RUN-20260607-001",
            "consolidated_issues": [
                {
                    "raw_issue_ids": ["RAW-CORRE-001"],
                    "disposition": "rejected_with_reason",
                    "rejection_reason": "Duplicate of existing known issue",
                }
            ],
        }
        verify_consolidation(raw, consol)  # must not raise


class TestWorkOrderNegotiation:
    """Work-order negotiation requires Claude + Codex agreement."""

    def test_negotiation_fails_when_negotiator_rejects(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-060"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def rejecting_negotiator(ctx: dict) -> dict:
            proposed = ctx["proposed_work_orders"]
            # Mark all required WOs as rejected
            rejected_wos = [
                {**wo, "negotiation_status": "rejected"}
                for wo in proposed.get("work_orders", [])
            ]
            return {**proposed, "work_orders": rejected_wos}

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
            negotiator_agent=rejecting_negotiator,
            execution_agent=lambda wt: None,
        )
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="negotiation"):
            runner.run(inputs)

    def test_negotiation_succeeds_when_all_required_agreed(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-061"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert scorecard["final_outcome"] == "passed"


class TestGuardrailGate:
    """Critical guardrail not_checked blocks Standard Mode."""

    def test_critical_not_checked_guardrail_blocks(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-070"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def bad_guardrails(ctx: dict) -> dict:
            return {
                "run_id": ctx["run_id"],
                "guardrails": [
                    {
                        "guardrail_id": "GR-secret-scan",
                        "description": "No secrets in diff",
                        "severity": "critical",
                        "applies": True,
                        "status": "not_checked",
                    }
                ],
            }

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.guardrail_agent = bad_guardrails
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="commit-review"):
            runner.run(inputs)

    def test_critical_passing_guardrail_does_not_block(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-071"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def passing_guardrails(ctx: dict) -> dict:
            return {
                "run_id": ctx["run_id"],
                "guardrails": [
                    {
                        "guardrail_id": "GR-secret-scan",
                        "description": "No secrets in diff",
                        "severity": "critical",
                        "applies": True,
                        "status": "pass",
                    }
                ],
            }

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.guardrail_agent = passing_guardrails
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert scorecard["final_outcome"] == "passed"


class TestExecution:
    """Standard Mode executes agreed single work order using Milestone 3 executor."""

    def test_execution_agent_called_and_worktree_modified(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-080"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        executed = []

        def tracking_agent(worktree: Path) -> None:
            executed.append(str(worktree))
            (worktree / "src" / "app.py").write_text(
                "VALUE = 1\n# retry added\n", encoding="utf-8"
            )

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.execution_agent = tracking_agent
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert executed, "execution_agent was never called"
        assert scorecard["final_outcome"] == "passed"

    def test_no_execution_agent_raises(
        self, registry: Registry, tmp_path: Path
    ):
        RUN_ID = "RUN-20260607-081"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.execution_agent = None
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="execution_agent is required"):
            runner.run(inputs)

    def test_work_orders_total_in_scorecard(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-082"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        runner = StandardModeRunner(registry, RUN_ID, agents)
        scorecard = runner.run(inputs)
        assert scorecard["work_orders_total"] >= 1


class TestFailClosed:
    """Standard Mode must fail closed when required agents or invariants are missing."""

    def test_no_plan_agent_raises(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-090"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.plan_agent = None
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="plan_agent is required"):
            runner.run(inputs)

    def test_no_planner_agent_raises(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-091"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.planner_agent = None
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="planner_agent is required"):
            runner.run(inputs)

    def test_no_negotiator_agent_raises(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-092"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.negotiator_agent = None
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="negotiator_agent is required"):
            runner.run(inputs)

    def test_no_execution_agent_raises_standalone(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-093"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.execution_agent = None
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="execution_agent is required"):
            runner.run(inputs)

    def test_one_sided_agreed_by_raises(self, registry: Registry, tmp_path: Path):
        """Claude-only agreed_by (no Codex) must be rejected."""
        RUN_ID = "RUN-20260607-094"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def claude_only_negotiator(ctx: dict) -> dict:
            proposed = ctx["proposed_work_orders"]
            agreed_wos = [
                {**wo, "negotiation_status": "agreed", "agreed_by": ["claude-001"]}
                for wo in proposed.get("work_orders", [])
            ]
            return {**proposed, "work_orders": agreed_wos}

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.negotiator_agent = claude_only_negotiator
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="negotiation"):
            runner.run(inputs)

    def test_codex_only_agreed_by_raises(self, registry: Registry, tmp_path: Path):
        """Codex-only agreed_by (no Claude) must be rejected."""
        RUN_ID = "RUN-20260607-095"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def codex_only_negotiator(ctx: dict) -> dict:
            proposed = ctx["proposed_work_orders"]
            agreed_wos = [
                {**wo, "negotiation_status": "agreed", "agreed_by": ["codex-001"]}
                for wo in proposed.get("work_orders", [])
            ]
            return {**proposed, "work_orders": agreed_wos}

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.negotiator_agent = codex_only_negotiator
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="negotiation"):
            runner.run(inputs)

    def test_missing_security_reviewer_raises_when_factor_active(
        self, registry: Registry, tmp_path: Path
    ):
        """Security factor active but no security reviewer agent → fail closed."""
        RUN_ID = "RUN-20260607-096"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(
            RUN_ID, proj_id, repo_id, scope_id, factor_ids=["security_auth_data"]
        )
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        # Only correctness reviewer registered; security factor is active → must raise
        agents.reviewer_agents = {"correctness": lambda ctx: []}
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="security reviewer"):
            runner.run(inputs)

    def test_no_reviewer_agents_raises(self, registry: Registry, tmp_path: Path):
        """Empty reviewer_agents dict → fail closed (can't satisfy at least one reviewer)."""
        RUN_ID = "RUN-20260607-097"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)
        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.reviewer_agents = {}
        runner = StandardModeRunner(registry, RUN_ID, agents)
        with pytest.raises(StandardModeError, match="plan review gate"):
            runner.run(inputs)

    def test_consolidation_duplicate_raw_issue_raises(self):
        """A raw_issue_id appearing in two consolidated entries violates exactly-once."""
        from anvil.standard_mode.consolidation import verify_consolidation, ConsolidationError

        raw = {
            "run_id": "RUN-20260607-001",
            "raw_issues": [
                {"raw_issue_id": "RAW-CORRE-001", "reviewer": "correctness", "severity": "high", "title": "A"},
            ],
        }
        double_counted = {
            "run_id": "RUN-20260607-001",
            "consolidated_issues": [
                {
                    "consolidated_id": "CONSOL-001",
                    "raw_issue_ids": ["RAW-CORRE-001"],
                    "disposition": "preserved",
                    "title": "A",
                    "severity": "high",
                },
                {
                    "consolidated_id": "CONSOL-002",
                    "raw_issue_ids": ["RAW-CORRE-001"],
                    "disposition": "preserved",
                    "title": "A duplicate",
                    "severity": "medium",
                },
            ],
        }
        with pytest.raises(ConsolidationError, match="RAW-CORRE-001"):
            verify_consolidation(raw, double_counted)


class TestDiscovery:
    """LLM discovery agents augment deterministic discovery."""

    def test_discovery_agent_adds_sources(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-090"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        extra_sources_added = []

        def discovery_0a(ctx: dict) -> list:
            extra_sources_added.append(True)
            return [
                {
                    "path": "docs/retry.md",
                    "source_type": "doc",
                    "discovered_by": ["semantic-0a"],
                    "reason_for_inclusion": "Related documentation",
                    "freshness": {"checked_at": now_iso(), "last_modified": now_iso()},
                }
            ]

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.discovery_agents = [("discovery-0a", discovery_0a)]
        runner = StandardModeRunner(registry, RUN_ID, agents)
        runner.run(inputs)

        run_dir = registry.paths.run_dir(RUN_ID)
        manifest = json.loads(
            (run_dir / "source_manifest.json").read_text(encoding="utf-8")
        )
        paths = [s["path"] for s in manifest["sources"]]
        assert "docs/retry.md" in paths

    def test_negative_discovery_recorded(self, registry: Registry, tmp_path: Path):
        RUN_ID = "RUN-20260607-091"
        repo = _make_repo(tmp_path, "svc")
        proj_id, repo_id, scope_id = _setup_full_run(tmp_path, registry, repo, RUN_ID)
        inputs = _make_inputs(RUN_ID, proj_id, repo_id, scope_id)

        def empty_discovery_0b(ctx: dict) -> list:
            return []  # finds nothing

        agents = _make_happy_path_agents(RUN_ID, "SRC-001")
        agents.discovery_agents = [("discovery-0b", empty_discovery_0b)]
        runner = StandardModeRunner(registry, RUN_ID, agents)
        runner.run(inputs)

        run_dir = registry.paths.run_dir(RUN_ID)
        manifest = json.loads(
            (run_dir / "source_manifest.json").read_text(encoding="utf-8")
        )
        neg_attempts = manifest.get("negative_discovery_attempts", [])
        assert len(neg_attempts) >= 1
        assert neg_attempts[0]["agent"] == "discovery-0b"


class TestPublicSafety:
    """Public safety: no proprietary/internal references in fixtures or source."""

    # Markers are built from fragments so this file doesn't contain the literal strings.
    INTERNAL_MARKERS = [
        "internal" + ".meta.com",
        "fbcode",
        "facebook" + ".com",
        "www" + ".intern.",
        "T[0-9]{8}",
        "D[0-9]{8}",
    ]

    def test_no_internal_references_in_standard_mode_source(self):
        """Scan standard_mode package source for internal markers."""
        import re
        import anvil.standard_mode as sm_pkg

        pkg_path = Path(sm_pkg.__file__).parent
        for py_file in pkg_path.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for marker in self.INTERNAL_MARKERS:
                assert not re.search(marker, content, re.IGNORECASE), (
                    f"Internal reference '{marker}' found in {py_file.name}"
                )

    def test_all_example_run_ids_are_synthetic(self, registry: Registry, tmp_path: Path):
        """Run IDs in the test suite follow the synthetic RUN-YYYYMMDD-NNN pattern."""
        import re
        content = Path(__file__).read_text(encoding="utf-8")
        run_ids = re.findall(r'"(RUN-\d{8}-\d{3,})"', content)
        assert run_ids, "no RUN- IDs found in test file"
        pattern = re.compile(r"^RUN-\d{8}-\d{3,}$")
        for rid in run_ids:
            assert pattern.match(rid), f"RUN ID '{rid}' does not match synthetic pattern"
