"""
src/gui/pages/engine.py — Engine control page.

Shows service status, heartbeat, and provides Start / Stop / Restart controls.
Also exposes the Install / Uninstall service action (via InstallerService).
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE_RAISED, LINE, LINE_STRONG,
    DANGER_BG, DANGER_BORDER, INFO_BG, INFO_BORDER,
    SUCCESS_BG, SUCCESS_BORDER, WARNING_BG, WARNING_BORDER,
    section_rule, info_row, page_header,
)
from src.gui.components import ActionBanner, PrimaryButton

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI


class EnginePage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.app = app
        self._current_status = "unknown"
        self._ws_connected   = False
        self._build()
        self._tick()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "AQ Agent", "Task control and status")

        content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=16)

        # ── Status card ────────────────────────────────────────────────────────
        section_rule(content, "SERVICE STATUS").pack(fill="x", pady=(0, 8))

        status_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        status_card.pack(fill="x", pady=(0, 16))

        self._accent_bar = ctk.CTkFrame(status_card, height=3, fg_color=MUTED, corner_radius=0)
        self._accent_bar.pack(fill="x")

        inner = ctk.CTkFrame(status_card, fg_color="transparent")
        inner.pack(padx=20, pady=14, fill="x")

        self._lbl_status_dot = ctk.CTkLabel(
            inner, text="●  Checking…",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=MUTED,
        )
        self._lbl_status_dot.pack(anchor="w")

        sub_row = ctk.CTkFrame(inner, fg_color="transparent")
        sub_row.pack(fill="x", pady=(10, 0))

        self._lbl_heartbeat = ctk.CTkLabel(
            sub_row, text="Last signal: --",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._lbl_heartbeat.pack(side="left", padx=(0, 24))

        self._lbl_gateway = ctk.CTkLabel(
            sub_row, text="Gateway: Disconnected",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._lbl_gateway.pack(side="left")

        # ── Controls ───────────────────────────────────────────────────────────
        section_rule(content, "CONTROLS").pack(fill="x", pady=(8, 8))

        btn_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        btn_card.pack(fill="x", pady=(0, 6))

        btn_inner = ctk.CTkFrame(btn_card, fg_color="transparent")
        btn_inner.pack(padx=20, pady=16)

        self._btn_start = ctk.CTkButton(
            btn_inner, text="▶  Start AQ Agent", width=160, height=44,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER,
            text_color=GREEN,
            command=self._start,
        )
        self._btn_start.grid(row=0, column=0, padx=8, pady=4)

        self._btn_stop = ctk.CTkButton(
            btn_inner, text="■  Stop AQ Agent", width=160, height=44,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=DANGER_BG, hover_color=DANGER_BORDER,
            border_width=1, border_color=DANGER_BORDER,
            text_color=RED,
            command=self._stop,
        )
        self._btn_stop.grid(row=0, column=1, padx=8, pady=4)

        self._btn_restart = ctk.CTkButton(
            btn_inner, text="↺  Restart AQ Agent", width=160, height=44,
            font=ctk.CTkFont(size=13),
            fg_color=INFO_BG, hover_color=INFO_BORDER,
            border_width=1, border_color=INFO_BORDER,
            text_color=INFO,
            command=self._restart,
        )
        self._btn_restart.grid(row=0, column=2, padx=8, pady=4)

        self._action_banner = ActionBanner(content)
        self._action_banner.pack(fill="x", pady=(0, 8))
        self._action_banner.hide()

        # ── Error panel ────────────────────────────────────────────────────────
        self._error_banner = ActionBanner(content)
        self._error_banner.pack(fill="x", pady=(0, 12))
        self._error_banner.hide()

        # ── Service installation ───────────────────────────────────────────────
        section_rule(content, "SERVICE INSTALLATION").pack(fill="x", pady=(8, 8))

        install_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        install_card.pack(fill="x", pady=(0, 16))

        inst_inner = ctk.CTkFrame(install_card, fg_color="transparent")
        inst_inner.pack(padx=20, pady=14, fill="x")

        ctk.CTkLabel(
            inst_inner,
            text="AQ Agent runs as a Windows Task Scheduler task. "
                 "Install it once and it will start automatically when you log in.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=560, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(inst_inner, fg_color="transparent")
        btn_row.pack(fill="x")

        self._btn_install = PrimaryButton(
            btn_row, text="Install Service", tone="good", width=150, height=38,
            command=self._install,
        )
        self._btn_install.pack(side="left", padx=(0, 8))

        PrimaryButton(
            btn_row, text="Reinstall / Update", tone="info", width=160, height=38,
            command=self._reinstall,
        ).pack(side="left", padx=(0, 8))

        PrimaryButton(
            btn_row, text="Uninstall Service", tone="danger", width=150, height=38,
            command=self._uninstall,
        ).pack(side="left")

        ctk.CTkLabel(
            inst_inner,
            text="A Windows Administrator prompt will appear. Click Yes to continue.",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        ).pack(anchor="w", pady=(8, 0))

        self._install_banner = ActionBanner(content)
        self._install_banner.pack(fill="x", pady=(0, 12))
        self._install_banner.hide()

        # ── Technical details ──────────────────────────────────────────────────
        section_rule(content, "TECHNICAL DETAILS").pack(fill="x", pady=(8, 8))

        tech_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        tech_card.pack(fill="x", pady=(0, 16))
        tech_inner = ctk.CTkFrame(tech_card, fg_color="transparent")
        tech_inner.pack(padx=20, pady=14, fill="x")

        port = self.app.config.get("engine", "monitoring_port") or 8080
        info_row(tech_inner, "Task name",       "AQ Agent")
        info_row(tech_inner, "Task folder",     "\\Apex Quantel\\")
        info_row(tech_inner, "Control method",  "Task Scheduler (schtasks)")
        info_row(tech_inner, "UIBridge",        f"ws://localhost:{port}")
        info_row(tech_inner, "Config file",     str(self.app.config.path))

    # ── Heartbeat ticker ──────────────────────────────────────────────────────

    def _tick(self) -> None:
        last = self.app.app_state.last_heartbeat
        if last > 0:
            secs = int(time.time() - last)
            txt   = (
                "Last signal: just now" if secs < 5 else
                f"Last signal: {secs}s ago" if secs < 120 else
                f"Last signal: {secs // 60}m ago"
            )
            color = GREEN if secs < 15 else YELLOW if secs < 60 else RED
            self._lbl_heartbeat.configure(text=txt, text_color=color)
        else:
            self._lbl_heartbeat.configure(text="Last signal: --", text_color=MUTED)
        self.after(1000, self._tick)

    # ── Status callbacks ──────────────────────────────────────────────────────

    def on_trade_event(self, event_type: str, payload: dict) -> None:
        """Show a persistent banner when AutoTrading is disabled in MT5."""
        if event_type == "trade.error" and payload and payload.get("reason") == "AUTOTRADING_DISABLED":
            self.after(0, lambda: self._error_banner.show(
                "⚠  AutoTrading is DISABLED in MT5. "
                "Click the 'Algo Trading' button in your MT5 terminal to enable it.",
                "danger",
            ))

    def on_engine_status(self, status: str, detail: str | None) -> None:
        from src.gui.service_controller import ServiceStatus

        self._current_status = status

        _colors = {
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
        color = _colors.get(status, MUTED)

        self._lbl_status_dot.configure(text=f"●  AQ Agent {label}", text_color=color)
        self._accent_bar.configure(fg_color=color)

        is_running = status == ServiceStatus.RUNNING
        is_stopped = status in (ServiceStatus.STOPPED, ServiceStatus.NOT_INSTALLED)
        is_busy    = status in (ServiceStatus.STARTING, ServiceStatus.STOPPING)

        self._btn_start.configure(state="normal" if is_stopped else "disabled")
        self._btn_stop.configure(state="normal" if is_running else "disabled")
        self._btn_restart.configure(state="normal" if is_running else "disabled")

        # NOT_INSTALLED → show install banner once
        if status == ServiceStatus.NOT_INSTALLED:
            self._install_banner.show(
                "AQ Agent is not installed. Click Install Service below.", "warn",
            )
        elif detail:
            self._error_banner.show(f"⚠  {detail}", "danger")
        else:
            self._error_banner.hide()

        if is_busy:
            self._action_banner.show("Please wait…", "warn")
        else:
            self._action_banner.hide()

    def on_ws_connected(self) -> None:
        self._ws_connected = True
        self._lbl_gateway.configure(text="Gateway: Connected", text_color=GREEN)

    def on_ws_disconnected(self) -> None:
        self._ws_connected = False
        self._lbl_gateway.configure(text="Gateway: Disconnected", text_color=MUTED)

    def on_mt5_error(self, message: str) -> None:
        if ":" in message:
            message = message.split(":", 1)[-1].strip()
        self._error_banner.show(f"MT5 error: {message}", "danger")

    # ── Button actions ────────────────────────────────────────────────────────

    def _start(self) -> None:
        from src.gui.service_controller import ServiceStatus
        if self.app.svc.query() == ServiceStatus.NOT_INSTALLED:
            self._action_banner.show(
                "AQ Agent is not installed. Use Install Service below.", "warn",
            )
            return
        self._action_banner.show("Starting AQ Agent…", "warn")
        threading.Thread(target=self.app.svc.start, daemon=True).start()

    def _stop(self) -> None:
        self._action_banner.show("Stopping AQ Agent…", "warn")
        threading.Thread(target=self.app.svc.stop, daemon=True).start()

    def _restart(self) -> None:
        self._action_banner.show("Restarting AQ Agent…", "warn")
        threading.Thread(target=self.app.svc.restart, daemon=True).start()

    # ── Installer actions ─────────────────────────────────────────────────────

    def _install(self) -> None:
        self._install_banner.show("Installing… A UAC prompt may appear.", "warn")
        self._btn_install.configure(state="disabled")
        self.app.installer.on_result = self._on_install_result
        self.app.installer.install_async(str(self.app.config.path))

    def _reinstall(self) -> None:
        self._install_banner.show("Reinstalling… A UAC prompt may appear.", "warn")
        self.app.installer.on_result = self._on_install_result
        self.app.installer.reinstall_async(str(self.app.config.path))

    def _uninstall(self) -> None:
        self._install_banner.show("Uninstalling… A UAC prompt may appear.", "warn")
        self.app.installer.on_result = self._on_install_result
        self.app.installer.uninstall_async()

    def _on_install_result(self, ok: bool, msg: str) -> None:
        def _apply():
            tone = "good" if ok else "danger"
            self._install_banner.show(msg, tone)
            self._btn_install.configure(state="normal")
        self.after(0, _apply)
