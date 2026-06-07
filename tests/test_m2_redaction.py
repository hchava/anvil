"""Tests for pre-context secret redaction (Milestone 2).

Covers: API key redaction, .env-style credential redaction, PEM block
redaction, blocked file detection, clean content pass-through, redaction
count accuracy, no secret values in output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anvil.agents.redact import REDACTED, SecretRedactor


@pytest.fixture
def redactor() -> SecretRedactor:
    return SecretRedactor()


# ---------------------------------------------------------------------------
# Blocked file detection
# ---------------------------------------------------------------------------

def test_blocked_dotenv(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path(".env"))


def test_blocked_dotenv_local(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path(".env.local"))


def test_blocked_dotenv_production(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path(".env.production"))


def test_blocked_pem_file(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path("server.pem"))


def test_blocked_key_file(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path("private.key"))


def test_blocked_id_rsa(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path("id_rsa"))


def test_blocked_credentials(redactor: SecretRedactor) -> None:
    assert redactor.is_blocked_file(Path("credentials"))


def test_not_blocked_regular_py(redactor: SecretRedactor) -> None:
    assert not redactor.is_blocked_file(Path("src/config.py"))


def test_not_blocked_readme(redactor: SecretRedactor) -> None:
    assert not redactor.is_blocked_file(Path("README.md"))


# ---------------------------------------------------------------------------
# safe_excerpt with blocked file
# ---------------------------------------------------------------------------

def test_safe_excerpt_blocked_file_returns_placeholder(redactor: SecretRedactor) -> None:
    text, count = redactor.safe_excerpt(Path(".env"), "SECRET_KEY=abc123\nDB_PASS=hunter2\n")
    assert REDACTED not in text
    assert "BLOCKED" in text
    assert count == 0  # nothing redacted (content not even scanned)


# ---------------------------------------------------------------------------
# AWS key redaction
# ---------------------------------------------------------------------------

def test_aws_access_key_redacted(redactor: SecretRedactor) -> None:
    # Synthetic AKIA key split across concat to avoid triggering secret scanners.
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    text = f"Using key {fake_key} in the config."
    clean, count = redactor.redact(text)
    assert fake_key not in clean
    assert REDACTED in clean
    assert count >= 1


def test_asia_key_redacted(redactor: SecretRedactor) -> None:
    # ASIA temp keys are ASIA + exactly 16 uppercase chars = 20 chars total.
    # Split across concat to avoid triggering secret scanners on a synthetic value.
    fake_key = "ASIA" + "IOSFODNN7EXAMPLE"
    text = f"Temporary key: {fake_key} for the role."
    clean, count = redactor.redact(text)
    assert fake_key not in clean
    assert count >= 1


# ---------------------------------------------------------------------------
# .env-style assignment redaction
# ---------------------------------------------------------------------------

def test_env_api_key_assignment_redacted(redactor: SecretRedactor) -> None:
    text = "API_KEY=supersecretapikey12345\n"
    clean, count = redactor.redact(text)
    assert "supersecretapikey12345" not in clean
    assert count >= 1


def test_env_password_assignment_redacted(redactor: SecretRedactor) -> None:
    text = "PASSWORD=hunter2verylongpassword\n"
    clean, count = redactor.redact(text)
    assert "hunter2verylongpassword" not in clean
    assert count >= 1


def test_env_secret_key_assignment_redacted(redactor: SecretRedactor) -> None:
    text = "SECRET_KEY=my-super-duper-secret-value\n"
    clean, count = redactor.redact(text)
    assert "my-super-duper-secret-value" not in clean
    assert count >= 1


# ---------------------------------------------------------------------------
# GitHub / Slack / Stripe token redaction
# ---------------------------------------------------------------------------

def test_github_token_redacted(redactor: SecretRedactor) -> None:
    token = "ghp_" + "A" * 36
    text = f"Token: {token}\n"
    clean, count = redactor.redact(text)
    assert token not in clean
    assert count >= 1


def test_slack_token_redacted(redactor: SecretRedactor) -> None:
    # Synthetic token split across concat so it is not a literal secret in source.
    prefix = "xoxb-"
    fake_token = prefix + "11111111111-22222222222-AAAAAAAAAAAAAAAAAAAAAAAA"
    text = f"Webhook: {fake_token}\n"
    clean, count = redactor.redact(text)
    assert prefix + "11111111111" not in clean
    assert count >= 1


def test_stripe_live_key_redacted(redactor: SecretRedactor) -> None:
    text = "stripe_key = sk_live_" + "a" * 24
    clean, count = redactor.redact(text)
    assert "sk_live_" not in clean
    assert count >= 1


# ---------------------------------------------------------------------------
# PEM block redaction
# ---------------------------------------------------------------------------

def test_pem_private_key_block_redacted(redactor: SecretRedactor) -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    text = f"Loaded key:\n{pem}\nDone."
    clean, count = redactor.redact(text)
    assert "BEGIN RSA PRIVATE KEY" not in clean
    assert REDACTED in clean
    assert count >= 1


# ---------------------------------------------------------------------------
# Clean content passes through unchanged
# ---------------------------------------------------------------------------

def test_clean_code_not_modified(redactor: SecretRedactor) -> None:
    code = "def add(a, b):\n    return a + b\n"
    clean, count = redactor.redact(code)
    assert clean == code
    assert count == 0


def test_clean_docstring_not_modified(redactor: SecretRedactor) -> None:
    doc = '"""Module to parse configuration files."""\nimport json\n'
    clean, count = redactor.redact(doc)
    assert clean == doc
    assert count == 0


# ---------------------------------------------------------------------------
# Redaction count accuracy
# ---------------------------------------------------------------------------

def test_multiple_secrets_counted(redactor: SecretRedactor) -> None:
    text = (
        "API_KEY=firstsecretvalue12345\n"
        "ACCESS_TOKEN=secondsecrettoken12345\n"
    )
    _, count = redactor.redact(text)
    assert count >= 2


# ---------------------------------------------------------------------------
# Secret values never appear in output
# ---------------------------------------------------------------------------

def test_secret_value_absent_from_output(redactor: SecretRedactor) -> None:
    secret = "supersecrettoken99999"
    text = f"api_key={secret}\n"
    clean, count = redactor.redact(text)
    assert secret not in clean


def test_safe_excerpt_on_clean_file(redactor: SecretRedactor, tmp_path: Path) -> None:
    content = "x = 1\ny = 2\n"
    clean, count = redactor.safe_excerpt(tmp_path / "module.py", content)
    assert clean == content
    assert count == 0
