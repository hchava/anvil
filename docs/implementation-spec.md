# Edge Harness: Phase 1 + Standard Mode MVP Implementation Specification

## Based on v2.2 Architecture — Implementation Detail Pass

**Purpose**: This document converts the v2.2 conceptual architecture into buildable specifications. It defines the controller state machine, JSON schemas for every core artifact, gate logic, failure semantics, and concrete protocols. After review, this document should be sufficient to start writing code.

**Scope**: Phase 1 (deterministic controller skeleton) and Phase 2 (Standard Mode MVP) from the v2.2 build order. Critical Mode expansion (Phase 3) is referenced where it affects design but not fully specified.

---

## Part 1: Controller State Machine

### 1.1 States

```
INIT                        → Run workspace created, manifest written
BASELINE_CAPTURED           → Pre-change test/lint/type results recorded
TASK_CONTRACT_PROPOSED      → Claude proposed task contract
TASK_CONTRACT_ACCEPTED      → Codex reviewed, controller validated schema, mode set
SOURCES_DISCOVERED          → Deterministic + LLM discovery complete
SOURCE_GAPS_RESOLVED        → Gap analysis complete, coverage sufficient
CLAIMS_RESEARCHED           → Claim ledger populated
BLINDSPOT_SCAN_COMPLETE     → Codex independent review complete, initial issues filed
CLAIMS_STRESS_TESTED        → Layer 2 scrutiny complete (Critical Mode only)
CROSS_VALIDATION_COMPLETE   → Issue-based debate complete, all blocking issues resolved
PLAN_CREATED                → Implementation plan produced
PLAN_REVIEWED               → 5-agent review complete, feedback incorporated (Critical Mode only)
WORK_ORDERS_AGREED          → Claude-Codex negotiation complete, all work orders agreed
EXECUTION_COMPLETE          → All work orders executed, per-WO validation passed
INTEGRATION_VALIDATED       → System integration work order passed (multi-WO runs only)
COMMIT_REVIEWED             → Codex commit review complete, all blocking issues resolved
FINALIZED                   → Run scorecard written, run complete
ABORTED                     → Run terminated due to unrecoverable failure
HUMAN_ESCALATED             → Waiting for human decision on blocked item
```

### 1.2 State Transition Table

Each row defines: current state, trigger, gate checks the controller runs, next state on pass, behavior on fail.

