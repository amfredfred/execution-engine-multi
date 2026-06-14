"""
risk/risk_engine.py — evaluates a signal against all configured risk rules.

Change: accepts optional loss_tracker and passes it into RuleContext so
guard rules have access to trade-count circuit-breaker state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, TYPE_CHECKING

from src.domain.signal_interface import InboundSignal
from src.domain.trade import Trade
from src.domain.position import SymbolInfo
from src.config.settings import RiskConfig
from src.infra.metrics import metrics
from .rules import ALL_RULES, RuleContext, RuleResult, RiskRule

if TYPE_CHECKING:
    from src.risk.loss_tracker import LossTracker
    from src.risk.cluster_tracker import ClusterRiskTracker
    from src.risk.equity_throttle import EquityThrottleTracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: Optional[str] = None
    data: dict = field(default_factory=dict)

    @property
    def risk_multiplier(self) -> float:
        return float(self.data.get("risk_multiplier", 1.0))


class RiskEngine:

    def __init__(
        self,
        config: RiskConfig,
        rules: Optional[List[RiskRule]] = None,
        loss_tracker: Optional["LossTracker"] = None,
        cluster_tracker: Optional["ClusterRiskTracker"] = None,
        equity_throttle: Optional["EquityThrottleTracker"] = None,
    ) -> None:
        self._config = config
        self._rules = rules if rules is not None else ALL_RULES
        self._loss_tracker = loss_tracker
        self._cluster_tracker = cluster_tracker
        self._equity_throttle = equity_throttle

    def set_loss_tracker(self, tracker: "LossTracker") -> None:
        """Wire in LossTracker after construction (container convenience)."""
        self._loss_tracker = tracker

    def evaluate(
        self,
        signal: InboundSignal,
        open_trades: Sequence[Trade],
        daily_loss_pct: float,
        effective_open: int = 0,
        effective_symbol: int = 0,
        symbol_info: Optional[SymbolInfo] = None,
    ) -> RiskDecision:

        if not self._rules:
            raise ValueError("No risk rules configured")

        ctx = RuleContext(
            signal=signal,
            open_trades=list(open_trades),
            config=self._config,
            daily_loss_pct=daily_loss_pct,
            effective_open=effective_open,
            effective_symbol=effective_symbol,
            symbol_info=symbol_info,  # type: ignore[arg-type]
            loss_tracker=self._loss_tracker,
            cluster_tracker=self._cluster_tracker,
            equity_throttle=self._equity_throttle,
        )

        decision_data: dict = {}

        for rule in self._rules:
            result: RuleResult = rule(ctx)
            if not result.approved:
                fill_price = None
                spread = None
                if symbol_info is not None:
                    fill_price = (
                        symbol_info.ask
                        if signal.direction.value == "LONG"
                        else symbol_info.bid
                    )
                    if symbol_info.ask is not None and symbol_info.bid is not None:
                        spread = symbol_info.ask - symbol_info.bid
                logger.warning(
                    "Risk rejected",
                    extra={
                        "signal_id": signal.id,
                        "symbol": signal.resolved_symbol,
                        "direction": signal.direction.value,
                        "reason": result.reason,
                        "signal_entry": signal.entry_price,
                        "signal_stop_loss": signal.stop_loss,
                        "signal_tp1": signal.tp1,
                        "signal_tp2": signal.tp2,
                        "signal_rr": signal.risk_reward_ratio,
                        "broker_bid": symbol_info.bid if symbol_info else None,
                        "broker_ask": symbol_info.ask if symbol_info else None,
                        "broker_fill_price": fill_price,
                        "broker_spread": spread,
                        "setup_candle_close_at": signal.setup_candle_close_at,
                        "triggered_at": signal.triggered_at,
                        "emitted_at": signal.emitted_at,
                        "received_at": signal.received_at,
                    },
                )
                metrics.increment("risk.rejected")
                return RiskDecision(approved=False, reason=result.reason)

            decision_data.update(result.data)

        # Compose the equity-throttle multiplier multiplicatively with the
        # cluster multiplier — a plain dict update would overwrite one with
        # the other.
        throttle_mult = float(decision_data.pop("equity_throttle_multiplier", 1.0))
        if throttle_mult < 1.0:
            decision_data["risk_multiplier"] = (
                float(decision_data.get("risk_multiplier", 1.0)) * throttle_mult
            )

        logger.info(
            "Risk approved",
            extra={
                "signal_id": signal.id,
                "symbol": signal.resolved_symbol,
                "direction": signal.direction.value,
                "rr": signal.risk_reward_ratio,
                "risk_multiplier": decision_data.get("risk_multiplier", 1.0),
                "cluster_name": decision_data.get("cluster_name"),
                "equity_throttle_dd_r": decision_data.get("equity_throttle_dd_r"),
            },
        )
        metrics.increment("risk.approved")
        return RiskDecision(approved=True, data=decision_data)
