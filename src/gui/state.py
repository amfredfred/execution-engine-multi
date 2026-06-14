"""
src/gui/state.py — Central application state for the Apex Quantel GUI.

Single source of truth.  Pages subscribe to named events; mutations call
the setter helpers so observers are always notified.

Events emitted by AppState:
    "engine"        — EngineLifecycle changed
    "account"       — balance / equity / daily-PnL changed
    "trades"        — open-positions list changed
    "setup"         — setup-completeness changed
    "mt5_error"     — MT5 error received
    "gateway_error" — gateway / WS error received
    "config"        — config reloaded / saved
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


# ── Stable value helper ───────────────────────────────────────────────────────

@dataclass
class StableValue:
    """
    Wraps a polled value so transient failures don't immediately flip the UI.

    Rules:
    - A "good" value is accepted immediately.
    - A degrading value (None / unknown) must repeat `failure_threshold` times
      before being accepted, preventing single-poll flips.
    """
    value: Any
    last_changed_at: float = field(default_factory=time.time)
    last_seen_at: float    = field(default_factory=time.time)
    _fail_streak: int      = field(default=0, repr=False)

    # Values that are considered "degrading"
    _UNKNOWN_SENTINELS: tuple = field(
        default=("unknown", None, False), repr=False, compare=False,
    )

    def update(self, new_val: Any, failure_threshold: int = 2) -> bool:
        """
        Update the tracked value.  Returns True if the stored value changed
        (caller should update UI).  Returns False if the new reading was
        absorbed as noise.
        """
        now = time.time()
        self.last_seen_at = now

        if new_val == self.value:
            self._fail_streak = 0
            return False

        is_degrading = new_val in self._UNKNOWN_SENTINELS
        if is_degrading:
            self._fail_streak += 1
            if self._fail_streak < failure_threshold:
                return False  # absorb transient dip, keep good value
        else:
            self._fail_streak = 0

        self.value = new_val
        self.last_changed_at = now
        return True


# ── Engine lifecycle ──────────────────────────────────────────────────────────

class EngineLifecycle(str, Enum):
    CHECKING              = "checking"
    NOT_CONFIGURED        = "not_configured"
    SERVICE_NOT_INSTALLED = "service_not_installed"
    INSTALLED_STOPPED     = "installed_stopped"
    STARTING              = "starting"
    RUNNING_CONNECTED     = "running_connected"
    RUNNING_NO_HEARTBEAT  = "running_no_heartbeat"
    STOPPING              = "stopping"
    FAILED                = "failed"
    UNKNOWN               = "unknown"

    # ── Human-readable strings ────────────────────────────────────────────────

    @property
    def label(self) -> str:
        return {
            "checking":              "Checking AQ Agent status…",
            "not_configured":        "Setup required",
            "service_not_installed": "AQ Agent is not installed",
            "installed_stopped":     "AQ Agent is installed but stopped",
            "starting":              "AQ Agent starting…",
            "running_connected":     "AQ Agent connected",
            "running_no_heartbeat":  "AQ Agent process is running but not responding yet",
            "stopping":              "AQ Agent stopping…",
            "failed":                "AQ Agent failed",
            "unknown":               "Status unknown",
        }.get(self.value, self.value)

    @property
    def description(self) -> str:
        return {
            "checking":
                "Checking whether AQ Agent is installed and running.",
            "not_configured":
                "Complete setup before starting AQ Agent.",
            "service_not_installed":
                "Install AQ Agent before Apex can trade automatically.",
            "installed_stopped":
                "Start AQ Agent to connect MetaTrader and the signal gateway.",
            "starting":
                "AQ Agent is starting up. This usually takes a few seconds.",
            "running_connected":
                "AQ Agent is running and responding.",
            "running_no_heartbeat":
                "AQ Agent process started, but the GUI has not received "
                "a heartbeat yet. It may still be initialising.",
            "stopping":
                "AQ Agent is shutting down.",
            "failed":
                "AQ Agent stopped unexpectedly. Check the Activity page for details.",
            "unknown":
                "AQ Agent status could not be determined.",
        }.get(self.value, "")

    # ── Derived booleans used by buttons ──────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self in (
            EngineLifecycle.RUNNING_CONNECTED,
            EngineLifecycle.RUNNING_NO_HEARTBEAT,
        )

    @property
    def is_busy(self) -> bool:
        return self in (EngineLifecycle.STARTING, EngineLifecycle.STOPPING)

    @property
    def can_start(self) -> bool:
        return self in (
            EngineLifecycle.INSTALLED_STOPPED,
            EngineLifecycle.FAILED,
        )

    @property
    def can_stop(self) -> bool:
        return self.is_running or self == EngineLifecycle.STARTING

    @property
    def can_restart(self) -> bool:
        return self not in (
            EngineLifecycle.CHECKING,
            EngineLifecycle.NOT_CONFIGURED,
            EngineLifecycle.SERVICE_NOT_INSTALLED,
            EngineLifecycle.UNKNOWN,
        )

    @property
    def needs_install(self) -> bool:
        return self == EngineLifecycle.SERVICE_NOT_INSTALLED

    @property
    def needs_setup(self) -> bool:
        return self == EngineLifecycle.NOT_CONFIGURED

    @property
    def is_checking(self) -> bool:
        return self == EngineLifecycle.CHECKING

    @property
    def color_key(self) -> str:
        """Maps to theme.Tone values."""
        if self == EngineLifecycle.RUNNING_CONNECTED:
            return "good"
        if self in (
            EngineLifecycle.STARTING,
            EngineLifecycle.STOPPING,
            EngineLifecycle.RUNNING_NO_HEARTBEAT,
        ):
            return "warn"
        if self == EngineLifecycle.FAILED:
            return "danger"
        return "normal"


# ── Sub-state dataclasses ─────────────────────────────────────────────────────

@dataclass
class AccountState:
    login:         Optional[str]   = None
    server:        Optional[str]   = None
    balance:       Optional[float] = None
    equity:        Optional[float] = None
    currency:      str             = ""
    daily_pnl:     Optional[float] = None
    open_trades:   int             = 0
    mt5_connected: bool            = False


@dataclass
class ReadinessIssue:
    """One item that must be resolved before the engine can trade."""
    key:          str
    title:        str            # Short plain-English summary
    detail:       str            # One-sentence elaboration
    is_blocking:  bool          = True
    action_label: Optional[str] = None
    action_page:  Optional[str] = None
    action_fn:    Optional[Callable] = field(default=None, repr=False)


# ── Central state object ──────────────────────────────────────────────────────

class AppState:
    """
    Owns all mutable state.  Pages call subscribe() to receive change
    notifications; mutations call emit() automatically.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[str, List[Callable]] = {}

        # Engine
        self._lifecycle:      EngineLifecycle = EngineLifecycle.CHECKING
        self._checking_since: float           = time.time()
        self._service_raw:    str             = "unknown"
        self._ws_alive:       bool            = False
        self._last_heartbeat: float           = 0.0
        self._is_paused:      bool            = False

        # Stable wrappers — transient failures don't immediately flip the UI
        self._stable_service_raw       = StableValue("unknown")
        self._stable_service_installed = StableValue(False)

        # Connections
        self._mt5_connected:     bool = False
        self._gateway_connected: bool = False

        # Account / market
        self.account:       AccountState = AccountState()
        self._trades:       list         = []
        self._risk_guards:  list         = []

        # Errors
        self._mt5_error:     Optional[str] = None
        self._gateway_error: Optional[str] = None
        self._engine_error:  Optional[str] = None

        # Setup
        self._setup_complete:   bool = False
        self._service_installed: bool = False

    # ── Pub/sub ───────────────────────────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable) -> None:
        self._subs.setdefault(event, []).append(callback)

    def emit(self, event: str, **kwargs: Any) -> None:
        for cb in list(self._subs.get(event, [])):
            try:
                cb(**kwargs)
            except Exception:
                pass

    # ── Engine state ──────────────────────────────────────────────────────────

    @property
    def lifecycle(self) -> EngineLifecycle:
        return self._lifecycle

    @property
    def last_heartbeat(self) -> float:
        return self._last_heartbeat

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def has_open_trades(self) -> bool:
        return len(self._trades) > 0

    @property
    def trades(self) -> list:
        return list(self._trades)

    @property
    def risk_guards(self) -> list:
        return list(self._risk_guards)

    @property
    def mt5_error(self) -> Optional[str]:
        return self._mt5_error

    @property
    def gateway_error(self) -> Optional[str]:
        return self._gateway_error

    @property
    def engine_error(self) -> Optional[str]:
        return self._engine_error

    @property
    def setup_complete(self) -> bool:
        return self._setup_complete

    @property
    def service_installed(self) -> bool:
        return self._service_installed

    # ── Setters ───────────────────────────────────────────────────────────────

    def update_service_status(
        self,
        service_status: str,
        setup_complete: bool,
        engine_error: Optional[str] = None,
    ) -> None:
        """Called from service poll thread — recomputes lifecycle."""
        from src.gui.service_controller import ServiceStatus

        old_was_running = self._lifecycle.is_running
        now_stopped     = service_status == ServiceStatus.STOPPED

        # Crash detection
        if old_was_running and now_stopped and engine_error is None:
            engine_error = "AQ Agent stopped unexpectedly."

        # Apply StableValue debounce — a single "unknown/bad" poll is ignored,
        # only a sustained change (2+ consecutive identical values) is accepted.
        # Good values (installed, running, stopped) are always accepted immediately.
        raw_changed       = self._stable_service_raw.update(service_status, failure_threshold=2)
        install_changed   = self._stable_service_installed.update(
            service_status != ServiceStatus.NOT_INSTALLED, failure_threshold=2,
        )

        self._service_raw       = self._stable_service_raw.value
        self._service_installed = self._stable_service_installed.value

        self._setup_complete = setup_complete
        self._engine_error   = engine_error

        self._recompute_lifecycle(engine_error)

    def on_ws_connected(self) -> None:
        self._ws_alive          = True
        self._last_heartbeat    = time.time()
        self._gateway_connected = True
        self._gateway_error     = None
        self._recompute_lifecycle()

    def on_ws_disconnected(self) -> None:
        self._ws_alive          = False
        self._gateway_connected = False
        self.account.mt5_connected = False
        self._recompute_lifecycle()
        self.emit("account")

    def on_heartbeat(self) -> None:
        self._last_heartbeat = time.time()
        self._ws_alive       = True
        # Upgrade from NO_HEARTBEAT to CONNECTED
        if self._lifecycle == EngineLifecycle.RUNNING_NO_HEARTBEAT:
            self._lifecycle = EngineLifecycle.RUNNING_CONNECTED
            self.emit("engine", lifecycle=self._lifecycle)

    def tick_heartbeat(self) -> None:
        """Call every second to detect stale heartbeats and CHECKING timeout."""
        now = time.time()

        # Safety: if still CHECKING after 5 s, something went wrong — show UNKNOWN
        if self._lifecycle == EngineLifecycle.CHECKING:
            if now - self._checking_since > 5.0:
                self._lifecycle = EngineLifecycle.UNKNOWN
                self.emit("engine", lifecycle=self._lifecycle)
            return

        # Heartbeat staleness: RUNNING_CONNECTED → RUNNING_NO_HEARTBEAT
        if self._ws_alive and self._last_heartbeat > 0:
            age = now - self._last_heartbeat
            if age > 30 and self._lifecycle == EngineLifecycle.RUNNING_CONNECTED:
                self._lifecycle = EngineLifecycle.RUNNING_NO_HEARTBEAT
                self.emit("engine", lifecycle=self._lifecycle)

    def apply_snapshot(self, snap: dict) -> None:
        m = snap.get("metrics", {})
        self.account.balance       = m.get("balance") or m.get("current_balance")
        self.account.equity        = m.get("equity")
        self.account.currency      = m.get("currency", "")
        self.account.daily_pnl     = m.get("daily_pnl")
        self.account.open_trades   = len(snap.get("trades", []))
        self.account.mt5_connected = bool(snap.get("connected", False))
        self._mt5_connected        = self.account.mt5_connected
        self._mt5_error            = None

        engine_data    = snap.get("engine", {})
        self._is_paused = bool(engine_data.get("is_paused", False))

        self._trades      = list(snap.get("trades", []))
        self._risk_guards = list(snap.get("riskGuards", []))

        self._recompute_lifecycle()
        self.emit("account")
        self.emit("trades")

    def apply_metrics(self, m: dict) -> None:
        self.account.balance   = m.get("balance") or m.get("current_balance")
        self.account.equity    = m.get("equity")
        self.account.currency  = m.get("currency", "")
        self.account.daily_pnl = m.get("daily_pnl")
        self.emit("account")

    def on_trade_event(self, event_type: str, payload: dict) -> None:
        if event_type == "trade.opened":
            tid = payload.get("id", "")
            if tid and not any(t.get("id") == tid for t in self._trades):
                self._trades.append(payload)
        elif event_type in ("trade.tp2_hit", "trade.sl_hit", "trade.closed"):
            tid = payload.get("trade_id", "")
            self._trades = [t for t in self._trades if t.get("id") != tid]
        elif event_type == "trade.tp1_hit":
            tid = payload.get("trade_id", "")
            for t in self._trades:
                if t.get("id") == tid:
                    t["state"] = "tp1_hit"
        self.account.open_trades = len(self._trades)
        self.emit("trades")
        self.emit("account")

    def on_mt5_error(self, message: str) -> None:
        if ":" in message:
            message = message.split(":", 1)[-1].strip()
        self._mt5_error            = message
        self.account.mt5_connected = False
        self._mt5_connected        = False
        self.emit("mt5_error", message=message)
        self.emit("account")

    def on_gateway_error(self, message: str) -> None:
        self._gateway_error = message
        self.emit("gateway_error", message=message)

    def mark_setup_complete(self, complete: bool = True) -> None:
        changed = self._setup_complete != complete
        self._setup_complete = complete
        if changed:
            self._recompute_lifecycle()
            self.emit("setup")

    def get_readiness_issues(self, config: dict) -> List[ReadinessIssue]:
        """Return ordered list of issues blocking the engine from trading."""
        issues: List[ReadinessIssue] = []
        mt5 = config.get("mt5", {})
        gw  = config.get("gateway", {})

        if not str(mt5.get("path", "")).strip():
            issues.append(ReadinessIssue(
                key="mt5_path",
                title="Trading platform not selected",
                detail="Choose a MetaTrader terminal from the Platform page.",
                action_label="Select Platform",
                action_page="Platform",
            ))
        if not str(mt5.get("login", "")).strip():
            issues.append(ReadinessIssue(
                key="mt5_login",
                title="MT5 account login missing",
                detail="Enter your MT5 account number on the Platform page.",
                action_label="Enter Credentials",
                action_page="Platform",
            ))
        if not str(mt5.get("password", "")).strip():
            issues.append(ReadinessIssue(
                key="mt5_password",
                title="MT5 password missing",
                detail="Enter your MT5 account password on the Platform page.",
                action_label="Enter Credentials",
                action_page="Platform",
            ))
        if not str(mt5.get("server", "")).strip():
            issues.append(ReadinessIssue(
                key="mt5_server",
                title="MT5 server name missing",
                detail="Enter your broker's server name on the Platform page.",
                action_label="Enter Credentials",
                action_page="Platform",
            ))
        if not str(gw.get("activation_key", "")).strip():
            issues.append(ReadinessIssue(
                key="activation_key",
                title="License key required",
                detail="Purchase or copy your license key from the web dashboard.",
                action_label="Open Dashboard",
                action_page="__dashboard__",
            ))
        if not str(gw.get("ws_url", "")).strip():
            issues.append(ReadinessIssue(
                key="gateway_url",
                title="Connection server not configured",
                detail="The server address is missing. Check your account dashboard.",
                action_label="Open Dashboard",
                action_page="__dashboard__",
            ))
        risk = config.get("risk", {})
        if not (risk.get("max_daily_loss_percent") or risk.get("max_losing_streak")):
            issues.append(ReadinessIssue(
                key="risk_profile",
                title="Risk profile not configured",
                detail="Set your daily loss limit and risk tolerance on the Risk page.",
                action_label="Set Risk Profile",
                action_page="Risk",
            ))
        if not self._service_installed:
            issues.append(ReadinessIssue(
                key="service_not_installed",
                title="AQ Agent not installed",
                detail="Install AQ Agent before trading can start.",
                action_label="Install AQ Agent",
                action_page="Engine",
            ))
        return issues

    def force_out_of_checking(self) -> None:
        """Safety valve: called if CHECKING never resolves naturally."""
        if self._lifecycle == EngineLifecycle.CHECKING:
            self._lifecycle = EngineLifecycle.UNKNOWN
            self.emit("engine", lifecycle=self._lifecycle)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _recompute_lifecycle(self, engine_error: Optional[str] = None) -> None:
        from src.gui.service_controller import ServiceStatus

        prev = self._lifecycle

        if not self._setup_complete:
            new = EngineLifecycle.NOT_CONFIGURED
        elif self._service_raw == ServiceStatus.NOT_INSTALLED:
            new = EngineLifecycle.SERVICE_NOT_INSTALLED
        elif self._service_raw == ServiceStatus.RUNNING:
            if self._ws_alive and self._last_heartbeat > 0:
                age = time.time() - self._last_heartbeat
                new = (
                    EngineLifecycle.RUNNING_CONNECTED
                    if age <= 30
                    else EngineLifecycle.RUNNING_NO_HEARTBEAT
                )
            else:
                new = EngineLifecycle.RUNNING_NO_HEARTBEAT
        elif self._service_raw == ServiceStatus.STOPPED:
            new = (
                EngineLifecycle.FAILED
                if (engine_error or self._engine_error)
                else EngineLifecycle.INSTALLED_STOPPED
            )
        elif self._service_raw == ServiceStatus.STARTING:
            new = EngineLifecycle.STARTING
        elif self._service_raw == ServiceStatus.STOPPING:
            new = EngineLifecycle.STOPPING
        else:
            new = EngineLifecycle.UNKNOWN

        if new != prev:
            self._lifecycle = new
            self.emit("engine", lifecycle=new)
