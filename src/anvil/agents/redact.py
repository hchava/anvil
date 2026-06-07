"""Pre-context secret redaction (Milestone 2).

Scans file excerpts and artifact text before they are placed into an agent
prompt. Replaces secret-looking strings with a redaction marker. Logs the
count of redactions, never the secret values themselves.

Secret patterns covered:
  - AWS access keys:  AKIA... / ASIA...
  - Generic API keys: api_key=..., API_KEY=..., apikey: ...
  - Bearer / auth tokens in header-style lines
  - GitHub / GitLab / Slack / Stripe tokens (format-based)
  - Private key PEM blocks (BEGIN ... PRIVATE KEY)
  - .env-style assignments: KEY=value on a line where value looks secret
  - Generic high-entropy token patterns (≥20 random-looking chars after = or :)

Blocked files (never included in context by default):
  .env, .env.*, *.pem, *.key, *.p12, *.pfx, id_rsa, id_dsa, id_ecdsa,
  credentials, secrets.*, *.secret, vault.*, *.vault

Usage:
  redactor = SecretRedactor()
  clean, count = redactor.redact(text)
  context_text = redactor.safe_excerpt(file_path, raw_text)
"""

from __future__ import annotations

import re
from pathlib import Path

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Blocked file patterns (never include in agent context)
# ---------------------------------------------------------------------------

_BLOCKED_NAME_EXACT: frozenset[str] = frozenset(
    {
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        ".netrc",
        ".htpasswd",
    }
)

_BLOCKED_SUFFIXES: frozenset[str] = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".secret", ".vault", ".keystore"}
)

_BLOCKED_STEM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\.env(\..+)?$"),
    re.compile(r"^secrets?(\..+)?$"),
    re.compile(r"^vault(\..+)?$"),
    re.compile(r"^.*_secret$"),
    re.compile(r"^.*_credentials?$"),
]

# ---------------------------------------------------------------------------
# Redaction patterns (ordered: most-specific first)
# ---------------------------------------------------------------------------

# Each entry: (name, compiled pattern, replacement group or callable).
# The pattern must match the entire secret value (not just a prefix) so
# we can replace only the value portion while keeping the key name.

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # PEM private key blocks — replace the whole block.
    (
        "pem_block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |)PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |)PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # AWS-style access keys.
    (
        "aws_key",
        re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"),
    ),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"),
    ),
    # Slack tokens: xoxb-, xoxp-, xoxa-
    (
        "slack_token",
        re.compile(r"\bxox[bpas]-[0-9A-Za-z\-]{10,}\b"),
    ),
    # Stripe keys: sk_live_, rk_live_, sk_test_
    (
        "stripe_key",
        re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}\b"),
    ),
    # .env-style key=value where value looks like a secret (≥16 non-space chars
    # that are not a URL, path, or plain word).
    (
        "env_assignment",
        re.compile(
            r"(?i)(?:^|(?<=\n))[ \t]*"
            r"(?:api[_\-]?key|secret[_\-]?key?|auth[_\-]?token|access[_\-]?token|"
            r"password|passwd|private[_\-]?key|bearer|credentials?)"
            r"[ \t]*[=:][ \t]*([^\s\n]{8,})",
            re.MULTILINE,
        ),
    ),
    # Authorization / X-API-Key header values.
    (
        "auth_header",
        re.compile(
            r"(?i)(?:Authorization|X-Api-Key|X-Auth-Token):\s*([^\s\n]{8,})",
        ),
    ),
    # Generic high-entropy token after = or : (≥24 chars, mixed case + digits).
    (
        "generic_token",
        re.compile(
            r'(?<=[=:\'"])([A-Za-z0-9+/]{24,}={0,2})(?=[\'"\s\n,}]|$)'
        ),
    ),
]

# These patterns have a capture group for the VALUE only — replace group 1.
_VALUE_GROUP_PATTERNS: frozenset[str] = frozenset(
    {"env_assignment", "auth_header", "generic_token"}
)


class SecretRedactor:
    """Scans text for secrets and replaces them with [REDACTED]."""

    def is_blocked_file(self, path: Path | str) -> bool:
        """Return True if the file should never appear in agent context."""
        p = Path(path)
        name = p.name
        suffix = p.suffix.lower()

        if name in _BLOCKED_NAME_EXACT:
            return True
        if suffix in _BLOCKED_SUFFIXES:
            return True
        for pat in _BLOCKED_STEM_PATTERNS:
            if pat.match(name):
                return True
        return False

    def redact(self, text: str) -> tuple[str, int]:
        """Return (redacted_text, redaction_count).

        Secret values are replaced with [REDACTED]. The number of replacements
        is returned so callers can log the count without logging the values.
        """
        count = 0
        for name, pattern in _PATTERNS:
            if name in _VALUE_GROUP_PATTERNS:
                # Replace only the captured value group.
                def _replace_value(m: re.Match[str]) -> str:
                    nonlocal count
                    full = m.group(0)
                    val = m.group(1)
                    count += 1
                    return full[: m.start(1) - m.start()] + REDACTED + full[m.end(1) - m.start():]

                text = pattern.sub(_replace_value, text)
            else:
                new_text, n = pattern.subn(REDACTED, text)
                count += n
                text = new_text
        return text, count

    def safe_excerpt(self, file_path: Path | str, raw_text: str) -> tuple[str, int]:
        """Redact raw_text from file_path. Returns (clean_text, redaction_count).

        If the file itself is blocked, returns a placeholder instead of the content.
        """
        if self.is_blocked_file(file_path):
            return f"[FILE BLOCKED: {Path(file_path).name}]", 0
        return self.redact(raw_text)
