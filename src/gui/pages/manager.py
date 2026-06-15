"""
src/gui/pages/manager.py — AQ Manager control page.

Mirrors engine.py but targets the AQ Manager scheduled task
(the orchestrator that runs all MT5 agent subprocesses).
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT,
    BASE, SURFACE_RAISED, LINE, LINE_STRONG,
    DANGER_BG, DANGER_BORDER, INFO_BG, INFO_BORDER,
    SUCCESS_BG, SUCCESS_BORDER,
    section_rule, info_row, page_header,
)
from src.gui.components import ActionBanner, PrimaryButton

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI


class ManagerPage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.app = app
        self._online: bool | None = None
        self._build()
        self._tick()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "AQ Manager", "Orchestrator task control and status")

        content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=16)

        # ── Status card ────────────────────────────────────────────────────────
        section_rule(content, "MANAGER STATUS").pack(fill="x", pady=(0, 8))

        status_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        status_card.pack(fill="x", pady=(0, 16))

        self._accent_bar = ctk.CTkFrame(status_card, height=3, fg_color=MUTED, corner_radius=0)
        self._accent_bar.pack(fill="x")

        inner = ctk.CTkFrame(status_card, fg_color="transparent")
        inner.pack(padx=20, pady=14, fill="x")

        self._lbl_status = ctk.CTkLabel(
            inner, text="●  Checking…",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=MUTED,
        )
        self._lbl_status.pack(anchor="w")

        self._lbl_heartbeat = ctk.CTkLabel(
            inner, text="Last contact: --",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._lbl_heartbeat.pack(anchor="w", pady=(10, 0))

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
            btn_inner, text="▶  Start Manager", width=160, height=44,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER, text_color=GREEN,
            command=self.app.restart_manager,
        )
        self._btn_start.grid(row=0, column=0, padx=8, pady=4)

        self._btn_stop = ctk.CTkButton(
            btn_inner, text="■  Stop Manager", width=160, height=44,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=DANGER_BG, hover_color=DANGER_BORDER,
            border_width=1, border_color=DANGER_BORDER, text_color=RED,
            command=self._stop,
        )
        self._btn_stop.grid(row=0, column=1, padx=8, pady=4)

        self._btn_restart = ctk.CTkButton(
            btn_inner, text="↺  Restart Manager", width=160, height=44,
            font=ctk.CTkFont(size=13),
            fg_color=INFO_BG, hover_color=INFO_BORDER,
            border_width=1, border_color=INFO_BORDER, text_color=INFO,
            command=self._restart,
        )
        self._btn_restart.grid(row=0, column=2, padx=8, pady=4)

        self._action_banner = ActionBanner(content)
        self._action_banner.pack(fill="x", pady=(0, 8))
        self._action_banner.hide()

        section_rule(content, "MANAGER LICENSE").pack(fill="x", pady=(8, 8))

        license_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        license_card.pack(fill="x", pady=(0, 16))
        license_inner = ctk.CTkFrame(license_card, fg_color="transparent")
        license_inner.pack(padx=20, pady=14, fill="x")

        self._license_status = ctk.CTkLabel(
            license_inner, text="Checking manager license...",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=MUTED, anchor="w",
        )
        self._license_status.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(
            license_inner,
            text="Agents and the shared signal connection use this encrypted manager-level license key.",
            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(0, 8))

        self._license_key_var = tk.StringVar()
        self._license_entry = ctk.CTkEntry(
            license_inner, textvariable=self._license_key_var, show="*", height=36,
            placeholder_text="Enter a replacement license key",
            fg_color=BASE, border_color=LINE_STRONG, border_width=1, corner_radius=6,
        )
        self._license_entry.pack(fill="x", pady=(0, 8))

        license_buttons = ctk.CTkFrame(license_inner, fg_color="transparent")
        license_buttons.pack(fill="x")
        ctk.CTkButton(
            license_buttons, text="Preflight entered key", width=150, height=34,
            fg_color="transparent", hover_color=LINE_STRONG, text_color=TEXT,
            border_width=1, border_color=LINE_STRONG, corner_radius=6,
            command=self._preflight_license,
        ).pack(side="left", padx=(0, 8))
        PrimaryButton(
            license_buttons, text="Verify and save", tone="good", width=140, height=34,
            command=self._save_license,
        ).pack(side="left")
        ctk.CTkButton(
            license_buttons, text="Refresh stored license", width=150, height=34,
            fg_color="transparent", hover_color=LINE_STRONG, text_color=MUTED,
            border_width=1, border_color=LINE, corner_radius=6,
            command=self._load_license,
        ).pack(side="right")

        self._license_banner = ActionBanner(license_inner)
        self._license_banner.pack(fill="x", pady=(10, 0))
        self._license_banner.hide()

        # ── Installation ───────────────────────────────────────────────────────
        section_rule(content, "TASK INSTALLATION").pack(fill="x", pady=(8, 8))

        install_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        install_card.pack(fill="x", pady=(0, 16))

        inst_inner = ctk.CTkFrame(install_card, fg_color="transparent")
        inst_inner.pack(padx=20, pady=14, fill="x")

        ctk.CTkLabel(
            inst_inner,
            text="AQ Manager runs as a Windows Task Scheduler task and starts "
                 "automatically 20 seconds after login. Use these controls to "
                 "register, update, or remove the task. A UAC prompt will appear.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=560, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(inst_inner, fg_color="transparent")
        btn_row.pack(fill="x")

        PrimaryButton(
            btn_row, text="Install Task", tone="good", width=150, height=38,
            command=self._install,
        ).pack(side="left", padx=(0, 8))

        PrimaryButton(
            btn_row, text="Reinstall / Update", tone="info", width=160, height=38,
            command=self._reinstall,
        ).pack(side="left", padx=(0, 8))

        PrimaryButton(
            btn_row, text="Uninstall Task", tone="danger", width=150, height=38,
            command=self._uninstall,
        ).pack(side="left")

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

        info_row(tech_inner, "Task name",      "AQ Manager")
        info_row(tech_inner, "Task folder",    "\\Apex Quantel\\")
        info_row(tech_inner, "REST API",       "http://localhost:8870")
        info_row(tech_inner, "Worker event IPC", "tcp://127.0.0.1:8871")
        info_row(tech_inner, "Data directory", "C:\\ProgramData\\Apex Quantel\\Multi\\")
        info_row(tech_inner, "Log file",       "C:\\ProgramData\\Apex Quantel\\Multi\\manager\\logs\\manager.log")

    def on_navigate_to(self) -> None:
        self._load_license()

    def _load_license(self) -> None:
        self._license_status.configure(text="Checking stored manager license...", text_color=MUTED)
        self.app.manager_client.get_license_info(
            lambda info: self.after(0, lambda: self._show_license_info(info))
        )

    def _preflight_license(self) -> None:
        key = self._license_key_var.get().strip()
        if not key:
            self._license_banner.show("Enter a license key to preflight.", "warn")
            return
        self._license_banner.show("Preflighting license key...", "info")
        self.app.manager_client.preflight_license(
            key, lambda info: self.after(0, lambda: self._show_license_info(info, save=False))
        )

    def _save_license(self) -> None:
        key = self._license_key_var.get().strip()
        if not key:
            self._license_banner.show("Enter a license key to verify and save.", "warn")
            return
        self._license_banner.show("Verifying and saving manager license...", "info")
        self.app.manager_client.set_license_key(
            key, lambda info: self.after(0, lambda: self._show_license_info(info, save=True))
        )

    def _show_license_info(self, info: dict, save: bool | None = None) -> None:
        if info.get("valid"):
            symbols = ", ".join(info.get("symbols") or []) or "none"
            available = info.get("available_devices", "?")
            maximum = info.get("max_devices", "?")
            self._license_status.configure(
                text=f"License verified | {available}/{maximum} device slots available | {symbols}",
                text_color=GREEN,
            )
            if save is True:
                self._license_key_var.set("")
                self._license_banner.show("Manager license key verified and saved.", "good")
            elif save is False:
                self._license_banner.show("License key is valid. Click Verify and save to use it.", "good")
            else:
                self._license_banner.hide()
        else:
            error = info.get("error") or "License key is invalid"
            self._license_status.configure(text=error, text_color=YELLOW)
            self._license_banner.show(error, "danger")

    # ── Live ticker ───────────────────────────────────────────────────────────

    def _tick(self) -> None:
        online = getattr(self.app, "_manager_online", False)
        last   = getattr(self.app, "_last_manager_contact", 0.0)

        if online != self._online:
            self._online = online
            if online:
                self._lbl_status.configure(text="●  AQ Manager Running", text_color=GREEN)
                self._accent_bar.configure(fg_color=GREEN)
                self._btn_start.configure(state="disabled")
                self._btn_stop.configure(state="normal")
                self._btn_restart.configure(state="normal")
            else:
                self._lbl_status.configure(text="○  AQ Manager Offline", text_color=RED)
                self._accent_bar.configure(fg_color=RED)
                self._btn_start.configure(state="normal")
                self._btn_stop.configure(state="disabled")
                self._btn_restart.configure(state="disabled")

        if last > 0:
            secs = int(time.time() - last)
            txt = (
                "Last contact: just now" if secs < 5 else
                f"Last contact: {secs}s ago" if secs < 120 else
                f"Last contact: {secs // 60}m ago"
            )
            color = GREEN if secs < 10 else YELLOW if secs < 30 else RED
            self._lbl_heartbeat.configure(text=txt, text_color=color)
        else:
            self._lbl_heartbeat.configure(text="Last contact: --", text_color=MUTED)

        self.after(1000, self._tick)

    # ── Controls ─────────────────────────────────────────────────────────────

    def _start(self) -> None:
        self._action_banner.show("Starting Manager…", "warn")
        self._btn_start.configure(state="disabled")

        def _run():
            ok, msg = self.app.installer.start_manager_task()
            def _apply():
                if ok:
                    self._action_banner.show("Start command sent — waiting for Manager.", "good")
                else:
                    self._action_banner.show("Task not registered — running installer…", "warn")
                    self.app.installer.on_result = self._on_install_result
                    self.app.installer.install_manager_async()
            self.after(0, _apply)

        threading.Thread(target=_run, daemon=True).start()

    def _stop(self) -> None:
        self._action_banner.show("Stopping Manager…", "warn")
        self._btn_stop.configure(state="disabled")

        def _run():
            ok, msg = self.app.installer.stop_manager_task()
            def _apply():
                tone = "good" if ok else "danger"
                self._action_banner.show(msg, tone)
                self._btn_stop.configure(state="normal")
            self.after(0, _apply)

        threading.Thread(target=_run, daemon=True).start()

    def _restart(self) -> None:
        self._action_banner.show("Restarting Manager…", "warn")

        def _run():
            self.app.installer.stop_manager_task()
            time.sleep(2)
            ok, msg = self.app.installer.start_manager_task()
            def _apply():
                tone = "good" if ok else "danger"
                self._action_banner.show("Restart command sent." if ok else msg, tone)
            self.after(0, _apply)

        threading.Thread(target=_run, daemon=True).start()

    # ── Installation ─────────────────────────────────────────────────────────

    def _install(self) -> None:
        self._install_banner.show("Installing… A UAC prompt will appear.", "warn")
        self.app.installer.on_result = self._on_install_result
        self.app.installer.install_manager_async()

    def _reinstall(self) -> None:
        self._install_banner.show("Reinstalling… A UAC prompt will appear.", "warn")
        self.app.installer.on_result = self._on_install_result
        self.app.installer.reinstall_manager_async()

    def _uninstall(self) -> None:
        self._install_banner.show("Uninstalling… A UAC prompt will appear.", "warn")
        self.app.installer.on_result = self._on_install_result
        self.app.installer.uninstall_manager_async()

    def _on_install_result(self, ok: bool, msg: str) -> None:
        def _apply():
            self._install_banner.show(msg, "good" if ok else "danger")
            self._action_banner.hide()
            self._btn_start.configure(state="normal")
        self.after(0, _apply)
