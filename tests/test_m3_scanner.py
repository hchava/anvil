"""Tests for the diff-level secret scanner (Milestone 3).

Credential-format strings are split across concatenation so GitHub secret
scanning does not flag this test file itself. No network, no real API keys.
"""

from __future__ import annotations

from anvil.executor.scanner import ScanResult, scan_diff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _diff_adding(content: str) -> str:
    """Wrap content in a minimal unified diff 'added' line."""
    return f"+{content}\n"


def _multi_line_diff(*lines: str) -> str:
    return "".join(f"+{line}\n" for line in lines)


# ---------------------------------------------------------------------------
# Clean diffs pass through
# ---------------------------------------------------------------------------

def test_clean_diff_no_findings() -> None:
    diff = _diff_adding("def hello(): return 'hello world'")
    result = scan_diff(diff)
    assert not result.has_secrets
    assert result.findings == []


def test_empty_diff_passes() -> None:
    result = scan_diff("")
    assert not result.has_secrets


def test_removed_lines_not_scanned() -> None:
    # '-' prefixed lines (removed) should never trigger alerts.
    diff = "-AKIAIOSFODNN7" + "EXAMPLE removed line\n"
    result = scan_diff(diff)
    assert not result.has_secrets


def test_context_lines_not_scanned() -> None:
    # Lines without '+' or '-' prefix are context lines; never scanned.
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    diff = f" context line with {fake_key} in it\n"
    result = scan_diff(diff)
    assert not result.has_secrets


# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------

def test_aws_access_key_detected_in_diff() -> None:
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    diff = _diff_adding(f"aws_key = '{fake_key}'")
    result = scan_diff(diff)
    assert result.has_secrets
    assert any(f.pattern_name == "aws_key" for f in result.findings)


def test_aws_temp_key_detected_in_diff() -> None:
    fake_key = "ASIA" + "IOSFODNN7EXAMPLE"
    diff = _diff_adding(f"temp_key = '{fake_key}'")
    result = scan_diff(diff)
    assert result.has_secrets


def test_github_token_detected_in_diff() -> None:
    token = "ghp_" + "A" * 36
    diff = _diff_adding(f"token: {token}")
    result = scan_diff(diff)
    assert result.has_secrets


def test_env_api_key_detected_in_diff() -> None:
    diff = _diff_adding("API_KEY=supersecretlongvalue99999")
    result = scan_diff(diff)
    assert result.has_secrets


def test_slack_token_detected_in_diff() -> None:
    prefix = "xoxb-"
    token = prefix + "11111111111-22222222222-AAAAAAAAAAAAAAAAAAAAAAAA"
    diff = _diff_adding(f"slack_token = '{token}'")
    result = scan_diff(diff)
    assert result.has_secrets


def test_stripe_key_detected_in_diff() -> None:
    stripe_key = "sk_live_" + "a" * 24
    diff = _diff_adding(f"stripe = '{stripe_key}'")
    result = scan_diff(diff)
    assert result.has_secrets


# ---------------------------------------------------------------------------
# Findings do not contain secret values
# ---------------------------------------------------------------------------

def test_findings_do_not_expose_secret_value() -> None:
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    diff = _diff_adding(f"key = '{fake_key}'")
    result = scan_diff(diff)
    assert result.has_secrets
    for finding in result.findings:
        # The raw key must not appear in the finding excerpt.
        assert fake_key not in finding.redacted_excerpt


def test_logs_are_safe_for_github_token() -> None:
    token = "ghp_" + "B" * 36
    diff = _diff_adding(f"GITHUB_TOKEN={token}")
    result = scan_diff(diff)
    assert result.has_secrets
    for finding in result.findings:
        assert token not in finding.redacted_excerpt


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def test_allowlist_skips_pattern() -> None:
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    diff = _diff_adding(f"key = '{fake_key}'")
    result = scan_diff(diff, allowlist=["aws_key"])
    # Pattern skipped; should have no findings (unless another pattern matched).
    assert not any(f.pattern_name == "aws_key" for f in result.findings)


def test_allowlist_only_skips_named_pattern() -> None:
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    env_line = "API_KEY=supersecretlongvalue99999"
    diff = _diff_adding(f"key = '{fake_key}'") + _diff_adding(env_line)
    # Skip aws_access_key but env_assignment should still fire.
    result = scan_diff(diff, allowlist=["aws_key"])
    assert any(f.pattern_name != "aws_key" for f in result.findings)


# ---------------------------------------------------------------------------
# Line number tracking
# ---------------------------------------------------------------------------

def test_line_numbers_in_findings() -> None:
    diff = (
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "+regular line\n"
        "+API_KEY=toplongsecretvalue12345\n"
    )
    result = scan_diff(diff)
    assert result.has_secrets
    # The secret is on diff line 4 (1-indexed).
    assert any(f.line_number == 4 for f in result.findings)
