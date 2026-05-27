# Edge Harness: Enhancement Roadmap v2

**Version**: 2.0 — Reordered based on GPT critical review. Multi-project runtime model is now foundational, not a late feature.

**Key structural change**: The controller's data model is project/repo/run from day one. SQLite for operational state, JSON for per-run artifacts.

---

## Milestone 0: Schema Fixtures & Validation Foundation

*Build all schemas and prove they work before writing any controller logic.*

### Gate-Critical Schemas

- [ ] `run_manifest.schema.json`
- [ ] `task_contract.schema.json`
- [ ] `risk_assessment.schema.json`
- [ ] `source_manifest.schema.json`
- [ ] `gap_matrix.schema.json`
- [ ] `claim_ledger.schema.json`
- [ ] `issue_ledger.schema.json`
- [ ] `guardrail_matrix.schema.json`
- [ ] `execution_work_orders.schema.json`
- [ ] `run_scorecard.schema.json`
- [ ] `event_log_line.schema.json`
- [ ] `worktree_manifest.schema.json` (promoted from Milestone 10)
- [ ] `validation_results.schema.json` (promoted from Milestone 10)

### Supplementary Schemas

- [ ] `controller_state.schema.json`
- [ ] `baseline_validation.schema.json`
- [ ] `command_result.schema.json`
- [ ] `agent_status.schema.json`
- [ ] `agent_quality_report.schema.json`
- [ ] `review_findings_raw.schema.json`
- [ ] `review_findings_consolidated.schema.json`
- [ ] `human_decision.schema.json`
- [ ] `project.schema.json` (new)
- [ ] `repo.schema.json` (new)

### Schema Hardening (all gate-critical)

- [ ] `additionalProperties: false`
- [ ] `minItems` on non-empty arrays (sources, claims, evidence, goals, acceptance_criteria)
- [ ] Uniqueness constraints for all ID fields (controller-level, since JSON Schema `uniqueItems` only checks value equality)
- [ ] Conditional `required` with `if/then` (code sources require `commit_sha`; docs require `last_modified`)
- [ ] `freshness` sub-objects have required internal fields
- [ ] `task_contract_ref` required on claims
- [ ] `safe_to_continue_without_resolution` required on issues
- [ ] `blocks_work_orders` and `blocks_layers` required on issues (can be empty arrays)
- [ ] `counter_evidence` and `closure_evidence` typed as structured evidence refs
- [ ] Structured rollback primitives (not shell strings)
- [ ] Work-order ID pattern: `EXEC-INT-001` format (sortable)

### Fixtures & Tests

- [ ] Valid + invalid fixture files for every schema
- [ ] Schema validation test suite
- [ ] Schema versioning with `$id`

### Acceptance Criteria

Every schema has valid and invalid fixtures. All validation tests pass.

---

## Milestone 0.5: Local Runtime Registry & Controller State Model

*Define the multi-project data model before building the controller. This is the foundation everything else sits on.*

### Design Principle

The controller has two kinds of state:

1. **Installation-level operational state** — all projects, repos, active runs, leases, worktrees. Lives in SQLite.
2. **Per-run pipeline state** — current pipeline state, mode, artifacts, agents, work orders, issues. Lives in JSON files inside the run directory.

### Entity Hierarchy

```
Installation
  ├── Projects
  │     ├── Project-Repo Bindings
  │     └── Task Scopes
  ├── Repositories
  │     ├── Worktrees
  │     └── Repo-level leases
  └── Runs
        ├── Lifecycle state (operational)
        ├── Pipeline state (pipeline progress)
        ├── Work orders
        ├── Agents
        └── Artifacts
```

Key rule: **a project is not a repo.** A project is a business effort. A repo is a shared code asset. A task scope is a bounded operating area inside a repo. Two different projects can use the same repo safely.

### Global Directory Layout

```
~/.edge-harness/
  installation.json
  registry.sqlite

  schemas/
    *.schema.json

  shared/
    guardrails/
    prompts/
    risk_weights_base.json
    command_policy_base.json

  projects/
    {project_id}/
      project.json
      guardrails/
      prompts/
      scorecard_history/

  repos/
    {repo_id}/
      repo.json

  runs/
    {run_id}/
      controller_state.json
      run_manifest.json
      task_contract.json
      ... all pipeline artifacts ...

  worktrees/
    {repo_id}/
      {run_id}/
        (git worktree checkout)
```

