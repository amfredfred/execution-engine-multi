"""
manager/registry.py — SQLite persistence for the manager layer.

Single WAL-mode database at <storage_path>/registry.db.
All tables created idempotently on init().
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from src.manager.models import (
    AgentRegistration,
    AgentSnapshot,
    AgentStatus,
    OperationRecord,
    TerminalLease,
)

logger = logging.getLogger(__name__)

_UNSET = object()   # sentinel distinguishing "pid not supplied" from pid=None


class AgentRegistry:
    def __init__(self, storage_path: str) -> None:
        self._path = Path(storage_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._db_path = str(self._path / "registry.db")
        self._lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id        TEXT PRIMARY KEY,
                    display_name    TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'PROVISIONED',
                    desired_status  TEXT NOT NULL DEFAULT 'stopped',
                    config_path     TEXT NOT NULL,
                    data_dir        TEXT NOT NULL,
                    terminal_path   TEXT,
                    mt5_login       INTEGER,
                    mt5_server      TEXT,
                    monitoring_port INTEGER NOT NULL,
                    symbols_json    TEXT NOT NULL DEFAULT '[]',
                    created_at      INTEGER NOT NULL,
                    updated_at      INTEGER NOT NULL,
                    last_seen_at    INTEGER,
                    pid             INTEGER,
                    crash_count     INTEGER NOT NULL DEFAULT 0,
                    last_crash_at   INTEGER,
                    error_message   TEXT
                );

                CREATE TABLE IF NOT EXISTS terminal_leases (
                    terminal_path   TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    leased_at       INTEGER NOT NULL,
                    pid             INTEGER
                );

                CREATE TABLE IF NOT EXISTS operations (
                    op_id           TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    op_type         TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    created_at      INTEGER NOT NULL,
                    completed_at    INTEGER,
                    error           TEXT
                );

                CREATE TABLE IF NOT EXISTS manager_events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    event           TEXT NOT NULL,
                    agent_id        TEXT,
                    payload_json    TEXT NOT NULL,
                    created_at      INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_secrets (
                    agent_id    TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    updated_at  INTEGER NOT NULL,
                    PRIMARY KEY (agent_id, key)
                );

                CREATE TABLE IF NOT EXISTS device_state (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL
                );
            """)
        logger.info("AgentRegistry initialised at %s", self._db_path)

    # ── Agent CRUD ────────────────────────────────────────────────────────

    def upsert_agent(self, reg: AgentRegistration) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO agents
                    (agent_id, display_name, status, desired_status, config_path,
                     data_dir, terminal_path, mt5_login, mt5_server, monitoring_port,
                     symbols_json, created_at, updated_at, last_seen_at, pid,
                     crash_count, last_crash_at, error_message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name    = excluded.display_name,
                    status          = excluded.status,
                    desired_status  = excluded.desired_status,
                    config_path     = excluded.config_path,
                    data_dir        = excluded.data_dir,
                    terminal_path   = excluded.terminal_path,
                    mt5_login       = excluded.mt5_login,
                    mt5_server      = excluded.mt5_server,
                    monitoring_port = excluded.monitoring_port,
                    symbols_json    = excluded.symbols_json,
                    updated_at      = excluded.updated_at,
                    last_seen_at    = excluded.last_seen_at,
                    pid             = excluded.pid,
                    crash_count     = excluded.crash_count,
                    last_crash_at   = excluded.last_crash_at,
                    error_message   = excluded.error_message
            """, (
                reg.agent_id, reg.display_name, reg.status.value,
                reg.desired_status, reg.config_path, reg.data_dir,
                reg.terminal_path, reg.mt5_login, reg.mt5_server,
                reg.monitoring_port, json.dumps(reg.symbols),
                reg.created_at, reg.updated_at, reg.last_seen_at,
                reg.pid, reg.crash_count, reg.last_crash_at,
                reg.error_message,
            ))

    def get_agent(self, agent_id: str) -> AgentRegistration | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._row_to_agent(row) if row else None

    def list_agents(self) -> list[AgentRegistration]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY created_at"
            ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def set_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        error: str | None = None,
        pid: int | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        now = _now_ms()
        with self._connect() as conn:
            if pid is _UNSET:
                conn.execute(
                    "UPDATE agents SET status=?, error_message=?, updated_at=? WHERE agent_id=?",
                    (status.value, error, now, agent_id),
                )
            else:
                conn.execute(
                    "UPDATE agents SET status=?, error_message=?, pid=?, updated_at=? WHERE agent_id=?",
                    (status.value, error, pid, now, agent_id),
                )

    def set_desired_status(self, agent_id: str, desired: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET desired_status=?, updated_at=? WHERE agent_id=?",
                (desired, _now_ms(), agent_id),
            )

    def touch_last_seen(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET last_seen_at=? WHERE agent_id=?",
                (_now_ms(), agent_id),
            )

    def increment_crash_count(self, agent_id: str) -> int:
        now = _now_ms()
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET crash_count=crash_count+1, last_crash_at=?, updated_at=? WHERE agent_id=?",
                (now, now, agent_id),
            )
            row = conn.execute(
                "SELECT crash_count FROM agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return row[0] if row else 0

    def reset_crash_count(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET crash_count=0, last_crash_at=NULL, updated_at=? WHERE agent_id=?",
                (_now_ms(), agent_id),
            )

    # ── Terminal leases ───────────────────────────────────────────────────

    def acquire_terminal_lease(
        self, terminal_path: str, agent_id: str, pid: int | None = None
    ) -> bool:
        """Atomic: returns True if lease acquired, False if already held."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO terminal_leases (terminal_path, agent_id, leased_at, pid) VALUES (?,?,?,?)",
                    (terminal_path, agent_id, _now_ms(), pid),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_terminal_lease(self, terminal_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM terminal_leases WHERE terminal_path=?", (terminal_path,)
            )

    def release_agent_leases(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM terminal_leases WHERE agent_id=?", (agent_id,)
            )

    def get_terminal_lease(self, terminal_path: str) -> TerminalLease | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_path=?", (terminal_path,)
            ).fetchone()
        if not row:
            return None
        return TerminalLease(
            terminal_path=row["terminal_path"],
            agent_id=row["agent_id"],
            leased_at=row["leased_at"],
            pid=row["pid"],
        )

    def list_terminal_leases(self) -> list[TerminalLease]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM terminal_leases").fetchall()
        return [
            TerminalLease(
                terminal_path=r["terminal_path"],
                agent_id=r["agent_id"],
                leased_at=r["leased_at"],
                pid=r["pid"],
            )
            for r in rows
        ]

    # ── Operations ────────────────────────────────────────────────────────

    def upsert_operation(self, op: OperationRecord) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO operations (op_id, agent_id, op_type, status, created_at, completed_at, error)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(op_id) DO UPDATE SET
                    status       = excluded.status,
                    completed_at = excluded.completed_at,
                    error        = excluded.error
            """, (
                op.op_id, op.agent_id, op.op_type, op.status,
                op.created_at, op.completed_at, op.error,
            ))

    def get_operation(self, op_id: str) -> OperationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE op_id=?", (op_id,)
            ).fetchone()
        if not row:
            return None
        return OperationRecord(
            op_id=row["op_id"],
            agent_id=row["agent_id"],
            op_type=row["op_type"],
            status=row["status"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            error=row["error"],
        )

    # ── Events ────────────────────────────────────────────────────────────

    def emit_event(self, event: str, agent_id: str | None, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO manager_events (event, agent_id, payload_json, created_at) VALUES (?,?,?,?)",
                (event, agent_id, json.dumps(payload), _now_ms()),
            )

    # ── Device state (KV) ─────────────────────────────────────────────────

    def save_device_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO device_state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def load_device_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM device_state WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    # ── Internal ──────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> AgentRegistration:
        return AgentRegistration(
            agent_id=row["agent_id"],
            display_name=row["display_name"],
            status=AgentStatus(row["status"]),
            desired_status=row["desired_status"],
            config_path=row["config_path"],
            data_dir=row["data_dir"],
            terminal_path=row["terminal_path"],
            mt5_login=row["mt5_login"],
            mt5_server=row["mt5_server"],
            monitoring_port=row["monitoring_port"],
            symbols=json.loads(row["symbols_json"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"],
            pid=row["pid"],
            crash_count=row["crash_count"],
            last_crash_at=row["last_crash_at"],
            error_message=row["error_message"],
        )


def _now_ms() -> int:
    return int(time.time() * 1000)
