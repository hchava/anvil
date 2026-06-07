"""Human escalation flow (Milestone 2).

Implements the human_decision.json round-trip:
  1. Controller writes a pending decision record (human_decision_pending.json)
     containing a controller-generated nonce.
  2. Human fills in / returns human_decision.json.
  3. Controller loads, validates schema, verifies the content hash has not been
     tampered with (using the nonce from the pending record), and applies the
     decision.

Content hash:
  SHA-256 over a canonical JSON representation of the stable fields
  (decision_id, run_id, issue_ids, decision, rationale, approved_by,
  created_at) PLUS a controller-generated nonce that is stored only in
  human_decision_pending.json.

  Format: "sha256:<64-hex-digits>"

  When run_dir is provided to build_decision / validate_and_observe, the
  nonce is used and the hash covers all decision-affecting fields + nonce.
  Without run_dir (legacy / unit-test mode), the nonce is the empty string
  and the hash covers only the stable fields — tamper detection still works
  for simple mutations, but a determined forger who can recompute the hash
  is not stopped unless they also have the pending record.

Tamper detection:
  validate_and_observe reads the nonce from human_decision_pending.json,
  recomputes the expected hash, and raises TamperError if it differs from
  the stored content_hash. A forger who changes decision, rationale, or
  approved_by cannot produce the correct hash without knowing the nonce.

modify_scope:
  When decision == "modify_scope", the caller MUST run contract amendment +
  risk re-score. This module signals the requirement but does not execute it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from ..errors import AnvilError
from ..schemas_util import assert_valid
from ..timeutil import now_iso


class TamperError(AnvilError):
    """Raised when a human_decision.json content hash does not match."""


class HumanDecisionError(AnvilError):
    """Raised for other human decision protocol violations."""


_PENDING_FILENAME = "human_decision_pending.json"

# Fields included in the content hash (stable / decision-affecting).
_HASH_FIELDS = (
    "decision_id",
    "run_id",
    "issue_ids",
    "decision",
    "rationale",
    "approved_by",
    "created_at",
)


def compute_content_hash(decision: dict[str, Any], *, controller_nonce: str = "") -> str:
    """Return the sha256 content hash for a human decision document.

    When controller_nonce is provided (non-empty), it is included in the hash
    so that a forger cannot produce a valid hash without knowing the nonce.
    The nonce is stored in human_decision_pending.json (controller-private).
    """
    canonical = {k: decision[k] for k in _HASH_FIELDS if k in decision}
    if controller_nonce:
        canonical["controller_nonce"] = controller_nonce
    serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _load_pending_nonce_strict(run_dir: Path, decision_id: str) -> str:
    """Load and return the controller nonce from the pending record.

    Fails closed: raises TamperError if the pending file is missing, if its
    decision_id does not match, or if the nonce is empty. Never returns "".
    """
    pending_path = run_dir / _PENDING_FILENAME
    if not pending_path.exists():
        raise TamperError(
            "human_decision_pending.json is missing — cannot verify content hash. "
            "The controller pending record is required for secure hash verification when "
            "run_dir is provided."
        )
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if pending.get("decision_id") != decision_id:
        raise TamperError(
            f"Pending record decision_id '{pending.get('decision_id')}' does not match "
            f"document decision_id '{decision_id}' — cannot verify content hash."
        )
    nonce = pending.get("controller_nonce", "")
    if not nonce:
        raise TamperError(
            "Pending record has empty controller_nonce — cannot verify content hash."
        )
    return nonce


def _write_pending(run_dir: Path, decision_id: str, nonce: str) -> None:
    """Write the controller-private pending record with the nonce."""
    pending = {"decision_id": decision_id, "controller_nonce": nonce}
    pending_path = run_dir / _PENDING_FILENAME
    with pending_path.open("w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2)
        f.write("\n")


def build_decision(
    *,
    decision_id: str,
    run_id: str,
    issue_ids: list[str],
    decision: str,
    rationale: str,
    approved_by: str,
    scope_delta: dict[str, Any] | None = None,
    expires_after_run: bool = True,
    requires_contract_amendment: bool = False,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Build and return a valid human_decision dict (with content hash).

    When run_dir is provided, a controller nonce is generated and stored in
    human_decision_pending.json. The content hash then covers all stable
    decision fields PLUS the nonce, preventing a forger from changing the
    decision and recomputing a valid hash without knowing the nonce.

    When run_dir is None (legacy / unit-test mode), the nonce is the empty
    string. The returned dict includes a private '_nonce' key for use by
    write_decision — this key is stripped before schema validation and before
    the file is written.
    """
    nonce = secrets.token_hex(16) if run_dir is not None else ""
    created_at = now_iso()
    doc: dict[str, Any] = {
        "decision_id": decision_id,
        "run_id": run_id,
        "issue_ids": issue_ids,
        "decision": decision,
        "rationale": rationale,
        "approved_by": approved_by,
        "created_at": created_at,
        "expires_after_run": expires_after_run,
        "requires_contract_amendment": requires_contract_amendment,
    }
    if scope_delta:
        doc["scope_delta"] = scope_delta
    doc["content_hash"] = compute_content_hash(doc, controller_nonce=nonce)

    # Validate the clean document (without _nonce) against the schema.
    assert_valid("human_decision", doc)

    # Write the pending record before returning; also stash the nonce privately
    # so write_decision can extract it without requiring callers to change API.
    if run_dir is not None and nonce:
        _write_pending(run_dir, decision_id, nonce)
    if nonce:
        doc["_nonce"] = nonce  # stripped by write_decision before writing to disk
    return doc