### SQLite Registry (V1 — 4 core tables)

```sql
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  config_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE repos (
  repo_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  default_branch TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  repo_id TEXT NOT NULL REFERENCES repos(repo_id),
  task_scope_id TEXT,
  lifecycle_state TEXT NOT NULL DEFAULT 'created',
  pipeline_state TEXT NOT NULL DEFAULT 'INIT',
  mode TEXT NOT NULL DEFAULT 'pending',
  task_summary TEXT,
  base_commit TEXT NOT NULL,
  target_branch TEXT NOT NULL,
  current_target_head TEXT,
  worktree_path TEXT,
  initiated_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finalized_at TEXT
);

CREATE TABLE leases (
  lease_id TEXT PRIMARY KEY,
  lease_type TEXT NOT NULL,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  work_order_id TEXT,
  scope TEXT NOT NULL,
  access TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  acquired_at TEXT NOT NULL,
  expires_at TEXT
);
```

Additional tables (`worktrees`, `agents`, `task_scopes`, `scorecards`) added when needed, not upfront.

### Lifecycle State vs. Pipeline State

Lifecycle states (operational):
```
created → active → paused → active → finalized
                 → blocked → active
                 → waiting_for_human → active
           active → aborted
           finalized → archived
```

Pipeline states (progress): the full state machine from the impl spec (INIT through FINALIZED/ABORTED).

A run at any moment has both:
```json
{
  "lifecycle_state": "paused",
  "pipeline_state": "PLAN_CREATED"
}
```

This means the run is paused by the user at the planning stage — not failed, not abandoned.

### Lease Model (V1 — 2 types)

| Lease Type | Purpose |
|-----------|---------|
| `file_write` | Exclusive write access to a file path within a repo |
| `merge_queue` | Exclusive access to merge into a repo's target branch |

Lease rules:
- Two active `file_write` leases on same repo + same path → block new run or require human approval
- Two `merge_queue` leases on same repo → block (only one run merges at a time)
- Leases are acquired when entering WORK_ORDERS_AGREED (file_write) and READY_FOR_COMMIT_REVIEW (merge_queue)
- Leases are released at FINALIZED or ABORTED
- Paused runs retain leases but with a configurable expiration (default 4 hours)
- Stale leases (past expiration with run not active) can be force-released via CLI

Conflict detection is **repo-level**, not project-level. This catches cross-project same-repo conflicts:
```
Project A → repo data-pipelines → ingestion scope → writes shared/config/
Project B → repo data-pipelines → export scope → writes shared/config/
→ lease conflict detected at repo level
```

### Worktree Allocation Protocol

Every run gets a per-run worktree. No harness code writes to the user's normal checkout.

```
1. Resolve project_id, repo_id, task_scope_id
2. Query SQLite for repo path and default branch
3. Capture base_commit = HEAD of target branch
4. Create run branch: edge/{project_id}/{run_id}
5. Create worktree: ~/.edge-harness/worktrees/{repo_id}/{run_id}/
6. Register in SQLite
7. All agents operate only inside that worktree
```

### Base-Commit Drift Protocol

Before execution (entering WORK_ORDERS_AGREED) and before final validation (entering READY_FOR_COMMIT_REVIEW), the controller checks whether the target branch has moved:

```json
{
  "base_commit": "abc123",
  "target_branch": "main",
  "target_head_at_start": "abc123",
  "target_head_current": "def456",
  "base_is_stale": true,
  "rebase_required": true
}
```

Rules:
- If target moved before execution: rebase run worktree or ask user
- If target moved before final validation: rebase/merge latest target, rerun validation
- If rebase creates conflict: create Critical issue, escalate
- Tests passing only on stale base do not count as passing

### project.json

