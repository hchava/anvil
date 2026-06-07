"""Regression tests for the Milestone 1 review fixes.

Covers: baseline read-only policy, real resume continuation + idempotency,
project-awareness, concurrent event-seq atomicity, discovery focus-path
containment, late-stage risk escalation propagation, worktree status, and
registry-lifecycle finalization.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.controller import Controller, ControllerError, RunInputs
from anvil.controller import states
from anvil.controller.command_policy import check_read_only
from anvil.controller.events import EventLog
from anvil.controller.risk import FloorRules
from anvil.discovery import discover_sources
from anvil.errors import ValidationError

from tests import m1_fixtures


def _inputs(env, **kw) -> RunInputs:
    base = dict(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )
    base.update(kw)
    return RunInputs(**base)


# ----- Blocker 1: baseline read-only policy ---------------------------------

def test_command_policy_rejects_shell_and_inline_code():
    assert check_read_only(["sh", "-c", "echo X > f"])[0] is False
    assert check_read_only(["bash", "-c", "rm -rf /"])[0] is False
    assert check_read_only(["python", "-c", "open('x','w')"])[0] is False
    assert check_read_only(["rm", "-rf", "src"])[0] is False
    # Read-only validators are allowed.
    assert check_read_only(["pytest", "-q"])[0] is True
    assert check_read_only(["git", "status", "--porcelain"])[0] is True
    assert check_read_only(["python", "-m", "pytest"])[0] is True
    # git write subcommands are blocked.
    assert check_read_only(["git", "commit", "-m", "x"])[0] is False


def test_command_policy_rejects_write_flags_on_allowed_binaries():
    """Binary-level allowlisting is bypassable via flags; argv policy blocks them."""
    assert check_read_only(["git", "diff", "--output=src/app.py"])[0] is False
    assert check_read_only(["git", "diff", "--output", "src/app.py"])[0] is False
    assert check_read_only(["ruff", "check", "--fix", "."])[0] is False
    assert check_read_only(["eslint", "--fix", "."])[0] is False
    assert check_read_only(["pytest", "--cache-clear"])[0] is False
    assert check_read_only(["black", "--write", "."])[0] is False


def test_baseline_rejects_mutating_command_without_executing(controller_env):
    """A scope baseline command that would mutate the worktree is rejected and
    the run fails closed BEFORE the command runs (worktree untouched)."""
    env = controller_env
    # Re-create the project with a malicious baseline command in the scope.
    cfg = env.registry.load_project_config(env.project_id)
    scope = cfg.task_scopes[env.scope_id]
    scope.baseline_commands = [{"command_array": ["sh", "-c", "echo MUTATED > src/config/loader.py"]}]
    cfg.save(env.registry.paths.project_json(env.project_id))

    m1_fixtures.write_all(env.run_dir, env.run_id)
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])
    before = (worktree / "src" / "config" / "loader.py").read_text()

    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_inputs(env))

    after = (worktree / "src" / "config" / "loader.py").read_text()
    assert after == before, "worktree must not be mutated by a rejected baseline command"


def test_baseline_cleanliness_backstop_reverts_worktree_mutation(controller_env, monkeypatch):
    """The pre/post worktree cleanliness check is the backstop for anything the
    argv policy misses: with the policy stubbed to allow-through, a command that
    mutates a tracked file is reverted and the run fails closed."""
    import anvil.controller as controller_mod

    monkeypatch.setattr(controller_mod, "check_read_only", lambda cmd: (True, None))

    env = controller_env
    cfg = env.registry.load_project_config(env.project_id)
    scope = cfg.task_scopes[env.scope_id]
    # Passes the (stubbed) policy but overwrites a tracked file.
    scope.baseline_commands = [
        {"command_array": ["git", "diff", "--output=src/config/loader.py"]}
    ]
    cfg.save(env.registry.paths.project_json(env.project_id))

    m1_fixtures.write_all(env.run_dir, env.run_id)
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])
    before = (worktree / "src" / "config" / "loader.py").read_text()

    with pytest.raises(ControllerError) as excinfo:
        Controller(env.registry, env.run_id).run(_inputs(env))
    assert "mutated the worktree" in str(excinfo.value)

    after = (worktree / "src" / "config" / "loader.py").read_text()
    assert after == before, "tracked file must be reverted to its pre-command content"


# ----- Blocker 2: resume actually continues (no restart/duplication) --------

def test_second_run_is_idempotent_no_duplicate_events(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    card1 = Controller(env.registry, env.run_id).run(_inputs(env))
    log = env.run_dir / "event_log.jsonl"
    lines_after_first = len(log.read_text().splitlines())

    # A second run() over a FINALIZED run must NOT re-run or duplicate events.
    card2 = Controller(env.registry, env.run_id).run(_inputs(env))
    lines_after_second = len(log.read_text().splitlines())

    assert card2 == card1
    assert lines_after_second == lines_after_first


def test_resume_continues_from_interrupted_state(controller_env):
    """Interrupt mid-run (a gate raises), fix the fixture, resume, and finish
    without re-running completed phases."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    # Break the guardrail matrix so the run stops at READY_FOR_COMMIT_REVIEW.
    bad = m1_fixtures.guardrail_matrix(
        env.run_id,
        guardrails=[{
            "guardrail_id": "SEC-001", "description": "d", "severity": "critical",
            "applies": True, "status": "not_checked", "waiver": None,
        }],
    )
    m1_fixtures.write_one(env.run_dir, "guardrail_matrix.json", bad)

    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_inputs(env))
    interrupted = json.loads((env.run_dir / "controller_state.json").read_text())["current_state"]
    assert interrupted != states.FINALIZED
    lines_at_interrupt = len((env.run_dir / "event_log.jsonl").read_text().splitlines())

    # Fix the guardrail and resume.
    m1_fixtures.write_one(env.run_dir, "guardrail_matrix.json", m1_fixtures.guardrail_matrix(env.run_id))
    card = Controller(env.registry, env.run_id).run(_inputs(env))
    assert card["final_outcome"] == "passed"

    final = json.loads((env.run_dir / "controller_state.json").read_text())["current_state"]
    assert final == states.FINALIZED
    # Resume appended events (did not restart from zero).
    seqs = [json.loads(l)["seq"] for l in (env.run_dir / "event_log.jsonl").read_text().splitlines() if l.strip()]
    assert seqs == list(range(len(seqs)))  # still contiguous, no resets
    assert len(seqs) > lines_at_interrupt
    # Risk assessments were not duplicated by the resume.
    risk = json.loads((env.run_dir / "risk_assessment.json").read_text())
    stages = [a["stage"] for a in risk["assessments"]]
    assert len(stages) == len(set(stages)), f"duplicate risk stages: {stages}"


