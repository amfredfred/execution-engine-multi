"""
src/gui/pages/agents.py — Fleet view: grid of managed agent cards.

Shows all registered agents with live status, account metrics, and
controls (Open / Start / Stop / Remove).  The "Open" button drills into
that agent's full 7-page management panel.

AddAgentDialog collects MT5 credentials and calls POST /agents.
"""
from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, MUTED, INFO,
    TEXT, TEXT_SOFT, BASE, SURFACE, SURFACE_RAISED,
    LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER, WARNING_BG, WARNING_BORDER,
    DANGER_BG, DANGER_BORDER, INFO_BG, INFO_BORDER,
    page_header,
)

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI
    from src.gui.manager_state import AgentCardState

# ── Status → badge tone mapping ───────────────────────────────────────────────
_STATUS_TONE: dict[str, str] = {
    "RUNNING":     "good",
    "STARTING":    "info",
    "STOPPING":    "warn",
    "PROVISIONED": "info",
    "STOPPED":     "normal",
    "CRASH_LOOP":  "danger",
    "ERROR":       "danger",
}
_STATUS_LABEL: dict[str, str] = {
    "RUNNING":     "● RUNNING",
    "STARTING":    "◌ STARTING",
    "STOPPING":    "◌ STOPPING",
    "PROVISIONED": "◌ PROVISIONED",
    "STOPPED":     "○ STOPPED",
    "CRASH_LOOP":  "⚠ CRASH LOOP",
    "ERROR":       "✕ ERROR",
}
_TONE_BG   = {"good": SUCCESS_BG, "warn": WARNING_BG, "danger": DANGER_BG, "info": INFO_BG, "normal": SURFACE_RAISED}
_TONE_BD   = {"good": SUCCESS_BORDER, "warn": WARNING_BORDER, "danger": DANGER_BORDER, "info": INFO_BORDER, "normal": LINE_STRONG}
_TONE_TEXT = {"good": GREEN, "warn": YELLOW, "danger": RED, "info": INFO, "normal": MUTED}


# ── Agent card ────────────────────────────────────────────────────────────────

