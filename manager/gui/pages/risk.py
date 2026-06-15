"""
src/gui/pages/risk.py — Default risk profile for the multi-agent manager.

These are global defaults applied when a new agent is provisioned.
Per-agent overrides can be set via PATCH /agents/{id}/config after provisioning.

User-configurable fields:
  - Daily loss limit %
  - Max losing streak
  - Calculated risk per trade (read-only formula)
  - Max profit drawdown %
  - Drawdown risk throttle toggle
  - Max lot size
  - Prevent hedging toggle
"""
from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

from manager.gui.theme import (
    GREEN, MUTED, TEXT,
    BASE, SURFACE, LINE,
    SUCCESS_BG, SUCCESS_BORDER,
    section_rule, page_header,
)
from manager.gui.components import SectionCard, ActionBanner

if TYPE_CHECKING:
    from manager.gui.app import ApexTraderGUI


class RiskPage(ctk.CTkScrollableFrame):
    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._vars: dict[str, tk.StringVar] = {}
        self._var_no_hedging = tk.BooleanVar(value=True)
        self._var_equity_throttle = tk.BooleanVar(value=True)
        self._build()
        self._load()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Risk Profile", "Default settings applied to newly provisioned agents")

        ctk.CTkLabel(
            self,
            text="Changes here apply to new agents. To adjust a running agent's risk, "
                 "use the agent's dashboard.",
            font=ctk.CTkFont(size=11), text_color=MUTED,
            wraplength=680, justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 4))

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        # ── Daily Budget ──────────────────────────────────────────────────────
        section_rule(content, "DAILY BUDGET").pack(fill="x")

        budget_card = SectionCard(content)
        budget_card.pack(fill="x", pady=(0, 16))

        _risk_field(
            budget_card.body, label="Daily Loss Limit",
            key="risk.max_daily_loss_percent", unit="%",
            tip="AQ Agent stops trading for the day once this % of balance is lost. "
                "Recommended: 0.5% – 10%.",
            vars_dict=self._vars, on_change=self._update_formula,
        )
        _risk_field(
            budget_card.body, label="Max Losing Streak",
            key="risk.max_losing_streak", unit="trades",
            tip="Consecutive losses used to divide daily budget into per-trade risk. "
                "Higher streak = smaller risk per trade. Recommended: 2 – 5.",
            vars_dict=self._vars, on_change=self._update_formula,
        )

        formula_row = ctk.CTkFrame(
            budget_card.body, fg_color=BASE,
            corner_radius=6, border_width=1, border_color=LINE,
        )
        formula_row.pack(fill="x", pady=(10, 0))
        formula_inner = ctk.CTkFrame(formula_row, fg_color="transparent")
        formula_inner.pack(padx=16, pady=10, fill="x")

        ctk.CTkLabel(formula_inner, text="Risk per trade",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left")
        self._lbl_formula = ctk.CTkLabel(formula_inner, text="--",
                                          font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
                                          text_color=GREEN)
        self._lbl_formula.pack(side="left", padx=(12, 0))
        self._lbl_formula_detail = ctk.CTkLabel(formula_inner, text="",
                                                 font=ctk.CTkFont(size=11), text_color=MUTED)
        self._lbl_formula_detail.pack(side="left", padx=(8, 0))

        # ── Equity Protection ─────────────────────────────────────────────────
        section_rule(content, "EQUITY PROTECTION").pack(fill="x")

        equity_card = SectionCard(content)
        equity_card.pack(fill="x", pady=(0, 16))

        _risk_field(
            equity_card.body, label="Max Profit Drawdown",
            key="risk.max_profit_drawdown_percent", unit="%",
            tip="Pauses until midnight if today's closed profit gives back this % from its session peak. "
                "Recommended: 2% – 10%.",
            vars_dict=self._vars,
        )

        throttle_row = ctk.CTkFrame(equity_card.body, fg_color="transparent")
        throttle_row.pack(fill="x", pady=(0, 4))
        tl = ctk.CTkFrame(throttle_row, fg_color="transparent")
        tl.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(tl, text="Drawdown Risk Throttle", anchor="w",
                     font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(
            tl,
            text="Halves position risk while recent results sit deep below peak. "
                 "Restores full size on recovery. Recommended: On.",
            anchor="w", font=ctk.CTkFont(size=11), text_color=MUTED,
            justify="left", wraplength=420,
        ).pack(anchor="w", pady=(1, 0))
        ctk.CTkSwitch(throttle_row, text="", variable=self._var_equity_throttle,
                      onvalue=True, offvalue=False,
                      command=self._on_throttle_changed).pack(side="right")

        self._throttle_banner = ActionBanner(equity_card.body)
        self._throttle_banner.pack(fill="x", pady=(4, 0))
        self._throttle_banner.hide()

        # ── Order Limits ──────────────────────────────────────────────────────
        section_rule(content, "ORDER LIMITS").pack(fill="x")

        limits_card = SectionCard(content)
        limits_card.pack(fill="x", pady=(0, 16))

        _risk_field(
            limits_card.body, label="Max Lot Size",
            key="risk.max_lot_size", unit="lots",
            tip="Hard cap on position size regardless of account size or risk calculation.",
            vars_dict=self._vars,
        )

        hedge_row = ctk.CTkFrame(limits_card.body, fg_color="transparent")
        hedge_row.pack(fill="x", pady=(0, 4))
        hl = ctk.CTkFrame(hedge_row, fg_color="transparent")
        hl.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(hl, text="Prevent Hedging", anchor="w",
                     font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(hl,
                     text="Block opposite-direction trades on the same symbol. Recommended: On.",
                     anchor="w", font=ctk.CTkFont(size=11), text_color=MUTED,
                     justify="left", wraplength=420).pack(anchor="w", pady=(1, 0))
        ctk.CTkSwitch(hedge_row, text="", variable=self._var_no_hedging,
                      onvalue=True, offvalue=False,
                      command=self._on_hedging_changed).pack(side="right")

        self._hedge_banner = ActionBanner(limits_card.body)
        self._hedge_banner.pack(fill="x", pady=(4, 0))
        self._hedge_banner.hide()

        # ── Save ──────────────────────────────────────────────────────────────
        self._save_banner = ActionBanner(content)
        self._save_banner.pack(fill="x", pady=(8, 0))
        self._save_banner.hide()

        ctk.CTkButton(
            content, text="Save Risk Settings",
            height=44, width=240,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER, text_color=GREEN,
            command=self._save,
        ).pack(pady=(8, 20))

        # ── Engine Guardrails (read-only) ─────────────────────────────────────
        section_rule(content, "ENGINE GUARDRAILS").pack(fill="x")
        ctk.CTkLabel(
            content,
            text="These protections are managed by the engine and cannot be changed here.",
            font=ctk.CTkFont(size=11), text_color=MUTED, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        guard_card = SectionCard(content)
        guard_card.pack(fill="x", pady=(0, 24))
        _guard_row(guard_card.body, "Spread / SL Protection",  "Rejects trades when spread costs too much of the stop loss distance")
        _guard_row(guard_card.body, "Symbol Exposure Guard",   "Caps maximum concurrent risk per symbol")
        _guard_row(guard_card.body, "Rolling Drawdown Guard",  "Pauses trading when recent-window losses exceed the limit")
        _guard_row(guard_card.body, "Minimum R:R Filter",      "Blocks trades below the minimum reward-to-risk ratio")
        _guard_row(guard_card.body, "Cluster Risk Guard",       "Manages correlated-symbol risk budget across groups")

    # ── Formula update ────────────────────────────────────────────────────────

    def _update_formula(self, *_) -> None:
        try:
            limit_pct = float(self._vars["risk.max_daily_loss_percent"].get())
            streak    = int(float(self._vars["risk.max_losing_streak"].get()))
            if streak < 1:
                raise ValueError
            per_trade = limit_pct / streak
            self._lbl_formula.configure(text=f"{per_trade:.2f}%", text_color=GREEN)
            self._lbl_formula_detail.configure(text=f"({limit_pct:.1f}% ÷ {streak} trades)")
        except Exception:
            self._lbl_formula.configure(text="--", text_color=MUTED)
            self._lbl_formula_detail.configure(text="")

    def _on_throttle_changed(self) -> None:
        if not self._var_equity_throttle.get():
            self._throttle_banner.show(
                "Disabling the drawdown throttle keeps position sizes at full risk through "
                "losing stretches.", "warn",
            )
        else:
            self._throttle_banner.hide()

    def _on_hedging_changed(self) -> None:
        if not self._var_no_hedging.get():
            self._hedge_banner.show(
                "Disabling hedge protection may allow opposite positions on the same symbol. "
                "This increases risk.", "warn",
            )
        else:
            self._hedge_banner.hide()

    # ── Load / Save ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        cfg  = self.app.config.load(force=True)
        risk = cfg.get("risk", {})
        _FIELD_MAP = {
            "risk.max_daily_loss_percent":      ("max_daily_loss_percent",      "2.5"),
            "risk.max_losing_streak":           ("max_losing_streak",           "3"),
            "risk.max_profit_drawdown_percent": ("max_profit_drawdown_percent", "2.0"),
            "risk.max_lot_size":                ("max_lot_size",                "100.0"),
        }
        for key, (field, default) in _FIELD_MAP.items():
            if key in self._vars:
                v = risk.get(field)
                self._vars[key].set(str(v) if v is not None else default)

        self._var_no_hedging.set(bool(risk.get("no_hedging", True)))
        throttle_cfg = risk.get("equity_throttle") or {}
        self._var_equity_throttle.set(bool(throttle_cfg.get("enabled", True)))
        self._update_formula()

    def on_navigate_to(self) -> None:
        self._load()

    def _save(self) -> None:
        _WRITE_MAP = {
            "risk.max_daily_loss_percent":      ("max_daily_loss_percent",      float),
            "risk.max_losing_streak":           ("max_losing_streak",           int),
            "risk.max_profit_drawdown_percent": ("max_profit_drawdown_percent", float),
            "risk.max_lot_size":                ("max_lot_size",                float),
        }
        errors: list[str] = []
        updates: dict     = {}

        for key, (field, typ) in _WRITE_MAP.items():
            if key not in self._vars:
                continue
            raw = self._vars[key].get().strip()
            if not raw:
                continue
            try:
                updates[field] = typ(raw)
            except Exception:
                errors.append(f"'{field}' has an invalid value")

        if "max_daily_loss_percent" in updates:
            v = float(updates["max_daily_loss_percent"])
            if v <= 0 or v > 20:
                errors.append("Daily Loss Limit must be between 0.5% and 20%")
        if "max_losing_streak" in updates:
            v = int(updates["max_losing_streak"])
            if v < 1 or v > 10:
                errors.append("Max Losing Streak must be between 1 and 10")
        if "max_profit_drawdown_percent" in updates:
            v = float(updates["max_profit_drawdown_percent"])
            if v <= 0 or v > 50:
                errors.append("Max Profit Drawdown must be between 0 and 50%")
        if "max_lot_size" in updates:
            v = float(updates["max_lot_size"])
            if v <= 0:
                errors.append("Max Lot Size must be greater than 0")

        if errors:
            self._save_banner.show("  |  ".join(errors), "warn")
            return

        updates["no_hedging"] = self._var_no_hedging.get()
        updates["equity_throttle"] = {"enabled": self._var_equity_throttle.get()}

        err = self.app.config.update("risk", updates)
        if err:
            self._save_banner.show(err, "danger")
            return

        self._save_banner.show(
            "Risk settings saved. New agents will use these defaults.",
            "good", auto_dismiss_after_ms=4000,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _risk_field(
    parent: tk.Widget, label: str, key: str, unit: str, tip: str,
    vars_dict: dict[str, tk.StringVar], on_change=None,
) -> None:
    from manager.gui.theme import TEXT, MUTED
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=(0, 12))
    left = ctk.CTkFrame(row, fg_color="transparent")
    left.pack(side="left", fill="x", expand=True)
    ctk.CTkLabel(left, text=label, anchor="w", font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(left, text=tip, anchor="w", font=ctk.CTkFont(size=11), text_color=MUTED,
                 justify="left", wraplength=420).pack(anchor="w", pady=(1, 0))
    right = ctk.CTkFrame(row, fg_color="transparent")
    right.pack(side="right", padx=(16, 0))
    var = tk.StringVar()
    if on_change:
        var.trace_add("write", on_change)
    vars_dict[key] = var
    ctk.CTkEntry(right, textvariable=var, width=80,
                 font=ctk.CTkFont(family="Consolas", size=13), justify="center").pack(side="left")
    ctk.CTkLabel(right, text=unit, font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left", padx=(6, 0))


def _guard_row(parent: tk.Widget, label: str, detail: str) -> None:
    from manager.gui.theme import GREEN, MUTED, TEXT
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=5)
    ctk.CTkLabel(row, text="✓", width=20,
                 font=ctk.CTkFont(size=12, weight="bold"), text_color=GREEN).pack(side="left")
    col = ctk.CTkFrame(row, fg_color="transparent")
    col.pack(side="left", fill="x", expand=True, padx=(6, 0))
    ctk.CTkLabel(col, text=label, anchor="w", font=ctk.CTkFont(size=12), text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(col, text=detail, anchor="w", font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w")
    ctk.CTkLabel(row, text="Engine managed", font=ctk.CTkFont(size=10), text_color=MUTED).pack(side="right")
