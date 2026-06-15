"""
src/gui/pages/agent_dashboard.py — Per-agent control panel.

Navigated to via app.navigate("AgentDashboard") when the user clicks
"Open" on an agent card in the fleet view.  Connects to the selected
agent's UIBridge WebSocket (monitoring_port) and shows live state:
  - Balance / Equity / Open trades / Daily P&L KPI cards
  - Open positions table with close controls
  - Pause / Resume trading commands
  - Stop agent shortcut
"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE, SURFACE_RAISED, LINE, LINE_STRONG,
    DANGER_BG, DANGER_BORDER, INFO_BG, INFO_BORDER,
    SUCCESS_BG, SUCCESS_BORDER, WARNING_BG, WARNING_BORDER,
    KpiCard,
)
from src.gui.ws_client import WSClient

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI


class AgentDashboardPage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app         = app
        self._ws:        WSClient | None = None
        self._agent_id:  str | None = None
        self._agent_name: str = "Agent"
        self._paused     = False
        self._connected  = False
        self._trades:    dict[str, str] = {}   # trade_id → treeview iid
        self._queue:     queue.Queue = queue.Queue()
        self._build()
        app.manager_state.subscribe(
            "agent_selected",
            lambda agent_id, monitoring_port:
                self.after(0, lambda: self._connect(agent_id, monitoring_port)),
        )
        self.after(50, self._drain)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=BASE, height=52, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkButton(
            hdr, text="← Agents", width=90, height=30,
            fg_color="transparent", hover_color=LINE_STRONG,
            text_color=MUTED, font=ctk.CTkFont(size=12),
            command=lambda: self.app.navigate("Agents"),
        ).pack(side="left", padx=16, pady=11)

        self._title_lbl = ctk.CTkLabel(
            hdr, text="Agent",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT,
        )
        self._title_lbl.pack(side="left")

        # Connection status dots
        dots = ctk.CTkFrame(hdr, fg_color="transparent")
        dots.pack(side="right", padx=16)
        self._dot_ws  = _Dot(dots, "UIBridge", MUTED)
        self._dot_mt5 = _Dot(dots, "MT5",      MUTED)
        self._dot_gw  = _Dot(dots, "Gateway",  MUTED)

        # ── Connecting / disconnected banner ──────────────────────────────────
        self._conn_banner = ctk.CTkFrame(self, fg_color=INFO_BG, corner_radius=0,
                                         border_width=1, border_color=INFO_BORDER)
        self._conn_lbl = ctk.CTkLabel(
            self._conn_banner,
            text="◌  Connecting to agent…",
            font=ctk.CTkFont(size=12), text_color=INFO, anchor="w",
        )
        self._conn_lbl.pack(side="left", padx=14, pady=8)
        self._conn_banner.pack(fill="x")

        # ── Scrollable body ───────────────────────────────────────────────────
        body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=0, pady=0)

        inner = ctk.CTkFrame(body, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=12)

        # ── KPI cards ─────────────────────────────────────────────────────────
        kpi_row = ctk.CTkFrame(inner, fg_color="transparent")
        kpi_row.pack(fill="x", pady=(0, 10))
        for i in range(4):
            kpi_row.grid_columnconfigure(i, weight=1, uniform="kpi")

        self._kpi_balance  = KpiCard(kpi_row, label="Balance",     value="--")
        self._kpi_equity   = KpiCard(kpi_row, label="Equity",      value="--")
        self._kpi_pnl      = KpiCard(kpi_row, label="Daily P&L",   value="--")
        self._kpi_trades   = KpiCard(kpi_row, label="Open Trades", value="--")
        for i, card in enumerate([self._kpi_balance, self._kpi_equity,
                                   self._kpi_pnl, self._kpi_trades]):
            card.grid(row=0, column=i, sticky="nsew", padx=4)

        # ── Action buttons ────────────────────────────────────────────────────
        act = ctk.CTkFrame(inner, fg_color="transparent")
        act.pack(fill="x", pady=(0, 10))

        self._btn_pause = ctk.CTkButton(
            act, text="⏸  Pause Trading", width=148, height=34,
            fg_color=WARNING_BG, hover_color=WARNING_BORDER,
            border_width=1, border_color=WARNING_BORDER, text_color=YELLOW,
            font=ctk.CTkFont(size=12),
            command=self._toggle_pause,
        )
        self._btn_pause.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            act, text="⚡  Close All", width=110, height=34,
            fg_color=DANGER_BG, hover_color=DANGER_BORDER,
            border_width=1, border_color=DANGER_BORDER, text_color=RED,
            font=ctk.CTkFont(size=12),
            command=self._emergency_close,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            act, text="■  Stop Agent", width=120, height=34,
            fg_color="transparent", hover_color=DANGER_BG,
            border_width=1, border_color=DANGER_BORDER, text_color=RED,
            font=ctk.CTkFont(size=12),
            command=self._stop_agent,
        ).pack(side="left")

        # ── Positions table ───────────────────────────────────────────────────
        pos_card = ctk.CTkFrame(
            inner, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        pos_card.pack(fill="both", expand=True, pady=(0, 8))

        pos_hdr = ctk.CTkFrame(pos_card, fg_color="transparent")
        pos_hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            pos_hdr, text="OPEN POSITIONS",
            font=ctk.CTkFont(size=10, weight="bold"), text_color=MUTED,
        ).pack(side="left")
        ctk.CTkButton(
            pos_hdr, text="Close Selected", width=120, height=26,
            fg_color=DANGER_BG, hover_color=DANGER_BORDER,
            border_width=1, border_color=DANGER_BORDER, text_color=RED,
            font=ctk.CTkFont(size=11),
            command=self._close_selected,
        ).pack(side="right", padx=4)

        self._style_tree()
        tree_wrap = ctk.CTkFrame(pos_card, fg_color=SURFACE_RAISED, corner_radius=4)
        tree_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        cols = ("id", "symbol", "side", "entry", "sl", "tp1", "tp2", "lots", "state")
        self._tree = ttk.Treeview(
            tree_wrap, columns=cols, show="headings",
            style="AgentDash.Treeview", height=10,
        )
        col_cfg = {
            "id":     ("Trade ID", 90), "symbol": ("Symbol", 72),
            "side":   ("Side",     52), "entry":  ("Entry",  82),
            "sl":     ("SL",       82), "tp1":    ("TP1",    82),
            "tp2":    ("TP2",      82), "lots":   ("Lots",   56),
            "state":  ("State",    76),
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

    # ── Connection management ─────────────────────────────────────────────────

    def _connect(self, agent_id: str, monitoring_port: int) -> None:
        if self._ws:
            self._ws.stop()
            self._ws = None

        self._agent_id = agent_id
        self._connected = False
        self._trades.clear()
        self._tree.delete(*self._tree.get_children())
        self._reset_kpis()

        state = self.app.manager_state.get_agent(agent_id)
        name  = (state.display_name or agent_id) if state else agent_id
        self._agent_name = name
        self._title_lbl.configure(text=name)
        self._dot_ws.set("Connecting…", YELLOW)
        self._dot_mt5.set("--", MUTED)
        self._dot_gw.set("--", MUTED)
        self._conn_lbl.configure(text="◌  Connecting to agent…", text_color=INFO)
        self._conn_banner.configure(fg_color=INFO_BG, border_color=INFO_BORDER)
        self._conn_banner.pack(fill="x")

        url = f"ws://127.0.0.1:{monitoring_port}"
        self._ws = WSClient(
            url,
            on_message=lambda msg: self._queue.put(msg),
            on_connect=lambda: self._queue.put({"type": "__connected__"}),
            on_disconnect=lambda: self._queue.put({"type": "__disconnected__"}),
        )
        self._ws.start()

    # ── Message drain (runs on Tk main thread via after()) ────────────────────

    def _drain(self) -> None:
        try:
            while True:
                self._handle(self._queue.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._drain)

    def _handle(self, msg: dict) -> None:
        t = msg.get("type", "")

        if t == "__connected__":
            self._connected = True
            self._dot_ws.set("Live", GREEN)
            self._conn_banner.pack_forget()

        elif t == "__disconnected__":
            self._connected = False
            self._dot_ws.set("Offline", MUTED)
            self._dot_mt5.set("--", MUTED)
            self._dot_gw.set("--", MUTED)
            self._conn_lbl.configure(
                text="○  Agent disconnected — waiting for reconnect…",
                text_color=YELLOW,
            )
            self._conn_banner.configure(fg_color=WARNING_BG, border_color=WARNING_BORDER)
            self._conn_banner.pack(fill="x")

        elif t == "STATE_SNAPSHOT":
            self._on_snapshot(msg.get("payload", {}))

        elif t == "METRICS_UPDATE":
            self._update_metrics(msg.get("payload", {}))

        elif t == "TRADE_EVENT":
            self._on_trade_event(msg.get("payload", {}))

    # ── Snapshot / metrics ────────────────────────────────────────────────────

    def _on_snapshot(self, snap: dict) -> None:
        self._update_metrics(snap.get("metrics", {}))

        self._tree.delete(*self._tree.get_children())
        self._trades.clear()
        for trade in snap.get("trades", []):
            self._insert_trade(trade)

        engine = snap.get("engine", {})
        self._paused = bool(engine.get("is_paused", False))
        self._sync_pause_btn()

        mt5_ok = snap.get("connected", False)
        gw_ok  = snap.get("gateway_connected", False)
        self._dot_mt5.set("Connected" if mt5_ok else "Connecting…",
                          GREEN if mt5_ok else YELLOW)
        self._dot_gw.set("Live" if gw_ok else "Offline",
                         GREEN if gw_ok else MUTED)

    def _update_metrics(self, m: dict) -> None:
        currency = m.get("currency", "")

        def _money(v) -> str:
            if v is None:
                return "--"
            prefix = f"{currency} " if currency else ""
            return f"{prefix}{v:,.2f}"

        self._kpi_balance.set(_money(m.get("balance", m.get("current_balance"))))
        self._kpi_equity.set(_money(m.get("equity")))

        pnl = m.get("daily_pnl")
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            self._kpi_pnl.set(f"{sign}{pnl:,.2f}", tone="good" if pnl >= 0 else "danger")
        else:
            self._kpi_pnl.set("--")

        opens = m.get("open_trades")
        self._kpi_trades.set(str(opens) if opens is not None else "--")

    def _on_trade_event(self, payload: dict) -> None:
        event_type = payload.get("event_type", payload.get("type", ""))
        data       = payload.get("data", payload)

        if event_type == "trade.opened":
            self._insert_trade(data)
        elif event_type in ("trade.tp2_hit", "trade.sl_hit", "trade.closed"):
            tid = data.get("trade_id", "")
            if tid in self._trades:
                try:
                    self._tree.delete(self._trades.pop(tid))
                except Exception:
                    pass
        elif event_type == "trade.tp1_hit":
            tid = data.get("trade_id", "")
            if tid in self._trades:
                self._tree.item(self._trades[tid], tags=("tp1",))

    # ── Positions table helpers ───────────────────────────────────────────────

    def _insert_trade(self, t: dict) -> None:
        tid  = t.get("id", "")
        side = str(t.get("side", "")).upper()

        def _fmt(v) -> str:
            try:
                return f"{float(v):.4f}"
            except Exception:
                return str(v) if v else "--"

        iid = self._tree.insert(
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
            self._trades[tid] = iid

    def _reset_kpis(self) -> None:
        for card in (self._kpi_balance, self._kpi_equity,
                     self._kpi_pnl, self._kpi_trades):
            card.set("--")

    # ── Button actions ────────────────────────────────────────────────────────

    def _toggle_pause(self) -> None:
        ws = self._ws
        if not ws or not self._connected:
            return
        if self._paused:
            ws.send("cmd.resume", {})
        else:
            ws.send("cmd.pause", {})

    def _emergency_close(self) -> None:
        ws = self._ws
        if ws and self._connected:
            ws.send("cmd.emergency_stop", {})

    def _close_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        trade_id = self._tree.item(sel[0], "values")[0]
        ws = self._ws
        if ws and self._connected:
            ws.send("cmd.close_trade", {"trade_id": trade_id})

    def _stop_agent(self) -> None:
        if self._agent_id:
            self.app.manager_client.submit_operation(self._agent_id, "stop")

    def _sync_pause_btn(self) -> None:
        if self._paused:
            self._btn_pause.configure(
                text="▶  Resume Trading",
                fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
                border_color=SUCCESS_BORDER, text_color=GREEN,
            )
        else:
            self._btn_pause.configure(
                text="⏸  Pause Trading",
                fg_color=WARNING_BG, hover_color=WARNING_BORDER,
                border_color=WARNING_BORDER, text_color=YELLOW,
            )

    # ── Treeview style ────────────────────────────────────────────────────────

    @staticmethod
    def _style_tree() -> None:
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(
            "AgentDash.Treeview",
            background=SURFACE_RAISED, foreground=TEXT_SOFT,
            fieldbackground=SURFACE_RAISED, rowheight=26,
            font=("Consolas", 11),
        )
        s.configure(
            "AgentDash.Treeview.Heading",
            background=LINE_STRONG, foreground=MUTED,
            font=("Consolas", 11, "bold"), relief="flat",
        )
        s.map("AgentDash.Treeview", background=[("selected", INFO_BG)])


# ── Helpers ───────────────────────────────────────────────────────────────────

class _Dot(ctk.CTkLabel):
    def __init__(self, parent: tk.Widget, name: str, color: str) -> None:
        super().__init__(
            parent,
            text=f"●  {name}: --",
            font=ctk.CTkFont(size=11),
            text_color=color,
        )
        self.pack(side="left", padx=10)
        self._name = name

    def set(self, status: str, color: str) -> None:
        self.configure(text=f"●  {self._name}: {status}", text_color=color)
