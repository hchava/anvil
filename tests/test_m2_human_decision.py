"""Tests for the human escalation flow (Milestone 2).

Covers: schema validation, content hash computation, tamper detection,
modify_scope forces amendment flag, partial decisions, multi-issue decisions,
nonce-based forgery prevention with run_dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.controller.human_decision import (
    HumanDecisionError,
    TamperError,
    build_decision,
    compute_content_hash,
    load_and_observe,
    requires_amendment,
    validate_and_observe,
    write_decision,
)
from anvil.schemas_util import assert_valid, validate_artifact


# ---------------------------------------------------------------------------
# build_decision — creates valid documents
# ---------------------------------------------------------------------------

def test_build_decision_schema_valid() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="Risk is acceptable given the scope.",
        approved_by="alice",
    )
    errors = validate_artifact("human_decision", doc)
    assert not errors, errors


def test_build_decision_content_hash_present() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="acceptable",
        approved_by="alice",
    )
    assert doc["content_hash"].startswith("sha256:")
    assert len(doc["content_hash"]) == 7 + 64  # "sha256:" + 64 hex chars


def test_build_decision_with_scope_delta() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="modify_scope",
        rationale="Scope needs to include the auth module.",
        approved_by="alice",
        scope_delta={"goals_added": ["Include auth module validation"]},
        requires_contract_amendment=True,
    )
    assert doc["decision"] == "modify_scope"
    assert doc["scope_delta"]["goals_added"]
    assert doc["requires_contract_amendment"] is True
    errors = validate_artifact("human_decision", doc)
    assert not errors, errors


def test_build_decision_multiple_issue_ids() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001", "ISSUE-002", "ISSUE-003"],
        decision="defer",
        rationale="Will revisit next sprint.",
        approved_by="bob",
    )
    assert len(doc["issue_ids"]) == 3
    errors = validate_artifact("human_decision", doc)
    assert not errors, errors


# ---------------------------------------------------------------------------
# Content hash computation
# ---------------------------------------------------------------------------

def test_content_hash_is_deterministic() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    h1 = compute_content_hash(doc)
    h2 = compute_content_hash(doc)
    assert h1 == h2


def test_content_hash_changes_when_decision_changes() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    h_approve = compute_content_hash(doc)
    doc2 = dict(doc)
    doc2["decision"] = "reject"
    h_reject = compute_content_hash(doc2)
    assert h_approve != h_reject


def test_content_hash_changes_with_nonce() -> None:
    """Hash must differ when a non-empty nonce is used."""
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    h_no_nonce = compute_content_hash(doc, controller_nonce="")
    h_with_nonce = compute_content_hash(doc, controller_nonce="deadbeefcafe1234")
    assert h_no_nonce != h_with_nonce


# ---------------------------------------------------------------------------
# Tamper detection — legacy mode (no run_dir)
# ---------------------------------------------------------------------------

def test_tamper_detection_rejects_modified_decision(tmp_path: Path) -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    # Tamper with the decision field after the hash was computed.
    tampered = {k: v for k, v in doc.items() if not k.startswith("_")}
    tampered["decision"] = "reject"

    with pytest.raises(TamperError, match="tampered"):
        validate_and_observe(tampered, run_id="RUN-20260606-001")


def test_tamper_detection_rejects_modified_rationale(tmp_path: Path) -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="original rationale",
        approved_by="alice",
    )
    tampered = {k: v for k, v in doc.items() if not k.startswith("_")}
    tampered["rationale"] = "changed rationale"

    with pytest.raises(TamperError):
        validate_and_observe(tampered, run_id="RUN-20260606-001")


def test_valid_decision_passes_tamper_check() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    observed = validate_and_observe(clean, run_id="RUN-20260606-001")
    assert "controller_observed_at" in observed


def test_validate_stamps_controller_observed_at() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="defer",
        rationale="ok",
        approved_by="carol",
    )
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    observed = validate_and_observe(clean, run_id="RUN-20260606-001")
    assert observed["controller_observed_at"]


# ---------------------------------------------------------------------------
# Nonce-based tamper detection (with run_dir — prevents hash forgery)
# ---------------------------------------------------------------------------

def test_nonce_prevents_hash_forgery(tmp_path: Path) -> None:
    """Forger changes decision + recomputes hash without nonce — must be rejected.

    This is the fix for Codex blocker B5: with a controller nonce stored only
    in human_decision_pending.json, a forger who changes decision/rationale/
    approved_by cannot produce a valid content_hash without knowing the nonce.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
        run_dir=run_dir,
    )
    write_decision(run_dir, doc)

    # Forger changes decision and recomputes hash WITHOUT the nonce.
    forged = {k: v for k, v in doc.items() if not k.startswith("_")}
    forged["decision"] = "reject"
    forged["approved_by"] = "attacker"
    # Recompute hash without the nonce (attacker doesn't have the pending file nonce).
    forged["content_hash"] = compute_content_hash(forged, controller_nonce="")

    with pytest.raises(TamperError):
        validate_and_observe(forged, run_id="RUN-20260606-001", run_dir=run_dir)