def validate_and_observe(
    decision: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate schema, verify content hash, stamp controller_observed_at.

    Returns an updated copy of the decision with controller_observed_at set.

    When run_dir is provided, the controller nonce is read from
    human_decision_pending.json and included in the expected hash, preventing
    a forger from producing a valid hash without knowing the nonce.

    Raises TamperError if the hash is wrong, HumanDecisionError on other
    protocol violations.
    """
    # Schema validation first (strip internal _nonce if present).
    clean = {k: v for k, v in decision.items() if not k.startswith("_")}
    assert_valid("human_decision", clean)

    # Run-id sanity check.
    if clean.get("run_id") != run_id:
        raise HumanDecisionError(
            f"human_decision run_id '{clean.get('run_id')}' != current run '{run_id}'"
        )

    # Content hash verification.
    if run_dir is not None:
        # Fail closed: pending file MUST exist and match when run_dir is provided.
        nonce = _load_pending_nonce_strict(run_dir, clean.get("decision_id", ""))
    else:
        # Legacy / unit-test mode (no run_dir): tamper detection works for simple
        # mutations but a determined forger who can recompute the hash is not blocked.
        nonce = ""

    stored_hash = clean.get("content_hash", "")
    expected_hash = compute_content_hash(clean, controller_nonce=nonce)
    if stored_hash != expected_hash:
        raise TamperError(
            f"human_decision content hash mismatch — document may have been tampered with. "
            f"stored={stored_hash!r}, expected={expected_hash!r}"
        )

    updated = dict(clean)
    updated["controller_observed_at"] = now_iso()
    return updated


def requires_amendment(decision: dict[str, Any]) -> bool:
    """True if this decision forces a contract amendment + risk re-score."""
    return decision.get("decision") == "modify_scope" or bool(
        decision.get("requires_contract_amendment")
    )


def write_decision(run_dir: Path, decision: dict[str, Any]) -> Path:
    """Write a human_decision.json into the run directory.

    Extracts and strips the private '_nonce' key (if present, written by
    build_decision). Writes human_decision_pending.json if nonce is non-empty
    (idempotent if build_decision already wrote it with run_dir).
    """
    nonce = decision.get("_nonce", "")
    doc = {k: v for k, v in decision.items() if not k.startswith("_")}

    if nonce:
        _write_pending(run_dir, doc["decision_id"], nonce)

    path = run_dir / "human_decision.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return path


def load_and_observe(run_dir: Path, run_id: str) -> dict[str, Any]:
    """Load human_decision.json, validate, and stamp observed_at.

    Passes run_dir to validate_and_observe so the pending nonce is used for
    hash verification when available.
    """
    path = run_dir / "human_decision.json"
    if not path.exists():
        raise HumanDecisionError("human_decision.json not found in run directory")
    with path.open(encoding="utf-8") as f:
        doc = json.load(f)
    return validate_and_observe(doc, run_id=run_id, run_dir=run_dir)