```
┌─────────────────────────────┬──────────────────────────┬──────────────────────────────────────┬─────────────────────────────┬──────────────────────────────────┐
│ Current State               │ Trigger                  │ Controller Gate Checks               │ Next State (pass)           │ On Fail                          │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ (start)                     │ run initiated            │ workspace created                    │ INIT                        │ abort                            │
│                             │                          │ run_manifest.json valid              │                             │                                  │
│                             │                          │ event_log.jsonl writable             │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ INIT                        │ baseline commands finish  │ baseline_validation.json valid       │ BASELINE_CAPTURED           │ abort (can't establish baseline)  │
│                             │                          │ all commands recorded with exit codes │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ BASELINE_CAPTURED           │ Claude returns contract  │ task_contract.json schema valid      │ TASK_CONTRACT_PROPOSED       │ retry (max 2) → abort             │
│                             │                          │ goals non-empty                      │                             │                                  │
│                             │                          │ acceptance_criteria non-empty         │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ TASK_CONTRACT_PROPOSED      │ Codex review complete    │ Codex co-sign recorded               │ TASK_CONTRACT_ACCEPTED      │ re-negotiate (max 2) → escalate   │
│                             │                          │ risk_assessment.json valid            │                             │                                  │
│                             │                          │ mode set (fast/standard/critical)     │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ TASK_CONTRACT_ACCEPTED      │ discovery agents done    │ source_manifest.json schema valid    │ SOURCES_DISCOVERED          │ retry discovery → escalate        │
│                             │                          │ ≥1 source found                      │                             │                                  │
│                             │                          │ all sources have freshness metadata   │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ SOURCES_DISCOVERED          │ gap analysis done        │ gap_matrix.json schema valid         │ SOURCE_GAPS_RESOLVED        │ re-run discovery with broader     │
│                             │                          │ no required gaps unaddressed          │                             │ params (max 2) → escalate         │
│                             │                          │ risk re-scored (discovery stage)      │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ SOURCE_GAPS_RESOLVED        │ research agents done     │ claim_ledger.json schema valid       │ CLAIMS_RESEARCHED           │ retry failed agents (max 2) →     │
│                             │                          │ all high-impact claims have direct    │                             │ continue with gap logged (std)    │
│                             │                          │   evidence                           │                             │ escalate (critical)               │
│                             │                          │ all claims ref task_contract          │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ CLAIMS_RESEARCHED           │ Codex blindspot done     │ codex review file exists             │ BLINDSPOT_SCAN_COMPLETE     │ retry Codex (max 2) → continue    │
│                             │                          │ new issues filed in issue_ledger     │                             │ without blindspot scan            │
│                             │                          │ no critical blindspot issues open     │                             │ (standard) / escalate (critical)  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ BLINDSPOT_SCAN_COMPLETE     │ [Standard Mode]          │ (skip Layer 2 in Standard)           │ CROSS_VALIDATION_COMPLETE   │ —                                │
│                             │ [Critical Mode]          │ scrutiny_findings.json valid         │ CLAIMS_STRESS_TESTED        │ retry → escalate                  │
│                             │ stress test done         │ all high-impact claims stress-tested │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ CLAIMS_STRESS_TESTED        │ cross-validation done    │ issue_ledger.json valid              │ CROSS_VALIDATION_COMPLETE   │ escalate unresolved critical/     │
│ (or BLINDSPOT_SCAN_COMPLETE │                          │ no critical/high issues open          │                             │ high issues to human              │
│  in Standard Mode)          │                          │ all closures have evidence            │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ CROSS_VALIDATION_COMPLETE   │ plan agents done         │ implementation_plan.md exists        │ PLAN_CREATED                │ retry → escalate                  │
│                             │                          │ plan references task_contract         │                             │                                  │
│                             │                          │ risk re-scored (plan stage)           │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ PLAN_CREATED                │ [Standard Mode]          │ (skip 5-agent review in Standard     │ PLAN_REVIEWED               │ —                                │
│                             │                          │  unless risk re-score elevated mode)  │                             │                                  │
│                             │ [Critical Mode]          │ review_findings_raw.json valid       │ PLAN_REVIEWED               │ retry → escalate                  │
│                             │ review panel done        │ review_findings_consolidated.json    │                             │                                  │
│                             │                          │   valid                              │                             │                                  │
│                             │                          │ every raw issue accounted for in      │                             │                                  │
│                             │                          │   consolidated (preserved/merged/     │                             │                                  │
│                             │                          │   rejected_with_reason)              │                             │                                  │
│                             │                          │ Codex reviewed consolidation diff     │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ PLAN_REVIEWED               │ WO negotiation done      │ execution_work_orders.json valid     │ WORK_ORDERS_AGREED          │ re-negotiate (max 2) → escalate   │
│                             │                          │ all required WOs status = "agreed"    │                             │                                  │
│                             │                          │ both Claude + Codex in agreed_by      │                             │                                  │
│                             │                          │ integration WO exists if multi-WO     │                             │                                  │
│                             │                          │ dependency matrix has no cycles        │                             │                                  │
│                             │                          │ file ownership has no conflicts        │                             │                                  │
│                             │                          │ risk re-scored (plan stage)           │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ WORK_ORDERS_AGREED          │ all WOs executed         │ per-WO: validation_commands pass     │ EXECUTION_COMPLETE          │ mode-sensitive:                    │
│                             │                          │ per-WO: no files outside allowed      │                             │ required WO fail → fail-closed    │
│                             │                          │ per-WO: diff artifact exists          │                             │   or escalate                     │
│                             │                          │ risk re-scored (execution stage)      │                             │ optional WO fail → continue       │
│                             │                          │ no new test failures vs baseline       │                             │   with gap logged                 │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ EXECUTION_COMPLETE          │ [single WO run]          │ (skip integration)                   │ COMMIT_REVIEWED             │ —                                │
│                             │ [multi-WO run]           │ integration WO validation passes     │ INTEGRATION_VALIDATED       │ create issues for failures →      │
│                             │ integration done         │ full affected test suite passes       │                             │ rework or escalate                │
│                             │                          │ no new failures vs baseline            │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ INTEGRATION_VALIDATED       │ commit review done       │ codex_commit_review exists           │ COMMIT_REVIEWED             │ rework → re-review (max 3) →      │
│ (or EXECUTION_COMPLETE      │                          │ no critical/high issues open          │                             │ escalate                          │
│  for single-WO)             │                          │ all deterministic checks pass         │                             │                                  │
│                             │                          │ no new test failures vs baseline       │                             │                                  │
│                             │                          │ guardrail_matrix.json: no critical    │                             │                                  │
│                             │                          │   guardrails failed                   │                             │                                  │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ COMMIT_REVIEWED             │ scorecard written        │ run_scorecard.json valid             │ FINALIZED                   │ write best-effort scorecard →     │
│                             │                          │ all artifacts archived                │                             │ finalize anyway                   │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ (any state)                 │ unrecoverable failure    │ —                                    │ ABORTED                     │ write abort reason to event log,  │
│                             │                          │                                      │                             │ write partial scorecard           │
├─────────────────────────────┼──────────────────────────┼──────────────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ (any state)                 │ human decision needed    │ —                                    │ HUMAN_ESCALATED             │ park, notify via tmux,            │
│                             │                          │                                      │                             │ wait for human_decision.json      │
│                             │                          │                                      │                             │ then return to previous state     │
│                             │                          │                                      │                             │ and re-evaluate gate              │
└─────────────────────────────┴──────────────────────────┴──────────────────────────────────────┴─────────────────────────────┴──────────────────────────────────┘
```

### 1.3 Controller State File

The controller persists its state to disk after every transition. This makes runs resumable after controller restart.

