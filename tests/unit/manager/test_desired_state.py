"""
Unit test: DesiredStateSupervisor crash-loop detection and reconciliation.

Uses mock registry/supervisor/channel — no real processes spawned.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, call

import pytest

from manager.app.desired_state import (
    DesiredStateSupervisor,
    _BACKOFF_DELAYS,
    _CRASH_THRESHOLD,
    _CRASH_WINDOW_S,
)
from manager.app.models import AgentRegistration, AgentStatus


def _make_reg(
    agent_id: str = "agent-0",
    status: str = "RUNNING",
    desired: str = "running",
    crash_count: int = 0,
    last_crash_at: int | None = None,
) -> AgentRegistration:
    now = int(time.time() * 1000)
    return AgentRegistration(
        agent_id=agent_id,
        display_name="Test",
        status=AgentStatus(status),
        desired_status=desired,
        config_path="/tmp/cfg.yaml",
        data_dir="/tmp",
        terminal_path=None,
        mt5_login=12345,
        mt5_server="broker",
        monitoring_port=8081,
        symbols=["XAUUSD"],
        created_at=now,
        updated_at=now,
        last_seen_at=None,
        pid=None,
        crash_count=crash_count,
        last_crash_at=last_crash_at,
    )


def _make_svc(registry=None, supervisor=None):
    return DesiredStateSupervisor(
        registry=registry  or MagicMock(),
        supervisor=supervisor or MagicMock(),
    )


class TestCrashLoopDetection:

    def test_crash_loop_after_threshold(self):
        """CRASH_LOOP is set when crash_count reaches threshold within window."""
        registry  = MagicMock()
        agent_id  = "agent-0"
        # Simulate registry returning threshold count + recent crash_at
        now_ms = int(time.time() * 1000)
        reg_crashed = _make_reg(
            desired="running",
            crash_count=_CRASH_THRESHOLD,
            last_crash_at=now_ms,
        )
        # get_agent returns the agent with desired=running
        registry.get_agent.return_value = reg_crashed
        registry.increment_crash_count.return_value = _CRASH_THRESHOLD

        svc = _make_svc(registry=registry)
        svc.notify_crashed(agent_id)

        # Should have called set_agent_status with CRASH_LOOP
        registry.set_agent_status.assert_called_once()
        args = registry.set_agent_status.call_args
        assert args[0][1] == AgentStatus.CRASH_LOOP, (
            f"Expected CRASH_LOOP, got {args[0][1]}"
        )

    def test_below_threshold_schedules_backoff(self):
        """Fewer than threshold crashes schedule a restart, not CRASH_LOOP."""
        registry = MagicMock()
        agent_id = "agent-0"
        now_ms   = int(time.time() * 1000)
        reg      = _make_reg(desired="running", crash_count=1, last_crash_at=now_ms)
        registry.get_agent.return_value = reg
        registry.increment_crash_count.return_value = 1

        svc = _make_svc(registry=registry)
        svc.notify_crashed(agent_id)

        # CRASH_LOOP must NOT be set
        for c in registry.set_agent_status.call_args_list:
            assert c[0][1] != AgentStatus.CRASH_LOOP

        # A restart_at must be scheduled
        assert agent_id in svc._restart_at
        assert svc._restart_at[agent_id] > time.time()

    def test_desired_stopped_crashes_ignored(self):
        """Crashes for desired=stopped agents are not counted."""
        registry = MagicMock()
        reg      = _make_reg(desired="stopped")
        registry.get_agent.return_value = reg

        svc = _make_svc(registry=registry)
        svc.notify_crashed("agent-0")

        registry.increment_crash_count.assert_not_called()

    def test_reset_clears_state(self):
        """reset_crash_loop() clears _restart_at and resets registry counters."""
        registry = MagicMock()
        agent_id = "agent-0"
        svc = _make_svc(registry=registry)
        svc._restart_at[agent_id] = time.time() + 60

        svc.reset_crash_loop(agent_id)

        registry.reset_crash_count.assert_called_once_with(agent_id)
        registry.set_agent_status.assert_called_once_with(agent_id, AgentStatus.STOPPED)
        assert agent_id not in svc._restart_at

    def test_backoff_delay_sequence(self):
        """Each successive crash uses the next backoff delay."""
        registry = MagicMock()
        agent_id = "agent-0"
        now_ms   = int(time.time() * 1000)

        delays_seen = []
        for crash_num in range(1, len(_BACKOFF_DELAYS) + 1):
            reg = _make_reg(desired="running", crash_count=crash_num, last_crash_at=now_ms)
            registry.get_agent.return_value = reg
            registry.increment_crash_count.return_value = crash_num

            svc = _make_svc(registry=registry)
            if crash_num < _CRASH_THRESHOLD:
                svc.notify_crashed(agent_id)
                if agent_id in svc._restart_at:
                    delay = svc._restart_at[agent_id] - time.time()
                    delays_seen.append(delay)

        # Delays must be non-decreasing
        for i in range(1, len(delays_seen)):
            assert delays_seen[i] >= delays_seen[i - 1] - 1.0


class TestReconcileLoop:

    def test_desired_running_spawns_stopped_agent(self):
        """_reconcile() spawns an agent that is STOPPED and desired=running."""
        supervisor = MagicMock()
        svc = _make_svc(supervisor=supervisor)
        reg = _make_reg(status="STOPPED", desired="running")

        svc._reconcile(reg)

        supervisor.spawn.assert_called_once_with(reg.agent_id)

    def test_desired_stopped_terminates_running_agent(self):
        """_reconcile() terminates an agent that is RUNNING and desired=stopped."""
        supervisor = MagicMock()
        svc = _make_svc(supervisor=supervisor)
        reg = _make_reg(status="RUNNING", desired="stopped")

        svc._reconcile(reg)

        supervisor.terminate.assert_called_once()

    def test_crash_loop_agent_skipped(self):
        """_reconcile() does nothing for CRASH_LOOP agents."""
        supervisor = MagicMock()
        svc = _make_svc(supervisor=supervisor)
        reg = _make_reg(status="CRASH_LOOP", desired="running")

        svc._reconcile(reg)

        supervisor.spawn.assert_not_called()
        supervisor.terminate.assert_not_called()

    def test_backoff_prevents_premature_restart(self):
        """_reconcile() does not spawn if the restart_at deadline hasn't passed."""
        supervisor = MagicMock()
        svc = _make_svc(supervisor=supervisor)
        reg = _make_reg(status="STOPPED", desired="running")

        # Set restart deadline in the far future
        svc._restart_at[reg.agent_id] = time.time() + 9999

        svc._reconcile(reg)

        supervisor.spawn.assert_not_called()
