"""
src/gui/pages/advanced.py

License key management, internal status display, and service management.

Users can edit:
  - License key (gateway.activation_key)
  - Trading pairs (gateway.symbols) — toggle per entitlement

Everything else is read-only status — users cannot edit gateway URL,
execution parameters, engine internals, or risk guardrails from here.
"""
from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    SURFACE, SURFACE_RAISED, BASE, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER,
    WARNING_BG, WARNING_BORDER,
    INFO_BG, INFO_BORDER,
    section_rule, page_header,
)
from src.gui.components import SectionCard, ActionBanner, PrimaryButton, InfoTable

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI


class AdvancedPage(ctk.CTkScrollableFrame):
    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._var_key = tk.StringVar()
        self._build()
        self._load()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Advanced", "License key and internal status")

        # ── License Key ───────────────────────────────────────────────────────
        section_rule(self, "LICENSE KEY").pack(fill="x", padx=24, pady=(20, 8))

        key_card = SectionCard(self)
        key_card.pack(fill="x", padx=24)

        ctk.CTkLabel(
            key_card.body,
            text="Your activation key connects this installation to your Apex Quantel account.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=540, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        key_row = ctk.CTkFrame(key_card.body, fg_color="transparent")
        key_row.pack(fill="x")
        ctk.CTkLabel(
            key_row, text="License Key", width=110, anchor="w",
            font=ctk.CTkFont(size=12), text_color=TEXT,
        ).pack(side="left")
        ctk.CTkEntry(
            key_row, textvariable=self._var_key, width=320, show="●",
            font=ctk.CTkFont(family="Consolas", size=12),
            placeholder_text="XXXX-XXXX-XXXX-XXXX",
        ).pack(side="left", padx=(8, 8))

        self._key_banner = ActionBanner(key_card.body)
        self._key_banner.pack(fill="x", pady=(8, 0))
        self._key_banner.hide()

        PrimaryButton(
            key_card.body, text="Save License Key", tone="good",
            width=160, height=34,
            command=self._save_key,
        ).pack(anchor="w", pady=(12, 0))

        # ── Trading Pairs ─────────────────────────────────────────────────────
        section_rule(self, "TRADING PAIRS").pack(fill="x", padx=24, pady=(24, 8))

        pairs_card = SectionCard(self)
        pairs_card.pack(fill="x", padx=24)

        ctk.CTkLabel(
            pairs_card.body,
            text="Select the pairs you want to receive signals for. "
                 "Changes take effect after the engine restarts.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=540, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        self._sym_toggles_wrap = ctk.CTkFrame(pairs_card.body, fg_color="transparent")
        self._sym_toggles_wrap.pack(fill="x")
        self._sym_vars: dict[str, tk.BooleanVar] = {}

        self._pairs_banner = ActionBanner(self)
        self._pairs_banner.pack(fill="x", padx=24, pady=(4, 0))
        self._pairs_banner.hide()

        # ── Gateway Status ────────────────────────────────────────────────────
        section_rule(self, "GATEWAY STATUS").pack(fill="x", padx=24, pady=(24, 8))

        self._gw_card = SectionCard(self)
        self._gw_card.pack(fill="x", padx=24)
        self._gw_table = InfoTable(self._gw_card.body)
        self._gw_table.pack(fill="x")

        # ── Internal Protections ──────────────────────────────────────────────
        section_rule(self, "INTERNAL PROTECTIONS").pack(fill="x", padx=24, pady=(24, 8))

        ctk.CTkLabel(
            self,
            text="These settings are managed by the engine to ensure consistent, safe execution. "
                 "They are not user-editable.",
            font=ctk.CTkFont(size=11), text_color=MUTED,
            wraplength=580, justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 8))

        mt5_card = SectionCard(self)
        mt5_card.pack(fill="x", padx=24, pady=(0, 10))
        _section_label(mt5_card.body, "MT5 Trade Tracking")
        _status_row(mt5_card.body, "Magic Number",       "Managed by engine")
        _status_row(mt5_card.body, "Execution Slippage", "Platform policy")
        _status_row(mt5_card.body, "Trade Attribution",  "Protected")

        exec_card = SectionCard(self)
        exec_card.pack(fill="x", padx=24, pady=(0, 10))
        _section_label(exec_card.body, "Execution Protection")
        _status_row(exec_card.body, "Breakeven Logic",       "Enabled")
        _status_row(exec_card.body, "Signal Expiry",         "120 seconds")
        _status_row(exec_card.body, "Order Retry",           "2 attempts")
        _status_row(exec_card.body, "Slippage Protection",   "Enabled")
        _status_row(exec_card.body, "TP1 Logic",             "Engine managed")

        risk_card = SectionCard(self)
        risk_card.pack(fill="x", padx=24, pady=(0, 10))
        _section_label(risk_card.body, "Risk Guardrails")
        _status_row(risk_card.body, "Spread / SL Protection",  "Enabled")
        _status_row(risk_card.body, "Symbol Exposure Guard",   "Enabled")
        _status_row(risk_card.body, "Rolling Drawdown Guard",  "Enabled")
        _status_row(risk_card.body, "Cluster Risk Guard",      "Engine managed")

        # ── Service Management ────────────────────────────────────────────────
        section_rule(self, "SERVICE MANAGEMENT").pack(fill="x", padx=24, pady=(24, 8))

        svc_card = SectionCard(self)
        svc_card.pack(fill="x", padx=24)

        ctk.CTkLabel(
            svc_card.body,
            text="Reinstall if the AQ Agent executable has changed or if the task is not starting correctly.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=540, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(svc_card.body, fg_color="transparent")
        btn_row.pack(anchor="w")

        ctk.CTkButton(
            btn_row, text="Reinstall Service", width=160, height=34,
            fg_color=WARNING_BG, hover_color="#2a2210",
            border_width=1, border_color=WARNING_BORDER,
            text_color=YELLOW,
            command=self._reinstall,
        ).pack(side="left", padx=(0, 12))

        self._lbl_svc = ctk.CTkLabel(
            btn_row, text="",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self._lbl_svc.pack(side="left")

        # ── Diagnostics ───────────────────────────────────────────────────────
        section_rule(self, "DIAGNOSTICS").pack(fill="x", padx=24, pady=(24, 8))

        diag_card = SectionCard(self)
        diag_card.pack(fill="x", padx=24, pady=(0, 24))

        _diag_row(
            diag_card.body, "View logs",
            "Open the log folder to inspect recent engine output.",
            "Open Logs",
            self._open_logs,
        )
        _diag_row(
            diag_card.body, "Open data folder",
            "Browse the local trade data and storage files.",
            "Open Folder",
            self._open_data,
        )
        _diag_row(
            diag_card.body, "Export diagnostics",
            "Copy a diagnostic summary to the clipboard for support.",
            "Copy Info",
            self._export_diagnostics,
        )

        self._diag_banner = ActionBanner(diag_card.body)
        self._diag_banner.pack(fill="x", pady=(4, 0))
        self._diag_banner.hide()

    # ── Load ──────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        cfg = self.app.config.load(force=True)
        self._var_key.set(cfg.get("gateway", {}).get("activation_key", ""))
        self._refresh_gateway_table(cfg)
        self._refresh_pair_toggles(cfg)

    def _refresh_pair_toggles(self, cfg: dict) -> None:
        from src.gui.onboarding import _SYMBOL_LABELS
        gw = cfg.get("gateway", {})
        # All symbols the license entitles — stored after preflight during onboarding.
        # Fall back to current selection + defaults so existing installs still work.
        known = list(gw.get("symbols", ["XAUUSD"]))
        enabled = set(known)

        for w in self._sym_toggles_wrap.winfo_children():
            w.destroy()
        self._sym_vars.clear()

        if not known:
            ctk.CTkLabel(
                self._sym_toggles_wrap,
                text="No pairs configured. Re-run setup to configure your license.",
                font=ctk.CTkFont(size=12), text_color=MUTED,
            ).pack(anchor="w")
            return

        for sym in known:
            var = tk.BooleanVar(value=sym in enabled)
            self._sym_vars[sym] = var

            row = ctk.CTkFrame(
                self._sym_toggles_wrap,
                fg_color=SURFACE_RAISED, corner_radius=8,
                border_width=1, border_color=LINE,
            )
            row.pack(fill="x", pady=3)
            inner = ctk.CTkFrame(row, fg_color="transparent")
            inner.pack(padx=14, pady=8, fill="x")

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
                ).pack(anchor="w")

            ctk.CTkSwitch(
                inner, text="", variable=var,
                onvalue=True, offvalue=False, width=46,
                command=self._save_pairs,
            ).pack(side="right")

    def _refresh_gateway_table(self, cfg: dict) -> None:
        gw      = cfg.get("gateway", {})
        version = gw.get("engine_version", "—")
        for widget in self._gw_table.winfo_children():
            widget.destroy()
        self._gw_table.add_row("Gateway URL",    gw.get("ws_url", "—"))
        self._gw_table.add_row("Engine Version", version)

    def _save_pairs(self) -> None:
        selected = [sym for sym, var in self._sym_vars.items() if var.get()]
        if not selected:
            self._pairs_banner.show("At least one pair must be enabled.", "warn")
            # Re-check the first var to keep at least one on
            if self._sym_vars:
                first = next(iter(self._sym_vars))
                self._sym_vars[first].set(True)
            return
        err = self.app.config.update("gateway", {"symbols": selected})
        if err:
            self._pairs_banner.show(err, "danger")
        else:
            self._pairs_banner.show(
                "Pairs saved — restart the engine for changes to take effect.",
                "good",
                auto_dismiss_after_ms=4000,
            )

    # ── Actions ───────────────────────────────────────────────────────────────

    def _save_key(self) -> None:
        key = self._var_key.get().strip()
        if not key:
            self._key_banner.show("License key cannot be empty.", "warn")
            return
        if len(key) < 16:
            self._key_banner.show("License key is too short — check that you copied it correctly.", "warn")
            return
        err = self.app.config.update("gateway", {"activation_key": key})
        if err:
            self._key_banner.show(err, "danger")
            return
        self._key_banner.show("License key saved — restarting engine…", "good")
        self.app.app_state.mark_setup_complete(self.app.config.is_setup_complete())
        threading.Thread(target=self._delayed_restart, daemon=True).start()

    def _delayed_restart(self) -> None:
        import time
        time.sleep(0.4)
        self.app.restart_with_new_config()

    def _reinstall(self) -> None:
        self._lbl_svc.configure(text="Reinstalling…", text_color=YELLOW)
        self.app.installer.on_result = lambda ok, msg: self.after(
            0,
            lambda: self._lbl_svc.configure(
                text=msg[:80], text_color=GREEN if ok else RED,
            ),
        )
        self.app.installer.reinstall_async(str(self.app.config.path))

    def _open_logs(self) -> None:
        from src.gui.config_manager import ConfigManager
        path = ConfigManager.programdata_logs_path()
        try:
            path.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(path)])
        except Exception:
            pass

    def _open_data(self) -> None:
        from src.gui.config_manager import ConfigManager
        path = ConfigManager.programdata_data_path()
        try:
            path.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(path)])
        except Exception:
            pass

    def _export_diagnostics(self) -> None:
        import sys
        cfg = self.app.config.masked_copy()
        gw  = cfg.get("gateway", {})
        mt5 = cfg.get("mt5",     {})
        eng = cfg.get("engine",  {})
        lines = [
            f"Apex Quantel — Diagnostics",
            f"Engine Version : {gw.get('engine_version', '?')}",
            f"Python         : {sys.version.split()[0]}",
            f"MT5 Login      : {mt5.get('login', '?')}",
            f"MT5 Server     : {mt5.get('server', '?')}",
            f"Gateway URL    : {gw.get('ws_url', '?')}",
            f"Log Level      : {eng.get('log_level', '?')}",
            f"Config Path    : {self.app.config.path}",
        ]
        text = "\n".join(lines)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._diag_banner.show("Diagnostics copied to clipboard.", "good", auto_dismiss_after_ms=3000)
        except Exception:
            self._diag_banner.show("Could not copy to clipboard.", "warn")

    # ── Broadcast callbacks ───────────────────────────────────────────────────

    def on_engine_status(self, status: str, detail: str | None) -> None:
        from src.gui.service_controller import ServiceStatus
        if status == ServiceStatus.STOPPED and detail:
            self._lbl_svc.configure(text=detail[:80], text_color=MUTED)
        elif status == ServiceStatus.RUNNING:
            self._lbl_svc.configure(text="")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _section_label(parent: tk.Widget, text: str) -> None:
    ctk.CTkLabel(
        parent, text=text, anchor="w",
        font=ctk.CTkFont(size=11, weight="bold"), text_color=MUTED,
    ).pack(anchor="w", pady=(0, 6))


def _status_row(parent: tk.Widget, label: str, value: str) -> None:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=3)
    ctk.CTkLabel(
        row, text=label, width=200, anchor="w",
        font=ctk.CTkFont(size=12), text_color=TEXT,
    ).pack(side="left")
    ctk.CTkLabel(
        row, text=value, anchor="w",
        font=ctk.CTkFont(size=12), text_color=MUTED,
    ).pack(side="left", padx=(8, 0))


def _diag_row(
    parent: tk.Widget,
    label: str,
    detail: str,
    btn_label: str,
    command,
) -> None:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=6)
    left = ctk.CTkFrame(row, fg_color="transparent")
    left.pack(side="left", fill="x", expand=True)
    ctk.CTkLabel(left, text=label, anchor="w", font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(left, text=detail, anchor="w", font=ctk.CTkFont(size=11), text_color=MUTED,
                 wraplength=400).pack(anchor="w")
    PrimaryButton(row, text=btn_label, tone="info", width=110, height=30, command=command).pack(side="right")