```json
{
  "run_id": "RUN-20260527-001",
  "current_state": "CLAIMS_RESEARCHED",
  "mode": "standard",
  "risk_scores": {
    "initial": 4,
    "post_discovery": 5,
    "post_plan": null,
    "post_execution": null
  },
  "state_history": [
    {"state": "INIT", "entered_at": "2026-05-27T13:00:00Z", "exited_at": "2026-05-27T13:00:12Z"},
    {"state": "BASELINE_CAPTURED", "entered_at": "2026-05-27T13:00:12Z", "exited_at": "2026-05-27T13:01:45Z"}
  ],
  "pending_human_decisions": [],
  "retry_counts": {
    "task_contract_schema": 0,
    "source_discovery": 0,
    "gap_analysis_rerun": 0
  },
  "task_contract_amendments": [],
  "active_agents": [],
  "locked_files": []
}
```

### 1.4 Standard Mode State Path (skip list)

In Standard Mode, the controller skips these states:
- `CLAIMS_STRESS_TESTED` (Layer 2 why-chain — skipped, goes directly from BLINDSPOT_SCAN_COMPLETE to CROSS_VALIDATION_COMPLETE)
- Full 5-agent review panel in `PLAN_REVIEWED` (use 2-3 targeted reviewers based on risk profile)

In Fast Mode, the controller additionally skips:
- `CLAIMS_RESEARCHED` / `BLINDSPOT_SCAN_COMPLETE` / `CROSS_VALIDATION_COMPLETE` (no deep research)
- `PLAN_REVIEWED` (no review panel)
- But still requires: lightweight task contract, deterministic discovery, single work order, scope enforcement, baseline diff, deterministic checks

---

## Part 2: JSON Schemas for Core Artifacts

### 2.1 run_manifest.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "created_at", "task_description", "repo", "models", "prompt_versions", "guardrails", "artifact_root"],
  "properties": {
    "run_id": {"type": "string", "pattern": "^RUN-[0-9]{8}-[0-9]{3,}$"},
    "created_at": {"type": "string", "format": "date-time"},
    "task_description": {"type": "string", "minLength": 10},
    "repo": {
      "type": "object",
      "required": ["name", "branch", "base_commit"],
      "properties": {
        "name": {"type": "string"},
        "branch": {"type": "string"},
        "base_commit": {"type": "string", "pattern": "^[a-f0-9]{7,40}$"}
      }
    },
    "models": {
      "type": "object",
      "required": ["claude_orchestrator", "codex_validator"],
      "properties": {
        "claude_orchestrator": {"type": "string"},
        "codex_validator": {"type": "string"}
      }
    },
    "prompt_versions": {
      "type": "object",
      "additionalProperties": {"type": "string"}
    },
    "prompt_hashes": {
      "type": "object",
      "additionalProperties": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"}
    },
    "guardrails": {
      "type": "object",
      "required": ["version", "source_path"],
      "properties": {
        "version": {"type": "string"},
        "source_path": {"type": "string"}
      }
    },
    "mode": {"type": "string", "enum": ["fast", "standard", "critical", "pending_risk_assessment"]},
    "artifact_root": {"type": "string"}
  }
}
```

### 2.2 task_contract.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "task_summary", "goals", "acceptance_criteria"],
  "properties": {
    "run_id": {"type": "string"},
    "task_summary": {"type": "string", "minLength": 10},
    "goals": {
      "type": "array",
      "items": {"type": "string"},
      "minItems": 1
    },
    "non_goals": {
      "type": "array",
      "items": {"type": "string"}
    },
    "constraints": {
      "type": "array",
      "items": {"type": "string"}
    },
    "forbidden_changes": {
      "type": "array",
      "items": {"type": "string"}
    },
    "expected_behavior": {
      "type": "array",
      "items": {"type": "string"}
    },
    "acceptance_criteria": {
      "type": "array",
      "items": {"type": "string"},
      "minItems": 1
    },
    "risk_assumptions": {
      "type": "array",
      "items": {"type": "string"}
    },
    "human_approval_required_for": {
      "type": "array",
      "items": {"type": "string"}
    },
    "status": {
      "type": "string",
      "enum": ["proposed", "accepted", "amended"]
    },
    "amendments": {
      "type": "array",
      "items": {"$ref": "#/$defs/amendment"}
    }
  },
  "$defs": {
    "amendment": {
      "type": "object",
      "required": ["amendment_id", "reason", "proposed_change", "evidence", "approved_by"],
      "properties": {
        "amendment_id": {"type": "string", "pattern": "^AMEND-[0-9]{3,}$"},
        "reason": {"type": "string"},
        "proposed_change": {"type": "string"},
        "field_changed": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "risk_change": {"type": "string"},
        "approved_by": {"type": "array", "items": {"type": "string"}},
        "human_required": {"type": "boolean"},
        "human_approved": {"type": "boolean"},
        "timestamp": {"type": "string", "format": "date-time"}
      }
    }
  }
}
```

**Amendment rules (controller-enforced)**:
- Goals can be refined with Claude + Codex co-sign
- Non-goals can only be relaxed with explicit rationale + co-sign
- Forbidden changes require human approval to relax in Standard/Critical
- Constraints can be amended with evidence + co-sign
- All amendments are recorded in the task contract and referenced by downstream work orders
- Risk score is recalculated after any amendment