# ----- Blocker 3: project-awareness -----------------------------------------

def test_run_rejects_mismatched_project(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_inputs(env, project_id="proj-other"))


def test_run_rejects_mismatched_repo(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_inputs(env, repo_id="repo-other"))


def test_run_rejects_mismatched_scope(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_inputs(env, scope_id="other-scope"))


# ----- Blocker 4: concurrent event-seq atomicity ----------------------------

def test_two_event_logs_do_not_collide(tmp_path: Path):
    path = tmp_path / "event_log.jsonl"
    a = EventLog(path)
    b = EventLog(path)  # opened before either writes
    s1 = a.append("run_finalized")
    s2 = b.append("gate_passed")
    s3 = a.append("artifact_written", artifact_ref="x.json")
    seqs = sorted([s1, s2, s3])
    assert seqs == [0, 1, 2], seqs
    # On-disk seqs are unique.
    disk = [json.loads(l)["seq"] for l in path.read_text().splitlines() if l.strip()]
    assert sorted(disk) == [0, 1, 2]
    assert len(set(disk)) == 3


# ----- High 1: discovery focus-path containment -----------------------------

def test_discovery_rejects_traversal_focus_path(controller_env):
    env = controller_env
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])

    class _Scope:
        discovery_focus_paths = ["../outside"]
        root_paths = ["../outside"]

    with pytest.raises(ValidationError):
        discover_sources(env.run_id, worktree, _Scope())


def test_discovery_rejects_absolute_focus_path(controller_env):
    env = controller_env
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])

    class _Scope:
        discovery_focus_paths = ["/etc"]
        root_paths = ["/etc"]

    with pytest.raises(ValidationError):
        discover_sources(env.run_id, worktree, _Scope())


# ----- High 2: late-stage escalation propagates -----------------------------

def test_post_plan_escalation_propagates_to_scorecard(controller_env):
    """A run that only becomes risky at post_plan still escalates mode and the
    scorecard records mode_escalated=True with the higher mode."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    inputs = _inputs(
        env,
        initial_factor_ids=["generated_or_docs_only"],  # fast
        post_discovery_factor_ids=["generated_or_docs_only"],  # still fast
        post_plan_factor_ids=["security_auth_data", "production_runtime_path"],  # 7 => critical
        floor=FloorRules(),
    )
    card = Controller(env.registry, env.run_id).run(inputs)
    assert card["mode"] == "critical"
    assert card["mode_escalated"] is True

    risk = json.loads((env.run_dir / "risk_assessment.json").read_text())
    post_plan = [a for a in risk["assessments"] if a["stage"] == "post_plan"][0]
    assert post_plan["mode"] == "critical"
    assert post_plan["mode_changed"] is True

    # The escalation must also EXTEND the remaining path: a run that becomes
    # critical at post_plan must still traverse PLAN_REVIEWED (it was not in the
    # fast path the tail was first entered with).
    events = [json.loads(l) for l in (env.run_dir / "event_log.jsonl").read_text().splitlines() if l.strip()]
    transitions = [e["state_after"] for e in events if e["event_type"] == "state_transition"]
    assert states.PLAN_CREATED in transitions
    assert states.PLAN_REVIEWED in transitions
    assert transitions.index(states.PLAN_CREATED) < transitions.index(states.PLAN_REVIEWED)


def test_git_history_summary_rejects_traversal_focus(controller_env):
    """The public git_history_summary helper applies the same focus-path
    containment as discover_sources."""
    from anvil.discovery import git_history_summary

    env = controller_env
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])

    class _Scope:
        discovery_focus_paths = ["../outside"]
        root_paths = ["../outside"]

    with pytest.raises(ValidationError):
        git_history_summary(worktree, _Scope())


# ----- High 3 + Medium: worktree status + registry finalize -----------------

def test_worktree_manifest_not_marked_merged(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    wt = json.loads((env.run_dir / "worktree_manifest.json").read_text())
    assert wt["status"] != "merged"
    assert wt["status"] in ("active", "discarded")


def test_registry_lifecycle_finalized(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    row = env.registry.get_run(env.run_id)
    assert row["lifecycle_state"] == "finalized"
    assert row["pipeline_state"] == states.FINALIZED
