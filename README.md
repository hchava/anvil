# Anvil

**Where AI-generated code gets hardened before it ships.**

Anvil is an evidence-gated agent harness for reliable AI-assisted engineering. It wraps Claude Code, Codex, and future coding agents inside a deterministic controller with schema gates, evidence ledgers, scoped worktrees, validation commands, and human-in-the-loop escalation.

> This project is a clean-room public version of a Claude Code + Codex collaboration pattern used extensively in production engineering workflows. It contains no proprietary code, prompts, data, or internal system details.

---

## Why Anvil?

AI coding agents are powerful but unreliable at scale. They hallucinate references, overlook edge cases, rubber-stamp their own work, and silently degrade when context windows fill up.

Anvil addresses this by treating LLMs as reasoning workers inside a deterministic control plane — not as the runtime authority. Every claim needs evidence. Every plan gets cross-validated. Every gate is enforced by code, not by model agreement.

**The core principle:** no claim, plan, or code change advances unless it satisfies deterministic gates, evidence thresholds, guardrail compliance, test passage, and unresolved-issue closure.

## Why this matters

Modern AI coding agents can generate code quickly, but production teams still need evidence, tests, rollback plans, scoped execution, and reviewable decisions. Anvil turns AI-assisted development from an open-ended chat workflow into an auditable engineering workflow.

It is designed for developers who use Claude Code, Codex, or similar coding agents but want deterministic quality gates before trusting AI-generated changes.

---

## Current Status

Anvil is currently in early implementation.

- ✅ Architecture and roadmap finalized
- 🚧 Milestone 0: JSON schemas and validation fixtures
- ⏳ Next: local project/repo/run registry
- ⏳ Next: deterministic controller dry run
- ⏳ Later: Claude/Codex execution loop

## What makes Anvil different?

Most AI coding tools focus on generation. Anvil focuses on trust.

- LLMs are workers, not runtime authorities
- Every accepted claim must cite evidence
- Disagreements become structured issues, not chat arguments
- Code changes execute only through scoped work orders
- Validation is deterministic: tests, linting, type checks, secret scans, baseline diffs
- Every run produces reviewable artifacts and a scorecard

## Architecture

```
User Task
   │
   ▼
┌─────────────────────────────────────────────────────┐
│              Deterministic Controller                │
│                                                     │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │   SQLite     │  │ JSON Schema  │  │ Worktree  │  │
│  │  Registry    │  │    Gates     │  │  Manager  │  │
│  └─────────────┘  └──────────────┘  └───────────┘  │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │  Command     │  │ Validation   │  │  Event    │  │
│  │   Policy     │  │   Runner     │  │   Log     │  │
│  └─────────────┘  └──────────────┘  └───────────┘  │
└──────────────────────┬──────────────────────────────┘
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
     Claude Worker          Codex Validator
     (research, plan,       (blind-spot scan,
      execute, review)       cross-validation,
                             commit review)
            │                     │
            └──────────┬──────────┘
                       ▼
                  Work Orders
            (scoped, negotiated,
             with acceptance criteria)
                       │
                       ▼
               Scoped Execution
            (per-run worktrees,
             file locking, leases)
                       │
                       ▼
            Validation + Scorecard
            (baseline diff, tests,
             linters, secret scan)
```

### Runtime Model

```
Installation
  └── Project
        └── Repo Binding
              └── Task Scope
                    └── Run
                          └── Work Order
                                └── Lease
```

A project is not a repo. A repo is a shared resource. A task scope is a bounded operating area inside a repo. Two different projects can safely use the same repo.

---

## Key Concepts

### Deterministic Controller
The controller is a state machine that owns all gate transitions, schema validation, file locks, test execution, retries, and scoring. LLMs propose; the controller decides.

### Evidence Ledgers
Every research claim must cite specific evidence — file paths, line ranges, commit SHAs, content hashes. Claims without evidence cannot pass gates.

### Issue-Based Debate
Disagreements between Claude and Codex become tracked issues with severity levels, evidence requirements, and closure criteria — not open-ended prose debates.

### Negotiated Work Orders
Before any code is written, Claude and Codex co-negotiate atomic work orders with allowed files, forbidden files, acceptance criteria, validation commands, and rollback plans.

### Risk-Routed Modes
Not every task needs the full adversarial pipeline. A numeric risk score routes tasks to Fast, Standard, or Critical mode — matching rigor to complexity.
| Mode | When | Pipeline Depth |
|---|---|---|
| Fast | Config tweaks, typo fixes, simple refactors | Lightweight plan → execute → validate |
| Standard | Normal feature work, moderate refactors | Full research → review → negotiate → execute → validate |
| Critical | Production-critical, security-sensitive, cross-system changes | Full adversarial pipeline with human escalation |


