"""Tests for the command policy engine (Milestone 2).

Covers: allowed binaries, blocked binaries, blocked arg patterns, network
binaries, env allowlist, timeout validation, from_dict / to_dict round-trip.
"""

from __future__ import annotations

import pytest

from anvil.controller.policy import CommandPolicy, PolicyResult


@pytest.fixture
def policy() -> CommandPolicy:
    return CommandPolicy.default()


# ---------------------------------------------------------------------------
# Allowed binaries
# ---------------------------------------------------------------------------

def test_pytest_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["pytest", "tests/"])
    assert result.allowed


def test_ruff_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["ruff", "check", "src/"])
    assert result.allowed


def test_mypy_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["mypy", "src/anvil/"])
    assert result.allowed


def test_git_status_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["git", "status"])
    assert result.allowed


def test_npm_run_test_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["npm", "run", "test"])
    assert result.allowed


def test_cargo_test_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["cargo", "test"])
    assert result.allowed


# ---------------------------------------------------------------------------
# Blocked binaries
# ---------------------------------------------------------------------------

def test_curl_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["curl", "https://example.com"])
    assert not result.allowed
    assert result.rule in ("blocked_binary", "network_not_allowed")


def test_wget_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["wget", "https://example.com"])
    assert not result.allowed


def test_ssh_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["ssh", "user@host"])
    assert not result.allowed


def test_sudo_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["sudo", "apt", "install", "vim"])
    assert not result.allowed


def test_rm_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["rm", "-rf", "/tmp/test"])
    assert not result.allowed


def test_bash_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["bash", "-c", "echo hello"])
    assert not result.allowed


def test_sh_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["sh", "-c", "ls"])
    assert not result.allowed


# ---------------------------------------------------------------------------
# Blocked arg patterns
# ---------------------------------------------------------------------------

def test_python_dash_c_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["python", "-c", "import os; os.remove('file')"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_python3_dash_c_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["python3", "-c", "print('hello')"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_python_http_server_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["python", "-m", "http.server", "8080"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_npm_install_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["npm", "install"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_pip_install_blocked(policy: CommandPolicy) -> None:
    # pip is not in the default allowlist, so it is blocked regardless.
    result = policy.validate(["pip", "install", "requests"])
    assert not result.allowed


def test_pip_install_blocked_by_arg_pattern_when_in_allowlist() -> None:
    # When pip IS in the allowlist the arg pattern gate blocks "install".
    p = CommandPolicy.from_dict({
        "allowed_binaries": ["pip"],
        "blocked_binaries": [],
        "blocked_arg_patterns": [{"binary": "pip", "args_contains": ["install"]}],
        "network_allowed": False,
    })
    result = p.validate(["pip", "install", "requests"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_git_push_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["git", "push", "origin", "main"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_git_fetch_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["git", "fetch"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_git_clone_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["git", "clone", "https://github.com/x/y"])
    assert not result.allowed
    assert result.rule == "blocked_arg_pattern"


def test_python_m_pytest_allowed(policy: CommandPolicy) -> None:
    result = policy.validate(["python", "-m", "pytest", "tests/"])
    assert result.allowed


# ---------------------------------------------------------------------------
# Empty command
# ---------------------------------------------------------------------------

def test_empty_command_blocked(policy: CommandPolicy) -> None:
    result = policy.validate([])
    assert not result.allowed
    assert result.rule == "empty_command"


# ---------------------------------------------------------------------------
# Not in allowlist
# ---------------------------------------------------------------------------

def test_unknown_binary_blocked(policy: CommandPolicy) -> None:
    result = policy.validate(["arbitrary_tool", "--flag"])
    assert not result.allowed
    assert result.rule == "not_in_allowlist"


# ---------------------------------------------------------------------------
# Custom policy via from_dict
# ---------------------------------------------------------------------------

def test_from_dict_custom_allowed_binaries() -> None:
    p = CommandPolicy.from_dict({
        "allowed_binaries": ["my_tool"],
        "blocked_binaries": [],
        "blocked_arg_patterns": [],
        "network_allowed": False,
    })
    assert p.validate(["my_tool", "--run"]).allowed
    assert not p.validate(["pytest"]).allowed  # not in custom allowlist


def test_from_dict_network_allowed() -> None:
    p = CommandPolicy.from_dict({
        "allowed_binaries": ["curl"],
        "blocked_binaries": [],
        "blocked_arg_patterns": [],
        "network_allowed": True,
    })
    result = p.validate(["curl", "https://example.com"])
    assert result.allowed


def test_from_dict_empty_allowed_binaries_means_no_allowlist() -> None:
    """Empty allowed_binaries list means NO allowlist enforcement — all non-blocked binaries pass."""
    p = CommandPolicy.from_dict({
        "allowed_binaries": [],
        "blocked_binaries": ["rm"],
        "blocked_arg_patterns": [],
        "network_allowed": True,
    })
    assert p.validate(["any_tool"]).allowed
    assert not p.validate(["rm", "-rf", "/"]).allowed


# ---------------------------------------------------------------------------
# to_dict round-trip
# ---------------------------------------------------------------------------

def test_to_dict_round_trip() -> None:
    p = CommandPolicy.default()
    d = p.to_dict()
    p2 = CommandPolicy.from_dict(d)
    assert p2.allowed_binaries == p.allowed_binaries
    assert p2.blocked_binaries == p.blocked_binaries
    assert p2.network_allowed == p.network_allowed
    assert p2.max_timeout_seconds == p.max_timeout_seconds


# ---------------------------------------------------------------------------
# Env allowlist
# ---------------------------------------------------------------------------

def test_env_allowlist_detects_violation(policy: CommandPolicy) -> None:
    violations = policy.validate_env({"PATH": "/usr/bin", "SECRET_KEY": "abc"})
    assert "SECRET_KEY" in violations
    assert "PATH" not in violations


def test_env_allowlist_clean(policy: CommandPolicy) -> None:
    violations = policy.validate_env({"PATH": "/usr/bin", "PYTHONPATH": "/app"})
    assert not violations


# ---------------------------------------------------------------------------
# Timeout validation
# ---------------------------------------------------------------------------

def test_timeout_within_limit_allowed(policy: CommandPolicy) -> None:
    result = policy.validate_timeout(300)
    assert result.allowed


def test_timeout_at_limit_allowed(policy: CommandPolicy) -> None:
    result = policy.validate_timeout(600)
    assert result.allowed


def test_timeout_exceeds_limit_blocked(policy: CommandPolicy) -> None:
    result = policy.validate_timeout(601)
    assert not result.allowed
    assert "timeout" in result.rule


# ---------------------------------------------------------------------------
# PolicyResult convenience methods
# ---------------------------------------------------------------------------

def test_policy_result_permit() -> None:
    r = PolicyResult.permit()
    assert r.allowed
    assert r.reason is None


def test_policy_result_block() -> None:
    r = PolicyResult.block("bad command", "blocked_binary")
    assert not r.allowed
    assert r.reason == "bad command"
    assert r.rule == "blocked_binary"
