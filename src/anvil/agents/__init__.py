"""Anvil agents module (Milestone 2).

Provides the agent IO contract, launcher abstraction, monitor, and pre-context
secret redaction used by the Milestone 2 Claude/Codex contract loop.
"""

from .io import AgentTask, AgentWorkspace
from .launcher import AgentLauncher, FakeAgentLauncher, StaleFakeAgentLauncher
from .monitor import AgentMonitor, MonitorResult
from .redact import SecretRedactor

__all__ = [
    "AgentTask",
    "AgentWorkspace",
    "AgentLauncher",
    "FakeAgentLauncher",
    "StaleFakeAgentLauncher",
    "AgentMonitor",
    "MonitorResult",
    "SecretRedactor",
]