### 2.3 risk_assessment.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "assessments"],
  "properties": {
    "run_id": {"type": "string"},
    "current_mode": {"type": "string", "enum": ["fast", "standard", "critical"]},
    "current_score": {"type": "integer"},
    "assessments": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["stage", "score", "factors", "mode", "timestamp"],
        "properties": {
          "stage": {
            "type": "string",
            "enum": ["initial", "post_discovery", "post_plan", "post_execution"]
          },
          "score": {"type": "integer"},
          "factors": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["factor", "value", "reason"],
              "properties": {
                "factor": {"type": "string"},
                "value": {"type": "integer"},
                "reason": {"type": "string"}
              }
            }
          },
          "mode": {"type": "string", "enum": ["fast", "standard", "critical"]},
          "mode_changed": {"type": "boolean"},
          "escalation_reason": {"type": "string"},
          "assessed_by": {"type": "array", "items": {"type": "string"}},
          "timestamp": {"type": "string", "format": "date-time"}
        }
      }
    }
  }
}
```

**Multi-stage scoring rules (controller-enforced)**:
- Risk is re-scored at: initial (from task contract), post-discovery (after source/gap analysis), post-plan (after work orders agreed), post-execution (after diffs produced)
- Risk can automatically move UP at any stage
- Risk can move DOWN only with Codex co-sign or human decision
- Any dependency change or new permission requirement triggers immediate re-score
- If mode changes upward mid-run, controller activates the additional layers for the new mode from the current state forward (does not restart the run)

### 2.4 source_manifest.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "sources"],
  "properties": {
    "run_id": {"type": "string"},
    "sources": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["source_id", "path", "source_type", "discovered_by", "reason_for_inclusion", "freshness"],
        "properties": {
          "source_id": {"type": "string", "pattern": "^SRC-[0-9]{3,}$"},
          "path": {"type": "string"},
          "source_type": {
            "type": "string",
            "enum": ["code", "test", "config", "migration", "api_contract", "ci_cd", "doc", "workplace_post", "runbook", "lockfile", "other"]
          },
          "discovered_by": {
            "type": "array",
            "items": {"type": "string"}
          },
          "reason_for_inclusion": {"type": "string"},
          "freshness": {
            "type": "object",
            "properties": {
              "commit_sha": {"type": "string"},
              "last_modified": {"type": "string", "format": "date-time"},
              "checked_at": {"type": "string", "format": "date-time"}
            }
          }
        }
      }
    }
  }
}
```

### 2.5 gap_matrix.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "coverage_areas"],
  "properties": {
    "run_id": {"type": "string"},
    "coverage_areas": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["area", "required_level", "evidence_found", "gap_status"],
        "properties": {
          "area": {"type": "string"},
          "required_level": {"type": "string", "enum": ["required", "conditional", "optional"]},
          "evidence_found": {"type": "boolean"},
          "source_ids": {"type": "array", "items": {"type": "string"}},
          "gap_status": {"type": "string", "enum": ["covered", "partial", "gap", "not_applicable"]},
          "gap_reason": {"type": "string"},
          "blocking": {"type": "boolean"},
          "reviewed_by": {"type": "string"},
          "closure_decision": {"type": "string"}
        }
      }
    },
    "overall_sufficient": {"type": "boolean"}
  }
}
```

**Gate logic**: Controller checks `overall_sufficient == true` and no `required` areas have `gap_status == "gap"` with `blocking == true`.

### 2.6 claim_ledger.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "claims"],
  "properties": {
    "run_id": {"type": "string"},
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["claim_id", "claim", "claim_type", "impact", "evidence", "confidence", "validated_by"],
        "properties": {
          "claim_id": {"type": "string", "pattern": "^CLAIM-[0-9]{3,}$"},
          "claim": {"type": "string"},
          "claim_type": {
            "type": "string",
            "enum": ["code_behavior", "requirement_interpretation", "dependency_behavior", "performance_assumption", "security_assumption", "test_coverage", "operational", "historical"]
          },
          "impact": {
            "type": "string",
            "enum": ["critical", "high", "medium", "low"]
          },
          "task_contract_ref": {"type": "array", "items": {"type": "string"}},
          "evidence": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["source_id", "evidence_type", "supports_claim_because"],
              "properties": {
                "source_id": {"type": "string"},
                "source_type": {"type": "string"},
                "path": {"type": "string"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
                "symbol": {"type": "string"},
                "commit_sha": {"type": "string"},
                "content_hash": {"type": "string"},
                "checked_at": {"type": "string", "format": "date-time"},
                "evidence_type": {"type": "string", "enum": ["direct", "indirect", "negative", "counter"]},
                "supports_claim_because": {"type": "string"}
              }
            }
          },
          "counter_evidence": {"type": "array"},
          "assumptions": {"type": "array", "items": {"type": "string"}},
          "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
          "validated_by": {"type": "array", "items": {"type": "string"}},
          "stress_test_result": {
            "type": "object",
            "properties": {
              "method": {"type": "string", "enum": ["why_chain", "counter_evidence_search", "executable_validation", "skipped"]},
              "depth": {"type": "integer"},
              "outcome": {"type": "string", "enum": ["confirmed", "weakened", "refuted", "inconclusive", "not_tested"]},
              "notes": {"type": "string"}
            }
          }
        }
      }
    }
  }
}
```

