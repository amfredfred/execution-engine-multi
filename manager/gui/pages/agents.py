"""
src/gui/pages/agents.py — Fleet view + Add Agent page.

AgentsPage   — scrollable grid of managed-agent cards (fleet mode).
AddAgentPage — full-page form for provisioning a new agent; navigated to
               via app.navigate("AddAgent"), not opened as a dialog.
"""
from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

from manager.gui.theme import (
    GREEN, RED, YELLOW, MUTED, INFO,
    TEXT, TEXT_SOFT, BASE, SURFACE, SURFACE_RAISED,
    LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER, WARNING_BG, WARNING_BORDER,
    DANGER_BG, DANGER_BORDER, INFO_BG, INFO_BORDER,
    page_header,
)
from manager.gui.components import ActionBanner, PrimaryButton

if TYPE_CHECKING:
    from manager.gui.app import ApexTraderGUI
    from manager.gui.manager_state import AgentCardState

# ── Symbol display names ──────────────────────────────────────────────────────

_SYMBOL_LABELS: dict[str, str] = {
    "XAUUSD": "Gold / US Dollar",
    "XAGUSD": "Silver / US Dollar",
    "EURUSD": "Euro / US Dollar",
    "GBPUSD": "British Pound / Dollar",
    "USDJPY": "US Dollar / Japanese Yen",
    "USDCHF": "US Dollar / Swiss Franc",
    "AUDUSD": "Australian Dollar / USD",
    "USDCAD": "US Dollar / Canadian Dollar",
    "NZDUSD": "New Zealand Dollar / USD",
    "US100":  "Nasdaq 100",
    "US500":  "S&P 500",
    "US30":   "Dow Jones 30",
    "BTCUSD": "Bitcoin / US Dollar",
    "ETHUSD": "Ethereum / US Dollar",
}

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
        self._app   = app
        self._agent = agent
        self._build(agent)

    def _build(self, a: "AgentCardState") -> None:
        tone = _STATUS_TONE.get(a.status, "normal")

        # Top accent bar — kept as instance attr for in-place color update
        self._accent = ctk.CTkFrame(self, height=2, fg_color=_TONE_TEXT.get(tone, MUTED), corner_radius=0)
        self._accent.pack(fill="x")

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
        self._badge = ctk.CTkFrame(row1, fg_color=badge_bg, border_width=1, border_color=badge_bd, corner_radius=4)
        self._badge.pack(side="right")
        self._badge_lbl = ctk.CTkLabel(
            self._badge, text=_STATUS_LABEL.get(a.status, a.status),
            font=ctk.CTkFont(size=10, weight="bold"), text_color=badge_fg,
        )
        self._badge_lbl.pack(padx=8, pady=2)

        # Row 2 — MT5 login + server (static — never changes after provisioning)
        login_str  = str(a.mt5_login) if a.mt5_login else "—"
        server_str = a.mt5_server or "—"
        ctk.CTkLabel(
            body, text=f"Login: {login_str}  ·  Server: {server_str}",
            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(4, 0))

        # Row 3 — live metrics
        bal    = f"${a.balance:,.2f}" if a.balance is not None else "—"
        eq     = f"${a.equity:,.2f}"  if a.equity  is not None else "—"
        gw_dot = "●" if a.gateway_connected else "○"
        gw_col = GREEN if a.gateway_connected else MUTED
        metrics_row = ctk.CTkFrame(body, fg_color="transparent")
        metrics_row.pack(fill="x", pady=(6, 0))
        self._metrics_lbl = ctk.CTkLabel(
            metrics_row, text=f"Bal: {bal}  Eq: {eq}  Trades: {a.open_trades}",
            font=ctk.CTkFont(family="Consolas", size=11), text_color=TEXT_SOFT, anchor="w",
        )
        self._metrics_lbl.pack(side="left")
        self._gw_lbl = ctk.CTkLabel(
            metrics_row, text=f"{gw_dot} GW",
            font=ctk.CTkFont(size=11), text_color=gw_col,
        )
        self._gw_lbl.pack(side="right")

        # Error message (hidden when no error)
        self._error_lbl = ctk.CTkLabel(
            body, text="",
            font=ctk.CTkFont(size=10), text_color=RED, anchor="w", wraplength=280,
        )
        if a.error_message and a.status in ("CRASH_LOOP", "ERROR"):
            self._error_lbl.configure(text=a.error_message[:80])
            self._error_lbl.pack(anchor="w", pady=(4, 0))

        # Action buttons — kept as frame so buttons can be swapped on status change
        self._btn_row = ctk.CTkFrame(body, fg_color="transparent")
        self._btn_row.pack(fill="x", pady=(10, 0))
        self._add_buttons(self._btn_row, a)

    def update(self, a: "AgentCardState") -> None:
        """Patch mutable display fields without destroying and recreating the card."""
        tone = _STATUS_TONE.get(a.status, "normal")

        self._accent.configure(fg_color=_TONE_TEXT.get(tone, MUTED))
        self._badge.configure(
            fg_color=_TONE_BG.get(tone, SURFACE_RAISED),
            border_color=_TONE_BD.get(tone, LINE_STRONG),
        )
        self._badge_lbl.configure(
            text=_STATUS_LABEL.get(a.status, a.status),
            text_color=_TONE_TEXT.get(tone, MUTED),
        )

        bal    = f"${a.balance:,.2f}" if a.balance is not None else "—"
        eq     = f"${a.equity:,.2f}"  if a.equity  is not None else "—"
        self._metrics_lbl.configure(text=f"Bal: {bal}  Eq: {eq}  Trades: {a.open_trades}")
        gw_dot = "●" if a.gateway_connected else "○"
        self._gw_lbl.configure(
            text=f"{gw_dot} GW",
            text_color=GREEN if a.gateway_connected else MUTED,
        )

        if a.error_message and a.status in ("CRASH_LOOP", "ERROR"):
            self._error_lbl.configure(text=a.error_message[:80])
            self._error_lbl.pack(anchor="w", pady=(4, 0))
        else:
            self._error_lbl.pack_forget()

        # Rebuild buttons only when status tier changes (different button set needed)
        if a.status != self._agent.status:
            for w in self._btn_row.winfo_children():
                w.destroy()
            self._add_buttons(self._btn_row, a)

        self._agent = a

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
        if hasattr(app, "manager_state"):
            app.manager_state.subscribe("agents", lambda agents, **_: self.after(0, lambda: self._refresh(agents)))

    def _build(self) -> None:
        hdr = page_header(self, "Agents", "Managed MT5 trading accounts")

        add_btn = ctk.CTkButton(
            hdr, text="+ Add Agent", width=110, height=30,
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER, text_color=GREEN,
            border_width=1, border_color=SUCCESS_BORDER, corner_radius=6,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.app.navigate("AddAgent"),
        )
        add_btn.pack(side="right", padx=16)

        # Manager offline banner — hidden when manager is reachable
        self._offline_bar = ctk.CTkFrame(
            self, fg_color=DANGER_BG, corner_radius=0,
            border_width=1, border_color=DANGER_BORDER,
        )
        self._offline_lbl = ctk.CTkLabel(
            self._offline_bar,
            text="⚠  AQ Manager is offline — agents cannot be started or monitored.",
            font=ctk.CTkFont(size=12), text_color=RED, anchor="w",
        )
        self._offline_lbl.pack(side="left", padx=14, pady=8)
        self._start_btn = ctk.CTkButton(
            self._offline_bar, text="▶  Start Manager", width=130, height=28,
            fg_color="transparent", hover_color=DANGER_BORDER, text_color=RED,
            border_width=1, border_color=DANGER_BORDER, corner_radius=4,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_start_manager,
        )
        self._start_btn.pack(side="right", padx=14)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self._scroll.pack(fill="both", expand=True, padx=16, pady=12)

        self._empty_lbl = ctk.CTkLabel(
            self._scroll,
            text="No agents yet.\nClick '+ Add Agent' to provision your first MT5 account.",
            font=ctk.CTkFont(size=13), text_color=MUTED, justify="center",
        )
        self._empty_lbl.pack(pady=60)

    def set_manager_online(self, online: bool) -> None:
        """Show/hide the offline banner based on manager reachability."""
        if online:
            self._offline_bar.pack_forget()
        else:
            self._offline_bar.pack(fill="x", before=self._scroll)

    def _on_start_manager(self) -> None:
        self._start_btn.configure(state="disabled", text="Starting…")
        self.app.restart_manager(on_done=self._on_start_done)

    def _on_start_done(self, ok: bool) -> None:
        def _apply():
            if ok:
                self._start_btn.configure(state="normal", text="▶  Start Manager")
            else:
                self._start_btn.configure(state="normal", text="▶  Retry")
        self.after(0, _apply)

    def _refresh(self, agents: list) -> None:
        new_ids = [a.agent_id for a in agents]
        old_ids = [c._agent.agent_id for c in self._cards]

        if new_ids == old_ids and agents:
            # Agent set unchanged — patch labels in-place to avoid flicker
            for card, a in zip(self._cards, agents):
                card.update(a)
            return

        # Agent set changed (add/remove) — full rebuild
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

    def on_navigate_to(self) -> None:
        self._refresh(self.app.manager_state.agents)