```json
{
  "project_id": "ingestion-retry-hardening",
  "repo_id": "REPO-data-pipelines",
  "task_scopes": {
    "ingestion-pipeline": {
      "root_paths": ["pipelines/ingestion/", "shared/config/", "tests/ingestion/"],
      "baseline_commands": [
        {"command_array": ["pytest", "tests/ingestion/"], "label": "ingestion_tests"}
      ],
      "discovery_focus_paths": ["pipelines/ingestion/"],
      "default_forbidden_changes": ["pipelines/export/", "infra/"]
    }
  },
  "guardrail_sources": [
    "~/.edge-harness/shared/guardrails/",
    "~/.edge-harness/projects/ingestion-retry-hardening/guardrails/"
  ],
  "second_brain_path": "/home/harish/second-brain/",
  "risk_weight_overrides": {},
  "command_policy_overrides": {}
}
```

### CLI Commands

```bash
harness project create --name "ingestion-retry-hardening" --repo REPO-data-pipelines
harness repo register --path /home/harish/repos/data-pipelines --name REPO-data-pipelines
harness scope create --project ingestion-retry-hardening --scope ingestion-pipeline --root-paths "pipelines/ingestion/,shared/config/"

harness run --project ingestion-retry-hardening --scope ingestion-pipeline --task "Add retry logic"
harness status                                  # all active runs
harness status --project ingestion-retry-hardening
harness status --repo REPO-data-pipelines       # all runs on this repo
harness pause RUN-001
harness resume RUN-001
harness abort RUN-001
harness leases --repo REPO-data-pipelines       # show active leases
```

### Tasks

- [ ] Design and document entity hierarchy
- [ ] Create SQLite schema (4 core tables)
- [ ] Build `project.json` and `repo.json` schema and loader
- [ ] Build task scope config loader
- [ ] Build guardrail merge function (shared + project, project overrides)
- [ ] Build CLI: `harness project create`, `harness repo register`, `harness scope create`
- [ ] Build CLI: `harness status`, `harness pause`, `harness resume`, `harness abort`
- [ ] Build CLI: `harness leases`
- [ ] Build worktree allocation function
- [ ] Build lease acquire/release/check functions
- [ ] Build base-commit drift check function
- [ ] Build `installation.json` init

### Acceptance Criteria

Two projects registered, one sharing a repo. Worktree allocation works for both. Lease conflict detected when two runs claim the same file on the same repo. `harness status` shows active runs across projects. Pause/resume updates lifecycle state without losing pipeline state.

---

## Milestone 1: Deterministic Controller Dry Run

*No LLM. No code writes. Controller runs end-to-end with fixture artifacts, project-aware from the start.*

### State Machine Fixes (from GPT impl-spec review)

**Added states:**
```
CROSS_VALIDATION_PENDING    ← between BLINDSPOT_SCAN_COMPLETE and CROSS_VALIDATION_COMPLETE
READY_FOR_COMMIT_REVIEW     ← between EXECUTION/INTEGRATION and COMMIT_REVIEWED
```

**Corrected transitions** (see impl spec for full table — key fixes):
- BLINDSPOT_SCAN_COMPLETE never jumps directly to CROSS_VALIDATION_COMPLETE
- Standard Mode goes through CROSS_VALIDATION_PENDING with full gate checks
- EXECUTION_COMPLETE goes to READY_FOR_COMMIT_REVIEW (single WO) or INTEGRATION_VALIDATED (multi WO) first
- Standard Mode still gets targeted plan review (not skipped entirely)

**Gate hardening:**

Issue closure gate (critical/high):
```
Pass ONLY if:
  (resolution == "resolved_by_evidence" AND closure_evidence non-empty)
  OR (resolution == "resolved_by_human" AND human_decision_ref exists with rationale)
"deferred" passes ONLY with human decision AND safe_to_continue == true
```

Guardrail gate:
```
Critical guardrail where applies == true:
  must be "pass" or human-approved "waived"
  "not_checked" BLOCKS in Standard/Critical
High guardrail: "pass", "waived", or deferred with issue
```

Fast Mode floor rules (in addition to score 0-2):
```
ALL must be true:
  - no production runtime path touched
  - no auth/security/data-access touched
  - no dependency/lockfile changed
  - no database migration/schema touched
  - no external API behavior changed
  - affected tests exist OR change is docs/config-only with no runtime effect
  - changed files <= 2
  - all changed files within one module/package
```

### Tasks

