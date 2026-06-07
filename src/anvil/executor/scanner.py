"""Diff-level secret scanner (Milestone 3).

Scans the unified diff produced after agent execution for credential patterns.
Only added lines (starting with '+', excluding '+++' headers) are examined so
the scan never false-positives on removed credential references.

Reuses the compiled regex patterns from Milestone 2's SecretRedactor. Scan
findings never contain the secret value itself — only the redacted line and the
matched pattern name are recorded. Logs must not expose secret values.

An optional allowlist of pattern names lets test infrastructure mark known
synthetic test-fixture secrets so they do not block the CI environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agents.redact import REDACTED, SecretRedactor, _PATTERNS


@dataclass
class ScanFinding:
    pattern_name: str
    line_number: int
    redacted_excerpt: str  # The secret value is replaced before logging.


@dataclass
class ScanResult:
    has_secrets: bool
    findings: list[ScanFinding] = field(default_factory=list)


_redactor = SecretRedactor()


def scan_diff(
    diff_text: str,
    allowlist: list[str] | None = None,
) -> ScanResult:
    """Scan a git unified diff for secret patterns.

    Parameters
    ----------
    diff_text:
        Output of ``git diff HEAD`` (unified format).
    allowlist:
        Pattern names to skip.  Use only in test-fixture contexts where a
        known synthetic secret is intentionally present.
    """
    skip: set[str] = set(allowlist or [])
    findings: list[ScanFinding] = []

    for lineno, line in enumerate(diff_text.splitlines(), start=1):
        # Only scan added lines; skip the unified diff '+++ b/...' header.
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]  # Strip the leading '+'.

        for name, pattern in _PATTERNS:
            if name in skip:
                continue
            if pattern.search(content):
                redacted, _ = _redactor.redact(content)
                findings.append(
                    ScanFinding(
                        pattern_name=name,
                        line_number=lineno,
                        redacted_excerpt=redacted[:200],
                    )
                )
                break  # One finding per line is enough; avoid duplicate alerts.

    return ScanResult(has_secrets=bool(findings), findings=findings)
