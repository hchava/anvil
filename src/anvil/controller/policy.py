"""Full command policy engine (Milestone 2).

Validates structured command arrays against a policy configuration. This
module validates ONLY — it does not execute commands (execution is Milestone 3).

Policy fields (all optional; missing fields use safe defaults):
  allowed_binaries        - list of permitted binary names
  blocked_binaries        - list of always-blocked binary names
  blocked_arg_patterns    - list of {binary, args_contains} dicts
  network_allowed         - bool (default: False)
  max_timeout_seconds     - int (default: 600)
  max_output_bytes        - int (default: 200_000)
  allowed_working_directory - "worktree_only" | "any" (default: "worktree_only")
  env_allowlist           - list of allowed env var names

Usage:
  policy = CommandPolicy.from_dict({...})
  result = policy.validate(["pytest", "tests/"])
  assert result.allowed, result.reason
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Default policy (mirrors the roadmap spec)
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_BINARIES: list[str] = [
    "pytest", "ruff", "mypy", "npm", "pnpm", "go", "cargo", "git",
    "python", "python3", "node", "make",
]

_DEFAULT_BLOCKED_BINARIES: list[str] = [
    "curl", "wget", "ssh", "scp", "sudo", "rm",
    "sh", "bash", "zsh", "fish", "csh", "tcsh",
    "nc", "netcat", "ncat", "telnet", "ftp", "sftp",
    "docker", "kubectl", "terraform",
]

_DEFAULT_BLOCKED_ARG_PATTERNS: list[dict[str, list[str]]] = [
    {"binary": "python", "args_contains": ["-c"]},
    {"binary": "python3", "args_contains": ["-c"]},
    {"binary": "python", "args_contains": ["-m", "http.server"]},
    {"binary": "python3", "args_contains": ["-m", "http.server"]},
    {"binary": "npm", "args_contains": ["install"]},
    {"binary": "pip", "args_contains": ["install"]},
    {"binary": "pip3", "args_contains": ["install"]},
    {"binary": "git", "args_contains": ["push"]},
    {"binary": "git", "args_contains": ["fetch"]},
    {"binary": "git", "args_contains": ["clone"]},
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PolicyResult:
    """Outcome of a policy validation check."""

    allowed: bool
    reason: str | None = None
    rule: str | None = None

    @classmethod
    def permit(cls) -> "PolicyResult":
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: str, rule: str) -> "PolicyResult":
        return cls(allowed=False, reason=reason, rule=rule)


# ---------------------------------------------------------------------------
# Policy object
# ---------------------------------------------------------------------------


@dataclass
class CommandPolicy:
    """Parsed command policy configuration."""

    allowed_binaries: list[str] = field(default_factory=lambda: list(_DEFAULT_ALLOWED_BINARIES))
    blocked_binaries: list[str] = field(default_factory=lambda: list(_DEFAULT_BLOCKED_BINARIES))
    blocked_arg_patterns: list[dict[str, list[str]]] = field(
        default_factory=lambda: list(_DEFAULT_BLOCKED_ARG_PATTERNS)
    )
    network_allowed: bool = False
    max_timeout_seconds: int = 600
    max_output_bytes: int = 200_000
    allowed_working_directory: str = "worktree_only"
    env_allowlist: list[str] = field(
        default_factory=lambda: ["PATH", "PYTHONPATH", "NODE_ENV", "HOME", "USER"]
    )

    @classmethod
    def default(cls) -> "CommandPolicy":
        return cls()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CommandPolicy":
        return cls(
            allowed_binaries=d.get("allowed_binaries", list(_DEFAULT_ALLOWED_BINARIES)),
            blocked_binaries=d.get("blocked_binaries", list(_DEFAULT_BLOCKED_BINARIES)),
            blocked_arg_patterns=d.get(
                "blocked_arg_patterns", list(_DEFAULT_BLOCKED_ARG_PATTERNS)
            ),
            network_allowed=bool(d.get("network_allowed", False)),
            max_timeout_seconds=int(d.get("max_timeout_seconds", 600)),
            max_output_bytes=int(d.get("max_output_bytes", 200_000)),
            allowed_working_directory=d.get("allowed_working_directory", "worktree_only"),
            env_allowlist=d.get("env_allowlist", ["PATH", "PYTHONPATH", "NODE_ENV", "HOME", "USER"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_binaries": self.allowed_binaries,
            "blocked_binaries": self.blocked_binaries,
            "blocked_arg_patterns": self.blocked_arg_patterns,
            "network_allowed": self.network_allowed,
            "max_timeout_seconds": self.max_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "allowed_working_directory": self.allowed_working_directory,
            "env_allowlist": self.env_allowlist,
        }

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #

    def validate(self, command_array: list[str]) -> PolicyResult:
        """Validate a structured command array against this policy.

        Checks are applied in order; the first failing check blocks the command.
        """
        if not command_array:
            return PolicyResult.block("empty command array", "empty_command")

        binary = os.path.basename(command_array[0])

        # 1. Always-blocked binaries.
        if binary in self.blocked_binaries:
            return PolicyResult.block(
                f"binary '{binary}' is in the blocked_binaries list",
                "blocked_binary",
            )

        # 2. Allowed binaries (if the list is non-empty, enforce it as an allowlist).
        if self.allowed_binaries and binary not in self.allowed_binaries:
            return PolicyResult.block(
                f"binary '{binary}' is not in the allowed_binaries list",
                "not_in_allowlist",
            )

        # 3. Blocked argument patterns.
        args = command_array[1:]
        for pattern in self.blocked_arg_patterns:
            if pattern.get("binary") != binary:
                continue
            required_args: list[str] = pattern.get("args_contains", [])
            # All required args must appear in the command args (in any order/position).
            if all(a in args for a in required_args):
                return PolicyResult.block(
                    f"command '{binary} {' '.join(args)}' matches blocked arg pattern "
                    f"args_contains={required_args}",
                    "blocked_arg_pattern",
                )

        # 4. Network binaries (network_allowed=false blocks curl/wget even if someone
        #    added them to allowed_binaries).
        _NETWORK_BINARIES = {"curl", "wget", "nc", "netcat", "ncat", "ssh", "telnet"}
        if not self.network_allowed and binary in _NETWORK_BINARIES:
            return PolicyResult.block(
                f"binary '{binary}' makes network calls and network_allowed=false",
                "network_not_allowed",
            )

        return PolicyResult.permit()

    def validate_env(self, env: dict[str, str]) -> list[str]:
        """Return a list of env vars not in the allowlist."""
        return [k for k in env if k not in self.env_allowlist]

    def validate_timeout(self, timeout_seconds: int) -> PolicyResult:
        if timeout_seconds > self.max_timeout_seconds:
            return PolicyResult.block(
                f"timeout {timeout_seconds}s exceeds max_timeout_seconds {self.max_timeout_seconds}",
                "timeout_exceeded",
            )
        return PolicyResult.permit()