def test_nonce_detects_silent_field_change(tmp_path: Path) -> None:
    """Changing decision without touching the hash must also be caught when run_dir used."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
        run_dir=run_dir,
    )
    write_decision(run_dir, doc)

    # Forger changes the decision but leaves the content_hash unchanged.
    tampered = {k: v for k, v in doc.items() if not k.startswith("_")}
    tampered["decision"] = "reject"

    with pytest.raises(TamperError):
        validate_and_observe(tampered, run_id="RUN-20260606-001", run_dir=run_dir)


def test_nonce_valid_round_trip_with_run_dir(tmp_path: Path) -> None:
    """An unmodified decision written and reloaded with run_dir must pass."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
        run_dir=run_dir,
    )
    write_decision(run_dir, doc)
    observed = load_and_observe(run_dir, "RUN-20260606-001")
    assert observed["decision"] == "approve"
    assert "controller_observed_at" in observed


# ---------------------------------------------------------------------------
# Run-id mismatch
# ---------------------------------------------------------------------------

def test_run_id_mismatch_raises_error() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    with pytest.raises(HumanDecisionError, match="run_id"):
        validate_and_observe(clean, run_id="RUN-20260606-999")


# ---------------------------------------------------------------------------
# requires_amendment
# ---------------------------------------------------------------------------

def test_modify_scope_requires_amendment() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="modify_scope",
        rationale="ok",
        approved_by="alice",
    )
    assert requires_amendment(doc) is True


def test_approve_does_not_require_amendment() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    assert requires_amendment(doc) is False


def test_requires_contract_amendment_flag() -> None:
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="defer",
        rationale="ok",
        approved_by="alice",
        requires_contract_amendment=True,
    )
    assert requires_amendment(doc) is True


# ---------------------------------------------------------------------------
# Fail-closed pending file checks
# ---------------------------------------------------------------------------

def test_pending_file_missing_raises_when_run_dir_provided(tmp_path: Path) -> None:
    """When run_dir is provided, missing pending file must raise TamperError — never fall back."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Build without run_dir so no pending file is written.
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
    )
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}

    # Passing run_dir with no pending file must raise — never silently fall back to nonce="".
    with pytest.raises(TamperError, match="missing"):
        validate_and_observe(clean, run_id="RUN-20260606-001", run_dir=run_dir)


def test_pending_file_decision_id_mismatch_raises(tmp_path: Path) -> None:
    """Pending file with mismatched decision_id must raise TamperError."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
        run_dir=run_dir,
    )
    write_decision(run_dir, doc)

    # Tamper with decision_id in the document — pending record still says DEC-001.
    tampered = {k: v for k, v in doc.items() if not k.startswith("_")}
    tampered["decision_id"] = "DEC-999"

    with pytest.raises(TamperError, match="decision_id"):
        validate_and_observe(tampered, run_id="RUN-20260606-001", run_dir=run_dir)


# ---------------------------------------------------------------------------
# File round-trip (with nonce — the secure path)
# ---------------------------------------------------------------------------

def test_write_and_load_decision(tmp_path: Path) -> None:
    """load_and_observe passes run_dir so it uses the secure nonce path."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_id = "RUN-20260606-001"

    # build_decision with run_dir writes the pending file; load_and_observe uses it.
    doc = build_decision(
        decision_id="DEC-001",
        run_id=run_id,
        issue_ids=["ISSUE-001"],
        decision="approve",
        rationale="ok",
        approved_by="alice",
        run_dir=run_dir,
    )
    write_decision(run_dir, doc)
    observed = load_and_observe(run_dir, run_id)
    assert observed["decision"] == "approve"
    assert "controller_observed_at" in observed


def test_load_missing_decision_raises(tmp_path: Path) -> None:
    with pytest.raises(HumanDecisionError, match="not found"):
        load_and_observe(tmp_path, "RUN-20260606-001")


def test_partial_decision_one_of_many_issues() -> None:
    """One human decision can cover a subset of open issues."""
    doc = build_decision(
        decision_id="DEC-001",
        run_id="RUN-20260606-001",
        issue_ids=["ISSUE-002"],  # partial: only issue 2 out of 3 open
        decision="approve",
        rationale="ISSUE-002 is low risk; others stay open.",
        approved_by="alice",
    )
    errors = validate_artifact("human_decision", doc)
    assert not errors, errors
