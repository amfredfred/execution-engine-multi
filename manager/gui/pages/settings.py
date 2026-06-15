"""
src/gui/pages/settings.py — Application settings for the multi-agent manager GUI.

Sections:
  • Files & Folders  — open config, manager logs, agent data folders
  • Setup            — re-run wizard, reload config
  • About            — version info
"""
from __future__ import annotations

import subprocess
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from manager.gui.theme import (
    GREEN, MUTED, TEXT,
    BASE, SURFACE, SURFACE_RAISED, LINE, LINE_STRONG,
    INFO_BG, INFO_BORDER,
    section_rule, page_header,
)
from manager.gui.components import SectionCard, ActionBanner, PrimaryButton, InfoTable

if TYPE_CHECKING:
    from manager.gui.app import ApexTraderGUI


class SettingsPage(ctk.CTkScrollableFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._build()

    def _build(self) -> None:
        page_header(self, "Settings", "File locations and application preferences")

        # ── Files and folders ────────────────────────────────────────────────────
        section_rule(self, "FILES & FOLDERS").pack(fill="x", padx=24, pady=(20, 8))

        files_card = SectionCard(self)
        files_card.pack(fill="x", padx=24)

        from manager.gui.config_manager import ConfigManager
        cfg_path    = ConfigManager().path
        mgr_logs    = ConfigManager.programdata_manager_logs_path()
        agents_dir  = ConfigManager.programdata_agents_path()

        def _folder_row(label: str, path: Path, btn_label: str = "Open folder") -> None:
            row = ctk.CTkFrame(files_card.body, fg_color="transparent")
            row.pack(fill="x", pady=6)
            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(
                left, text=label, anchor="w",
                font=ctk.CTkFont(size=13), text_color=TEXT,
            ).pack(anchor="w")
            ctk.CTkLabel(
                left, text=str(path), anchor="w",
                font=ctk.CTkFont(family="Consolas", size=10), text_color=MUTED,
                wraplength=460,
            ).pack(anchor="w", pady=(1, 0))
            ctk.CTkButton(
                row, text=btn_label, width=110, height=30,
                font=ctk.CTkFont(size=11),
                command=lambda p=path: self._open_folder(p),
            ).pack(side="right")

        _folder_row("Configuration file",  cfg_path,   "Open folder")
        _folder_row("Manager logs",        mgr_logs,   "Open folder")
        _folder_row("Agent data folders",  agents_dir, "Open folder")

        # ── Web Dashboard ────────────────────────────────────────────────────────
        section_rule(self, "LICENSE & WEB DASHBOARD").pack(fill="x", padx=24, pady=(20, 8))

        dash_card = SectionCard(self)
        dash_card.pack(fill="x", padx=24)

        ctk.CTkLabel(
            dash_card.body,
            text="License, subscription, and billing are managed through the Apex web dashboard.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=560, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(dash_card.body, fg_color="transparent")
        btn_row.pack(anchor="w")

        PrimaryButton(
            btn_row, text="Open Web Dashboard", tone="info",
            width=180, height=34,
            command=self._open_dashboard,
        ).pack(side="left", padx=(0, 8))

        PrimaryButton(
            btn_row, text="Manager License Key", tone="normal",
            width=180, height=34,
            command=lambda: self.app.navigate("Manager"),
        ).pack(side="left")

        # ── Setup & account ──────────────────────────────────────────────────────
        section_rule(self, "SETUP & ACCOUNT").pack(fill="x", padx=24, pady=(20, 8))

        acct_card = SectionCard(self)
        acct_card.pack(fill="x", padx=24)

        def _action_row(label: str, detail: str, btn_label: str, fn, tone: str = "info") -> None:
            row = ctk.CTkFrame(acct_card.body, fg_color="transparent")
            row.pack(fill="x", pady=6)
            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(left, text=label, anchor="w",
                         font=ctk.CTkFont(size=13), text_color=TEXT).pack(anchor="w")
            ctk.CTkLabel(left, text=detail, anchor="w",
                         font=ctk.CTkFont(size=11), text_color=MUTED,
                         wraplength=400).pack(anchor="w")
            PrimaryButton(row, text=btn_label, tone=tone, width=130, height=30,
                          command=fn).pack(side="right")

        _action_row(
            "Re-run setup wizard",
            "Change your MT5 credentials, gateway URL, or license key.",
            "Run Setup",
            lambda: self.app.navigate("__setup__"),
        )
        _action_row(
            "Reload configuration",
            "Reload config.yaml from disk (useful after manual edits).",
            "Reload",
            self._reload_config,
        )

        self._acct_banner = ActionBanner(self)
        self._acct_banner.pack(fill="x", padx=24, pady=(4, 0))
        self._acct_banner.hide()

        # ── About ────────────────────────────────────────────────────────────────
        section_rule(self, "ABOUT").pack(fill="x", padx=24, pady=(20, 8))

        about_card = SectionCard(self)
        about_card.pack(fill="x", padx=24, pady=(0, 24))

        t = InfoTable(about_card.body)
        t.add_row("AQ Agent (multi)", self._read_version())
        t.add_row("Python",           sys.version.split()[0])
        t.add_row("Manager API",      "http://localhost:8870")
        t.add_row("Config path",      str(self.app.config.path))
        t.pack(fill="x")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _open_dashboard(self) -> None:
        webbrowser.open(self.app.config.dashboard_url())

    def _open_folder(self, path: Path) -> None:
        target = path if path.is_dir() else path.parent
        try:
            target.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(target)])
        except Exception:
            pass

    def _reload_config(self) -> None:
        self.app.config.reload()
        self._acct_banner.show(
            "Configuration reloaded.", "good",
            auto_dismiss_after_ms=3000,
        )

    def _read_version(self) -> str:
        for c in (
            Path("version.txt"),
            Path(sys.executable).parent / "version.txt",
            Path(__file__).resolve().parent.parent.parent / "version.txt",
        ):
            try:
                return c.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return "?"