### Claim Impact Rubric (controller reference)

| Impact | Examples | Evidence Threshold |
|--------|---------|-------------------|
| **Critical** | Security/auth/data-loss/production deploy behavior | Direct evidence + counter-evidence search + Codex review |
| **High** | Shared runtime path, schema change, dependency behavior | Direct evidence + at least one corroborating source |
| **Medium** | Local module behavior, internal API | Direct or strong indirect evidence |
| **Low** | Naming, formatting, comments, docs-only | Direct evidence where available |

**Gate logic**: Controller checks that all `critical` and `high` impact claims have at least one evidence entry with `evidence_type: "direct"`. Claims with only `indirect` evidence at critical/high impact are flagged as issues.

### 2.7 issue_ledger.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "issues"],
  "properties": {
    "run_id": {"type": "string"},
    "issues": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["issue_id", "title", "severity", "raised_by", "layer", "resolution"],
        "properties": {
          "issue_id": {"type": "string", "pattern": "^ISSUE-[0-9]{3,}$"},
          "title": {"type": "string"},
          "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
          "raised_by": {"type": "string"},
          "layer": {"type": "string"},
          "task_contract_ref": {"type": "array", "items": {"type": "string"}},
          "related_claims": {"type": "array", "items": {"type": "string"}},
          "related_sources": {"type": "array", "items": {"type": "string"}},
          "claude_position": {"type": "string"},
          "codex_position": {"type": "string"},
          "evidence_required_to_close": {"type": "array", "items": {"type": "string"}},
          "resolution": {
            "type": "string",
            "enum": ["open", "resolved_by_evidence", "resolved_by_human", "deferred", "rejected_as_invalid"]
          },
          "closure_evidence": {"type": "array", "items": {"type": "string"}},
          "blocks_work_orders": {"type": "array", "items": {"type": "string"}},
          "blocks_layers": {"type": "array", "items": {"type": "string"}},
          "safe_to_continue_without_resolution": {"type": "boolean"},
          "rounds": {"type": "integer"},
          "human_decision_ref": {"type": "string"}
        }
      }
    }
  }
}
```

**Gate logic**: Controller checks that no issues with `severity: "critical"` or `severity: "high"` have `resolution: "open"`. If `safe_to_continue_without_resolution == false` and issue is open, controller blocks dependent work orders and layers listed in `blocks_work_orders` / `blocks_layers`.

### 2.8 guardrail_matrix.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "guardrails"],
  "properties": {
    "run_id": {"type": "string"},
    "guardrails": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["guardrail_id", "description", "severity", "applies", "status"],
        "properties": {
          "guardrail_id": {"type": "string"},
          "description": {"type": "string"},
          "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
          "applies": {"type": "boolean"},
          "applicability_reason": {"type": "string"},
          "checked_by": {"type": "array", "items": {"type": "string"}},
          "status": {"type": "string", "enum": ["pass", "fail", "not_checked", "waived"]},
          "waiver": {
            "type": "object",
            "properties": {
              "reason": {"type": "string"},
              "approved_by": {"type": "string"},
              "human_required": {"type": "boolean"},
              "human_approved": {"type": "boolean"}
            }
          }
        }
      }
    }
  }
}
```

**Gate logic**: No `critical` guardrail with `status: "fail"` may pass the commit review gate. Waivers for critical guardrails require `human_approved: true`.