class AgentCard(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, agent: "AgentCardState", app: "ApexTraderGUI") -> None:
        super().__init__(
            parent,
            corner_radius=8,
            fg_color=SURFACE_RAISED,
            border_width=1,
            border_color=LINE,
        )
        self._app    = app
        self._agent  = agent
        self._build(agent)

    def _build(self, a: "AgentCardState") -> None:
        tone = _STATUS_TONE.get(a.status, "normal")

        # Top accent bar
        ctk.CTkFrame(self, height=2, fg_color=_TONE_TEXT.get(tone, MUTED), corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # Row 1 — name + status badge
        row1 = ctk.CTkFrame(body, fg_color="transparent")
        row1.pack(fill="x")
        ctk.CTkLabel(
            row1, text=a.display_name or a.agent_id,
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT, anchor="w",
        ).pack(side="left")
        badge_bg = _TONE_BG.get(tone, SURFACE_RAISED)
        badge_bd = _TONE_BD.get(tone, LINE_STRONG)
        badge_fg = _TONE_TEXT.get(tone, MUTED)
        badge = ctk.CTkFrame(row1, fg_color=badge_bg, border_width=1, border_color=badge_bd, corner_radius=4)
        badge.pack(side="right")
        ctk.CTkLabel(
            badge, text=_STATUS_LABEL.get(a.status, a.status),
            font=ctk.CTkFont(size=10, weight="bold"), text_color=badge_fg,
        ).pack(padx=8, pady=2)

        # Row 2 — MT5 login + server
        login_str = str(a.mt5_login) if a.mt5_login else "—"
        server_str = a.mt5_server or "—"
        ctk.CTkLabel(
            body, text=f"Login: {login_str}  ·  Server: {server_str}",
            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(4, 0))

        # Row 3 — live metrics
        bal = f"${a.balance:,.2f}" if a.balance is not None else "—"
        eq  = f"${a.equity:,.2f}"  if a.equity  is not None else "—"
        gw_dot = "●" if a.gateway_connected else "○"
        gw_col = GREEN if a.gateway_connected else MUTED
        metrics_row = ctk.CTkFrame(body, fg_color="transparent")
        metrics_row.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(
            metrics_row, text=f"Bal: {bal}  Eq: {eq}  Trades: {a.open_trades}",
            font=ctk.CTkFont(family="Consolas", size=11), text_color=TEXT_SOFT, anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            metrics_row, text=f"{gw_dot} GW",
            font=ctk.CTkFont(size=11), text_color=gw_col,
        ).pack(side="right")

        # Error message
        if a.error_message and a.status in ("CRASH_LOOP", "ERROR"):
            ctk.CTkLabel(
                body, text=a.error_message[:80],
                font=ctk.CTkFont(size=10), text_color=RED, anchor="w", wraplength=280,
            ).pack(anchor="w", pady=(4, 0))

        # Action buttons
        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", pady=(10, 0))
        self._add_buttons(btn_row, a)

    def _add_buttons(self, parent: tk.Widget, a: "AgentCardState") -> None:
        is_running = a.status == "RUNNING"
        is_stopped = a.status in ("STOPPED", "PROVISIONED")
        is_crash   = a.status == "CRASH_LOOP"

        ctk.CTkButton(
            parent, text="Open", width=64, height=28,
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER, text_color=GREEN,
            border_width=1, border_color=SUCCESS_BORDER, corner_radius=4,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self._app.select_agent(a.agent_id, a.monitoring_port),
        ).pack(side="left", padx=(0, 4))

        if is_stopped or is_crash:
            ctk.CTkButton(
                parent, text="Start", width=56, height=28,
                fg_color="transparent", hover_color=INFO_BG, text_color=INFO,
                border_width=1, border_color=INFO_BORDER, corner_radius=4,
                font=ctk.CTkFont(size=11),
                command=lambda: self._app.manager_client.submit_operation(a.agent_id, "start"),
            ).pack(side="left", padx=(0, 4))

        if is_running:
            ctk.CTkButton(
                parent, text="Stop", width=56, height=28,
                fg_color="transparent", hover_color=WARNING_BG, text_color=YELLOW,
                border_width=1, border_color=WARNING_BORDER, corner_radius=4,
                font=ctk.CTkFont(size=11),
                command=lambda: self._app.manager_client.submit_operation(a.agent_id, "stop"),
            ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            parent, text="Remove", width=64, height=28,
            fg_color="transparent", hover_color=DANGER_BG, text_color=RED,
            border_width=1, border_color=DANGER_BORDER, corner_radius=4,
            font=ctk.CTkFont(size=11),
            command=lambda: self._app.manager_client.delete_agent(a.agent_id),
        ).pack(side="right")


# ── Agents fleet page ──────────────────────────────────────────────────────────

class AgentsPage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app   = app
        self._cards: list[AgentCard] = []
        self._build()
        # Subscribe to manager state changes
        if hasattr(app, "manager_state"):
            app.manager_state.subscribe("agents", lambda agents, **_: self.after(0, lambda: self._refresh(agents)))

    def _build(self) -> None:
        hdr = page_header(self, "Agents", "Managed MT5 trading accounts")

        # "+ Add Agent" button in header right area
        add_btn = ctk.CTkButton(
            hdr, text="+ Add Agent", width=110, height=30,
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER, text_color=GREEN,
            border_width=1, border_color=SUCCESS_BORDER, corner_radius=6,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_add_dialog,
        )
        add_btn.pack(side="right", padx=16)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self._scroll.pack(fill="both", expand=True, padx=16, pady=12)

        self._empty_lbl = ctk.CTkLabel(
            self._scroll,
            text="No agents yet.\nClick '+ Add Agent' to provision your first MT5 account.",
            font=ctk.CTkFont(size=13), text_color=MUTED, justify="center",
        )
        self._empty_lbl.pack(pady=60)

    def _refresh(self, agents: list) -> None:
        # Destroy old cards
        for card in self._cards:
            card.destroy()
        self._cards = []

        if not agents:
            self._empty_lbl.pack(pady=60)
            return

        self._empty_lbl.pack_forget()
        for a in agents:
            card = AgentCard(self._scroll, a, self.app)
            card.pack(fill="x", pady=(0, 8))
            self._cards.append(card)

    def _open_add_dialog(self) -> None:
        dlg = AddAgentDialog(self, self.app)
        dlg.grab_set()

    def on_snapshot(self, payload: dict) -> None:
        pass  # agent-mode snapshots handled by existing pages


# ── Add agent dialog ───────────────────────────────────────────────────────────

class AddAgentDialog(ctk.CTkToplevel):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent)
        self.app = app
        self.title("Add Agent")
        self.geometry("480x520")
        self.resizable(False, False)
        self._terminals: list[dict] = []
        self._build()
        # Load terminals in background
        if hasattr(app, "manager_client"):
            app.manager_client.get_terminals(
                lambda t: self.after(0, lambda: self._load_terminals(t))
            )

    def _build(self) -> None:
        body = ctk.CTkScrollableFrame(self, fg_color=SURFACE, corner_radius=0)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        ctk.CTkLabel(
            body, text="Add Managed Agent",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT,
        ).pack(anchor="w", pady=(0, 12))

        # Display name
        ctk.CTkLabel(body, text="Display Name", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w").pack(fill="x")
        self._name_var = ctk.StringVar(value="Agent")
        ctk.CTkEntry(body, textvariable=self._name_var, height=34, fg_color=SURFACE_RAISED, border_color=LINE_STRONG).pack(fill="x", pady=(2, 10))

        # MT5 Terminal
        ctk.CTkLabel(body, text="MT5 Terminal", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w").pack(fill="x")
        self._terminal_var = ctk.StringVar(value="Loading…")
        self._terminal_menu = ctk.CTkOptionMenu(
            body, variable=self._terminal_var,
            values=["Loading…"],
            fg_color=SURFACE_RAISED, button_color=LINE_STRONG,
            text_color=TEXT, height=34,
        )
        self._terminal_menu.pack(fill="x", pady=(2, 10))

        # MT5 Login
        ctk.CTkLabel(body, text="MT5 Login", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w").pack(fill="x")
        self._login_var = ctk.StringVar()
        ctk.CTkEntry(body, textvariable=self._login_var, height=34, fg_color=SURFACE_RAISED, border_color=LINE_STRONG).pack(fill="x", pady=(2, 10))

        # MT5 Password
        ctk.CTkLabel(body, text="MT5 Password", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w").pack(fill="x")
        self._password_var = ctk.StringVar()
        ctk.CTkEntry(body, textvariable=self._password_var, show="●", height=34, fg_color=SURFACE_RAISED, border_color=LINE_STRONG).pack(fill="x", pady=(2, 10))

        # MT5 Server
        ctk.CTkLabel(body, text="MT5 Server", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w").pack(fill="x")
        self._server_var = ctk.StringVar()
        ctk.CTkEntry(body, textvariable=self._server_var, height=34, fg_color=SURFACE_RAISED, border_color=LINE_STRONG).pack(fill="x", pady=(2, 10))

        # Symbols
        ctk.CTkLabel(body, text="Symbols (comma-separated)", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w").pack(fill="x")
        self._symbols_var = ctk.StringVar(value="XAUUSD")
        ctk.CTkEntry(body, textvariable=self._symbols_var, height=34, fg_color=SURFACE_RAISED, border_color=LINE_STRONG).pack(fill="x", pady=(2, 10))

        # Status label
        self._status_lbl = ctk.CTkLabel(body, text="", font=ctk.CTkFont(size=11), text_color=MUTED)
        self._status_lbl.pack(anchor="w", pady=(4, 0))

        # Buttons
        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", pady=(12, 0))
        ctk.CTkButton(
            btn_row, text="Cancel", width=100, height=34,
            fg_color="transparent", hover_color=SURFACE_RAISED, text_color=MUTED,
            border_width=1, border_color=LINE_STRONG, corner_radius=6,
            command=self.destroy,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_row, text="Add Agent", width=120, height=34,
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER, text_color=GREEN,
            border_width=1, border_color=SUCCESS_BORDER, corner_radius=6,
            font=ctk.CTkFont(weight="bold"),
            command=self._submit,
        ).pack(side="right")

    def _load_terminals(self, terminals: list[dict]) -> None:
        self._terminals = terminals
        if not terminals:
            self._terminal_menu.configure(values=["No terminals found"])
            self._terminal_var.set("No terminals found")
            return
        labels = []
        for t in terminals:
            state = t.get("state", "unknown")
            name  = t.get("name") or t.get("path", "")[:40]
            label = f"{name}  [{state}]"
            labels.append(label)
        self._terminal_menu.configure(values=labels)
        self._terminal_var.set(labels[0])

    def _get_terminal_path(self) -> str:
        selected = self._terminal_var.get()
        for i, t in enumerate(self._terminals):
            name  = t.get("name") or t.get("path", "")[:40]
            label = f"{name}  [{t.get('state', 'unknown')}]"
            if label == selected:
                return t.get("path", "")
        return ""

    def _submit(self) -> None:
        login_str = self._login_var.get().strip()
        if not login_str.isdigit():
            self._status_lbl.configure(text="MT5 login must be a number.", text_color=RED)
            return
        symbols = [s.strip().upper() for s in self._symbols_var.get().split(",") if s.strip()]
        if not symbols:
            self._status_lbl.configure(text="Enter at least one symbol.", text_color=RED)
            return

        payload = {
            "display_name":  self._name_var.get().strip() or "Agent",
            "terminal_path": self._get_terminal_path(),
            "mt5_login":     int(login_str),
            "mt5_password":  self._password_var.get(),
            "mt5_server":    self._server_var.get().strip(),
            "symbols":       symbols,
        }

        self._status_lbl.configure(text="Submitting…", text_color=MUTED)
        self.app.manager_client.provision_agent(
            payload,
            on_done=lambda op_id: self.after(0, lambda: self._on_done(op_id)),
        )

    def _on_done(self, op_id: str | None) -> None:
        if op_id:
            self._status_lbl.configure(text=f"Submitted (op: {op_id})", text_color=GREEN)
            self.after(1500, self.destroy)
        else:
            self._status_lbl.configure(text="Failed — is the manager running?", text_color=RED)
