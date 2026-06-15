"""
manager/gui/pages/agent_dashboard.py — Per-agent drill-down dashboard.

Tabs: Overview | Risk | Settings
  • Overview  — live metrics, controls, logs (manager API)
  • Risk      — per-agent risk editor (reads config.yaml, patches via PATCH /agents/{id}/config)
  • Settings  — read-only agent config summary
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk
import yaml

from manager.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE, SURFACE_RAISED, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER, DANGER_BG, DANGER_BORDER,
    WARNING_BG, WARNING_BORDER,
    section_rule,
)
from manager.gui.components import SectionCard, ActionBanner

if TYPE_CHECKING:
    from manager.gui.app import ApexTraderGUI
    from manager.gui.manager_state import AgentCardState

_STATUS_COLOUR = {
    "RUNNING":     (GREEN,  SUCCESS_BG, SUCCESS_BORDER),
    "STOPPED":     (MUTED,  SURFACE_RAISED, LINE),
    "STARTING":    (YELLOW, WARNING_BG, WARNING_BORDER),
    "CRASH_LOOP":  (RED,    DANGER_BG,  DANGER_BORDER),
    "ERROR":       (RED,    DANGER_BG,  DANGER_BORDER),
    "PROVISIONED": (INFO,   SURFACE_RAISED, LINE),
}

_TAB_NAMES = ("Overview", "Risk", "Settings")


class AgentDashboardPage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=BASE, corner_radius=0)
        self.app = app
        self._agent_id: str | None = None
        self._tab_frames: dict[str, tk.Widget] = {}
        self._tab_btns: dict[str, ctk.CTkButton] = {}
        self._build()
        self.app.manager_state.subscribe("agents", self._on_agents_updated)

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── Header row ────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=SURFACE_RAISED, corner_radius=0, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkButton(
            hdr, text="← Back", width=90, height=34,
            fg_color="transparent", hover_color=LINE,
            border_width=1, border_color=LINE, text_color=MUTED,
            font=ctk.CTkFont(size=12),
            command=self._go_back,
        ).pack(side="left", padx=12, pady=10)

        self._lbl_title = ctk.CTkLabel(
            hdr, text="Agent Dashboard",
            font=ctk.CTkFont(size=16, weight="bold"), text_color=TEXT,
        )
        self._lbl_title.pack(side="left", padx=8)

        self._status_pill = ctk.CTkFrame(
            hdr, fg_color=SURFACE_RAISED,
            corner_radius=12, border_width=1, border_color=LINE,
        )
        self._status_pill.pack(side="left", padx=12, pady=16)
        self._lbl_status = ctk.CTkLabel(
            self._status_pill, text="—",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=MUTED,
        )
        self._lbl_status.pack(padx=10, pady=2)

        # ── Tab bar ───────────────────────────────────────────────────────────
        tab_bar = ctk.CTkFrame(self, fg_color=SURFACE_RAISED, corner_radius=0, height=40)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)

        for name in _TAB_NAMES:
            btn = ctk.CTkButton(
                tab_bar, text=name, width=110, height=38, corner_radius=0,
                fg_color="transparent", text_color=MUTED,
                font=ctk.CTkFont(size=13),
                command=lambda n=name: self._show_tab(n),
            )
            btn.pack(side="left")
            self._tab_btns[name] = btn

        # ── Content area (tabs share this container) ──────────────────────────
        content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        content.pack(fill="both", expand=True)

        self._tab_frames["Overview"] = self._build_overview(content)
        self._tab_frames["Risk"]     = self._build_risk(content)
        self._tab_frames["Settings"] = self._build_settings_tab(content)

        self._show_tab("Overview")

    def _show_tab(self, name: str) -> None:
        for n, frame in self._tab_frames.items():
            if n == name:
                frame.pack(fill="both", expand=True)
                cb = getattr(frame, "on_tab_enter", None)
                if cb:
                    cb()
            else:
                frame.pack_forget()
        for n, btn in self._tab_btns.items():
            if n == name:
                btn.configure(fg_color=SUCCESS_BG, text_color=GREEN,
                              font=ctk.CTkFont(size=13, weight="bold"))
            else:
                btn.configure(fg_color="transparent", text_color=MUTED,
                              font=ctk.CTkFont(size=13))

    # ── Overview tab ───────────────────────────────────────────────────────────

    def _build_overview(self, parent: tk.Widget) -> ctk.CTkScrollableFrame:
        frame = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)

        # Identity
        id_card = SectionCard(frame)
        id_card.pack(fill="x", padx=24, pady=(16, 0))
        self._id_rows: dict[str, ctk.CTkLabel] = {}
        for key in ("Agent ID", "MT5 Login", "MT5 Server"):
            self._id_rows[key] = _info_row(id_card.body, key, "—")

        # Metrics
        section_rule(frame, "LIVE METRICS").pack(fill="x", padx=24, pady=(20, 8))
        metrics_row = ctk.CTkFrame(frame, fg_color="transparent")
        metrics_row.pack(fill="x", padx=24)
        self._metric_widgets: dict[str, ctk.CTkLabel] = {}
        for label in ("Balance", "Equity", "Open Trades", "Uptime", "MT5", "IPC"):
            card = ctk.CTkFrame(
                metrics_row, fg_color=SURFACE_RAISED,
                corner_radius=8, border_width=1, border_color=LINE,
            )
            card.pack(side="left", fill="x", expand=True, padx=4)
            ctk.CTkLabel(card, text=label,
                         font=ctk.CTkFont(size=10), text_color=MUTED).pack(pady=(10, 1))
            val = ctk.CTkLabel(card, text="—",
                               font=ctk.CTkFont(family="Consolas", size=15, weight="bold"),
                               text_color=TEXT_SOFT)
            val.pack(pady=(0, 10))
            self._metric_widgets[label] = val

        # Controls
        section_rule(frame, "CONTROLS").pack(fill="x", padx=24, pady=(20, 8))
        ctrl_card = SectionCard(frame)
        ctrl_card.pack(fill="x", padx=24)
        ctrl_row = ctk.CTkFrame(ctrl_card.body, fg_color="transparent")
        ctrl_row.pack(fill="x", pady=(0, 4))

        self._btn_pause = ctk.CTkButton(
            ctrl_row, text="⏸  Pause", width=120, height=38,
            fg_color=SURFACE_RAISED, hover_color=WARNING_BG,
            border_width=1, border_color=LINE, text_color=TEXT_SOFT,
            font=ctk.CTkFont(size=12),
            command=lambda: self._send_command("pause"),
        )
        self._btn_pause.pack(side="left", padx=(0, 8))

        self._btn_resume = ctk.CTkButton(
            ctrl_row, text="▶  Resume", width=120, height=38,
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER, text_color=GREEN,
            font=ctk.CTkFont(size=12),
            command=lambda: self._send_command("resume"),
        )
        self._btn_resume.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            ctrl_row, text="⛔  Emergency Stop", width=160, height=38,
            fg_color=DANGER_BG, hover_color=DANGER_BORDER,
            border_width=1, border_color=DANGER_BORDER, text_color=RED,
            font=ctk.CTkFont(size=12),
            command=self._emergency_stop,
        ).pack(side="left")

        self._ctrl_banner = ActionBanner(ctrl_card.body)
        self._ctrl_banner.pack(fill="x", pady=(8, 0))
        self._ctrl_banner.hide()

        # Logs
        section_rule(frame, "AGENT LOGS").pack(fill="x", padx=24, pady=(20, 8))
        log_card = SectionCard(frame)
        log_card.pack(fill="x", padx=24, pady=(0, 24))
        log_toolbar = ctk.CTkFrame(log_card.body, fg_color="transparent")
        log_toolbar.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(
            log_toolbar, text="↺ Refresh", width=90, height=28,
            fg_color="transparent", hover_color=LINE,
            border_width=1, border_color=LINE, text_color=MUTED,
            font=ctk.CTkFont(size=11),
            command=self._load_logs,
        ).pack(side="left")

        self._log_box = ctk.CTkTextbox(
            log_card.body,
            font=ctk.CTkFont(family="Consolas", size=10),
            fg_color="#060810", text_color=TEXT_SOFT,
            corner_radius=4, wrap="none", state="disabled",
            height=260,
        )
        self._log_box.pack(fill="x")
        inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
        for lvl, col in (("DEBUG", MUTED), ("INFO", TEXT_SOFT),
                         ("WARNING", YELLOW), ("ERROR", RED), ("CRITICAL", RED)):
            inner.tag_config(lvl, foreground=col)

        return frame

    # ── Risk tab ───────────────────────────────────────────────────────────────

    def _build_risk(self, parent: tk.Widget) -> ctk.CTkScrollableFrame:
        frame = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        self._risk_vars: dict[str, tk.StringVar] = {}
        self._risk_switch_throttle = tk.BooleanVar(value=True)
        self._risk_switch_hedging  = tk.BooleanVar(value=True)

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(fill="x", padx=24, pady=(16, 0))

        section_rule(inner, "DAILY BUDGET").pack(fill="x", pady=(0, 8))
        budget_card = SectionCard(inner)
        budget_card.pack(fill="x", pady=(0, 16))

        _risk_field(budget_card.body, "Daily Loss Limit", "risk.max_daily_loss_percent", "%",
                    "Stop trading for the day once this % of balance is lost.",
                    self._risk_vars, self._update_formula)
        _risk_field(budget_card.body, "Max Losing Streak", "risk.max_losing_streak", "trades",
                    "Consecutive losses used to divide daily budget into per-trade risk.",
                    self._risk_vars, self._update_formula)

        formula_row = ctk.CTkFrame(budget_card.body, fg_color=BASE,
                                   corner_radius=6, border_width=1, border_color=LINE)
        formula_row.pack(fill="x", pady=(10, 0))
        fi = ctk.CTkFrame(formula_row, fg_color="transparent")
        fi.pack(padx=16, pady=10, fill="x")
        ctk.CTkLabel(fi, text="Risk per trade",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left")
        self._lbl_formula = ctk.CTkLabel(fi, text="--",
                                          font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
                                          text_color=GREEN)
        self._lbl_formula.pack(side="left", padx=(12, 0))
        self._lbl_formula_detail = ctk.CTkLabel(fi, text="",
                                                 font=ctk.CTkFont(size=11), text_color=MUTED)
        self._lbl_formula_detail.pack(side="left", padx=(8, 0))

        section_rule(inner, "EQUITY PROTECTION").pack(fill="x", pady=(0, 8))
        eq_card = SectionCard(inner)
        eq_card.pack(fill="x", pady=(0, 16))

        _risk_field(eq_card.body, "Max Profit Drawdown", "risk.max_profit_drawdown_percent", "%",
                    "Pauses until midnight if today's profit gives back this % from its peak.",
                    self._risk_vars)

        _toggle_row(eq_card.body, "Drawdown Risk Throttle",
                    "Halves position risk while recent results sit deep below peak.",
                    self._risk_switch_throttle)

        section_rule(inner, "ORDER LIMITS").pack(fill="x", pady=(0, 8))
        lim_card = SectionCard(inner)
        lim_card.pack(fill="x", pady=(0, 16))

        _risk_field(lim_card.body, "Max Lot Size", "risk.max_lot_size", "lots",
                    "Hard cap on position size regardless of account or risk calculation.",
                    self._risk_vars)

        _toggle_row(lim_card.body, "Prevent Hedging",
                    "Block opposite-direction trades on the same symbol.",
                    self._risk_switch_hedging)

        self._risk_banner = ActionBanner(inner)
        self._risk_banner.pack(fill="x", pady=(8, 0))
        self._risk_banner.hide()

        ctk.CTkButton(
            inner, text="Save & Apply to Agent",
            height=44, width=220,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER, text_color=GREEN,
            command=self._save_risk,
        ).pack(pady=(8, 24))

        frame.on_tab_enter = self._load_risk  # type: ignore[attr-defined]
        return frame

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent: tk.Widget) -> ctk.CTkScrollableFrame:
        frame = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        self._settings_rows: dict[str, ctk.CTkLabel] = {}

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(fill="x", padx=24, pady=(16, 0))

        section_rule(inner, "AGENT CONFIGURATION").pack(fill="x", pady=(0, 8))
        cfg_card = SectionCard(inner)
        cfg_card.pack(fill="x", pady=(0, 16))

        for key in ("MT5 Login", "MT5 Server", "MT5 Path", "Symbols", "Storage Path"):
            self._settings_rows[key] = _info_row(cfg_card.body, key, "—")

        section_rule(inner, "CONFIG FILE").pack(fill="x", pady=(0, 8))
        path_card = SectionCard(inner)
        path_card.pack(fill="x", pady=(0, 16))
        self._lbl_config_path = ctk.CTkLabel(
            path_card.body, text="—",
            font=ctk.CTkFont(family="Consolas", size=10), text_color=MUTED,
            anchor="w", wraplength=580,
        )
        self._lbl_config_path.pack(anchor="w")
        ctk.CTkButton(
            path_card.body, text="Open folder", width=110, height=28,
            fg_color="transparent", hover_color=LINE,
            border_width=1, border_color=LINE, text_color=MUTED,
            font=ctk.CTkFont(size=11),
            command=self._open_config_folder,
        ).pack(anchor="w", pady=(8, 0))

        frame.on_tab_enter = self._load_settings_tab  # type: ignore[attr-defined]
        return frame

    # ── Navigation hooks ───────────────────────────────────────────────────────

    def on_navigate_to(self) -> None:
        aid = self.app.manager_state.selected_agent_id
        if aid and aid != self._agent_id:
            self._agent_id = aid
            self._refresh_from_state()
            self._load_logs()
        elif aid == self._agent_id:
            self._refresh_from_state()

    def _go_back(self) -> None:
        self.app.navigate("Agents")

    # ── State refresh ──────────────────────────────────────────────────────────

    def _on_agents_updated(self, agents: list["AgentCardState"]) -> None:
        if not self._agent_id:
            return
        agent = next((a for a in agents if a.agent_id == self._agent_id), None)
        if agent:
            self.after(0, lambda a=agent: self._apply_agent(a))

    def _refresh_from_state(self) -> None:
        if not self._agent_id:
            return
        agent = self.app.manager_state.get_agent(self._agent_id)
        if agent:
            self._apply_agent(agent)

    def _apply_agent(self, agent: "AgentCardState") -> None:
        self._lbl_title.configure(text=agent.display_name or agent.agent_id)
        color, bg, border = _STATUS_COLOUR.get(agent.status, (MUTED, SURFACE_RAISED, LINE))
        self._status_pill.configure(fg_color=bg, border_color=border)
        self._lbl_status.configure(text=agent.status, text_color=color)

        self._id_rows["Agent ID"].configure(text=agent.agent_id)
        self._id_rows["MT5 Login"].configure(text=str(agent.mt5_login or "—"))
        self._id_rows["MT5 Server"].configure(text=agent.mt5_server or "—")

        bal = f"${agent.balance:,.2f}" if agent.balance is not None else "—"
        eq  = f"${agent.equity:,.2f}"  if agent.equity  is not None else "—"
        self._metric_widgets["Balance"].configure(text=bal)
        self._metric_widgets["Equity"].configure(text=eq)
        self._metric_widgets["Open Trades"].configure(text=str(agent.open_trades))
        self._metric_widgets["Uptime"].configure(text=_fmt_uptime(agent.uptime_sec))
        self._metric_widgets["MT5"].configure(
            text="OK" if agent.mt5_connected else "✕",
            text_color=GREEN if agent.mt5_connected else RED,
        )
        self._metric_widgets["IPC"].configure(
            text="OK" if agent.gateway_connected else "✕",
            text_color=GREEN if agent.gateway_connected else RED,
        )
        can_command = agent.status in ("RUNNING", "DEGRADED")
        self._btn_pause.configure(state="normal" if can_command else "disabled")
        self._btn_resume.configure(state="normal" if can_command else "disabled")

    # ── Commands ───────────────────────────────────────────────────────────────

    def _send_command(self, command: str) -> None:
        if not self._agent_id:
            return
        self._ctrl_banner.show(f"Sending {command}…", "info")
        self.app.manager_client.send_agent_command(
            self._agent_id, command,
            on_done=lambda ok, err: self.after(0, lambda: self._on_command_done(command, ok, err)),
        )

    def _emergency_stop(self) -> None:
        if not self._agent_id:
            return
        self._ctrl_banner.show("Sending emergency stop…", "warn")
        self.app.manager_client.send_agent_command(
            self._agent_id, "emergency_stop",
            on_done=lambda ok, err: self.after(0, lambda: self._on_command_done("emergency_stop", ok, err)),
        )

    def _on_command_done(self, command: str, ok: bool, error: str | None) -> None:
        if ok:
            self._ctrl_banner.show(f"{command} acknowledged.", "good", auto_dismiss_after_ms=3000)
        else:
            self._ctrl_banner.show(f"Command failed: {error or 'unknown error'}", "danger")

    # ── Logs ───────────────────────────────────────────────────────────────────

    def _load_logs(self) -> None:
        if not self._agent_id:
            return
        self.app.manager_client.get_agent_logs(
            self._agent_id, lines=300,
            on_done=lambda lines: self.after(0, lambda ls=lines: self._show_logs(ls)),
        )

    def _show_logs(self, lines: list[str]) -> None:
        inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
        self._log_box.configure(state="normal")
        inner.delete("1.0", "end")
        for line in lines:
            lvl = _log_level(line)
            inner.insert("end", line + "\n", lvl)
        self._log_box.configure(state="disabled")
        inner.see("end")

    # ── Risk tab helpers ───────────────────────────────────────────────────────

    def _agent_config_path(self) -> Path | None:
        if not self._agent_id:
            return None
        from manager.gui.config_manager import ConfigManager
        return ConfigManager.programdata_agents_path() / self._agent_id / "config.yaml"

    def _read_agent_config(self) -> dict:
        p = self._agent_config_path()
        if p and p.exists():
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    return yaml.safe_load(fh) or {}
            except Exception:
                pass
        return {}

    def _load_risk(self) -> None:
        cfg  = self._read_agent_config()
        risk = cfg.get("risk", {})
        throttle = risk.get("equity_throttle") or {}

        _FIELD_MAP = {
            "risk.max_daily_loss_percent":      ("max_daily_loss_percent",      "2.5"),
            "risk.max_losing_streak":           ("max_losing_streak",           "3"),
            "risk.max_profit_drawdown_percent": ("max_profit_drawdown_percent", "2.0"),
            "risk.max_lot_size":                ("max_lot_size",                "100.0"),
        }
        for key, (field, default) in _FIELD_MAP.items():
            if key in self._risk_vars:
                v = risk.get(field)
                self._risk_vars[key].set(str(v) if v is not None else default)

        self._risk_switch_hedging.set(bool(risk.get("no_hedging", True)))
        self._risk_switch_throttle.set(bool(throttle.get("enabled", True)))
        self._update_formula()
        self._risk_banner.hide()

    def _update_formula(self, *_) -> None:
        try:
            limit_pct = float(self._risk_vars["risk.max_daily_loss_percent"].get())
            streak    = int(float(self._risk_vars["risk.max_losing_streak"].get()))
            if streak < 1:
                raise ValueError
            per_trade = limit_pct / streak
            self._lbl_formula.configure(text=f"{per_trade:.2f}%", text_color=GREEN)
            self._lbl_formula_detail.configure(text=f"({limit_pct:.1f}% ÷ {streak} trades)")
        except Exception:
            self._lbl_formula.configure(text="--", text_color=MUTED)
            self._lbl_formula_detail.configure(text="")

    def _save_risk(self) -> None:
        if not self._agent_id:
            return

        _WRITE_MAP = {
            "risk.max_daily_loss_percent":      ("max_daily_loss_percent",      float),
            "risk.max_losing_streak":           ("max_losing_streak",           int),
            "risk.max_profit_drawdown_percent": ("max_profit_drawdown_percent", float),
            "risk.max_lot_size":                ("max_lot_size",                float),
        }
        errors: list[str] = []
        updates: dict = {}
        for key, (field, typ) in _WRITE_MAP.items():
            if key not in self._risk_vars:
                continue
            raw = self._risk_vars[key].get().strip()
            if not raw:
                continue
            try:
                updates[field] = typ(raw)
            except Exception:
                errors.append(f"'{field}' is not a valid number")

        if errors:
            self._risk_banner.show("  |  ".join(errors), "warn")
            return

        updates["no_hedging"] = self._risk_switch_hedging.get()
        updates["equity_throttle"] = {"enabled": self._risk_switch_throttle.get()}

        patch = {"risk": updates}
        self._risk_banner.show("Saving and restarting agent…", "info")

        self.app.manager_client.patch_agent_config(
            self._agent_id, patch,
            on_done=lambda r: self.after(0, lambda result=r: self._on_risk_saved(result)),
        )

    def _on_risk_saved(self, result: dict) -> None:
        if result.get("error"):
            self._risk_banner.show(f"Failed: {result['error']}", "danger")
        else:
            self._risk_banner.show(
                "Config saved. Agent is restarting with new risk settings.",
                "good", auto_dismiss_after_ms=5000,
            )

    # ── Settings tab helpers ───────────────────────────────────────────────────

    def _load_settings_tab(self) -> None:
        cfg  = self._read_agent_config()
        mt5  = cfg.get("mt5",     {})
        gw   = cfg.get("gateway", {})
        eng  = cfg.get("engine",  {})

        self._settings_rows["MT5 Login"].configure(text=str(mt5.get("login",  "—")))
        self._settings_rows["MT5 Server"].configure(text=str(mt5.get("server", "—")))
        self._settings_rows["MT5 Path"].configure(text=str(mt5.get("path",   "—")))
        syms = gw.get("symbols") or []
        self._settings_rows["Symbols"].configure(
            text=", ".join(syms) if syms else "—",
        )
        self._settings_rows["Storage Path"].configure(
            text=str(eng.get("storage_path", "—")),
        )

        p = self._agent_config_path()
        self._lbl_config_path.configure(text=str(p) if p else "—")

    def _open_config_folder(self) -> None:
        import subprocess
        p = self._agent_config_path()
        if p:
            target = p.parent if p.is_file() else p
            try:
                target.mkdir(parents=True, exist_ok=True)
                subprocess.Popen(["explorer", str(target)])
            except Exception:
                pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _info_row(parent: tk.Widget, label: str, value: str) -> ctk.CTkLabel:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=4)
    ctk.CTkLabel(row, text=label, width=110, anchor="w",
                 font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left")
    val = ctk.CTkLabel(row, text=value, anchor="w",
                       font=ctk.CTkFont(family="Consolas", size=12), text_color=TEXT)
    val.pack(side="left", fill="x", expand=True)
    return val


def _risk_field(
    parent: tk.Widget, label: str, key: str, unit: str, tip: str,
    vars_dict: dict, on_change=None,
) -> None:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=(0, 12))
    left = ctk.CTkFrame(row, fg_color="transparent")
    left.pack(side="left", fill="x", expand=True)
    ctk.CTkLabel(left, text=label, anchor="w",
                 font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(left, text=tip, anchor="w",
                 font=ctk.CTkFont(size=11), text_color=MUTED,
                 justify="left", wraplength=420).pack(anchor="w", pady=(1, 0))
    right = ctk.CTkFrame(row, fg_color="transparent")
    right.pack(side="right", padx=(16, 0))
    var = tk.StringVar()
    if on_change:
        var.trace_add("write", on_change)
    vars_dict[key] = var
    ctk.CTkEntry(right, textvariable=var, width=80,
                 font=ctk.CTkFont(family="Consolas", size=13), justify="center").pack(side="left")
    ctk.CTkLabel(right, text=unit,
                 font=ctk.CTkFont(size=12), text_color=MUTED).pack(side="left", padx=(6, 0))


def _toggle_row(parent: tk.Widget, label: str, tip: str, var: tk.BooleanVar) -> None:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=(0, 4))
    left = ctk.CTkFrame(row, fg_color="transparent")
    left.pack(side="left", fill="x", expand=True)
    ctk.CTkLabel(left, text=label, anchor="w",
                 font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(left, text=tip, anchor="w",
                 font=ctk.CTkFont(size=11), text_color=MUTED,
                 justify="left", wraplength=420).pack(anchor="w", pady=(1, 0))
    ctk.CTkSwitch(row, text="", variable=var,
                  onvalue=True, offvalue=False).pack(side="right")


def _fmt_uptime(sec: int) -> str:
    if sec <= 0:
        return "—"
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log_level(line: str) -> str:
    for lvl in ("CRITICAL", "ERROR", "WARNING", "DEBUG", "INFO"):
        if lvl in line:
            return lvl
    return "INFO"
