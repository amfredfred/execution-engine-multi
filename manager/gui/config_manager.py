"""
src/gui/config_manager.py — Safe, validated config loading and saving.

Responsibilities
----------------
* Locate config.yaml (packaged EXE, ProgramData, CWD, dev layout).
* Create config from template if missing.
* Validate required fields — return plain-English errors.
* Save atomically (write temp → validate → replace).
* Never log passwords or activation keys.
* Provide a clean dict view for the GUI to read.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# Default web dashboard URL — override via config.yaml: dashboard.url
DEFAULT_DASHBOARD_URL = "https://app.somicast.com"

# Fields the user is permitted to set.  Everything outside this list is
# managed by internal defaults and must never be written to user config.
_ALLOWED_USER_PATHS: frozenset = frozenset({
    "gateway.activation_key",
    "gateway.symbols",
    "gateway.ws_url",
    "mt5.login",
    "mt5.password",
    "mt5.server",
    "mt5.path",
    "risk.max_losing_streak",
    "risk.max_daily_loss_percent",
    "risk.max_profit_drawdown_percent",
    "risk.max_lot_size",
    "risk.no_hedging",
    "risk.equity_throttle.enabled",
    "startup.auto_start_engine",
    "startup.minimise_on_start",
})

# ProgramData path (production install location)
_APPNAME    = "Apex Quantel"
_PROGDATA   = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / _APPNAME
_PROGDATA_CFG = _PROGDATA / "config.yaml"

_REQUIRED_FIELDS: List[Tuple[str, str]] = [
    # (dot-path,  plain-English name)
    ("mt5.login",              "MT5 account login"),
    ("mt5.password",           "MT5 password"),
    ("mt5.server",             "MT5 server name"),
    ("mt5.path",               "MetaTrader executable path"),
    ("gateway.ws_url",         "Gateway WebSocket URL"),
    ("gateway.activation_key", "Activation key"),
]

_SENSITIVE_KEYS = {"password", "activation_key"}


class ConfigManager:
    """Load, validate, and atomically save config.yaml."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._path: Path = (
            Path(config_path) if config_path
            else self._locate_config()
        )
        self._cache: Optional[dict] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.exists()

    def load(self, force: bool = False) -> dict:
        """Load and cache config.  Returns internal defaults on failure (no crash).

        The returned dict is a deep-merge of internal defaults (base layer) and
        the persisted user settings (override layer), so callers always receive a
        fully-populated config regardless of how slim the on-disk file is.
        """
        if self._cache is not None and not force:
            return self._cache
        try:
            from src.config.settings import _INTERNAL_DEFAULTS, _deep_merge  # type: ignore
            if not self._path.exists():
                self._cache = dict(_INTERNAL_DEFAULTS)
                return self._cache
            with open(self._path, "r", encoding="utf-8") as fh:
                user_data = yaml.safe_load(fh) or {}
            self._cache = _deep_merge(_INTERNAL_DEFAULTS, user_data)
            return self._cache
        except Exception as exc:
            logger.warning("Config load error: %s", exc)
            self._cache = {}
            return {}

    def reload(self) -> dict:
        """Force reload from disk."""
        self._cache = None
        return self.load()

    def validate(self, cfg: Optional[dict] = None) -> List[str]:
        """
        Return a list of plain-English error strings.
        Empty list means config is valid.
        """
        if cfg is None:
            cfg = self.load()
        errors: List[str] = []

        for dot_path, label in _REQUIRED_FIELDS:
            parts = dot_path.split(".")
            node: object = cfg
            for part in parts:
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(part)  # type: ignore[assignment]
            if not node:
                errors.append(f"{label} is not configured.")

        # Extra semantic checks
        mt5  = cfg.get("mt5",  {}) if isinstance(cfg, dict) else {}
        risk = cfg.get("risk", {}) if isinstance(cfg, dict) else {}

        try:
            if mt5.get("login"):
                int(mt5["login"])
        except (TypeError, ValueError):
            errors.append("MT5 account login must be a number.")

        def _check_float(field: str, label: str, lo: float, hi: float, inclusive_lo: bool = False) -> None:
            raw = risk.get(field)
            if raw is None:
                return
            try:
                v = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{label} must be a number.")
                return
            if inclusive_lo:
                if v <= 0 or v > hi:
                    errors.append(f"{label} must be > 0 and <= {hi}.")
            else:
                if v < lo or v > hi:
                    errors.append(f"{label} must be between {lo} and {hi}.")

        _check_float("max_daily_loss_percent",      "Daily Loss Limit",         lo=0,   hi=20,  inclusive_lo=True)
        _check_float("max_profit_drawdown_percent", "Max Profit Drawdown",      lo=0,   hi=50,  inclusive_lo=True)
        _check_float("max_lot_size",                "Max Lot Size",             lo=0,   hi=1e9, inclusive_lo=True)

        raw_streak = risk.get("max_losing_streak")
        if raw_streak is not None:
            try:
                streak = int(float(raw_streak))
                if streak < 1 or streak > 10:
                    errors.append("Max Losing Streak must be between 1 and 10.")
            except (TypeError, ValueError):
                errors.append("Max Losing Streak must be a number.")

        throttle = risk.get("equity_throttle")
        if isinstance(throttle, dict):
            enabled = throttle.get("enabled")
            if enabled is not None and not isinstance(enabled, (bool, int)):
                errors.append("Drawdown Risk Throttle must be On or Off.")

        return errors

    def is_setup_complete(self, cfg: Optional[dict] = None) -> bool:
        """True when all required fields are present."""
        return len(self.validate(cfg)) == 0

    def save(self, cfg: dict) -> Optional[str]:
        """
        Atomically write config.yaml containing only user-allowed fields.
        Returns None on success or a plain-English error string.

        Internal/platform fields are stripped before writing; they are always
        supplied at load time via _INTERNAL_DEFAULTS so the engine can run from
        a minimal user_settings file.
        """
        cfg = _extract_allowed(cfg)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)

            # Write to temp file in same directory
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=".config_tmp_",
                suffix=".yaml",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    yaml.dump(
                        cfg, fh,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
                # Validate the temp file is readable YAML
                with open(tmp, "r", encoding="utf-8") as fh:
                    yaml.safe_load(fh)
                # Atomically replace
                shutil.move(tmp, str(self._path))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

            self._cache = cfg
            logger.info("Config saved to %s", self._path)
            return None

        except PermissionError:
            return (
                f"Cannot write to {self._path}. "
                "Try running as Administrator, or check folder permissions."
            )
        except Exception as exc:
            logger.exception("Config save error")
            return f"Could not save settings: {exc}"

    def update(self, section: str, updates: dict) -> Optional[str]:
        """Load, merge updates into section, save atomically."""
        cfg = self.load()
        cfg.setdefault(section, {}).update(updates)
        return self.save(cfg)

    def get(self, *keys: str, default=None):
        """Dot-path or single key read.  E.g. .get('mt5', 'server')."""
        cfg = self.load()
        node = cfg
        for key in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(key, default)
        return node

    def dashboard_url(self) -> str:
        """Return the configured web-dashboard URL, or the well-known default."""
        return self.get("dashboard", "url") or DEFAULT_DASHBOARD_URL

    def masked_copy(self) -> dict:
        """Return config with sensitive values replaced by '••••••••'."""
        import copy
        cfg = copy.deepcopy(self.load())
        _mask_sensitive(cfg)
        return cfg

    def ensure_programdata_dirs(self) -> None:
        """Create %ProgramData%/Apex Quantel/{logs,data} if missing."""
        try:
            for subdir in ("logs", "data"):
                (_PROGDATA / subdir).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning("Cannot create ProgramData dirs (need elevation)")

    # ── Location logic ────────────────────────────────────────────────────────

    @staticmethod
    def _locate_config() -> Path:
        """
        Priority order:
        1. %ProgramData%\\Apex Quantel\\config.yaml  (production install)
        2. Next to the EXE  (packaged, portable install)
        3. Walk up 4 levels from EXE  (dev dist layout)
        4. sys._MEIPASS  (PyInstaller bundle)
        5. CWD
        6. Walk up from __file__  (editable venv)
        7. Fallback to ProgramData (will be created on first save)
        """
        # 1. ProgramData production install
        if _PROGDATA_CFG.exists():
            return _PROGDATA_CFG

        exe_dir = Path(sys.executable).parent

        # 2–3. Next to exe / walk up
        for depth in range(5):
            candidate = exe_dir
            for _ in range(depth):
                candidate = candidate.parent
            cfg = candidate / "config.yaml"
            if cfg.exists():
                return cfg

        # 4. PyInstaller _MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cfg = Path(meipass) / "config.yaml"
            if cfg.exists():
                return cfg

        # 5. CWD
        cfg = Path.cwd() / "config.yaml"
        if cfg.exists():
            return cfg

        # 6. Walk up from __file__
        for parent in Path(__file__).resolve().parents:
            cfg = parent / "config.yaml"
            if cfg.exists():
                return cfg

        # 7. Default to ProgramData (created on first save)
        return _PROGDATA_CFG

    @staticmethod
    def programdata_config_path() -> Path:
        return _PROGDATA_CFG

    @staticmethod
    def programdata_logs_path() -> Path:
        return _PROGDATA / "logs"

    @staticmethod
    def programdata_data_path() -> Path:
        return _PROGDATA / "data"

    @staticmethod
    def programdata_manager_path() -> Path:
        return _PROGDATA / "manager"

    @staticmethod
    def programdata_manager_logs_path() -> Path:
        return _PROGDATA / "manager" / "logs"

    @staticmethod
    def programdata_agents_path() -> Path:
        return _PROGDATA / "manager" / "agents"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mask_sensitive(node: object) -> None:
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in _SENSITIVE_KEYS and value:
            node[key] = "••••••••"
        elif isinstance(value, dict):
            _mask_sensitive(value)


def _extract_allowed(cfg: dict) -> dict:
    """Return a new dict containing only the user-allowed configuration paths.

    Any field not in _ALLOWED_USER_PATHS is silently dropped.  Internal
    defaults will supply those values at load time, so nothing is lost.
    """
    result: dict = {}
    for path in _ALLOWED_USER_PATHS:
        parts = path.split(".")
        src: object = cfg
        for part in parts[:-1]:
            if not isinstance(src, dict):
                src = None
                break
            src = src.get(part)  # type: ignore[assignment]
        if not isinstance(src, dict):
            continue
        value = src.get(parts[-1])
        if value is None:
            continue
        dst = result
        for part in parts[:-1]:
            dst = dst.setdefault(part, {})
        dst[parts[-1]] = value
    return result