### Anti-Rationalization
LLM evaluators consistently identify problems then talk themselves into approving anyway. Every evaluator agent in Anvil receives explicit instructions: "If a criterion is unmet, fail it. Do not rationalize. Approval requires criterion-by-criterion evidence."

---


## Quick Start

> Anvil is in active development. Installation commands below show the intended CLI experience.

```bash
# Planned package name
pip install anvil-harness
# Initialize local installation
anvil init
# Register a repo
anvil repo register --path /path/to/your/repo --name my-service
# Create a project
anvil project create --name my-feature --repo my-service
# Run the harness
anvil run --project my-feature --task "Add input validation to the config parser"
# Check status
anvil status
# View active leases
anvil leases --repo my-service
# Health check
anvil doctor
```

---

## Pipeline Layers (Critical Mode)

| Layer | Name | Purpose |
|-------|------|---------|
| -1 | Run Init | Create workspace, capture baseline, allocate worktree |
| -0.5 | Task Contract | Define goals, non-goals, constraints, acceptance criteria |
| 0 | Source Discovery | Deterministic tools first, then LLM semantic expansion |
| 0.5 | Gap Analysis | Structured coverage matrix — are sources sufficient? |
| 1 | Deep Research | Claim ledger with evidence references |
| 1.5 | Codex Blind-Spot Scan | Independent research catches Claude's blind spots early |
| 2 | Claim Stress Test | Why-chain + counter-evidence search + executable validation |
| 3 | Cross-Validation | Issue-based debate with severity and closure criteria |
| 4 | Implementation Planning | Translate research into actionable plans |
| 5 | Specialized Review | 5-agent panel: correctness, performance, security, quality, integration |
| 5.5 | Work-Order Negotiation | Claude and Codex co-produce atomic execution specs |
| 6 | Execution | Scoped code generation in isolated worktrees |
| 7 | Commit Review | Codex review + deterministic validation + baseline diff |

---

## Core Artifacts

Every run produces structured, schema-validated JSON artifacts:

| Artifact | Purpose |
|----------|---------|
| `run_manifest.json` | Immutable run metadata — models, commit, mode, prompt versions |
| `task_contract.json` | Goals, non-goals, constraints, acceptance criteria |
| `source_manifest.json` | All discovered sources with freshness metadata |
| `claim_ledger.json` | Research claims with evidence, counter-evidence, confidence |
| `issue_ledger.json` | Tracked disagreements with severity and closure status |
| `guardrail_matrix.json` | Applicable guardrails with pass/fail status |
| `execution_work_orders.json` | Atomic execution tasks — co-negotiated by Claude and Codex |
| `validation_results.json` | Centralized test/lint/type-check results |
| `run_scorecard.json` | Per-run metrics for measurement and pruning |
| `event_log.jsonl` | Append-only audit log of all pipeline events |

---

## Project Status

Anvil is in active development. See the [roadmap](docs/roadmap.md) for the full build plan.

| Milestone | Status | Description |
|-----------|--------|-------------|
| 0 | 🔨 In Progress | Schema fixtures and validation foundation |
| 0.5 | ⏳ Planned | Local runtime registry and controller state model |
| 1 | ⏳ Planned | Deterministic controller dry run |
| 2 | ⏳ Planned | Claude/Codex contract loop |
| 3 | ⏳ Planned | Single work order execution |
| 4 | ⏳ Planned | Standard Mode MVP |
| 5 | ⏳ Planned | Parallel work orders and merge protocol |
| 6 | ⏳ Planned | Critical Mode expansion |
| 7 | ⏳ Planned | Team mode — artifact export and PR integration |
| 8 | ⏳ Planned | Measurement and pruning |

---

## Design Philosophy

Anvil's design was refined through six rounds of adversarial cross-validation between Claude, Codex (ChatGPT), and GPT — the same multi-model review process it implements. Key design influences:

- **Anthropic's harness research** — generator-evaluator separation, context anxiety, sprint contracts, harness simplification principle
- **Production-inspired engineering workflow** — based on extensive use of Claude Code + Codex collaboration patterns in real-world data engineering work, rewritten here as a clean-room public implementation.

- **Adversarial review** — every architectural decision challenged by at least two independent models

See [docs/architecture.md](docs/architecture.md) for the full design document.

---

## Documentation

- [Architecture](docs/architecture.md) — full system design
- [Implementation Spec](docs/implementation-spec.md) — state machine, schemas, gate logic
- [Roadmap](docs/roadmap.md) — milestone-by-milestone build plan
- [Public Safety Boundary](docs/public-safety-boundary.md) — what this repo intentionally excludes

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
