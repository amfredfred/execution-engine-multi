"""
src/gui/pages/platform.py — Trading Platform + MT5 Account credentials

Replaces terminal.py.  Uses ConfigManager for atomic saves.
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE, SURFACE_RAISED, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER, WARNING_BG, WARNING_BORDER,
    section_rule, page_header,
)
from src.gui.components import (
    SectionCard, ActionBanner, PrimaryButton,
    labeled_field, InfoTable,
)

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI


class PlatformPage(ctk.CTkScrollableFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._selected_path: Optional[str] = None
        self._card_frames:   dict          = {}
        self._installs:      list          = []
        self._build()

    def _build(self) -> None:
        page_header(self, "Platform", "MetaTrader terminal selection and account")

        # ── Terminal selection ─────────────────────────────────────────────────
        section_rule(self, "TRADING TERMINAL").pack(fill="x", padx=24, pady=(16, 8))

        intro = ctk.CTkFrame(self, fg_color="transparent")
        intro.pack(fill="x", padx=24)
        ctk.CTkLabel(
            intro,
            text="Select your broker's MetaTrader 5 terminal. "
                 "Apex uses this terminal to open and close trades.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            justify="left",
        ).pack(anchor="w")

        scan_row = ctk.CTkFrame(self, fg_color="transparent")
        scan_row.pack(fill="x", padx=24, pady=(10, 6))
        self._scan_lbl = ctk.CTkLabel(
            scan_row, text="",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._scan_lbl.pack(side="left")
        ctk.CTkButton(
            scan_row, text="↺  Scan", width=90, height=30,
            command=self._scan,
        ).pack(side="right")

        self._cards_wrap = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_wrap.pack(fill="x", padx=24)

        # Manual path
        adv_card = SectionCard(self, pady=12)
        adv_card.pack(fill="x", padx=24, pady=(8, 4))
        adv_hdr = ctk.CTkFrame(adv_card.body, fg_color="transparent")
        adv_hdr.pack(fill="x")
        ctk.CTkLabel(
            adv_hdr, text="Manual path",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=TEXT,
        ).pack(side="left")

        self._var_path = tk.StringVar()
        path_row = ctk.CTkFrame(adv_card.body, fg_color="transparent")
        path_row.pack(fill="x", pady=(8, 0))
        ctk.CTkEntry(
            path_row, textvariable=self._var_path, width=360,
            font=ctk.CTkFont(family="Consolas", size=11),
            placeholder_text="e.g. C:\\Program Files\\MetaTrader 5\\terminal64.exe",
        ).pack(side="left")
        ctk.CTkButton(
            path_row, text="Browse…", width=80, height=30,
            command=self._browse, font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(6, 0))
        PrimaryButton(
            path_row, text="Apply", tone="good", width=80, height=30,
            command=self._apply_manual_path,
        ).pack(side="left", padx=(6, 0))

        self._terminal_banner = ActionBanner(self)
        self._terminal_banner.pack(fill="x", padx=24, pady=(4, 0))
        self._terminal_banner.hide()

        # ── MT5 Account credentials ────────────────────────────────────────────
        section_rule(self, "MT5 ACCOUNT").pack(fill="x", padx=24, pady=(20, 8))

        cred_card = SectionCard(self)
        cred_card.pack(fill="x", padx=24)

        self._var_login    = tk.StringVar()
        self._var_password = tk.StringVar()
        self._var_server   = tk.StringVar()

        labeled_field(cred_card.body, "Account number", self._var_login,
                      placeholder="e.g. 12345678")
        labeled_field(cred_card.body, "Password",       self._var_password,
                      masked=True)
        labeled_field(cred_card.body, "Server",         self._var_server,
                      placeholder="e.g. FBS-Real")

        self._cred_banner = ActionBanner(self)
        self._cred_banner.pack(fill="x", padx=24, pady=(8, 4))
        self._cred_banner.hide()

        save_row = ctk.CTkFrame(self, fg_color="transparent")
        save_row.pack(fill="x", padx=24, pady=(8, 24))
        PrimaryButton(
            save_row, text="Save Credentials", tone="good", width=180,
            command=self._save_credentials,
        ).pack(side="left")

        self._load_from_config()
        self._scan()

    def _load_from_config(self) -> None:
        cfg = self.app.config.load()
        mt5 = cfg.get("mt5", {})
        self._var_login.set(str(mt5.get("login", "")))
        self._var_password.set(str(mt5.get("password", "")))
        self._var_server.set(str(mt5.get("server", "")))
        saved_path = str(mt5.get("path", ""))
        self._var_path.set(saved_path)
        if saved_path:
            self._selected_path = saved_path

    # ── Terminal scan ─────────────────────────────────────────────────────────

    def _scan(self) -> None:
        self._scan_lbl.configure(text="Scanning…", text_color=MUTED)
        for w in self._cards_wrap.winfo_children():
            w.destroy()
        self._card_frames.clear()

        def _do():
            try:
                from src.gui.mt5_detector import detect_installs
                results = detect_installs()
            except Exception:
                results = []
            self._cards_wrap.after(0, lambda: self._on_scan_done(results))

        threading.Thread(target=_do, daemon=True).start()

    def _on_scan_done(self, installs: list) -> None:
        self._installs = installs
        for w in self._cards_wrap.winfo_children():
            w.destroy()
        self._card_frames.clear()

        if not installs:
            self._scan_lbl.configure(
                text="No MetaTrader installations found. Use Manual path below.",
                text_color=YELLOW,
            )
            ctk.CTkLabel(
                self._cards_wrap,
                text="Install MetaTrader 5 from your broker's website, then click Scan.",
                font=ctk.CTkFont(size=12), text_color=MUTED,
            ).pack(anchor="w", pady=8)
            return

        self._scan_lbl.configure(
            text=f"Found {len(installs)} installation(s)", text_color=GREEN,
        )
        saved = self._var_path.get().strip()

        for inst in installs:
            is_sel = inst.exe_path.lower() == saved.lower()
            if is_sel:
                self._selected_path = inst.exe_path

            card = ctk.CTkFrame(
                self._cards_wrap, corner_radius=8,
                fg_color=SURFACE_RAISED, border_width=2,
                border_color=GREEN if is_sel else LINE,
            )
            card.pack(fill="x", pady=5)
            self._card_frames[inst.id] = card

            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(padx=14, pady=10, fill="x")

            col = ctk.CTkFrame(inner, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True)

            ctk.CTkLabel(
                col, text=inst.name,
                font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT, anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                col, text=inst.exe_path,
                font=ctk.CTkFont(family="Consolas", size=10), text_color=MUTED, anchor="w",
            ).pack(anchor="w")

            st_text  = "Available" if inst.is_available else "Not available"
            st_color = GREEN if inst.is_available else RED
            ctk.CTkLabel(
                col, text=st_text,
                font=ctk.CTkFont(size=11), text_color=st_color, anchor="w",
            ).pack(anchor="w")

            ctk.CTkButton(
                inner, text="Select" if not is_sel else "Selected ✓",
                width=100, height=30, font=ctk.CTkFont(size=11),
                fg_color=SUCCESS_BG if is_sel else SURFACE_RAISED,
                hover_color=SUCCESS_BORDER,
                border_width=1, border_color=SUCCESS_BORDER if is_sel else LINE,
                text_color=GREEN if is_sel else MUTED,
                command=lambda i=inst.id, p=inst.exe_path: self._select(i, p),
            ).pack(side="right", anchor="n")

    def _select(self, install_id: str, path: str) -> None:
        self._selected_path = path
        self._var_path.set(path)
        # Update card borders
        for iid, card in self._card_frames.items():
            card.configure(border_color=GREEN if iid == install_id else LINE)
        # Save immediately
        err = self.app.config.update("mt5", {"path": path})
        if err:
            self._terminal_banner.show(err, "danger")
        else:
            self._terminal_banner.show(f"Terminal selected: {path.split(chr(92))[-2]}", "good")

    def _browse(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select MetaTrader executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self._var_path.set(path.replace("/", "\\"))
            self._selected_path = path.replace("/", "\\")
            for card in self._card_frames.values():
                card.configure(border_color=LINE)

    def _apply_manual_path(self) -> None:
        path = self._var_path.get().strip()
        if not path:
            self._terminal_banner.show("Enter a path first.", "warn"); return
        if not Path(path).exists():
            self._terminal_banner.show("File does not exist.", "danger"); return
        self._selected_path = path
        err = self.app.config.update("mt5", {"path": path})
        if err:
            self._terminal_banner.show(err, "danger")
        else:
            self._terminal_banner.show("Terminal path saved.", "good")

    # ── Credentials ───────────────────────────────────────────────────────────

    def _save_credentials(self) -> None:
        login_str = self._var_login.get().strip()
        password  = self._var_password.get().strip()
        server    = self._var_server.get().strip()

        if not login_str:
            self._cred_banner.show("Account number is required.", "warn"); return
        if not password:
            self._cred_banner.show("Password is required.", "warn"); return
        if not server:
            self._cred_banner.show("Server name is required.", "warn"); return
        try:
            login_int = int(login_str)
        except ValueError:
            self._cred_banner.show("Account number must be numeric.", "warn"); return

        err = self.app.config.update("mt5", {
            "login": login_int, "password": password, "server": server,
        })
        if err:
            self._cred_banner.show(err, "danger")
        else:
            self._cred_banner.show("Credentials saved successfully.", "good")
            self.app.app_state.mark_setup_complete(self.app.config.is_setup_complete())

    # ── Lifecycle callbacks ───────────────────────────────────────────────────

    def on_engine_status(self, status: str, detail=None) -> None:
        pass
