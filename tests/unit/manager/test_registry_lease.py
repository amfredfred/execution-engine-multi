"""
Unit test: terminal lease atomicity in AgentRegistry.

Two concurrent threads race to acquire the same terminal path — exactly one
must succeed.  Uses a temp SQLite DB so no real filesystem state is needed.
"""
from __future__ import annotations

import tempfile
import threading

import pytest

from manager.app.registry import AgentRegistry


@pytest.fixture
def registry(tmp_path):
    reg = AgentRegistry(str(tmp_path))
    reg.init()
    return reg


def _seed_agent(registry: AgentRegistry, agent_id: str) -> None:
    """Insert a minimal agent row so the lease FK constraint is satisfied."""
    import time
    registry._connect().__enter__().execute(
        """
        INSERT OR IGNORE INTO agents
            (agent_id, display_name, status, desired_status,
             config_path, data_dir, monitoring_port, symbols,
             created_at, updated_at)
        VALUES (?, ?, 'PROVISIONED', 'running', '', '', 8081, '[]', ?, ?)
        """,
        (agent_id, agent_id, int(time.time() * 1000), int(time.time() * 1000)),
    )


class TestTerminalLeaseAtomicity:

    def test_only_one_winner(self, registry):
        """Two threads competing for the same terminal path — one wins, one loses."""
        terminal_path = "/fake/terminal64.exe"
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def attempt(agent_id: str) -> None:
            barrier.wait()   # both threads start at exactly the same time
            won = registry.acquire_terminal_lease(terminal_path, agent_id)
            results.append(won)

        t1 = threading.Thread(target=attempt, args=("agent-0",))
        t2 = threading.Thread(target=attempt, args=("agent-1",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(results) == 2
        assert results.count(True)  == 1, "Exactly one thread must win the lease"
        assert results.count(False) == 1, "Exactly one thread must lose the lease"

    def test_second_acquire_same_path_fails(self, registry):
        """A second acquire on the same path always fails (IntegrityError), even same agent."""
        path = "/fake/terminal64.exe"
        assert registry.acquire_terminal_lease(path, "agent-0") is True
        assert registry.acquire_terminal_lease(path, "agent-0") is False

    def test_different_paths_independent(self, registry):
        """Two different terminals can each be leased by different agents."""
        assert registry.acquire_terminal_lease("/path/A/terminal64.exe", "agent-0") is True
        assert registry.acquire_terminal_lease("/path/B/terminal64.exe", "agent-1") is True

    def test_release_allows_re_lease(self, registry):
        """After releasing, another agent can acquire the same terminal."""
        path = "/fake/terminal64.exe"
        assert registry.acquire_terminal_lease(path, "agent-0") is True
        registry.release_terminal_lease(path)
        assert registry.acquire_terminal_lease(path, "agent-1") is True
