"""
Application configuration — loaded from config.yaml at startup.

Usage:
    cfg = AppConfig.from_yaml()                    # looks for config.yaml in cwd
    cfg = AppConfig.from_yaml("path/to/config.yaml")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from src.utils.symbol import normalise_symbol


# ── Internal defaults ─────────────────────────────────────────────────────────
# These values are platform-controlled and must not be editable by users.
# They are merged as the base layer before user config is applied, so a slim
# user_settings.yaml (containing only the allowed user paths) still produces a
# fully-populated AppConfig.


def _resolve_engine_version() -> str:
    """Best-effort read of version.txt (exe dir, cwd, repo root).

    The packaged build ships version.txt beside the exe (engine.spec) and the
    auto-updater rewrites it, so this is the single source of truth for the
    version the engine reports in its gateway handshake.
    """
    import sys

    candidates = [
        Path(sys.executable).parent / "version.txt",
        Path("version.txt"),
        Path(__file__).resolve().parents[2] / "version.txt",
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                version = candidate.read_text(encoding="utf-8-sig").strip()
                if version:
                    return version
        except Exception:
            continue
    return "0.1.0"


_INTERNAL_DEFAULTS: dict = {
    "gateway": {
        "ws_url": "wss://apex-gateway.somicast.com/engine",
        "engine_version": _resolve_engine_version(),
        "room_ttl_seconds": 3600,
        "symbols": ["XAUUSD"],
    },
    "mt5": {
        "magic": 8858,
        "slippage": 10,
        "comment": "bobisquote",
    },
    "risk": {
        "max_exposure_per_symbol": 2,
        "min_rr_ratio": 1.0,
        "min_lot_size": 0.01,
        "sl_ratio_threshold": 0.35,
        "symbol_sl_ratio_threshold": {
            "XAUUSD": 0.35,
            "US100": 0.20,
            "US500": 0.20,
        },
        "rolling_window_size": 2,
        "rolling_drawdown_pct": 2.0,
        "equity_throttle": {
            "enabled": True,
            "drawdown_threshold_r": 8.0,
            "release_threshold_r": 6.0,
            "risk_multiplier": 0.5,
            "window_days": 30,
        },
        "cluster_risk": {
            "enabled": False,
            "groups": [
                {
                    "name": "indices",
                    "symbols": ["US100", "US500", "US30"],
                    "max_same_day_loss_r": 1.5,
                    "max_concurrent_positions": 2,
                    "max_same_day_losses": 2,
                    "after_first_loss_risk_multiplier": 0.5,
                    "min_trade_risk_multiplier": 0.25,
                },
                {
                    "name": "metals",
                    "symbols": ["XAUUSD", "XAGUSD"],
                    "max_same_day_loss_r": 1.5,
                    "max_concurrent_positions": 2,
                    "max_same_day_losses": 2,
                    "after_first_loss_risk_multiplier": 0.5,
                    "min_trade_risk_multiplier": 0.25,
                },
            ],
        },
    },
    "execution": {
        "tp1_trigger_pct": 50.0,
        "tp1_percentage": 0.0,
        "move_sl_to_be_on_tp1": True,
        "breakeven_spread_multiplier": 1.5,
        "breakeven_max_buffer_pct_of_risk": 10.0,
        "tf_overrides": {
            "*": {
                "5/5":   {"tp1_trigger_pct": 45.0},
                "30/30": {"tp1_trigger_pct": 45.0},
            },
        },
        "spread_risk_multiplier": 1.0,
        "order_retry_count": 2,
        "order_retry_delay_sec": 0.5,
        "max_entry_slippage_pct_of_stop": 0.20,
        "max_signal_age_ms": 120_000,
        "close_on_slippage_exceed": False,
        "adjust_levels_on_slippage": False,
    },
    "engine": {
        "timezone": "UTC",
        "log_level": "INFO",
        "storage_path": "./data",
        "monitoring_port": 8080,
        "position_poll_interval": 0.6,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: override wins on scalar conflicts; dicts are merged recursively."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_pct_range(name: str, value: float) -> None:
    if value <= 0.0 or value >= 100.0:
        raise ValueError(f"{name} must be > 0 and < 100.")


def _validate_pct_inclusive(name: str, value: float) -> None:
    if value < 0.0 or value > 100.0:
        raise ValueError(f"{name} must be between 0 and 100.")


def _parse_tf_overrides(raw: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Parse per-symbol, per-timeframe TP1 overrides.

    YAML form:
      XAUUSD:
        "5/5":
          tp1_trigger_pct: 45.0
        "*":              # wildcard TF for this symbol
          tp1_trigger_pct: 42.0
      "*":                # wildcard symbol
        "5/5":
          tp1_trigger_pct: 40.0

    Resolution priority: symbol+TF > symbol+* > *+TF > *+* > global default.
    Symbol keys are uppercased; "*" is kept as-is.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("execution.tf_overrides must be a mapping.")

    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for raw_symbol, tf_map in raw.items():
        if not isinstance(tf_map, dict):
            raise ValueError(
                f"execution.tf_overrides.{raw_symbol} must be a mapping."
            )
        symbol = str(raw_symbol)
        if symbol != "*":
            symbol = symbol.upper().replace("/", "")
        parsed_tf: Dict[str, Dict[str, Any]] = {}
        for pair, values in tf_map.items():
            if not isinstance(values, dict):
                raise ValueError(
                    f"execution.tf_overrides.{raw_symbol}.{pair} must be a mapping."
                )
            override: Dict[str, Any] = {}
            if "tp1_trigger_pct" in values:
                override["tp1_trigger_pct"] = float(values["tp1_trigger_pct"])
            if "tp1_percentage" in values:
                override["tp1_percentage"] = float(values["tp1_percentage"])
            if "tp1_close_pct" in values:
                override["tp1_percentage"] = float(values["tp1_close_pct"])
            if override:
                parsed_tf[str(pair)] = override
        if parsed_tf:
            result[symbol] = parsed_tf
    return result


def _tf_pair_key(htf_interval: str | None, ltf_interval: str | None) -> str | None:
    if not htf_interval or not ltf_interval:
        return None
    return f"{_interval_to_minutes(htf_interval)}/{_interval_to_minutes(ltf_interval)}"


def _interval_to_minutes(interval: str) -> int:
    value = interval.strip().lower()
    units = (
        ("minutes", 1),
        ("minute", 1),
        ("mins", 1),
        ("min", 1),
        ("m", 1),
        ("hours", 60),
        ("hour", 60),
        ("hrs", 60),
        ("hr", 60),
        ("h", 60),
        ("days", 1440),
        ("day", 1440),
        ("d", 1440),
    )
    for suffix, multiplier in units:
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * multiplier
    return int(value)


@dataclass(frozen=True)
class ClusterGroupConfig:
    name: str
    symbols: tuple[str, ...]
    max_same_day_loss_r: float = 1.5
    max_concurrent_positions: int = 2
    max_same_day_losses: int = 2
    after_first_loss_risk_multiplier: float = 0.5
    min_trade_risk_multiplier: float = 0.25

    def __post_init__(self) -> None:
        if self.max_same_day_loss_r <= 0:
            raise ValueError("max_same_day_loss_r must be > 0")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be >= 1")
        if self.max_same_day_losses < 1:
            raise ValueError("max_same_day_losses must be >= 1")
        if not (0 < self.after_first_loss_risk_multiplier <= 1):
            raise ValueError("after_first_loss_risk_multiplier must be > 0 and <= 1")
        if not (0 < self.min_trade_risk_multiplier <= 1):
            raise ValueError("min_trade_risk_multiplier must be > 0 and <= 1")


@dataclass(frozen=True)
class ClusterRiskConfig:
    enabled: bool = False
    groups: tuple[ClusterGroupConfig, ...] = ()


@dataclass(frozen=True)
class EquityThrottleConfig:
    """Equity-curve risk throttle — platform-internal except `enabled`.

    Sizes new positions at `risk_multiplier` while the rolling R-equity of
    closed trades sits more than `drawdown_threshold_r` below its window
    peak; releases once drawdown recovers below `release_threshold_r`.
    """

    enabled: bool = True
    drawdown_threshold_r: float = 8.0
    release_threshold_r: float = 6.0
    risk_multiplier: float = 0.5
    window_days: int = 30

    def __post_init__(self) -> None:
        if self.drawdown_threshold_r <= 0:
            raise ValueError("risk.equity_throttle.drawdown_threshold_r must be > 0.")
        if not (0 < self.release_threshold_r <= self.drawdown_threshold_r):
            raise ValueError(
                "risk.equity_throttle.release_threshold_r must be > 0 and "
                "<= drawdown_threshold_r."
            )
        if not (0 < self.risk_multiplier <= 1.0):
            raise ValueError(
                "risk.equity_throttle.risk_multiplier must be in (0, 1]."
            )
        if self.window_days < 1:
            raise ValueError("risk.equity_throttle.window_days must be >= 1.")


def _parse_equity_throttle(raw: Any) -> "EquityThrottleConfig":
    if not raw:
        return EquityThrottleConfig()
    if not isinstance(raw, dict):
        raise ValueError("risk.equity_throttle must be a mapping.")
    defaults = EquityThrottleConfig()
    return EquityThrottleConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        drawdown_threshold_r=float(
            raw.get("drawdown_threshold_r", defaults.drawdown_threshold_r)
        ),
        release_threshold_r=float(
            raw.get("release_threshold_r", defaults.release_threshold_r)
        ),
        risk_multiplier=float(raw.get("risk_multiplier", defaults.risk_multiplier)),
        window_days=int(raw.get("window_days", defaults.window_days)),
    )


def _parse_cluster_risk(raw: Any) -> "ClusterRiskConfig":
    if not raw:
        return ClusterRiskConfig(enabled=False)
    if not isinstance(raw, dict):
        raise ValueError("risk.cluster_risk must be a mapping.")

    groups_raw = raw.get("groups", [])
    if groups_raw is None:
        groups_raw = []
    if not isinstance(groups_raw, list):
        raise ValueError("risk.cluster_risk.groups must be a list.")

    groups: list[ClusterGroupConfig] = []
    for item in groups_raw:
        if not isinstance(item, dict):
            raise ValueError("Each risk.cluster_risk.groups item must be a mapping.")

        symbols_raw = item.get("symbols", [])
        if isinstance(symbols_raw, str):
            symbols_raw = [s.strip() for s in symbols_raw.split(",") if s.strip()]
        if not symbols_raw:
            raise ValueError("Cluster group must include at least one symbol.")

        group = ClusterGroupConfig(
            name=str(item["name"]),
            symbols=tuple(normalise_symbol(str(s)) for s in symbols_raw),
            max_same_day_loss_r=float(item.get("max_same_day_loss_r", 1.5)),
            max_concurrent_positions=int(item.get("max_concurrent_positions", 2)),
            max_same_day_losses=int(item.get("max_same_day_losses", 2)),
            after_first_loss_risk_multiplier=float(
                item.get("after_first_loss_risk_multiplier", 0.5)
            ),
            min_trade_risk_multiplier=float(item.get("min_trade_risk_multiplier", 0.25)),
        )
        groups.append(group)

    return ClusterRiskConfig(
        enabled=bool(raw.get("enabled", False)),
        groups=tuple(groups),
    )


@dataclass(frozen=True)
class RiskConfig:
    max_losing_streak: int
    max_daily_loss_percent: float
    max_exposure_per_symbol: int
    min_rr_ratio: float
    max_lot_size: float
    min_lot_size: float
    sl_ratio_threshold: float
    symbol_sl_ratio_threshold: Dict[str, float]
    no_hedging: bool = True
    max_profit_drawdown_percent: float = 2.0
    rolling_window_size: int = 0
    rolling_drawdown_pct: float = 0.0
    cluster_risk: ClusterRiskConfig = field(default_factory=ClusterRiskConfig)
    equity_throttle: EquityThrottleConfig = field(default_factory=EquityThrottleConfig)

    def __post_init__(self) -> None:
        if self.max_losing_streak < 1:
            raise ValueError(
                f"risk.max_losing_streak must be >= 1, got: {self.max_losing_streak}"
            )


@dataclass(frozen=True)
class ExecutionConfig:
    tp1_trigger_pct: float   # 0–100: TP1 fires at this % of the entry→TP2 range
    tp1_percentage: float
    move_sl_to_be_on_tp1: bool
    slippage: int
    magic: int
    comment: str
    spread_risk_multiplier: float
    order_retry_count: int
    max_entry_slippage_pct_of_stop: float
    close_on_slippage_exceed: bool
    order_retry_delay_sec: float
    breakeven_spread_multiplier: float = 1.5
    breakeven_max_buffer_pct_of_risk: float = 10.0
    adjust_levels_on_slippage: bool = False
    max_signal_age_ms: int = 90_000
    tf_overrides: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_pct_range("execution.tp1_trigger_pct", self.tp1_trigger_pct)
        _validate_pct_inclusive("execution.tp1_percentage", self.tp1_percentage)
        if (
            self.breakeven_spread_multiplier < 0.0
            or 0.0 < self.breakeven_spread_multiplier < 1.0
        ):
            raise ValueError(
                "execution.breakeven_spread_multiplier must be 0 or >= 1."
            )
        _validate_pct_inclusive(
            "execution.breakeven_max_buffer_pct_of_risk",
            self.breakeven_max_buffer_pct_of_risk,
        )
        for sym, tf_map in self.tf_overrides.items():
            for tf_key, override in tf_map.items():
                if "tp1_trigger_pct" in override:
                    _validate_pct_range(
                        f"execution.tf_overrides.{sym}.{tf_key}.tp1_trigger_pct",
                        float(override["tp1_trigger_pct"]),
                    )
                if "tp1_percentage" in override:
                    _validate_pct_inclusive(
                        f"execution.tf_overrides.{sym}.{tf_key}.tp1_percentage",
                        float(override["tp1_percentage"]),
                    )

    def _resolve_tf_override(
        self, symbol: str | None, tf_key: str | None
    ) -> Dict[str, Any]:
        """Return the most-specific override dict for (symbol, tf_key).

        Priority: symbol+TF > symbol+* > *+TF > *+* > {} (no override).
        """
        if not self.tf_overrides:
            return {}
        norm = symbol.upper().replace("/", "") if symbol else None
        candidates: list[tuple[str | None, str | None]] = []
        if norm and tf_key:
            candidates.append((norm, tf_key))
        if norm:
            candidates.append((norm, "*"))
        if tf_key:
            candidates.append(("*", tf_key))
        candidates.append(("*", "*"))
        for sym, tf in candidates:
            if sym is None:
                continue
            sym_map = self.tf_overrides.get(sym)
            if not sym_map:
                continue
            entry = sym_map.get(tf or "*", {})
            if entry:
                return dict(entry)
        return {}

    def tp1_trigger_pct_for(
        self,
        symbol: str | None,
        htf_interval: str | None,
        ltf_interval: str | None,
    ) -> float:
        tf_key = _tf_pair_key(htf_interval, ltf_interval)
        override = self._resolve_tf_override(symbol, tf_key)
        return float(override.get("tp1_trigger_pct", self.tp1_trigger_pct))

    def tp1_percentage_for(
        self,
        symbol: str | None,
        htf_interval: str | None,
        ltf_interval: str | None,
    ) -> float:
        tf_key = _tf_pair_key(htf_interval, ltf_interval)
        override = self._resolve_tf_override(symbol, tf_key)
        return float(override.get("tp1_percentage", self.tp1_percentage))


@dataclass(frozen=True)
class Mt5Config:
    login: int
    password: str
    server: str
    path: str


@dataclass(frozen=True)
class GatewayConfig:
    ws_url: str
    activation_key: str
    engine_id: str
    engine_version: str
    symbols: list[str]
    room_ttl_seconds: int
    signal_hmac_secret: Optional[str] = None

    def __post_init__(self) -> None:
        if len(self.engine_id) < 8:
            raise ValueError("gateway.engine_id must be at least 8 characters.")
        if len(self.activation_key) < 16:
            raise ValueError("APEX_ACTIVATION_KEY must be at least 16 characters.")
        if self.room_ttl_seconds < 30 or self.room_ttl_seconds > 86400:
            raise ValueError("gateway.room_ttl_seconds must be between 30 and 86400.")
        if not self.symbols:
            raise ValueError("gateway.symbols must contain at least one symbol.")


@dataclass(frozen=True)
class AppConfig:
    risk: RiskConfig
    execution: ExecutionConfig
    mt5: Mt5Config
    gateway: GatewayConfig
    storage_path: str
    log_level: str
    position_poll_interval: float
    engine_timezone: ZoneInfo
    monitoring_port: int

    @classmethod
    def from_yaml(cls, path: Path | str = "config.yaml") -> "AppConfig":
        # Load .env if present — kept for backward compatibility with existing
        # installations that still have a .env file.  New installs write
        # everything into config.yaml and no longer need .env.
        load_dotenv(override=False)

        with open(path, "r", encoding="utf-8") as fh:
            raw: dict = _deep_merge(_INTERNAL_DEFAULTS, yaml.safe_load(fh) or {})

        gateway = raw.get("gateway", {})
        mt5 = raw.get("mt5", {})
        risk = raw.get("risk", {})
        exe = raw.get("execution", {})
        eng = raw.get("engine", {})

        gateway_symbols_raw = gateway.get("symbols", [])
        if isinstance(gateway_symbols_raw, str):
            gateway_symbols_raw = [s.strip() for s in gateway_symbols_raw.split(",")]

        # Secrets: prefer config.yaml values; fall back to env vars so that
        # existing .env-based installs continue to work without reconfiguration.
        mt5_password = str(mt5.get("password") or os.environ.get("MT5_PASSWORD", ""))
        activation_key = str(
            gateway.get("activation_key") or os.environ.get("APEX_ACTIVATION_KEY", "")
        )
        signal_hmac_secret = (
            gateway.get("signal_hmac_secret") or os.environ.get("SIGNAL_HMAC_SECRET") or None
        )

        return cls(
            gateway=GatewayConfig(
                ws_url=str(gateway["ws_url"]),
                activation_key=activation_key,
                engine_id=str(gateway.get("engine_id", f"execution-{mt5['login']}")),
                engine_version=str(gateway.get("engine_version", "0.1.0")),
                symbols=[
                    normalise_symbol(str(symbol)) for symbol in gateway_symbols_raw
                ],
                room_ttl_seconds=int(gateway.get("room_ttl_seconds", 3600)),
                signal_hmac_secret=signal_hmac_secret,
            ),
            mt5=Mt5Config(
                login=int(mt5["login"]),
                password=mt5_password,
                server=str(mt5["server"]),
                path=str(mt5.get("path", "")),
            ),
            risk=RiskConfig(
                max_losing_streak=int(risk["max_losing_streak"]),
                max_daily_loss_percent=float(risk["max_daily_loss_percent"]),
                max_exposure_per_symbol=int(risk["max_exposure_per_symbol"]),
                min_rr_ratio=float(risk["min_rr_ratio"]),
                max_lot_size=float(risk["max_lot_size"]),
                min_lot_size=float(risk.get("min_lot_size", 0.01)),
                sl_ratio_threshold=float(risk["sl_ratio_threshold"]),
                symbol_sl_ratio_threshold={
                    normalise_symbol(str(symbol)): float(threshold)
                    for symbol, threshold in risk.get(
                        "symbol_sl_ratio_threshold", {}
                    ).items()
                },
                no_hedging=bool(risk.get("no_hedging", True)),
                max_profit_drawdown_percent=float(risk.get("max_profit_drawdown_percent", 2.0)),
                rolling_window_size=int(risk.get("rolling_window_size", 0)),
                rolling_drawdown_pct=float(risk.get("rolling_drawdown_pct", 0.0)),
                cluster_risk=_parse_cluster_risk(risk.get("cluster_risk")),
                equity_throttle=_parse_equity_throttle(risk.get("equity_throttle")),
            ),
            execution=ExecutionConfig(
                tp1_trigger_pct=float(exe["tp1_trigger_pct"]),
                tp1_percentage=float(exe["tp1_percentage"]),
                move_sl_to_be_on_tp1=bool(exe.get("move_sl_to_be_on_tp1", True)),
                slippage=int(mt5.get("slippage", 10)),
                magic=int(mt5.get("magic", 20240101)),
                comment=str(mt5.get("comment", "signal-engine")),
                spread_risk_multiplier=float(exe.get("spread_risk_multiplier", 1.0)),
                order_retry_count=int(exe.get("order_retry_count", 2)),
                max_entry_slippage_pct_of_stop=float(exe.get("max_entry_slippage_pct_of_stop", 0.20)),
                close_on_slippage_exceed=bool(exe.get("close_on_slippage_exceed", False)),
                order_retry_delay_sec=float(exe.get("order_retry_delay_sec", 0.5)),
                breakeven_spread_multiplier=float(
                    exe.get("breakeven_spread_multiplier", 1.5)
                ),
                breakeven_max_buffer_pct_of_risk=float(
                    exe.get("breakeven_max_buffer_pct_of_risk", 10.0)
                ),
                adjust_levels_on_slippage=bool(exe.get("adjust_levels_on_slippage", False)),
                max_signal_age_ms=int(exe.get("max_signal_age_ms", 90_000)),
                tf_overrides=_parse_tf_overrides(exe.get("tf_overrides")),
            ),
            storage_path=str(eng.get("storage_path", "./data")),
            log_level=str(eng.get("log_level", "INFO")),
            position_poll_interval=float(eng.get("position_poll_interval", 5.0)),
            engine_timezone=ZoneInfo(str(eng.get("timezone", "UTC"))),
            monitoring_port=int(eng.get("monitoring_port", 8080)),
        )


@dataclass(frozen=True)
class ManagerConfig:
    storage_path: str
    agents_data_dir: str
    api_port: int = 8765
    channel_port: int = 8766
    legacy_config_path: str = ""
    gateway_ws_url: str = ""
    gateway_http_url: str = ""
    engine_version: str = "0.1.0"

    @classmethod
    def defaults(cls) -> "ManagerConfig":
        base = (
            Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
            / "Apex Quantel"
            / "Multi"
        )
        gw_ws = _INTERNAL_DEFAULTS["gateway"]["ws_url"]
        # Derive HTTP base URL from the WS URL:
        # wss://host/path → https://host
        from urllib.parse import urlparse
        parsed = urlparse(gw_ws)
        gw_http = f"https://{parsed.netloc}"
        return cls(
            storage_path=str(base / "manager"),
            agents_data_dir=str(base / "agents"),
            legacy_config_path=str(base.parent / "config.yaml"),
            gateway_ws_url=gw_ws,
            gateway_http_url=gw_http,
            engine_version=_resolve_engine_version(),
        )