### 2.9 execution_work_orders.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "work_orders", "dependency_matrix"],
  "properties": {
    "run_id": {"type": "string"},
    "work_orders": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["work_order_id", "title", "negotiation_status", "criticality", "fail_policy", "assigned_scope", "local_acceptance_criteria", "validation_commands", "rollback_plan"],
        "properties": {
          "work_order_id": {"type": "string", "pattern": "^EXEC-(INT-)?[0-9]{3,}$"},
          "title": {"type": "string"},
          "parent_plan_id": {"type": "string"},
          "task_contract_ref": {"type": "array", "items": {"type": "string"}},
          "negotiation_status": {"type": "string", "enum": ["proposed", "negotiating", "agreed", "rejected"]},
          "agreed_by": {"type": "array", "items": {"type": "string"}},
          "criticality": {"type": "string", "enum": ["required", "optional"]},
          "fail_policy": {"type": "string", "enum": ["fail_closed", "fail_open"]},
          "is_integration_wo": {"type": "boolean", "default": false},
          "assigned_scope": {
            "type": "object",
            "required": ["allowed_files"],
            "properties": {
              "allowed_files": {"type": "array", "items": {"type": "string"}},
              "forbidden_files": {"type": "array", "items": {"type": "string"}}
            }
          },
          "preconditions": {"type": "array", "items": {"type": "string"}},
          "implementation_guidance": {
            "type": "object",
            "properties": {
              "required_steps": {"type": "array", "items": {"type": "string"}},
              "suggested_steps": {"type": "array", "items": {"type": "string"}},
              "forbidden_steps": {"type": "array", "items": {"type": "string"}}
            }
          },
          "local_acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
          "global_acceptance_criteria": {"type": "array", "items": {"type": "string"}},
          "validation_commands": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["command_array", "expected_exit_code"],
              "properties": {
                "command_array": {"type": "array", "items": {"type": "string"}},
                "expected_exit_code": {"type": "integer"},
                "timeout_seconds": {"type": "integer"}
              }
            }
          },
          "rollback_plan": {"type": "array", "items": {"type": "string"}},
          "status": {
            "type": "string",
            "enum": ["ready_for_execution", "executing", "validation_passed", "validation_failed", "rolled_back"]
          }
        }
      }
    },
    "dependency_matrix": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["work_order_id", "depends_on", "can_run_parallel"],
        "properties": {
          "work_order_id": {"type": "string"},
          "depends_on": {"type": "array", "items": {"type": "string"}},
          "can_run_parallel": {"type": "boolean"}
        }
      }
    },
    "file_ownership": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["file_path", "work_order_id"],
        "properties": {
          "file_path": {"type": "string"},
          "work_order_id": {"type": "string"},
          "lock_status": {"type": "string", "enum": ["unlocked", "locked", "released"]}
        }
      }
    }
  }
}
```

**Critical design note — validation commands as structured arrays**: Validation commands are structured arrays (`["pytest", "tests/ingestion/"]`), not free-form shell strings. The controller constructs the subprocess call from the array. This prevents command injection from LLM-generated shell strings.

**Gate logic**: Controller checks all required WOs have `negotiation_status: "agreed"`, dependency matrix has no cycles (topological sort), and file ownership has no file assigned to multiple WOs (unless explicitly sequenced in dependency matrix).

### 2.10 run_scorecard.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["run_id", "mode", "final_outcome"],
  "properties": {
    "run_id": {"type": "string"},
    "mode": {"type": "string", "enum": ["fast", "standard", "critical"]},
    "duration_seconds": {"type": "number"},
    "token_cost_estimate": {"type": "number"},
    "agents_launched": {"type": "integer"},
    "risk_score_initial": {"type": "integer"},
    "risk_score_final": {"type": "integer"},
    "mode_escalated": {"type": "boolean"},
    "issues_by_severity": {
      "type": "object",
      "properties": {
        "critical": {"type": "integer"},
        "high": {"type": "integer"},
        "medium": {"type": "integer"},
        "low": {"type": "integer"}
      }
    },
    "issues_closed_by_evidence": {"type": "integer"},
    "issues_deferred_by_human": {"type": "integer"},
    "layer_yield": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "properties": {
          "issues_opened": {"type": "integer"},
          "blocking_issues": {"type": "integer"}
        }
      }
    },
    "deterministic_checks": {
      "type": "object",
      "properties": {
        "tests_passed": {"type": "boolean"},
        "linters_passed": {"type": "boolean"},
        "type_checks_passed": {"type": "boolean"},
        "secret_scan_passed": {"type": "boolean"},
        "new_test_failures_vs_baseline": {"type": "integer"}
      }
    },
    "work_orders_total": {"type": "integer"},
    "work_orders_passed": {"type": "integer"},
    "work_orders_reworked": {"type": "integer"},
    "task_contract_amendments": {"type": "integer"},
    "human_escalations": {"type": "integer"},
    "schema_validation_failures": {"type": "integer"},
    "agent_retries": {"type": "integer"},
    "final_outcome": {"type": "string", "enum": ["passed", "failed", "aborted", "human_deferred"]}
  }
}
```

### 2.11 event_log.jsonl (line schema)

```json
{
  "type": "object",
  "required": ["seq", "timestamp", "event_type", "actor"],
  "properties": {
    "seq": {"type": "integer", "description": "Monotonically increasing sequence number"},
    "timestamp": {"type": "string", "format": "date-time"},
    "event_type": {
      "type": "string",
      "enum": [
        "state_transition", "agent_launched", "agent_completed", "agent_failed",
        "agent_retried", "gate_passed", "gate_failed", "schema_validation_passed",
        "schema_validation_failed", "risk_rescored", "mode_escalated",
        "artifact_written", "file_locked", "file_unlocked",
        "command_executed", "human_escalated", "human_decided",
        "issue_opened", "issue_closed", "issue_deferred",
        "contract_amended", "run_aborted", "run_finalized"
      ]
    },
    "actor": {"type": "string", "description": "controller | claude-agent-{id} | codex | human"},
    "state_before": {"type": "string"},
    "state_after": {"type": "string"},
    "details": {"type": "object"},
    "artifact_ref": {"type": "string"},
    "error": {"type": "string"}
  }
}
```

---

## Part 3: Layer 2 — Targeted Claim Stress Test

Layer 2 now uses three methods depending on claim type, not just recursive why-chain.

### 3.1 Method Selection (controller logic)

| Claim Type | Impact Critical/High | Impact Medium/Low |
|-----------|---------------------|------------------|
| code_behavior | executable_validation (if testable) or counter_evidence_search | skip or 1-level why_chain |
| requirement_interpretation | why_chain (3-5 levels) | why_chain (1-2 levels) |
| dependency_behavior | counter_evidence_search | skip |
| performance_assumption | executable_validation (if benchmarkable) or counter_evidence_search | skip |
| security_assumption | counter_evidence_search (mandatory) | counter_evidence_search |
| test_coverage | executable_validation | skip |
| operational | why_chain or counter_evidence_search | skip |
| historical | why_chain (1-2 levels) | skip |