# ── Add agent page (replaces the old modal dialog) ────────────────────────────

class AddAgentPage(ctk.CTkFrame):
    """
    Full-page form for provisioning a new managed agent.

    Navigated to via app.navigate("AddAgent") from the Agents fleet page.
    on_navigate_to() is called by app._show_page() each time the page becomes
    visible, which triggers a fresh terminal scan and license symbol fetch.
    """

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._terminals: list[dict] = []
        self._sym_vars:  dict[str, tk.BooleanVar] = {}
        self._build()

    def _build(self) -> None:
        # ── Header with breadcrumb back button ───────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=BASE, height=52, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkButton(
            hdr, text="← Agents", width=90, height=30,
            fg_color="transparent", hover_color=LINE_STRONG,
            text_color=MUTED, font=ctk.CTkFont(size=12),
            command=lambda: self.app.navigate("Agents"),
        ).pack(side="left", padx=16, pady=11)

        ctk.CTkLabel(
            hdr, text="Add Agent",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT,
        ).pack(side="left")

        # ── Scrollable body ───────────────────────────────────────────────────
        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self._body.pack(fill="both", expand=True, padx=0, pady=0)

        inner = ctk.CTkFrame(self._body, fg_color="transparent")
        inner.pack(fill="x", padx=40, pady=20)

        # ── Display name ──────────────────────────────────────────────────────
        self._section_label(inner, "AGENT DETAILS")

        card1 = self._card(inner)
        _lbl(card1, "Display Name")
        self._name_var = tk.StringVar(value="My Agent")
        ctk.CTkEntry(
            card1, textvariable=self._name_var, height=36,
            fg_color=BASE, border_color=LINE_STRONG, border_width=1, corner_radius=6,
        ).pack(fill="x", padx=14, pady=(0, 10))

        # ── MT5 Terminal ──────────────────────────────────────────────────────
        self._section_label(inner, "MT5 TERMINAL")

        card2 = self._card(inner)

        self._terminal_var = tk.StringVar(value="Scanning…")
        self._terminal_menu = ctk.CTkOptionMenu(
            card2,
            variable=self._terminal_var,
            values=["Scanning…"],
            fg_color=BASE,
            button_color=LINE_STRONG,
            button_hover_color=LINE_STRONG,
            text_color=TEXT,
            height=36,
            corner_radius=6,
        )
        self._terminal_menu.pack(fill="x", padx=14, pady=(10, 4))

        scan_row = ctk.CTkFrame(card2, fg_color="transparent")
        scan_row.pack(fill="x", padx=14, pady=(0, 10))
        self._terminal_status = ctk.CTkLabel(
            scan_row, text="", font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
        )
        self._terminal_status.pack(side="left")
        ctk.CTkButton(
            scan_row, text="↺  Scan again", width=110, height=26,
            fg_color="transparent", hover_color=LINE_STRONG, text_color=MUTED,
            border_width=1, border_color=LINE, corner_radius=4,
            font=ctk.CTkFont(size=11),
            command=self._load_terminals,
        ).pack(side="right")

        # ── MT5 Credentials ───────────────────────────────────────────────────
        self._section_label(inner, "MT5 CREDENTIALS")

        card3 = self._card(inner)
        _lbl(card3, "Account Number")
        self._login_var = tk.StringVar()
        ctk.CTkEntry(
            card3, textvariable=self._login_var, height=36,
            placeholder_text="e.g. 12345678",
            fg_color=BASE, border_color=LINE_STRONG, border_width=1, corner_radius=6,
        ).pack(fill="x", padx=14, pady=(0, 10))

        _lbl(card3, "Password")
        self._password_var = tk.StringVar()
        ctk.CTkEntry(
            card3, textvariable=self._password_var, show="●", height=36,
            fg_color=BASE, border_color=LINE_STRONG, border_width=1, corner_radius=6,
        ).pack(fill="x", padx=14, pady=(0, 10))

        _lbl(card3, "Server")
        self._server_var = tk.StringVar()
        ctk.CTkEntry(
            card3, textvariable=self._server_var, height=36,
            placeholder_text="e.g. FBS-Real",
            fg_color=BASE, border_color=LINE_STRONG, border_width=1, corner_radius=6,
        ).pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkLabel(
            card3,
            text="Your password is encrypted with Windows DPAPI and never written to disk.",
            font=ctk.CTkFont(size=10), text_color=MUTED, anchor="w",
            wraplength=560, justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 10))

        # ── Trading Pairs ─────────────────────────────────────────────────────
        self._section_label(inner, "TRADING PAIRS")

        self._sym_card = self._card(inner)
        sym_actions = ctk.CTkFrame(self._sym_card, fg_color="transparent")
        sym_actions.pack(fill="x", padx=14, pady=(10, 0))
        self._license_status = ctk.CTkLabel(
            sym_actions, text="License details not loaded",
            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
        )
        self._license_status.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            sym_actions, text="Preflight license", width=120, height=26,
            fg_color="transparent", hover_color=LINE_STRONG, text_color=MUTED,
            border_width=1, border_color=LINE, corner_radius=4,
            font=ctk.CTkFont(size=11),
            command=self._preflight_symbols,
        ).pack(side="right")

        self._sym_loading = ctk.CTkLabel(
            self._sym_card, text="Loading available pairs from license…",
            font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w",
        )
        self._sym_loading.pack(anchor="w", padx=14, pady=14)

        self._sym_inner = ctk.CTkFrame(self._sym_card, fg_color="transparent")

        self._sym_err = ctk.CTkLabel(
            self._sym_card, text="",
            font=ctk.CTkFont(size=11), text_color=RED, anchor="w",
        )

        # ── Action banner + submit ─────────────────────────────────────────────
        self._banner = ActionBanner(inner)
        self._banner.pack(fill="x", pady=(12, 0))
        self._banner.hide()

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(fill="x", pady=(12, 0))

        ctk.CTkButton(
            btn_row, text="Cancel", width=100, height=36,
            fg_color="transparent", hover_color=LINE_STRONG, text_color=MUTED,
            border_width=1, border_color=LINE, corner_radius=6,
            command=lambda: self.app.navigate("Agents"),
        ).pack(side="right", padx=(8, 0))

        PrimaryButton(
            btn_row, text="Add Agent", tone="good", width=130, height=36,
            command=self._submit,
        ).pack(side="right")

    # ── Page lifecycle ────────────────────────────────────────────────────────

    def on_navigate_to(self) -> None:
        """Called by app._show_page() each time this page becomes visible."""
        self._banner.hide()
        self._load_terminals()
        self._load_symbols()

    # ── Terminal loading ──────────────────────────────────────────────────────

    def _load_terminals(self) -> None:
        self._terminal_var.set("Scanning…")
        self._terminal_menu.configure(values=["Scanning…"])
        self._terminal_status.configure(text="Scanning for MT5 terminals…", text_color=MUTED)

        if not hasattr(self.app, "manager_client"):
            self._on_terminals_loaded([])
            return
        self.app.manager_client.get_terminals(
            lambda t: self.after(0, lambda: self._on_terminals_loaded(t))
        )

    def _on_terminals_loaded(self, terminals: list[dict]) -> None:
        self._terminals = terminals

        if not terminals:
            values = ["No terminals found — is the Manager running?"]
            self._terminal_menu.configure(values=values)
            self._terminal_var.set(values[0])
            self._terminal_status.configure(
                text="No MT5 terminals found. Start the Manager and click Scan again.",
                text_color=YELLOW,
            )
            return

        labels = []
        for t in terminals:
            state = t.get("state", "unknown")
            name  = t.get("name") or t.get("path", "")[:40]
            if state == "available":
                label = f"{name}"
            elif state in ("managed_running", "managed_stopped"):
                label = f"{name}  [in use]"
            else:
                label = f"{name}  [{state}]"
            labels.append(label)

        self._terminal_menu.configure(values=labels)
        self._terminal_var.set(labels[0])

        avail = sum(1 for t in terminals if t.get("state") == "available")
        self._terminal_status.configure(
            text=f"{len(terminals)} terminal(s) found, {avail} available",
            text_color=GREEN,
        )

    def _get_selected_terminal_path(self) -> str:
        selected = self._terminal_var.get()
        for t in self._terminals:
            name  = t.get("name") or t.get("path", "")[:40]
            state = t.get("state", "unknown")
            if state == "available":
                label = f"{name}"
            elif state in ("managed_running", "managed_stopped"):
                label = f"{name}  [in use]"
            else:
                label = f"{name}  [{state}]"
            if label == selected:
                return t.get("path", "")
        return ""

    # ── Symbol loading ────────────────────────────────────────────────────────

    def _load_symbols(self) -> None:
        # Reset symbol area
        for w in self._sym_inner.winfo_children():
            w.destroy()
        self._sym_vars = {}
        self._sym_inner.pack_forget()
        self._sym_err.pack_forget()
        self._sym_loading.pack(anchor="w", padx=14, pady=14)

        if not hasattr(self.app, "manager_client"):
            self._on_symbols_loaded({"symbols": self._fallback_symbols(), "error": None})
            return
        self.app.manager_client.get_license_info(
            lambda info: self.after(0, lambda: self._on_symbols_loaded(info))
        )

    def _preflight_symbols(self) -> None:
        self._license_status.configure(
            text="Preflighting stored manager license...", text_color=MUTED,
        )
        self.app.manager_client.preflight_license(
            "",
            lambda info: self.after(0, lambda: self._on_symbols_loaded(info)),
        )

    def _on_symbols_loaded(self, info: dict) -> None:
        self._sym_loading.pack_forget()
        symbols = info.get("symbols") or []
        err     = info.get("error")
        if info.get("valid"):
            available = info.get("available_devices")
            suffix = f" | {available} device slot(s) available" if available is not None else ""
            self._license_status.configure(
                text=f"Manager license verified{suffix}", text_color=GREEN,
            )
        else:
            self._license_status.configure(
                text=err or "Manager license is invalid", text_color=YELLOW,
            )

        if not symbols:
            # Manager unreachable — fall back to symbols stored in config.yaml
            symbols = self._fallback_symbols()
            self._sym_err.configure(
                text=(
                    f"{err}. Showing pairs saved during onboarding."
                    if err else "Showing pairs saved during onboarding."
                )
            )
            self._sym_err.pack(anchor="w", padx=14, pady=(8, 0))

        self._build_symbol_toggles(symbols)

    def _fallback_symbols(self) -> list[str]:
        try:
            cfg = self.app.config.load()
            return list(cfg.get("gateway", {}).get("symbols") or ["XAUUSD"])
        except Exception:
            return ["XAUUSD"]

    def _build_symbol_toggles(self, symbols: list[str]) -> None:
        for w in self._sym_inner.winfo_children():
            w.destroy()
        self._sym_vars = {}

        if not symbols:
            ctk.CTkLabel(
                self._sym_inner, text="No trading pairs available for this license.",
                font=ctk.CTkFont(size=12), text_color=YELLOW, anchor="w",
            ).pack(anchor="w", padx=14, pady=14)
            self._sym_inner.pack(fill="x")
            return

        ctk.CTkLabel(
            self._sym_inner,
            text="Select the pairs this agent will receive signals for.",
            font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w",
        ).pack(anchor="w", padx=14, pady=(12, 4))

        for sym in symbols:
            var = tk.BooleanVar(value=True)
            self._sym_vars[sym] = var

            row = ctk.CTkFrame(
                self._sym_inner,
                fg_color=BASE, corner_radius=6,
                border_width=1, border_color=LINE,
            )
            row.pack(fill="x", padx=14, pady=3)
            inner = ctk.CTkFrame(row, fg_color="transparent")
            inner.pack(fill="x", padx=12, pady=8)

            col = ctk.CTkFrame(inner, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(
                col, text=sym,
                font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT, anchor="w",
            ).pack(anchor="w")
            sub = _SYMBOL_LABELS.get(sym, "")
            if sub:
                ctk.CTkLabel(
                    col, text=sub,
                    font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
                ).pack(anchor="w", pady=(1, 0))

            ctk.CTkSwitch(inner, text="", variable=var, onvalue=True, offvalue=False, width=46).pack(side="right")

        self._sym_inner.pack(fill="x", pady=(0, 12))

    # ── Submit ────────────────────────────────────────────────────────────────

    def _submit(self) -> None:
        self._banner.hide()

        login_str = self._login_var.get().strip()
        if not login_str.isdigit():
            self._banner.show("Account number must be a number (e.g. 12345678).", "warn")
            return

        password = self._password_var.get()
        if not password:
            self._banner.show("Password is required.", "warn")
            return

        server = self._server_var.get().strip()
        if not server:
            self._banner.show("Server name is required (e.g. FBS-Real).", "warn")
            return

        terminal_path = self._get_selected_terminal_path()
        if not terminal_path:
            self._banner.show("Select an MT5 terminal above.", "warn")
            return

        symbols = [sym for sym, var in self._sym_vars.items() if var.get()]
        if not symbols:
            self._banner.show("Enable at least one trading pair.", "warn")
            return

        payload = {
            "display_name":  self._name_var.get().strip() or "My Agent",
            "terminal_path": terminal_path,
            "mt5_login":     int(login_str),
            "mt5_password":  password,
            "mt5_server":    server,
            "symbols":       symbols,
        }

        self._banner.show("Submitting…", "info")
        self.app.manager_client.provision_agent(
            payload,
            on_done=lambda op_id: self.after(0, lambda: self._on_done(op_id)),
        )

    def _on_done(self, op_id: str | None) -> None:
        if op_id:
            self._banner.show("Agent provisioned. Returning to fleet…", "good")
            # Force an immediate re-poll so the new agent is visible on arrival
            self.app.manager_client.get_agents(
                lambda data: self.app._queue.put({"type": "agents", "payload": data})
            )
            self.after(1200, lambda: self.app.navigate("Agents"))
        else:
            self._banner.show(
                "Failed to provision agent — is the Manager running?\n"
                "Check that the AQ Manager scheduled task is active.",
                "danger",
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _card(self, parent: tk.Widget) -> ctk.CTkFrame:
        c = ctk.CTkFrame(parent, fg_color=SURFACE_RAISED, corner_radius=8, border_width=1, border_color=LINE)
        c.pack(fill="x", pady=(0, 12))
        return c

    def _section_label(self, parent: tk.Widget, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text,
            font=ctk.CTkFont(size=10, weight="bold"), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(4, 6))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lbl(parent: tk.Widget, text: str) -> None:
    ctk.CTkLabel(
        parent, text=text, anchor="w",
        font=ctk.CTkFont(size=12), text_color=MUTED,
    ).pack(anchor="w", padx=14, pady=(10, 2))
