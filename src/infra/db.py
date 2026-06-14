"""
SQLite persistence layer.

Single file database at <storage_path>/engine.db

Tables:
    trades           — full trade lifecycle, open and closed
    signals          — every inbound signal received
    metrics_counters — persisted counter values (survive restarts)
    metrics_gauges   — persisted gauge values (survive restarts)
    event_outbox     — reliable outbound event delivery
    device_state     — persistent KV store (device credential, etc.)

All writes are atomic. The DB is created and migrated on init().
Thread-safe — SQLite WAL mode + per-call connections via check_same_thread=False.

4.7 — Encrypted device state
    On Windows, sensitive values stored via save_device_state() are encrypted
    with DPAPI (CryptProtectData / CryptUnprotectData) using ctypes so that no
    extra dependencies are required.  The credential is tied to the current
    Windows user account and cannot be decrypted on another machine or by
    another user.  On non-Windows platforms (dev / Linux containers) values are
    stored as plaintext with a warning.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DPAPI helpers (Windows only)
# ---------------------------------------------------------------------------

_DPAPI_AVAILABLE = sys.platform == "win32"


def _dpapi_encrypt(plaintext: str) -> str:
    """
    Encrypt *plaintext* with Windows DPAPI.
    Returns a base64-encoded ciphertext string suitable for SQLite storage.
    Raises RuntimeError if DPAPI is unavailable or fails.
    """
    if not _DPAPI_AVAILABLE:
        raise RuntimeError("DPAPI is only available on Windows")

    data = plaintext.encode("utf-8")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    input_blob  = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    output_blob = DATA_BLOB()

    # CRYPTPROTECT_UI_FORBIDDEN = 0x01 — never show a dialog
    result = ctypes.windll.crypt32.CryptProtectData(  # type: ignore[attr-defined]
        ctypes.byref(input_blob),
        None,           # description (optional)
        None,           # optional entropy
        None,           # reserved
        None,           # prompt struct
        0x01,           # flags: CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    )

    if not result:
        raise RuntimeError(f"CryptProtectData failed (error {ctypes.GetLastError()})")

    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)  # type: ignore[attr-defined]


def _dpapi_decrypt(ciphertext_b64: str) -> str:
    """
    Decrypt a base64-encoded DPAPI ciphertext.
    Returns the original plaintext string.
    Raises RuntimeError if DPAPI is unavailable, the blob is corrupt, or
    the calling user/machine doesn't match the one that encrypted it.
    """
    if not _DPAPI_AVAILABLE:
        raise RuntimeError("DPAPI is only available on Windows")

    raw = base64.b64decode(ciphertext_b64)

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    input_blob  = DATA_BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_char)))
    output_blob = DATA_BLOB()

    result = ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
        ctypes.byref(input_blob),
        None,   # description out (ignored)
        None,   # optional entropy
        None,   # reserved
        None,   # prompt struct
        0x01,   # flags: CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    )

    if not result:
        raise RuntimeError(f"CryptUnprotectData failed (error {ctypes.GetLastError()})")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)  # type: ignore[attr-defined]


# Prefix used to distinguish encrypted blobs from legacy plaintext values
_DPAPI_PREFIX = "DPAPI:"


class Database:
    def __init__(self, storage_path: str) -> None:
        self._path = str(Path(storage_path) / "engine.db")
        self._lock = threading.Lock()

    def init(self) -> None:
        """Create tables if they don't exist. Safe to call on every startup."""
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS trades (
                    id              TEXT PRIMARY KEY,
                    signal_id       TEXT,
                    symbol          TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    entry_ticket    INTEGER,
                    entry_price     REAL,
                    entry_lots      REAL,
                    current_lots    REAL,
                    stop_loss       REAL,
                    tp1             REAL,
                    tp2             REAL,
                    tp1_hit         INTEGER DEFAULT 0,
                    tp1_hit_at      INTEGER,
                    tp2_hit         INTEGER DEFAULT 0,
                    tp2_hit_at      INTEGER,
                    sl_hit          INTEGER DEFAULT 0,
                    sl_hit_at       INTEGER,
                    close_reason    TEXT,
                    close_price     REAL,
                    realized_pnl    REAL,
                    realized_rr     REAL,
                    plan_json       TEXT,
                    opened_at       INTEGER,
                    closed_at       INTEGER,
                    created_at      INTEGER,
                    updated_at      INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_trades_status
                    ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol
                    ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_signal_id
                    ON trades(signal_id);
                CREATE INDEX IF NOT EXISTS idx_trades_ticket
                    ON trades(entry_ticket);

                CREATE TABLE IF NOT EXISTS signals (
                    id              TEXT PRIMARY KEY,
                    symbol          TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    entry_price     REAL,
                    stop_loss       REAL,
                    tp1             REAL,
                    tp2             REAL,
                    risk_reward     REAL,
                    risk_pips       REAL,
                    pattern         TEXT,
                    wick_ratio      REAL,
                    raw_json        TEXT,
                    received_at     INTEGER,
                    triggered_at    INTEGER,
                    outcome         TEXT,
                    trade_id        TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_signals_symbol
                    ON signals(symbol);
                CREATE INDEX IF NOT EXISTS idx_signals_status
                    ON signals(status);

                CREATE TABLE IF NOT EXISTS metrics_counters (
                    name        TEXT PRIMARY KEY,
                    value       INTEGER NOT NULL DEFAULT 0,
                    updated_at  INTEGER
                );

                CREATE TABLE IF NOT EXISTS metrics_gauges (
                    name        TEXT PRIMARY KEY,
                    value       REAL NOT NULL DEFAULT 0,
                    updated_at  INTEGER
                );

                CREATE TABLE IF NOT EXISTS event_outbox (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event       TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    sent        INTEGER NOT NULL DEFAULT 0,
                    created_at  INTEGER NOT NULL,
                    sent_at     INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_sent
                    ON event_outbox(sent);

                -- 1.16 — Persistent key/value store for device state
                -- (e.g. device_credential issued by the gateway)
                CREATE TABLE IF NOT EXISTS device_state (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            """
            )
        logger.info("Database initialised", extra={"path": self._path})

    # ── Trades ────────────────────────────────────────────────────────────

    def upsert_trade(self, trade) -> None:
        """Insert or update a trade record."""
        from src.utils.time import now_ms

        plan_json = None
        if trade.plan:
            try:
                plan_json = json.dumps(
                    trade.plan.to_dict()
                    if hasattr(trade.plan, "to_dict")
                    else {
                        "signalId": trade.plan.signal_id,
                        "lotSize": trade.plan.lot_size,
                        "riskAmount": trade.plan.risk_amount,
                        "riskPercent": trade.plan.risk_percent,
                        "riskRewardRatio": trade.plan.risk_reward_ratio,
                        "riskMultiplier": getattr(trade.plan, "risk_multiplier", 1.0),
                        # Originals — the trades.stop_loss column is mutated to
                        # breakeven after TP1, so R rebuilds need these.
                        "entryPrice": trade.plan.entry_price,
                        "stopLoss": trade.plan.stop_loss,
                    }
                )
            except Exception:
                pass

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    id, signal_id, symbol, side, status,
                    entry_ticket, entry_price, entry_lots, current_lots,
                    stop_loss, tp1, tp2,
                    tp1_hit, tp1_hit_at, tp2_hit, tp2_hit_at,
                    sl_hit, sl_hit_at,
                    close_reason, close_price, realized_pnl, realized_rr,
                    plan_json, opened_at, closed_at, created_at, updated_at
                ) VALUES (
                    :id, :signal_id, :symbol, :side, :status,
                    :entry_ticket, :entry_price, :entry_lots, :current_lots,
                    :stop_loss, :tp1, :tp2,
                    :tp1_hit, :tp1_hit_at, :tp2_hit, :tp2_hit_at,
                    :sl_hit, :sl_hit_at,
                    :close_reason, :close_price, :realized_pnl, :realized_rr,
                    :plan_json, :opened_at, :closed_at, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    status       = excluded.status,
                    current_lots = excluded.current_lots,
                    stop_loss    = excluded.stop_loss,
                    tp1_hit      = excluded.tp1_hit,
                    tp1_hit_at   = excluded.tp1_hit_at,
                    tp2_hit      = excluded.tp2_hit,
                    tp2_hit_at   = excluded.tp2_hit_at,
                    sl_hit       = excluded.sl_hit,
                    sl_hit_at    = excluded.sl_hit_at,
                    close_reason = excluded.close_reason,
                    close_price  = excluded.close_price,
                    realized_pnl = excluded.realized_pnl,
                    realized_rr  = excluded.realized_rr,
                    closed_at    = excluded.closed_at,
                    updated_at   = excluded.updated_at
            """,
                {
                    "id": trade.id,
                    "signal_id": trade.signal_id,
                    "symbol": trade.symbol,
                    "side": trade.side.value,
                    "status": trade.status.value,
                    "entry_ticket": trade.entry_ticket,
                    "entry_price": trade.entry_price,
                    "entry_lots": trade.entry_lots,
                    "current_lots": trade.current_lots,
                    "stop_loss": trade.stop_loss,
                    "tp1": trade.tp1,
                    "tp2": trade.tp2,
                    "tp1_hit": int(trade.tp1_hit or False),
                    "tp1_hit_at": trade.tp1_hit_at,
                    "tp2_hit": int(trade.tp2_hit or False),
                    "tp2_hit_at": trade.tp2_hit_at,
                    "sl_hit": int(trade.sl_hit or False),
                    "sl_hit_at": trade.sl_hit_at,
                    "close_reason": (
                        trade.close_reason.value if trade.close_reason else None
                    ),
                    "close_price": trade.close_price,
                    "realized_pnl": trade.realized_pnl,
                    "realized_rr": trade.realized_rr,
                    "plan_json": plan_json,
                    "opened_at": trade.opened_at,
                    "closed_at": trade.closed_at,
                    "created_at": trade.created_at,
                    "updated_at": trade.updated_at or now_ms(),
                },
            )

    def load_open_trades_raw(self) -> list[dict]:
        """Return all open/partially-closed trade rows as dicts."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT * FROM trades
                WHERE status IN ('OPEN', 'PARTIALLY_CLOSED')
                ORDER BY opened_at ASC
            """
            )
            return [dict(row) for row in cur.fetchall()]

    def load_all_trades_raw(self) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM trades ORDER BY opened_at DESC")
            return [dict(row) for row in cur.fetchall()]

    def load_closed_trades_since(self, ts_ms: int) -> list[dict]:
        """Closed trades with a realized R outcome, oldest first.

        Used to rebuild the equity-throttle rolling window after a restart.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT * FROM trades
                WHERE status = 'CLOSED'
                  AND realized_rr IS NOT NULL
                  AND closed_at IS NOT NULL
                  AND closed_at >= ?
                ORDER BY closed_at ASC
            """,
                (int(ts_ms),),
            )
            return [dict(row) for row in cur.fetchall()]

    # ── Signals ───────────────────────────────────────────────────────────

    def upsert_signal(
        self,
        signal,
        received_at: int,
        status: str,
        outcome: Optional[str] = None,
        trade_id: Optional[str] = None,
    ) -> None:
        rejection_candle = getattr(signal, "rejection_candle", None)
        pattern = (
            getattr(rejection_candle, "pattern", None) if rejection_candle else None
        )
        wick_ratio = (
            getattr(rejection_candle, "wick_ratio", None) if rejection_candle else None
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO signals (
                    id, symbol, direction, status,
                    entry_price, stop_loss, tp1, tp2,
                    risk_reward, risk_pips, pattern, wick_ratio,
                    raw_json, received_at, triggered_at, outcome, trade_id
                ) VALUES (
                    :id, :symbol, :direction, :status,
                    :entry_price, :stop_loss, :tp1, :tp2,
                    :risk_reward, :risk_pips, :pattern, :wick_ratio,
                    :raw_json, :received_at, :triggered_at, :outcome, :trade_id
                )
                ON CONFLICT(id) DO UPDATE SET
                    status       = excluded.status,
                    triggered_at = excluded.triggered_at,
                    outcome      = excluded.outcome,
                    trade_id     = excluded.trade_id
            """,
                {
                    "id": signal.id,
                    "symbol": signal.resolved_symbol,
                    "direction": signal.direction.value,
                    "status": status,
                    "entry_price": getattr(signal, "entry_price", None),
                    "stop_loss": getattr(signal, "stop_loss", None),
                    "tp1": getattr(signal, "tp1", None),
                    "tp2": getattr(signal, "tp2", None),
                    "risk_reward": getattr(signal, "risk_reward_ratio", None),
                    "risk_pips": getattr(signal, "risk_pips", None),
                    "pattern": pattern,
                    "wick_ratio": wick_ratio,
                    "raw_json": None,
                    "received_at": received_at,
                    "triggered_at": getattr(signal, "triggered_at", None),
                    "outcome": outcome,
                    "trade_id": trade_id,
                },
            )

    def load_signals_raw(self, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM signals ORDER BY received_at DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    # ── Metrics ───────────────────────────────────────────────────────────

    def save_metrics(self, counters: dict[str, int], gauges: dict[str, float]) -> None:
        from src.utils.time import now_ms

        ts = now_ms()
        with self._connect() as conn:
            for name, value in counters.items():
                conn.execute(
                    """
                    INSERT INTO metrics_counters (name, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                    (name, value, ts),
                )
            for name, value in gauges.items():
                conn.execute(
                    """
                    INSERT INTO metrics_gauges (name, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                    (name, value, ts),
                )

    def load_metrics(self) -> tuple[dict[str, int], dict[str, float]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            counters = {
                row["name"]: row["value"]
                for row in conn.execute(
                    "SELECT name, value FROM metrics_counters"
                ).fetchall()
            }
            gauges = {
                row["name"]: row["value"]
                for row in conn.execute(
                    "SELECT name, value FROM metrics_gauges"
                ).fetchall()
            }
        return counters, gauges

    # ── Event outbox (2.11 — reliable event delivery) ─────────────────────

    def outbox_enqueue(self, event: str, payload_json: str) -> int:
        """
        Persist an event to the outbox before transmission.
        Returns the auto-incremented row ID so the caller can later mark it sent.
        """
        from src.utils.time import now_ms

        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO event_outbox (event, payload_json, sent, created_at) VALUES (?, ?, 0, ?)",
                (event, payload_json, now_ms()),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def outbox_mark_sent(self, row_id: int) -> None:
        """Mark an outbox row as successfully delivered."""
        from src.utils.time import now_ms

        with self._connect() as conn:
            conn.execute(
                "UPDATE event_outbox SET sent=1, sent_at=? WHERE id=?",
                (now_ms(), row_id),
            )

    def outbox_load_pending(self, max_age_ms: int = 3_600_000) -> list[tuple[int, str, str]]:
        """
        Return all unsent outbox rows newer than ``max_age_ms`` milliseconds,
        oldest first.  Returns list of (row_id, event, payload_json).
        """
        from src.utils.time import now_ms

        cutoff = now_ms() - max_age_ms
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, event, payload_json FROM event_outbox "
                "WHERE sent=0 AND created_at >= ? ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
        return [(r["id"], r["event"], r["payload_json"]) for r in rows]

    def outbox_evict_sent(self, older_than_ms: int = 86_400_000) -> int:
        """Delete sent outbox rows older than ``older_than_ms`` ms. Returns count deleted."""
        from src.utils.time import now_ms

        cutoff = now_ms() - older_than_ms
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM event_outbox WHERE sent=1 AND sent_at < ?", (cutoff,)
            )
            return cur.rowcount

    # ── Device state (1.16 — credential storage, 4.7 — DPAPI encryption) ────

    def save_device_state(self, key: str, value: str) -> None:
        """
        Upsert a persistent device state entry.

        On Windows the value is encrypted with DPAPI before writing so that it
        cannot be read by other users or on other machines.  On non-Windows
        platforms a plaintext fallback is used with a one-time warning.

        Used primarily to store the device credential issued by the gateway so
        it survives engine restarts and can be presented in ``engine.hello``
        for fast-path reconnects.
        """
        from src.utils.time import now_ms

        stored_value: str
        if _DPAPI_AVAILABLE:
            try:
                stored_value = _DPAPI_PREFIX + _dpapi_encrypt(value)
            except Exception as exc:
                logger.warning("DPAPI encrypt failed — storing plaintext (%s)", exc)
                stored_value = value
        else:
            if not getattr(self, "_dpapi_warned", False):
                logger.warning(
                    "DPAPI not available on this platform — device credential stored in plaintext. "
                    "This is acceptable in dev/Linux environments but not recommended in production."
                )
                self._dpapi_warned = True  # type: ignore[attr-defined]
            stored_value = value

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO device_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                  SET value = excluded.value,
                      updated_at = excluded.updated_at
                """,
                (key, stored_value, now_ms()),
            )

    def load_device_state(self, key: str) -> Optional[str]:
        """
        Load a persistent device state entry.  Returns None if not set.

        Automatically detects DPAPI-encrypted values (``DPAPI:`` prefix) and
        decrypts them transparently.  Falls back to returning the raw value if
        decryption fails (e.g. credential was stored on a different machine).
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT value FROM device_state WHERE key = ?", (key,)
            ).fetchone()

        if not row:
            return None

        raw = str(row["value"])

        if raw.startswith(_DPAPI_PREFIX):
            if not _DPAPI_AVAILABLE:
                logger.warning(
                    "DPAPI-encrypted device state found but DPAPI is not available — "
                    "cannot decrypt.  The credential will be discarded."
                )
                return None
            try:
                return _dpapi_decrypt(raw[len(_DPAPI_PREFIX):])
            except Exception as exc:
                logger.warning(
                    "DPAPI decrypt failed for key %r — discarding stored value (%s). "
                    "The engine will re-activate on next connection.",
                    key, exc,
                )
                return None

        return raw

    # ── Helpers ───────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