### 3.2 Method Definitions

**Why-chain**: "This claim says X. Why is X true?" → recursive, 1-5 levels based on impact. Best for subjective or interpretive claims.

**Counter-evidence search**: "Assume this claim is wrong. What evidence would disprove it? Search for that evidence in the source manifest. If no counter-evidence is found, state exactly what was checked and where." Best for factual/behavioral claims about code.

**Executable validation**: "This claim is testable. Write or identify a test that would fail if the claim is false. Run it." Best for code behavior and performance claims. The controller runs the test — the agent proposes it.

### 3.3 Stress Test Output

Each claim in the claim ledger gets a `stress_test_result` field (see schema 2.6) recording the method used, depth (for why-chain), outcome (confirmed/weakened/refuted/inconclusive), and notes.

Claims that are `refuted` or `weakened` automatically create issues in the `issue_ledger.json`.

---

## Part 4: Review Consolidation Verification

### 4.1 Problem

Claude consolidating feedback from 5 review agents can drop or soften issues. This is a gate-adjacent role.

### 4.2 Protocol

1. Each reviewer writes findings to `review-{domain}.json` (raw findings)
2. Claude produces `review_findings_raw.json` (merged raw, no deduplication)
3. Claude produces `review_findings_consolidated.json` with every raw issue accounted for:

```json
{
  "consolidated_issues": [
    {
      "consolidated_id": "CONSOL-001",
      "raw_issue_ids": ["RAW-SEC-001", "RAW-INT-003"],
      "disposition": "preserved",
      "title": "...",
      "severity": "high"
    },
    {
      "consolidated_id": null,
      "raw_issue_ids": ["RAW-PERF-002"],
      "disposition": "merged_into",
      "merged_into": "CONSOL-001",
      "merge_reason": "Same root cause as CONSOL-001"
    },
    {
      "consolidated_id": null,
      "raw_issue_ids": ["RAW-QUAL-004"],
      "disposition": "rejected_with_reason",
      "rejection_reason": "Finding contradicted by evidence in CLAIM-017"
    }
  ]
}
```

4. **Controller verification**: Every issue ID in `review_findings_raw.json` must appear in `review_findings_consolidated.json` with one of: `preserved`, `merged_into`, `rejected_with_reason`, `converted_to_low_priority`. Any raw issue missing from consolidation fails the gate.

5. **Codex reviews the consolidation diff** (not just the revised plan) in Critical Mode.

---

## Part 5: Merge Protocol for Parallel Work Orders

### 5.1 Execution Isolation

Each work order executes in an isolated git worktree or branch:

```
Base commit (from run_manifest)
  ├── worktree: edge/RUN-001/EXEC-001 (agent-1 works here)
  ├── worktree: edge/RUN-001/EXEC-003 (agent-3 works here, parallel-safe)
  └── (EXEC-002 waits for EXEC-001 per dependency matrix)
```

### 5.2 Merge Queue

After execution, work orders merge in dependency order:

```
1. EXEC-001 completes → controller runs validation_commands → passes
2. Controller creates integration branch: edge/RUN-001/integration
3. Controller merges EXEC-001 patch into integration branch
4. EXEC-002 starts (depends on EXEC-001) in its own worktree based on integration branch
5. EXEC-002 completes → controller runs validation_commands → passes
6. Controller merges EXEC-002 into integration branch
7. EXEC-003 completed in parallel → controller merges into integration branch
8. If merge conflict: create ISSUE, block the conflicting WO, escalate
9. Controller runs integration work order (EXEC-INT) on integration branch
10. Full affected test suite, baseline diff, scope check
```

### 5.3 Conflict Handling

| Conflict Type | Detection | Response |
|--------------|-----------|----------|
| Textual merge conflict | git merge fails | Block WO, create Critical issue, escalate |
| Forbidden file touched | Controller checks diff against allowed_files | Reject patch automatically, create issue |
| Scope violation | File in diff not in any WO's allowed_files | Reject patch, create issue |
| Test regression | New failures vs baseline | Block merge, create issue |
| Semantic conflict (different files, conflicting behavior) | Integration work order test suite | Create issue, rework or escalate |

---

## Part 6: Fast Mode Minimum Spec

Even Fast Mode maintains basic safety:

| Component | Required in Fast Mode? |
|-----------|----------------------|
| Run manifest | Yes |
| Baseline validation | Yes (lightweight — affected tests only) |
| Task contract | Yes (lightweight — task summary, goals, forbidden changes, acceptance criteria only) |
| Risk scoring | Yes (single assessment) |
| Deterministic source discovery | Yes |
| LLM source discovery | No |
| Gap analysis | No |
| Claim ledger | No |
| Issue ledger | No (unless Codex flags something) |
| Codex blind-spot review | No |
| Claim stress test | No |
| Cross-validation debate | No |
| Implementation plan | Lightweight (single narrative) |
| 5-agent review panel | No |
| Work orders | Yes (single work order, simplified) |
| Scope enforcement | Yes |
| Execution | Yes |
| Baseline diff | Yes |
| Deterministic checks | Yes |
| Codex commit review | Only if flagged by risk score or controller |
| Guardrail matrix | Lightweight (critical guardrails only) |
| Run scorecard | Yes |

