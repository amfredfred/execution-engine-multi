"""
src/gui/pages/overview.py

Overview page — the home screen.

Two display states:
  Stopped  — big CTA, status summary cards, clear plain-English message.
  Running  — live metric cards + open-positions table + risk guards.

Transitions driven by:
  on_engine_status()  — service poll (every 5 s)
  on_ws_connected()   — UIBridge connection established
  on_ws_disconnected()— UIBridge connection lost
  on_snapshot()       — live STATE_SNAPSHOT from engine
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, MUTED, TEXT, TEXT_SOFT,
    SURFACE_RAISED, LINE, LINE_STRONG,
    DANGER_BG, SUCCESS_BG, SUCCESS_BORDER, WARNING_BG,
    INFO_BG,
    KpiCard, page_header,
)

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI


# ─────────────────────────────────────────────────────────────────────────────
# Overview page
# ─────────────────────────────────────────────────────────────────────────────

class OverviewPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.app = app
        self._ws_connected    = False
        self._engine_running  = False
        self._paused          = False
        self._trades: dict[str, str] = {}   # trade_id → treeview iid
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._build_header()

        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="both", expand=True)
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

        self._build_stopped_view()
        self._build_running_view()
        self._show_stopped_view()

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, height=52, fg_color=SURFACE_RAISED, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Left accent strip
        ctk.CTkFrame(hdr, width=3, fg_color=GREEN, corner_radius=0).pack(
            side="left", fill="y",
        )

        ctk.CTkLabel(
            hdr, text="Overview",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).pack(side="left", padx=(14, 0))

        status_row = ctk.CTkFrame(hdr, fg_color="transparent")
        status_row.pack(side="right", padx=16)

        self._dot_engine  = _StatusDot(status_row, "AQ Agent",  MUTED)
        self._dot_mt5     = _StatusDot(status_row, "MT5",     MUTED)
        self._dot_gateway = _StatusDot(status_row, "Gateway", MUTED)

    def _build_stopped_view(self) -> None:
        self._stopped_frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._stopped_frame.grid(row=0, column=0, sticky="nsew")

        # Vertical centering via an outer frame
        outer = ctk.CTkFrame(self._stopped_frame, fg_color="transparent")
        outer.place(relx=0.5, rely=0.40, anchor="center")

        ctk.CTkLabel(
            outer, text="⬡",
            font=ctk.CTkFont(size=60),
            text_color=LINE_STRONG,
        ).pack()

        self._stopped_title = ctk.CTkLabel(
            outer, text="AQ Agent Stopped",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=RED,
        )
        self._stopped_title.pack(pady=(6, 4))

        self._stopped_msg = ctk.CTkLabel(
            outer,
            text="AQ Agent is not running.\nStart it to begin automated trading.",
            font=ctk.CTkFont(size=13),
            text_color=MUTED,
            justify="center",
        )
        self._stopped_msg.pack(pady=(0, 22))

        # Primary CTA — Start Engine
        self._btn_start = ctk.CTkButton(
            outer,
            text="▶   Start AQ Agent",
            width=210, height=48,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER,
            text_color=GREEN,
            command=self._start_engine,
        )
        self._btn_start.pack(pady=4)

        # Install button (shown only when service not installed)
        self._btn_install = ctk.CTkButton(
            outer,
            text="Install Service",
            width=210, height=36,
            font=ctk.CTkFont(size=12),
            fg_color="#3a1a00", hover_color="#5a3000",
            command=self._install_service,
        )
        # Not packed by default

        self._stopped_detail = ctk.CTkLabel(
            outer, text="",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self._stopped_detail.pack(pady=(8, 0))

        # Mini status cards at the bottom of the stopped view
        cards_area = ctk.CTkFrame(self._stopped_frame, fg_color="transparent")
        cards_area.place(relx=0.5, rely=0.82, anchor="center")

        self._mini_cards: dict[str, ctk.CTkLabel] = {}
        for label in ("Engine", "MT5", "Gateway"):
            card = ctk.CTkFrame(
                cards_area, corner_radius=8,
                fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
                width=130, height=64,
            )
            card.pack(side="left", padx=10)
            card.pack_propagate(False)
            ctk.CTkLabel(
                card,
                text=label.upper(),
                font=ctk.CTkFont(size=9, weight="bold"),
                text_color=MUTED,
            ).place(relx=0.5, rely=0.28, anchor="center")
            val = ctk.CTkLabel(
                card, text="--",
                font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
                text_color=MUTED,
            )
            val.place(relx=0.5, rely=0.68, anchor="center")
            self._mini_cards[label] = val

    def _build_running_view(self) -> None:
        self._running_frame = ctk.CTkFrame(self._content, fg_color="transparent")
        # Not gridded yet — shown on WS connect

        # ── KPI metric cards ──────────────────────────────────────────────────
        cards_row = ctk.CTkFrame(self._running_frame, fg_color="transparent")
        cards_row.pack(fill="x", padx=16, pady=(12, 6))

        # configure 5 equal columns
        for i in range(5):
            cards_row.grid_columnconfigure(i, weight=1, uniform="kpi")

        self._mc_balance  = KpiCard(cards_row, label="Balance",     value="--")
        self._mc_equity   = KpiCard(cards_row, label="Equity",      value="--")
        self._mc_pnl      = KpiCard(cards_row, label="Daily P&L",   value="--")
        self._mc_drawdown = KpiCard(cards_row, label="Drawdown",    value="--")
        self._mc_open     = KpiCard(cards_row, label="Open Trades", value="--")

        for i, card in enumerate(
            [self._mc_balance, self._mc_equity, self._mc_pnl, self._mc_drawdown, self._mc_open]
        ):
            card.grid(row=0, column=i, sticky="nsew", padx=4, pady=0)

        # ── Engine action buttons ─────────────────────────────────────────────
        act_row = ctk.CTkFrame(self._running_frame, fg_color="transparent")
        act_row.pack(fill="x", padx=16, pady=(6, 4))

        ctk.CTkButton(
            act_row, text="■  Stop AQ Agent", width=140, height=32,
            fg_color=DANGER_BG, hover_color="#5a1e2a",
            border_width=1, border_color="#38141e",
            text_color=RED,
            command=lambda: self.app.svc.stop(),
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            act_row, text="↺  Restart", width=100, height=32,
            fg_color=INFO_BG, hover_color="#253850",
            border_width=1, border_color="#1d2c42",
            text_color="#8ab4ff",
            command=lambda: self.app.svc.restart(),
        ).pack(side="left", padx=(0, 6))

        self._btn_pause = ctk.CTkButton(
            act_row, text="⏸  Pause Trading", width=148, height=32,
            fg_color=WARNING_BG, hover_color="#2a2210",
            border_width=1, border_color="#382d14",
            text_color=YELLOW,
            command=self._toggle_pause,
        )
        self._btn_pause.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            act_row, text="⚡  Close All",  width=110, height=32,
            fg_color="#1d0d10", hover_color="#381220",
            border_width=1, border_color="#38141e",
            text_color=RED,
            command=self._emergency_close,
        ).pack(side="left")

        # ── Error/info banner ─────────────────────────────────────────────────
        self._banner = ctk.CTkFrame(
            self._running_frame,
            fg_color=DANGER_BG, corner_radius=6, height=34,
            border_width=1, border_color="#38141e",
        )
        self._banner.pack_propagate(False)

        ctk.CTkLabel(
            self._banner, text="⚠",
            font=ctk.CTkFont(size=13), text_color=RED,
        ).pack(side="left", padx=(10, 4))

        self._banner_lbl = ctk.CTkLabel(
            self._banner, text="",
            font=ctk.CTkFont(size=11), text_color="#ffb3bd", anchor="w",
        )
        self._banner_lbl.pack(side="left", fill="x", expand=True)

        # ── Positions table ───────────────────────────────────────────────────
        pos_outer = ctk.CTkFrame(
            self._running_frame,
            corner_radius=8, fg_color=SURFACE_RAISED,
            border_width=1, border_color=LINE,
        )
        pos_outer.pack(fill="both", expand=True, padx=16, pady=(4, 4))

        pos_hdr = ctk.CTkFrame(pos_outer, fg_color="transparent")
        pos_hdr.pack(fill="x", padx=12, pady=(10, 4))

        ctk.CTkLabel(
            pos_hdr, text="OPEN POSITIONS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=MUTED,
        ).pack(side="left")

        ctk.CTkButton(
            pos_hdr, text="Close Selected", width=120, height=26,
            fg_color=DANGER_BG, hover_color="#5a1e2a",
            border_width=1, border_color="#38141e",
            text_color=RED,
            command=self._close_selected,
        ).pack(side="right", padx=4)

        self._style_treeview()
        tree_wrap = ctk.CTkFrame(pos_outer, fg_color=SURFACE_RAISED, corner_radius=4)
        tree_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        cols = ("id", "symbol", "side", "entry", "sl", "tp1", "tp2", "lots", "state")
        self._tree = ttk.Treeview(
            tree_wrap, columns=cols, show="headings",
            style="Overview.Treeview", height=8,
        )
        col_cfg = {
            "id":    ("Trade ID", 90), "symbol": ("Symbol", 72),
            "side":  ("Side",     52), "entry":  ("Entry",  82),
            "sl":    ("SL",       82), "tp1":    ("TP1",    82),
            "tp2":   ("TP2",      82), "lots":   ("Lots",   56),
            "state": ("State",    76),
        }
        for col, (lbl, w) in col_cfg.items():
            self._tree.heading(col, text=lbl)
            self._tree.column(col, width=w, anchor="center", minwidth=40)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb.pack(side="right", fill="y", pady=4)
        self._tree.tag_configure("BUY",  foreground=GREEN)
        self._tree.tag_configure("SELL", foreground=RED)
        self._tree.tag_configure("tp1",  foreground=YELLOW)

        # ── Risk guards ───────────────────────────────────────────────────────
        guards_row = ctk.CTkFrame(self._running_frame, fg_color="transparent")
        guards_row.pack(fill="x", padx=16, pady=(2, 10))

        ctk.CTkLabel(
            guards_row, text="RISK GUARDS",
            font=ctk.CTkFont(size=10, weight="bold"), text_color=MUTED,
        ).pack(side="left", padx=(0, 8))

        self._guard_lbls: list[ctk.CTkLabel] = []
        for _ in range(3):
            lbl = ctk.CTkLabel(
                guards_row, text="--",
                font=ctk.CTkFont(size=11), text_color=MUTED,
            )
            lbl.pack(side="left", padx=10)
            self._guard_lbls.append(lbl)

    # ── View switching ────────────────────────────────────────────────────────

    def _show_stopped_view(self) -> None:
        self._running_frame.grid_remove()
        self._stopped_frame.grid(row=0, column=0, sticky="nsew")

    def _show_running_view(self) -> None:
        self._stopped_frame.grid_remove()
        self._running_frame.grid(row=0, column=0, sticky="nsew")

    # ── Callbacks from app.py ─────────────────────────────────────────────────

    def on_engine_status(self, status: str, detail: str | None) -> None:
        from src.gui.service_controller import ServiceStatus

        _colours = {
            ServiceStatus.NOT_INSTALLED: MUTED,
            ServiceStatus.STOPPED:       RED,
            ServiceStatus.STARTING:      YELLOW,
            ServiceStatus.RUNNING:       GREEN,
            ServiceStatus.STOPPING:      YELLOW,
            ServiceStatus.UNKNOWN:       MUTED,
        }
        _labels = {
            ServiceStatus.NOT_INSTALLED: "Not Installed",
            ServiceStatus.STOPPED:       "Stopped",
            ServiceStatus.STARTING:      "Starting…",
            ServiceStatus.RUNNING:       "Running",
            ServiceStatus.STOPPING:      "Stopping…",
            ServiceStatus.UNKNOWN:       "Unknown",
        }
        label = _labels.get(status, status)
        color = _colours.get(status, MUTED)

        self._dot_engine.set(label, color)
        self._mini_card_set("AQ Agent", label, color)

        if status == ServiceStatus.NOT_INSTALLED:
            self._show_stopped_view()
            self._stopped_title.configure(text="Not Installed", text_color=YELLOW)
            self._stopped_msg.configure(
                text="AQ Agent has not been installed yet.\n"
                     "Click below to install it automatically.",
            )
            self._btn_start.pack_forget()
            self._btn_install.pack(pady=6)
            self._stopped_detail.configure(text="")

        elif status == ServiceStatus.STOPPED and not self._ws_connected:
            self._show_stopped_view()
            self._stopped_title.configure(text="AQ Agent Stopped", text_color=RED)
            self._stopped_msg.configure(
                text="AQ Agent is not running.\n"
                     "Start it to begin automated trading.",
            )
            self._btn_install.pack_forget()
            self._btn_start.pack(pady=6)
            self._stopped_detail.configure(
                text=detail if detail else "", text_color=MUTED,
            )

        elif status == ServiceStatus.STARTING:
            if not self._ws_connected:
                self._show_stopped_view()
            self._stopped_title.configure(text="AQ Agent Starting…", text_color=YELLOW)
            self._stopped_msg.configure(text="Waiting for AQ Agent to come online…")
            self._btn_install.pack_forget()
            self._btn_start.pack(pady=6)
            self._stopped_detail.configure(text="")

        elif status == ServiceStatus.RUNNING:
            self._engine_running = True
            self._btn_install.pack_forget()
            self._btn_start.pack(pady=6)

        elif status == ServiceStatus.STOPPING:
            self._engine_running = False

    def on_ws_connected(self) -> None:
        self._ws_connected = True
        self._dot_gateway.set("Live", GREEN)
        self._mini_card_set("Gateway", "Live", GREEN)
        self._show_running_view()
        self._hide_banner()

    def on_ws_disconnected(self) -> None:
        self._ws_connected = False
        self._dot_gateway.set("Offline", MUTED)
        self._dot_mt5.set("--", MUTED)
        self._mini_card_set("Gateway", "Offline", MUTED)
        self._mini_card_set("MT5",     "--",      MUTED)
        # Don't force-switch to stopped view here — service poll handles that

    def on_snapshot(self, snap: dict) -> None:
        self._update_metrics(snap.get("metrics", {}))

        # Positions
        self._tree.delete(*self._tree.get_children())
        self._trades.clear()
        for trade in snap.get("trades", []):
            self._insert_trade(trade)

        for guard in snap.get("riskGuards", []):
            self._update_guard(guard)

        engine = snap.get("engine", {})
        self._paused = bool(engine.get("is_paused", False))
        self._btn_pause.configure(
            text="▶  Resume Trading" if self._paused else "⏸  Pause Trading",
            fg_color=SUCCESS_BG if self._paused else WARNING_BG,
            text_color=GREEN if self._paused else YELLOW,
        )

        mt5_ok = snap.get("connected", False)
        if mt5_ok:
            self._dot_mt5.set("Connected", GREEN)
            self._mini_card_set("MT5", "Connected", GREEN)
            self._hide_banner()
        else:
            self._dot_mt5.set("Connecting…", YELLOW)
            self._mini_card_set("MT5", "Connecting…", YELLOW)

    def on_metrics(self, m: dict) -> None:
        self._update_metrics(m)

    def on_trade_event(self, event_type: str, payload: dict) -> None:
        if event_type == "trade.opened":
            self._insert_trade(payload)
        elif event_type in ("trade.tp2_hit", "trade.sl_hit", "trade.closed"):
            tid = payload.get("trade_id", "")
            if tid in self._trades:
                try:
                    self._tree.delete(self._trades.pop(tid))
                except Exception:
                    pass
        elif event_type == "trade.tp1_hit":
            tid = payload.get("trade_id", "")
            if tid in self._trades:
                self._tree.item(self._trades[tid], tags=("tp1",))

    def on_mt5_error(self, message: str) -> None:
        if ":" in message:
            message = message.split(":", 1)[-1].strip()
        self._dot_mt5.set("Error", RED)
        self._mini_card_set("MT5", "Error", RED)
        self._show_banner(f"MT5: {message}")

    # ── Button actions ────────────────────────────────────────────────────────

    def _start_engine(self) -> None:
        from src.gui.service_controller import ServiceStatus
        if self.app.svc.query() == ServiceStatus.NOT_INSTALLED:
            self._install_service()
            return
        self._stopped_title.configure(text="AQ Agent Starting…", text_color=YELLOW)
        self._stopped_msg.configure(text="Waiting for AQ Agent to come online…")
        self._stopped_detail.configure(text="")
        self.app.svc.start()

    def _install_service(self) -> None:
        self._stopped_msg.configure(text="Installing AQ Agent… please wait.")
        self._stopped_detail.configure(text="")
        self.app.svc.install(self.app.config_path)

    def _toggle_pause(self) -> None:
        if self._paused:
            self.app.send_command("cmd.resume", {})
        else:
            self.app.send_command("cmd.pause", {})

    def _close_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        trade_id = self._tree.item(sel[0], "values")[0]
        self.app.send_command("cmd.close_trade", {"trade_id": trade_id})

    def _emergency_close(self) -> None:
        self.app.send_command("cmd.emergency_stop", {})

    # ── Banner ────────────────────────────────────────────────────────────────

    def _show_banner(self, msg: str) -> None:
        self._banner_lbl.configure(text=msg)
        self._banner.pack(fill="x", padx=16, pady=(0, 4))

    def _hide_banner(self) -> None:
        self._banner.pack_forget()

    # ── Mini-card helper ──────────────────────────────────────────────────────

    def _mini_card_set(self, name: str, text: str, color: str) -> None:
        lbl = self._mini_cards.get(name)
        if lbl:
            lbl.configure(text=text, text_color=color)

    # ── Live data helpers ─────────────────────────────────────────────────────

    def _update_metrics(self, m: dict) -> None:
        currency = m.get("currency", "")

        def _money(v) -> str:
            if v is None:
                return "--"
            prefix = f"{currency} " if currency else ""
            return f"{prefix}{v:,.2f}"

        self._mc_balance.set(_money(m.get("balance", m.get("current_balance"))))
        self._mc_equity.set(_money(m.get("equity")))

        pnl = m.get("daily_pnl")
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            self._mc_pnl.set(f"{sign}{pnl:,.2f}", tone="good" if pnl >= 0 else "danger")
        else:
            self._mc_pnl.set("--")

        dd = m.get("drawdown_pct")
        if dd is not None:
            tone = "danger" if dd > 1.5 else "warn" if dd > 0.5 else "normal"
            self._mc_drawdown.set(f"{dd:.2f}%", tone=tone)
        else:
            self._mc_drawdown.set("--")

        opens = m.get("open_trades")
        self._mc_open.set(str(opens) if opens is not None else "--")

    def _insert_trade(self, t: dict) -> None:
        tid  = t.get("id", "")
        side = str(t.get("side", "")).upper()

        def _fmt(v) -> str:
            try:
                return f"{float(v):.4f}"
            except Exception:
                return str(v) if v else "--"

        item = self._tree.insert(
            "", "end",
            values=(
                tid, t.get("symbol", ""), side,
                _fmt(t.get("entry_price")), _fmt(t.get("sl")),
                _fmt(t.get("tp1")),        _fmt(t.get("tp2")),
                t.get("lots", ""),         t.get("state", ""),
            ),
            tags=(side,),
        )
        if tid:
            self._trades[tid] = item

    def _update_guard(self, guard: dict) -> None:
        idx_map = {"guard1": 0, "guard2": 1, "guard3": 2}
        idx = idx_map.get(guard.get("id", ""), -1)
        if idx < 0 or idx >= len(self._guard_lbls):
            return
        status    = guard.get("status", "ACTIVE")
        current   = guard.get("current_value", 0.0)
        threshold = guard.get("threshold", 0.0)
        unit      = guard.get("unit", "")
        name      = guard.get("name", "")

        if status == "PAUSED":
            color = RED
        elif status == "DISABLED":
            color = MUTED
        elif threshold and current >= threshold * 0.75:
            color = YELLOW
        else:
            color = GREEN

        self._guard_lbls[idx].configure(
            text=f"{name}: {current:.2f}/{threshold}{unit}",
            text_color=color,
        )

    @staticmethod
    def _style_treeview() -> None:
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(
            "Overview.Treeview",
            background=SURFACE_RAISED, foreground=TEXT_SOFT,
            fieldbackground=SURFACE_RAISED, rowheight=26,
            font=("Consolas", 11),
        )
        s.configure(
            "Overview.Treeview.Heading",
            background=LINE_STRONG, foreground=MUTED,
            font=("Consolas", 11, "bold"), relief="flat",
        )
        s.map("Overview.Treeview", background=[("selected", INFO_BG)])


# ─────────────────────────────────────────────────────────────────────────────
# Shared widgets
# ─────────────────────────────────────────────────────────────────────────────

class _StatusDot(ctk.CTkLabel):
    def __init__(self, parent: tk.Widget, name: str, color: str) -> None:
        super().__init__(
            parent,
            text=f"●  {name}: --",
            font=ctk.CTkFont(size=12),
            text_color=color,
        )
        self.pack(side="left", padx=12)
        self._name = name

    def set(self, status: str, color: str) -> None:
        self.configure(text=f"●  {self._name}: {status}", text_color=color)
