"""
src/gui/pages/home.py — Home / Dashboard

Shows:
  • Engine status hero card (lifecycle + description)
  • Readiness checklist (items that need fixing before trading)
  • Account summary (balance, equity, daily P&L, open trades)
  • Active risk guards
"""
from __future__ import annotations

import time
import tkinter as tk
from typing import TYPE_CHECKING, Optional

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE, SURFACE_RAISED, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER, DANGER_BG, DANGER_BORDER,
    WARNING_BG, WARNING_BORDER, INFO_BG, INFO_BORDER,
    section_rule, page_header,
)
from src.gui.components import (
    StatusCard, ActionBanner, ReadinessPanel,
    PrimaryButton, SectionCard, InfoTable,
    EngineStatusBadge,
)
from src.gui.state import EngineLifecycle

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI
    from src.gui.state import AccountState


# ─────────────────────────────────────────────────────────────────────────────

class HomePage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._readiness_pending: bool = False   # debounce flag
        self._build()
        self._subscribe()
        # Sync labels with whatever lifecycle is already known at build time.
        # Use after(50) so the widget is fully realised before configure() calls.
        self.after(50, lambda: self._on_engine(
            lifecycle=self.app.app_state.lifecycle,
        ))

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Home", "System overview and readiness")
        # Scrollable body below fixed header
        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self._body.pack(fill="both", expand=True)

        # Two-column layout: left = engine + readiness, right = account
        body = ctk.CTkFrame(self._body, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=16)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left  = ctk.CTkFrame(body, fg_color="transparent")
        right = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right.grid(row=0, column=1, sticky="nsew")

        self._build_engine_card(left)
        self._build_readiness(left)
        self._build_account_card(right)
        self._build_risk_guards(right)

    # ── Engine status card ────────────────────────────────────────────────────

    def _build_engine_card(self, parent: tk.Widget) -> None:
        section_rule(parent, "AQ AGENT STATUS").pack(fill="x", pady=(0, 8))

        card = ctk.CTkFrame(
            parent, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        card.pack(fill="x", pady=(0, 16))

        # Accent bar (colour changes with lifecycle)
        self._engine_accent = ctk.CTkFrame(card, height=3, fg_color=MUTED, corner_radius=0)
        self._engine_accent.pack(fill="x")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=14)

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)

        # Use CHECKING copy as initial text — _on_engine() will update immediately
        self._lifecycle_lbl = ctk.CTkLabel(
            left, text=EngineLifecycle.CHECKING.label,
            font=ctk.CTkFont(size=18, weight="bold"), text_color=MUTED, anchor="w",
        )
        self._lifecycle_lbl.pack(anchor="w")

        self._lifecycle_desc = ctk.CTkLabel(
            left, text=EngineLifecycle.CHECKING.description,
            font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w",
            wraplength=340, justify="left",
        )
        self._lifecycle_desc.pack(anchor="w", pady=(4, 0))

        # ── Context-sensitive action buttons ──────────────────────────────────
        btn_col = ctk.CTkFrame(row, fg_color="transparent")
        btn_col.pack(side="right", padx=(8, 0))

        self._btn_start = PrimaryButton(
            btn_col, text="▶  Start", tone="good", width=124, height=34,
            command=self._do_start,
        )
        self._btn_start.pack(pady=2)

        self._btn_stop = PrimaryButton(
            btn_col, text="■  Stop", tone="danger", width=124, height=34,
            command=self._do_stop,
        )
        self._btn_stop.pack(pady=2)

        self._btn_restart = PrimaryButton(
            btn_col, text="↺  Restart", tone="warn", width=124, height=34,
            command=self._do_restart,
        )
        self._btn_restart.pack(pady=2)

        self._btn_install = PrimaryButton(
            btn_col, text="⬇  Install", tone="info", width=124, height=34,
            command=lambda: self.app.navigate("Engine"),
        )
        self._btn_install.pack(pady=2)

        self._btn_logs = PrimaryButton(
            btn_col, text="📋  Logs", tone="normal", width=124, height=34,
            command=lambda: self.app.navigate("Activity"),
        )
        self._btn_logs.pack(pady=2)

        # Initial button state comes from _on_engine() called in __init__
        self._update_engine_buttons(None)

    # ── Readiness checklist ───────────────────────────────────────────────────

    def _build_readiness(self, parent: tk.Widget) -> None:
        section_rule(parent, "SETUP CHECKLIST").pack(fill="x", pady=(0, 8))

        self._readiness_panel = ReadinessPanel(
            parent, navigate_fn=self.app.navigate,
        )
        self._readiness_panel.pack(fill="x")
        self._refresh_readiness()

    def _refresh_readiness(self) -> None:
        # Use cached config — force-reload on every poll is the primary cause
        # of the checklist flicker (every reload triggers a signature change).
        cfg    = self.app.config.load()
        issues = self.app.app_state.get_readiness_issues(cfg)

        # Build the full checklist including done items
        mt5  = cfg.get("mt5", {})
        gw   = cfg.get("gateway", {})
        risk = cfg.get("risk", {})
        svc  = self.app.app_state.service_installed

        all_items = [
            (
                "mt5_path",
                "Trading platform selected",
                mt5.get("path", "").split("\\")[-2]
                    if mt5.get("path") else "No platform selected",
                bool(mt5.get("path")),
                "Select Platform", "Platform",
            ),
            (
                "mt5_credentials",
                "MT5 credentials entered",
                f"{mt5.get('login', '—')} @ {mt5.get('server', '—')}"
                    if mt5.get("login") else "Login and server required",
                bool(mt5.get("login") and mt5.get("server")),
                "Enter Credentials", "Platform",
            ),
            (
                "activation",
                "License key",
                "License active"
                    if gw.get("activation_key")
                    else "Purchase or copy your key from the web dashboard.",
                bool(gw.get("activation_key")),
                "Open Dashboard" if not gw.get("activation_key") else "",
                "__dashboard__"  if not gw.get("activation_key") else "",
            ),
            (
                "risk",
                "Risk profile configured",
                f"Daily limit: {risk.get('max_daily_loss_percent', '—')}%"
                    if risk.get("max_daily_loss_percent") else "Risk limits required",
                bool(risk.get("max_daily_loss_percent")),
                "Set Risk Profile", "Risk",
            ),
            (
                "service",
                "AQ Agent installed",
                "AQ Agent registered"
                    if svc else "AQ Agent must be installed",
                svc,
                "Install AQ Agent", "Engine",
            ),
        ]
        self._readiness_panel.refresh(issues, all_items)

    # ── Account summary card ──────────────────────────────────────────────────

    def _build_account_card(self, parent: tk.Widget) -> None:
        section_rule(parent, "ACCOUNT").pack(fill="x", pady=(0, 8))

        self._acct_card = SectionCard(parent)
        self._acct_card.pack(fill="x", pady=(0, 16))

        self._kpi_balance  = self._kpi(self._acct_card.body, "Balance")
        self._kpi_equity   = self._kpi(self._acct_card.body, "Equity")
        self._kpi_pnl      = self._kpi(self._acct_card.body, "Today's P&L")
        self._kpi_trades   = self._kpi(self._acct_card.body, "Open trades")
        self._kpi_mt5      = self._kpi(self._acct_card.body, "MT5 connection")
        self._kpi_gateway  = self._kpi(self._acct_card.body, "Gateway")

        self._update_account_display()

    def _kpi(self, parent: tk.Widget, label: str) -> ctk.CTkLabel:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(
            row, text=label, width=140, anchor="w",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        ).pack(side="left")
        val = ctk.CTkLabel(
            row, text="Not detected yet", anchor="w",
            font=ctk.CTkFont(family="Consolas", size=12), text_color=TEXT_SOFT,
        )
        val.pack(side="left")
        return val

    def _update_account_display(self) -> None:
        acct     = self.app.app_state.account
        currency = acct.currency or "USD"

        def _fmt_money(v: float | None) -> str:
            if v is None:
                return "Not detected yet"
            return f"{v:,.2f} {currency}"

        self._kpi_balance.configure(
            text=_fmt_money(acct.balance), text_color=TEXT_SOFT,
        )
        self._kpi_equity.configure(
            text=_fmt_money(acct.equity), text_color=TEXT_SOFT,
        )

        if acct.daily_pnl is not None:
            pnl_color = GREEN if acct.daily_pnl >= 0 else RED
            pnl_text  = (
                f"{'+'if acct.daily_pnl>=0 else ''}{acct.daily_pnl:,.2f} {currency}"
            )
        else:
            pnl_color = MUTED
            pnl_text  = "Not detected yet"
        self._kpi_pnl.configure(text=pnl_text, text_color=pnl_color)

        trades_text = str(acct.open_trades) if acct.open_trades else "None"
        self._kpi_trades.configure(
            text=trades_text,
            text_color=TEXT_SOFT if acct.open_trades else MUTED,
        )

        mt5_color = GREEN if acct.mt5_connected else MUTED
        mt5_text  = "Connected" if acct.mt5_connected else "Not connected"
        self._kpi_mt5.configure(text=mt5_text, text_color=mt5_color)

        lc           = self.app.app_state.lifecycle
        gw_connected = lc == EngineLifecycle.RUNNING_CONNECTED
        gw_color     = GREEN if gw_connected else MUTED
        gw_text      = "Connected" if gw_connected else "Not connected"
        self._kpi_gateway.configure(text=gw_text, text_color=gw_color)

    # ── Risk guards ───────────────────────────────────────────────────────────

    def _build_risk_guards(self, parent: tk.Widget) -> None:
        section_rule(parent, "RISK GUARDS").pack(fill="x", pady=(0, 8))

        self._guards_wrap = ctk.CTkFrame(parent, fg_color="transparent")
        self._guards_wrap.pack(fill="x")
        self._refresh_guards([])

    def _refresh_guards(self, guards: list) -> None:
        for w in self._guards_wrap.winfo_children():
            w.destroy()
        if not guards:
            ctk.CTkLabel(
                self._guards_wrap, text="No active risk guards",
                font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w",
            ).pack(anchor="w")
            return
        for g in guards:
            name     = g.get("name", "Unknown guard")
            status   = str(g.get("status", "ACTIVE")).upper()
            triggered = status == "PAUSED"
            disabled  = status == "DISABLED"
            row = ctk.CTkFrame(
                self._guards_wrap,
                fg_color=DANGER_BG if triggered else SURFACE_RAISED,
                border_width=1,
                border_color=DANGER_BORDER if triggered else LINE,
                corner_radius=6,
            )
            row.pack(fill="x", pady=3)
            inner = ctk.CTkFrame(row, fg_color="transparent")
            inner.pack(padx=12, pady=8, fill="x")
            if triggered:
                dot        = "🔴"
                text_color = RED
            elif disabled:
                dot        = "⚪"
                text_color = MUTED
            else:
                dot        = "🟢"
                text_color = GREEN
            ctk.CTkLabel(
                inner, text=f"{dot}  {name}",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=text_color, anchor="w",
            ).pack(anchor="w")

    # ── Pub/sub ───────────────────────────────────────────────────────────────

    def _subscribe(self) -> None:
        s = self.app.app_state
        s.subscribe("engine",  self._on_engine)
        s.subscribe("account", self._on_account)
        s.subscribe("trades",  self._on_trades)
        s.subscribe("setup",   self._on_setup_changed)

    def _on_engine(self, lifecycle: "EngineLifecycle" = None, **_) -> None:
        from src.gui.state import EngineLifecycle as LC
        if lifecycle is None:
            return

        colors = {"good": GREEN, "warn": YELLOW, "danger": RED}
        color  = colors.get(lifecycle.color_key, MUTED)

        self._engine_accent.configure(fg_color=color)
        self._lifecycle_lbl.configure(text=lifecycle.label, text_color=color)
        self._lifecycle_desc.configure(text=lifecycle.description)
        self._update_engine_buttons(lifecycle)
        self._update_account_display()

    def _update_engine_buttons(self, lc: Optional["EngineLifecycle"]) -> None:
        from src.gui.state import EngineLifecycle as LC

        # All hidden by default; reveal only what makes sense for this state
        for btn in (
            self._btn_start, self._btn_stop,
            self._btn_restart, self._btn_install, self._btn_logs,
        ):
            btn.pack_forget()

        if lc is None or lc.is_checking:
            return   # Nothing actionable while checking

        if lc == LC.SERVICE_NOT_INSTALLED or lc == LC.NOT_CONFIGURED:
            self._btn_install.pack(pady=2)
            return

        if lc.can_start:
            self._btn_start.configure(state="normal")
            self._btn_start.pack(pady=2)

        if lc.can_stop:
            self._btn_stop.configure(state="normal")
            self._btn_stop.pack(pady=2)

        if lc == LC.RUNNING_NO_HEARTBEAT:
            self._btn_restart.pack(pady=2)
            self._btn_logs.pack(pady=2)

        if lc == LC.FAILED:
            self._btn_start.configure(state="normal")
            self._btn_start.pack(pady=2)
            self._btn_logs.pack(pady=2)

    def _on_account(self, **_) -> None:
        self._update_account_display()

    def _on_trades(self, **_) -> None:
        self._refresh_guards(self.app.app_state.risk_guards)
        self._update_account_display()

    def _on_setup_changed(self, **_) -> None:
        self._schedule_readiness_refresh()

    # ── Debounced readiness refresh ───────────────────────────────────────────

    _READINESS_DEBOUNCE_MS = 600

    def _schedule_readiness_refresh(self) -> None:
        """Coalesces rapid successive calls into a single repaint."""
        if self._readiness_pending:
            return
        self._readiness_pending = True
        self.after(self._READINESS_DEBOUNCE_MS, self._do_readiness_refresh)

    def _do_readiness_refresh(self) -> None:
        self._readiness_pending = False
        self._refresh_readiness()

    # ── Legacy broadcast handlers ─────────────────────────────────────────────

    def on_engine_status(self, status: str, detail=None) -> None:
        # AppState already updated by app._apply_svc_status before this fires.
        # Just request a debounced readiness repaint — don't call update_service_status
        # again (that would double-apply crash detection logic).
        self._schedule_readiness_refresh()

    def on_snapshot(self, payload: dict) -> None:
        pass  # AppState.apply_snapshot() handles this; we re-render via pub/sub

    def on_ws_connected(self) -> None:
        pass

    def on_ws_disconnected(self) -> None:
        self._update_account_display()

    def on_mt5_error(self, message: str) -> None:
        self._update_account_display()

    # ── Engine control ────────────────────────────────────────────────────────

    def _do_start(self) -> None:
        self._btn_start.configure(state="disabled")
        import threading
        threading.Thread(target=self.app.svc.start, daemon=True).start()

    def _do_stop(self) -> None:
        self._btn_stop.configure(state="disabled")
        import threading
        threading.Thread(target=self.app.svc.stop, daemon=True).start()

    def _do_restart(self) -> None:
        self._btn_restart.configure(state="disabled")
        import threading
        threading.Thread(target=self.app.svc.restart, daemon=True).start()