**Fast Mode state path**: INIT → BASELINE_CAPTURED → TASK_CONTRACT_ACCEPTED (lightweight) → SOURCES_DISCOVERED (deterministic only) → WORK_ORDERS_AGREED (single WO, no negotiation) → EXECUTION_COMPLETE → COMMIT_REVIEWED (deterministic only unless flagged) → FINALIZED

---

## Part 7: Directory Structure (Final)

```
/.harness/
  schemas/                              ← JSON schema files for validation
    run_manifest.schema.json
    task_contract.schema.json
    risk_assessment.schema.json
    source_manifest.schema.json
    gap_matrix.schema.json
    claim_ledger.schema.json
    issue_ledger.schema.json
    guardrail_matrix.schema.json
    execution_work_orders.schema.json
    run_scorecard.schema.json
    event_log_line.schema.json

  pipeline-{run-id}/
    controller_state.json               ← controller persists state here
    run_manifest.json
    task_contract.json
    risk_assessment.json
    baseline_validation.json
    event_log.jsonl
    split_decisions.jsonl
    run_scorecard.json

    layer0-sources/
      source_manifest.json
      gap_matrix.json

    layer1-research/
      claim_ledger.json
      research-topic-A.md
      research-topic-B.md

    layer1.5-codex-blindspot/
      codex_blindspot_review.md

    layer2-stress-test/
      stress_test_findings.json

    layer3-cross-validation/
      issue_ledger.json
      debate-rounds/
        ISSUE-{id}-round-{n}-{actor}.md
      final_research_report.md

    layer4-implementation/
      consolidated_implementation_plan.md

    layer5-review/
      review-correctness.json
      review-performance.json
      review-security.json
      review-quality.json
      review-integration.json
      review_findings_raw.json
      review_findings_consolidated.json
      revised_implementation_plan.md

    layer5.5-work-orders/
      execution_work_orders.json
      negotiation_log.md

    layer6-execution/
      worktrees/
        EXEC-001/
        EXEC-002/
      diffs/
        EXEC-001.patch
        EXEC-002.patch
      test_results/
        EXEC-001-validation.json
        EXEC-002-validation.json
      integration-branch.ref

    layer7-commit-review/
      codex_commit_review.md
      final_validation.json
      final_status.json

    guardrails/
      guardrail_matrix.json

    escalations/
      ISSUE-{id}-deadlock.md
      human_decision.json

    agents/
      {agent-id}/
        status.json
        quality_report.json
        partial_findings.md
        error.log
```

---

## Part 8: Phase 1 Build Checklist

What to build first. Each item is a concrete deliverable.

### 8.1 Controller Skeleton

- [ ] `harness.py` (or `harness.sh`) — main entry point
- [ ] State machine implementation with `controller_state.json` persistence
- [ ] State transition logic with gate check functions
- [ ] Schema validation function (loads schemas from `/.harness/schemas/`, validates any artifact)
- [ ] Run workspace creation (directory structure from Part 7)
- [ ] Run manifest generation
- [ ] Event log writer (atomic append with sequence numbers)
- [ ] Run scorecard generator (reads event log, computes metrics)

### 8.2 Deterministic Tools

- [ ] Baseline capture: runs configured test/lint/type-check commands, writes `baseline_validation.json`
- [ ] Deterministic source discovery: ripgrep wrapper, git log parser, test file finder, dependency graph builder
- [ ] File lock manager: lock/unlock files per work order, enforce scope on diffs
- [ ] Validation command runner: accepts structured command arrays, runs them, records results
- [ ] Baseline diff: compares post-change results against baseline, reports new failures
- [ ] Secret scanner: scans agent output files and diffs for known secret patterns

### 8.3 LLM Integration

- [ ] Claude agent launcher: spawns Claude Code in tmux pane with task prompt, monitors status.json
- [ ] Codex integration: writes task to file, triggers Codex in tmux pane, polls for completion file
- [ ] Agent monitor: periodic status.json reader, staleness detection, two-strike kill logic
- [ ] Retry handler: reads partial output, constructs continuation prompt, relaunches agent

### 8.4 Artifact Templates

- [ ] All 11 JSON schemas written to `/.harness/schemas/`
- [ ] Example prompts for each agent role that produce schema-compliant output
- [ ] Schema validation test: feed each schema a valid and invalid example, confirm pass/fail

### Phase 1 Definition of Done

A single Standard Mode run can execute end-to-end on a simple task:
- Controller creates workspace and captures baseline
- Claude proposes task contract, controller validates schema
- Codex reviews contract (can be manual trigger initially)
- Deterministic discovery runs, source manifest produced
- Claude produces claim ledger, controller validates
- Codex blind-spot review produces findings
- Issue ledger populated, no critical/high issues (for a simple task)
- Claude produces implementation plan
- Work order negotiated (can be single-round initially)
- Controller validates work order schema, locks files
- Claude executes work order in worktree
- Controller runs validation commands, diffs against baseline
- Controller writes scorecard
- Controller transitions to FINALIZED

All state transitions logged in event_log.jsonl. All artifacts pass schema validation.