- [ ] State machine implementation with `controller_state.json` persistence
- [ ] State transitions load project config from registry
- [ ] Gate check functions per transition
- [ ] Cross-reference validation functions:
  - `validate_source_refs()`
  - `validate_claim_refs()`
  - `validate_issue_refs()`
  - `validate_task_contract_refs()`
  - `validate_work_order_dependencies()`
  - `validate_guardrail_refs()`
  - `validate_file_scope_against_contract()`
- [ ] Event log writer (atomic append, monotonic sequence, mode-sensitive failure handling)
- [ ] Baseline capture with normalized test identities (test_id + status + failure_fingerprint)
- [ ] Deterministic source discovery (scope-aware: uses task scope's discovery_focus_paths)
- [ ] Risk scoring engine with factor IDs from controller-owned registry
- [ ] Multi-stage risk scoring (initial, post_discovery, post_plan, post_execution)
- [ ] Run scorecard generator with layer yield
- [ ] Resume logic from `controller_state.json`
- [ ] Base-commit drift check at execution and final validation transitions
- [ ] `worktree_manifest.json` creation and management
- [ ] `validation_results.json` for all deterministic check outputs

### Acceptance Criteria

Controller runs end-to-end with fixture artifacts on a registered project. All state transitions logged. All schemas validated. Resumes from `controller_state.json`. Base-commit drift detected on a repo where main moved. Risk re-score triggers mode escalation.

---

## Milestone 2: Claude/Codex Contract Loop

*First LLM integration. Claude proposes, Codex reviews, controller validates.*

### Tasks

- [ ] Agent launcher: spawn in tmux, write `agent_task.json` with `attempt_id`
- [ ] Agent IO contract: completion from status + output + schema validation (not tmux messages)
- [ ] Agent monitor: staleness detection, two-strike kill
- [ ] Schema retry loop (max 2 retries on validation failure)
- [ ] **Pre-context secret redaction** (promoted from Milestone 10):
  - Scan all file excerpts before sending to Claude/Codex
  - Redact common token formats (.env, API keys, private keys, credentials)
  - Block known secret-bearing files from context by default
  - Log redaction count (not values)
- [ ] Task contract generation (Claude proposes)
- [ ] Task contract review (Codex reviews)
- [ ] Task contract amendment protocol (provisional until Layer 0.5, amendable with evidence + co-sign)
- [ ] Risk scoring with Codex co-sign
- [ ] Human escalation flow with `human_decision.json`:
  - `modify_scope` forces contract amendment + risk re-score
  - Content hash for tamper detection
  - Controller revalidates all affected gates after human decision
- [ ] Codex integration via tmux + file system

### Command Policy Engine

```json
{
  "allowed_binaries": ["pytest", "ruff", "mypy", "npm", "pnpm", "go", "cargo", "git"],
  "blocked_binaries": ["curl", "wget", "ssh", "scp", "sudo", "rm"],
  "blocked_arg_patterns": [
    {"binary": "python", "args_contains": ["-c"]},
    {"binary": "python", "args_contains": ["-m", "http.server"]},
    {"binary": "npm", "args_contains": ["install"]},
    {"binary": "pip", "args_contains": ["install"]}
  ],
  "network_allowed": false,
  "max_timeout_seconds": 600,
  "max_output_bytes": 200000,
  "allowed_working_directory": "worktree_only",
  "env_allowlist": ["PATH", "PYTHONPATH", "NODE_ENV"]
}
```

### Acceptance Criteria

Claude proposes valid task_contract.json. Codex reviews. Schema retry loop works. Risk score computed, mode selected. Amendment protocol works. Human escalation round-trips. Secret redaction catches planted test credentials in source files.

---

## Milestone 3: Single Work Order Execution

*First code writes. One work order in a per-run worktree.*

### Tasks

- [ ] Per-run worktree creation (from registry — allocated in Milestone 0.5 protocol)
- [ ] Lease acquisition: file_write leases for work order's allowed_files
- [ ] Scope enforcement: reject diffs touching files outside allowed_files
- [ ] Validation command runner: structured arrays, policy-checked
- [ ] Baseline comparison at test-identity level
- [ ] Structured rollback execution (controller-owned primitives)
- [ ] Secret scanner: regex + entropy + test-fixture allowlist
- [ ] `worktree_manifest.json` updated with work order execution status
- [ ] `validation_results.json` updated with command results
- [ ] Lease release on completion
- [ ] File ownership with access-type and sequence tracking:
  ```json
  {
    "file_path": "services/ingestion/config.py",
    "owners": [
      {"work_order_id": "EXEC-001", "access": "write", "sequence": 1},
      {"work_order_id": "EXEC-002", "access": "read", "sequence": 2}
    ]
  }
  ```

### Acceptance Criteria

Work order executes in per-run worktree. Scope enforced (change to forbidden file rejected). Validation commands pass policy. Baseline diff detects new failures. Rollback works. Leases acquired before execution, released after. Secret scan catches planted credential.

---

## Milestone 4: Standard Mode MVP

*Full Standard Mode pipeline end-to-end.*

### Tasks

- [ ] LLM source discovery agents (scope-aware)
- [ ] Negative discovery recording (search attempts logged in source manifest)
- [ ] Gap analysis with `gap_matrix.json`
- [ ] Claim ledger with strengthened evidence (line ranges, content hashes)
- [ ] Codex blind-spot scan
- [ ] Issue ledger with dependency fields (blocks_work_orders, safe_to_continue)
- [ ] Targeted plan review (minimum 1 reviewer; Security required if risk factors match auth/data/deps)
- [ ] Review consolidation verification (raw + consolidated, controller checks nothing dropped)
- [ ] Work-order negotiation (Claude proposes, Codex challenges)
- [ ] System integration work order for multi-WO runs
- [ ] Guardrail matrix population
- [ ] Codex commit review with criterion-by-criterion approval
- [ ] Final validation with baseline comparison
- [ ] Run scorecard with layer yield
- [ ] Base-commit drift check before execution and before final validation

### Acceptance Criteria

Full Standard Mode run end-to-end on a real task with a registered project. All artifacts schema-valid. All gates enforced. Scorecard with layer yield. Base-commit drift handled.

---

## Milestone 5: Parallel Work Orders & Merge Protocol

*Multiple work orders in separate worktrees with safe merge.*

### Tasks

- [ ] Multiple per-WO worktrees (branched from run worktree)
- [ ] Dependency-ordered execution (topological sort)
- [ ] Integration branch management
- [ ] Merge queue with repo-level merge_queue lease (exclusive)
- [ ] Merge protocol: per-WO validation → merge to integration branch → revalidation
- [ ] Conflict handling: textual → block + issue; scope violation → auto-reject; regression → block + issue
- [ ] Integration work order: full affected test suite after all WOs merged
- [ ] Mode-sensitive failure recovery: required WO fail-closed, optional WO fail-open
- [ ] `worktree_manifest.json` extended with per-WO branches, merge status, patch paths
- [ ] Base-commit drift check with rebase before merge

### Acceptance Criteria

Two parallel WOs in separate worktrees. Merge in dependency order. Scope enforced on both. Integration tests run. Planted merge conflict detected and creates issue. Merge queue lease prevents concurrent merges to same repo.

---

## Milestone 6: Critical Mode Expansion

*Full adversarial pipeline.*

### Tasks

- [ ] Layer 2: Targeted Claim Stress Test (why-chain + counter-evidence search + executable validation)
- [ ] 5-agent specialized review panel with anti-rationalization prompts
- [ ] Review consolidation verification with Codex reviewing consolidation diff
- [ ] Full issue-based cross-model debate with evidence requirements
- [ ] Deadlock escalation with human decision workflow
- [ ] Context-anxiety preemption (>70% context + remaining work → split to fresh agent)
- [ ] Multi-round work-order negotiation
- [ ] Dependency change handling (auto-trigger Critical issue + security + license + human approval)

### Acceptance Criteria

Full Critical Mode run end-to-end. 5 reviewers produce findings. Consolidation verification catches planted dropped issue. Debate terminates on evidence. Deadlock escalation works.

---

## Milestone 7: Team Mode — Artifact Export & PR Integration

*Each teammate runs locally. Coordinate via git/PR. No live multi-user locking.*

### Design Principle

```
Each developer:
  local harness installation
  local registry.sqlite
  local runs/worktrees/scorecards

Shared coordination:
  git remote
  pull requests
  CI
  artifact bundle attached to PR
  optional central scorecard dashboard later
```

### Tasks

- [ ] Artifact bundle export: package run artifacts (scorecard, issue ledger, claim ledger, guardrail matrix) into a zip/tarball
- [ ] PR summary generation: markdown summary of run results for PR description
- [ ] Run scorecard attachment to PR (as comment or file)
- [ ] Evidence bundle path (link to full artifacts for reviewers)
- [ ] `initiated_by` propagation through all artifacts for attribution
- [ ] Shared scorecard aggregation (import scorecards from teammates' exported bundles)

### Acceptance Criteria

Run completes locally. Artifact bundle exported. PR opened with run summary and scorecard. Another developer can import the scorecard into their local harness for aggregated periodic review.

---

## Milestone 8: Measurement & Pruning

*Use accumulated data to optimize the harness.*

### Tasks

- [ ] Scorecard analysis (script reads scorecard_history/ per project per scope)
- [ ] Layer yield analysis
- [ ] Cost-benefit per layer
- [ ] Apply hard demotion/removal criteria:
  - Layer finds zero High/Critical issues across 20 Standard runs → demote to Critical-only
  - Reviewer produces >70% duplicate findings across 10 runs → merge or remove
  - Layer produces >50% false positives → retune prompt or remove from Standard
  - Layer increases runtime >30% but contributes <5% of closed issues → demote
- [ ] Risk scoring weight calibration from actual outcomes
- [ ] Prompt tuning based on schema failure rates
- [ ] Mode boundary adjustment

### Acceptance Criteria

After 20+ runs, produce review report identifying at least one layer for demotion and one risk weight for adjustment.

---

## Milestone 9: Deferred Features

*Build when observed failures justify them.*

### From Codex v2.0 Review
- [ ] Full measurement framework (defect escape rate, false approval rate)
- [ ] `assumption_ledger.json`
- [ ] Hash-chained event logs
- [ ] Semantic dependency matrix
- [ ] Random raw evidence audit (20% sampling)
- [ ] Negative prompt challenger agents

### From GPT v2.1 Review
- [ ] Full security threat model with instruction classifiers
- [ ] `handoff_manifest.json`
- [ ] Prompt regression test suite

### From Codex v2.2 Review
- [ ] Prompt smoke tests
- [ ] Model context manifest
- [ ] Prompt injection classifier for docs/posts/comments
- [ ] Test-quality review
- [ ] Counter-evidence requirement for all high/critical claims (mandatory)

### From GPT Impl-Spec Review
- [ ] Artifact immutability tracking (`artifact_index.json`)
- [ ] Secret scanning entropy mode
- [ ] Implementation plan as structured JSON (not just markdown)

### From GPT Roadmap Review
- [ ] Additional SQLite tables (worktrees, agents, task_scopes, artifacts as needed)
- [ ] Additional lease types (repo_read, branch, agent_slot, human_decision) as concurrency patterns emerge
- [ ] Central scorecard dashboard for team-wide analytics
- [ ] Live multi-user locking (only if central controller service is built)

### Trigger

Don't build speculatively. Build each when a real failure, scorecard gap, team growth, or model upgrade justifies it.

---

## Summary: Build Order

| Milestone | What | LLM? | Effort |
|-----------|------|------|--------|
| **0** | Schemas + fixtures | No | 2-3 days |
| **0.5** | Runtime registry + state model + CLI | No | 1 week |
| **1** | Deterministic controller dry run (project-aware) | No | 1-2 weeks |
| **2** | Claude/Codex contract loop + secret redaction | Yes | 1 week |
| **3** | Single WO execution in per-run worktree | Yes | 1 week |
| **4** | Standard Mode MVP | Yes | 2-3 weeks |
| **5** | Parallel WOs + merge protocol | Yes | 1-2 weeks |
| **6** | Critical Mode expansion | Yes | 2-3 weeks |
| **7** | Team mode: artifact export + PR integration | No | 1 week |
| **8** | Measurement + pruning | No | 1 week |
| **9** | Deferred features | Varies | Ongoing |

**Standard Mode MVP (M0–M4)**: ~7-9 weeks
**Full system (M0–M8)**: ~14-18 weeks
