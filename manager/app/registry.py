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
import uuid
from pathlib import Path

from manager.app.models import (
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

                CREATE TABLE IF NOT EXISTS agent_allocations (
                    agent_id        TEXT PRIMARY KEY,
                    monitoring_port INTEGER NOT NULL UNIQUE,
                    allocated_at    INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_crashes (
                    agent_id        TEXT NOT NULL,
                    crashed_at      INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_crashes
                    ON agent_crashes(agent_id, crashed_at);

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

                CREATE TABLE IF NOT EXISTS config_revisions (
                    agent_id       TEXT NOT NULL,
                    revision       INTEGER NOT NULL,
                    config_json    TEXT NOT NULL,
                    checksum       TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    error          TEXT,
                    created_at     INTEGER NOT NULL,
                    activated_at   INTEGER,
                    PRIMARY KEY (agent_id, revision)
                );

                CREATE TABLE IF NOT EXISTS processed_worker_events (
                    event_id       TEXT PRIMARY KEY,
                    agent_id       TEXT NOT NULL,
                    processed_at   INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS command_outcomes (
                    command_id     TEXT PRIMARY KEY,
                    agent_id       TEXT NOT NULL,
                    command_type   TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    error          TEXT,
                    created_at     INTEGER NOT NULL,
                    completed_at   INTEGER
                );

                CREATE TABLE IF NOT EXISTS signal_deliveries (
                    signal_id      TEXT NOT NULL,
                    agent_id       TEXT NOT NULL,
                    payload_json   TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    command_id     TEXT,
                    attempts       INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    expires_at     INTEGER NOT NULL,
                    error          TEXT,
                    updated_at     INTEGER NOT NULL,
                    PRIMARY KEY (signal_id, agent_id)
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

    def delete_agent(self, agent_id: str) -> None:
        """Delete an agent registration after its process and leases are gone."""
        with self._connect() as conn:
            conn.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
            conn.execute("DELETE FROM agent_allocations WHERE agent_id=?", (agent_id,))

    def allocate_agent_identity(
        self, base_port: int = 8081, max_port: int = 8999
    ) -> tuple[str, int]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            agent_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT agent_id FROM agents UNION SELECT agent_id FROM agent_allocations"
                )
            }
            ports = {
                int(row[0])
                for row in conn.execute(
                    """SELECT monitoring_port FROM agents
                       UNION SELECT monitoring_port FROM agent_allocations"""
                )
            }
            agent_id = next(
                (f"agent-{i}" for i in range(1000) if f"agent-{i}" not in agent_ids),
                f"agent-{uuid.uuid4().hex[:8]}",
            )
            port = next((p for p in range(base_port, max_port + 1) if p not in ports), None)
            if port is None:
                raise RuntimeError("No monitoring port available")
            conn.execute(
                "INSERT INTO agent_allocations VALUES (?,?,?)",
                (agent_id, port, _now_ms()),
            )
        return agent_id, port

    def release_agent_allocation(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_allocations WHERE agent_id=?", (agent_id,))

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

    def increment_crash_count(self, agent_id: str, window_ms: int = 300_000) -> int:
        now = _now_ms()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_crashes VALUES (?,?)",
                (agent_id, now),
            )
            conn.execute(
                "DELETE FROM agent_crashes WHERE crashed_at < ?",
                (now - window_ms,),
            )
            count = int(conn.execute(
                "SELECT COUNT(*) FROM agent_crashes WHERE agent_id=?",
                (agent_id,),
            ).fetchone()[0])
            conn.execute(
                "UPDATE agents SET crash_count=?, last_crash_at=?, updated_at=? WHERE agent_id=?",
                (count, now, now, agent_id),
            )
        return count

    def reset_crash_count(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET crash_count=0, last_crash_at=NULL, updated_at=? WHERE agent_id=?",
                (_now_ms(), agent_id),
            )
            conn.execute("DELETE FROM agent_crashes WHERE agent_id=?", (agent_id,))

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

    def set_agent_lease_pid(self, agent_id: str, pid: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE terminal_leases SET pid=? WHERE agent_id=?",
                (pid, agent_id),
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

    def recover_interrupted_operations(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE operations SET status='failed', completed_at=?, error=?
                   WHERE status IN ('pending','running')""",
                (_now_ms(), "Manager restarted before operation completed"),
            )
        return cursor.rowcount

    def enforce_retention(self, older_than_ms: int = 30 * 86_400_000) -> dict[str, int]:
        cutoff = _now_ms() - older_than_ms
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            for table, column in (
                ("manager_events", "created_at"),
                ("operations", "completed_at"),
                ("processed_worker_events", "processed_at"),
                ("command_outcomes", "completed_at"),
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {column} IS NOT NULL AND {column} < ?",
                    (cutoff,),
                )
                deleted[table] = cursor.rowcount
            cursor = conn.execute(
                """DELETE FROM signal_deliveries
                   WHERE status NOT IN ('pending','sent') AND updated_at < ?""",
                (cutoff,),
            )
            deleted["signal_deliveries"] = cursor.rowcount
        return deleted

    def health_check(self) -> bool:
        with self._connect() as conn:
            return int(conn.execute("SELECT 1").fetchone()[0]) == 1

    # ── Events ────────────────────────────────────────────────────────────

    def emit_event(self, event: str, agent_id: str | None, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO manager_events (event, agent_id, payload_json, created_at) VALUES (?,?,?,?)",
                (event, agent_id, json.dumps(payload), _now_ms()),
            )

    def create_config_revision(
        self, agent_id: str, config: dict, checksum: str,
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM config_revisions WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
            revision = int(row[0])
            conn.execute(
                """INSERT INTO config_revisions
                   (agent_id, revision, config_json, checksum, status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (agent_id, revision, json.dumps(config), checksum, "desired", _now_ms()),
            )
        return revision

    def latest_desired_config_revision(self, agent_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT revision FROM config_revisions
                   WHERE agent_id=? AND status='desired'
                   ORDER BY revision DESC LIMIT 1""",
                (agent_id,),
            ).fetchone()
        return int(row[0]) if row else None

    def current_config_revision(self, agent_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT revision FROM config_revisions
                   WHERE agent_id=? AND status IN ('desired', 'active')
                   ORDER BY CASE status WHEN 'desired' THEN 0 ELSE 1 END, revision DESC
                   LIMIT 1""",
                (agent_id,),
            ).fetchone()
        return int(row[0]) if row else 1

    def activate_config_revision(self, agent_id: str, revision: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE config_revisions SET status='superseded' WHERE agent_id=? AND status='active'",
                (agent_id,),
            )
            conn.execute(
                """UPDATE config_revisions SET status='active', activated_at=?
                   WHERE agent_id=? AND revision=?""",
                (_now_ms(), agent_id, revision),
            )

    def worker_event_processed(self, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_worker_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return row is not None

    def record_worker_event(self, event_id: str, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_worker_events VALUES (?,?,?)",
                (event_id, agent_id, _now_ms()),
            )

    def record_command(
        self, command_id: str, agent_id: str, command_type: str, status: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO command_outcomes
                   (command_id, agent_id, command_type, status, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(command_id) DO UPDATE SET status=excluded.status""",
                (command_id, agent_id, command_type, status, _now_ms()),
            )

    def complete_command(
        self, command_id: str, status: str, error: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE command_outcomes
                   SET status=?, error=?, completed_at=? WHERE command_id=?""",
                (status, error, _now_ms(), command_id),
            )

    def get_command_outcome(self, command_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM command_outcomes WHERE command_id=?", (command_id,)
            ).fetchone()
        return dict(row) if row else None

    def queue_signal_delivery(
        self, signal_id: str, agent_id: str, payload: dict, expires_at: int
    ) -> None:
        now = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO signal_deliveries
                   (signal_id, agent_id, payload_json, status, next_attempt_at,
                    expires_at, updated_at)
                   VALUES (?,?,?,'pending',?,?,?)""",
                (signal_id, agent_id, json.dumps(payload), now, expires_at, now),
            )

    def record_signal_outcome(
        self, signal_id: str, agent_id: str, payload: dict, status: str, error: str = ""
    ) -> None:
        now = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO signal_deliveries
                   (signal_id, agent_id, payload_json, status, next_attempt_at,
                    expires_at, error, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(signal_id, agent_id) DO UPDATE SET
                     status=excluded.status, error=excluded.error,
                     updated_at=excluded.updated_at""",
                (signal_id, agent_id, json.dumps(payload), status, now, now, error, now),
            )

    def list_due_signal_deliveries(self, now_ms: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM signal_deliveries
                   WHERE status IN ('pending','sent') AND next_attempt_at <= ?
                   ORDER BY next_attempt_at""",
                (now_ms,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_signal_delivery(
        self,
        signal_id: str,
        agent_id: str,
        *,
        status: str,
        command_id: str | None = None,
        attempts: int | None = None,
        next_attempt_at: int | None = None,
        error: str | None = None,
    ) -> None:
        fields = ["status=?", "updated_at=?"]
        values: list[object] = [status, _now_ms()]
        for name, value in (
            ("command_id", command_id),
            ("attempts", attempts),
            ("next_attempt_at", next_attempt_at),
            ("error", error),
        ):
            if value is not None:
                fields.append(f"{name}=?")
                values.append(value)
        values.extend([signal_id, agent_id])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE signal_deliveries SET {', '.join(fields)} "
                "WHERE signal_id=? AND agent_id=?",
                values,
            )

    def get_signal_delivery(self, signal_id: str, agent_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM signal_deliveries WHERE signal_id=? AND agent_id=?",
                (signal_id, agent_id),
            ).fetchone()
        return dict(row) if row else None

    # ── Device identity ───────────────────────────────────────────────────

    def get_or_create_device_id(self) -> str:
        """Return a stable UUID for this installation, generating one on first call."""
        existing = self.load_device_state("device_id")
        if existing:
            return existing
        import uuid as _uuid
        new_id = str(_uuid.uuid4())
        self.save_device_state("device_id", new_id)
        logger.info("Generated new device_id: %s", new_id)
        return new_id

    def get_or_create_device_name(self) -> str:
        """Return a stable human-readable device name (hostname), captured once."""
        existing = self.load_device_state("device_name")
        if existing:
            return existing
        import socket
        name = socket.gethostname()
        self.save_device_state("device_name", name)
        return name

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
