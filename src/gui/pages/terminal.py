"""
src/gui/pages/terminal.py

Trading platform selection page.

Shows detected MT5/MT4 installations by readable broker name — NOT raw paths.
Users pick a terminal from cards, enter account credentials, and save.
Raw path is accessible only via an expandable "Advanced" section.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog
from typing import TYPE_CHECKING

import customtkinter as ctk
import yaml

from src.gui.theme import (
    GREEN, RED, YELLOW, MUTED, TEXT, TEXT_SOFT,
    SURFACE_RAISED, BASE, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER,
    section_rule, page_header,
)

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI
    from src.gui.mt5_detector import MT5Install


class TerminalPage(ctk.CTkScrollableFrame):
    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.app = app
        self._installs: list[MT5Install] = []
        self._selected_id: str | None    = None
        self._card_frames: dict[str, ctk.CTkFrame] = {}
        self._build()
        # Auto-scan on load
        self.after(200, self._scan)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Trading Platform")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=16)

        # ── Terminal detection section ────────────────────────────────────────
        section_rule(content, "Detected Installations")

        detect_row = ctk.CTkFrame(content, fg_color="transparent")
        detect_row.pack(fill="x", pady=(0, 10))

        self._lbl_scan_status = ctk.CTkLabel(
            detect_row, text="Scanning…",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._lbl_scan_status.pack(side="left")

        ctk.CTkButton(
            detect_row, text="↺  Scan Again", width=110, height=28,
            command=self._scan,
        ).pack(side="right")

        # Container for terminal cards
        self._cards_frame = ctk.CTkFrame(content, fg_color="transparent")
        self._cards_frame.pack(fill="x", pady=(0, 20))

        # ── Account credentials ───────────────────────────────────────────────
        section_rule(content, "Account Credentials")

        creds_card = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=SURFACE_RAISED, border_width=1, border_color=LINE,
        )
        creds_card.pack(fill="x", pady=(0, 16))

        creds_inner = ctk.CTkFrame(creds_card, fg_color="transparent")
        creds_inner.pack(padx=24, pady=16, fill="x")

        self._var_login    = tk.StringVar()
        self._var_password = tk.StringVar()
        self._var_server   = tk.StringVar()

        _cred_row(creds_inner, "Login",    self._var_login,    False)
        _cred_row(creds_inner, "Password", self._var_password, True)
        _cred_row(creds_inner, "Server",   self._var_server,   False)

        # ── Save button ───────────────────────────────────────────────────────
        self._lbl_save_status = ctk.CTkLabel(
            content, text="",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._lbl_save_status.pack(pady=(0, 6))

        ctk.CTkButton(
            content,
            text="💾  Save & Restart AQ Agent",
            height=44, width=260,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
            border_width=1, border_color=SUCCESS_BORDER,
            text_color=GREEN,
            command=self._save,
        ).pack(pady=(0, 20))

        # ── Advanced: raw path ────────────────────────────────────────────────
        self._advanced_visible = False
        self._btn_advanced = ctk.CTkButton(
            content,
            text="▶  Advanced details",
            anchor="w", height=28, width=200,
            fg_color="transparent", hover_color=LINE_STRONG,
            text_color=MUTED, font=ctk.CTkFont(size=12),
            command=self._toggle_advanced,
        )
        self._btn_advanced.pack(anchor="w", pady=(0, 4))

        self._advanced_frame = ctk.CTkFrame(
            content, corner_radius=8,
            fg_color=BASE, border_width=1, border_color=LINE,
        )
        # Not packed yet

        adv_inner = ctk.CTkFrame(self._advanced_frame, fg_color="transparent")
        adv_inner.pack(padx=16, pady=12, fill="x")

        path_row = ctk.CTkFrame(adv_inner, fg_color="transparent")
        path_row.pack(fill="x")

        ctk.CTkLabel(
            path_row, text="Terminal path:", width=130, anchor="w",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        ).pack(side="left")

        self._var_path = tk.StringVar()
        self._entry_path = ctk.CTkEntry(
            path_row, textvariable=self._var_path, width=380,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self._entry_path.pack(side="left", padx=(4, 4))

        ctk.CTkButton(
            path_row, text="Browse…", width=80, height=26,
            command=self._browse_path,
        ).pack(side="left")

        # Load saved credentials on init
        self._load_saved()

    # ── Terminal cards ────────────────────────────────────────────────────────

    def _rebuild_cards(self) -> None:
        for widget in self._cards_frame.winfo_children():
            widget.destroy()
        self._card_frames.clear()

        if not self._installs:
            ctk.CTkLabel(
                self._cards_frame,
                text="No MetaTrader installation found.\n"
                     "Install MT5 from your broker, or use Browse below to locate it manually.",
                font=ctk.CTkFont(size=13), text_color=MUTED,
                justify="left",
            ).pack(anchor="w", pady=8)
            return

        for install in self._installs:
            card = ctk.CTkFrame(
                self._cards_frame, corner_radius=8,
                fg_color=SURFACE_RAISED, border_width=2, border_color=LINE,
            )
            card.pack(fill="x", pady=6)
            self._card_frames[install.id] = card

            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(padx=16, pady=12, fill="x")

            # Platform icon + name
            icon = "5" if install.platform == "mt5" else "4"
            ctk.CTkLabel(
                inner,
                text=f"MT{icon}",
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color="#1a2a4a", corner_radius=6,
                width=36, height=24,
                text_color="#6699cc",
            ).pack(side="left", padx=(0, 10))

            name_col = ctk.CTkFrame(inner, fg_color="transparent")
            name_col.pack(side="left", fill="x", expand=True)

            ctk.CTkLabel(
                name_col, text=install.name,
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=TEXT,
                anchor="w",
            ).pack(anchor="w")

            status_text  = "Detected  ·  Ready" if install.is_available else "Not available"
            status_color = GREEN if install.is_available else RED
            ctk.CTkLabel(
                name_col, text=status_text,
                font=ctk.CTkFont(size=11), text_color=status_color,
                anchor="w",
            ).pack(anchor="w")

            # Use button
            btn = ctk.CTkButton(
                inner, text="Use this terminal",
                width=140, height=32,
                command=lambda iid=install.id, path=install.exe_path: self._select(iid, path),
            )
            btn.pack(side="right")

        # Highlight the currently-selected card
        self._update_card_selection()

    def _select(self, install_id: str, exe_path: str) -> None:
        self._selected_id = install_id
        self._var_path.set(exe_path)
        self._update_card_selection()

    def _update_card_selection(self) -> None:
        for iid, card in self._card_frames.items():
            if iid == self._selected_id:
                card.configure(border_color=GREEN)
            else:
                card.configure(border_color=LINE)

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _scan(self) -> None:
        self._lbl_scan_status.configure(text="Scanning…", text_color=MUTED)
        for widget in self._cards_frame.winfo_children():
            widget.destroy()

        def _do_scan():
            from src.gui.mt5_detector import detect_installs
            installs = detect_installs()
            self.after(0, lambda: self._on_scan_done(installs))

        threading.Thread(target=_do_scan, daemon=True).start()

    def _on_scan_done(self, installs: list[MT5Install]) -> None:
        self._installs = installs

        if installs:
            count = len(installs)
            self._lbl_scan_status.configure(
                text=f"Found {count} installation{'s' if count != 1 else ''}",
                text_color=GREEN,
            )
        else:
            self._lbl_scan_status.configure(
                text="No installations detected", text_color=YELLOW,
            )

        # Auto-select the terminal whose path matches config
        saved_path = self._var_path.get().strip()
        if saved_path:
            for inst in installs:
                if inst.exe_path.lower() == saved_path.lower():
                    self._selected_id = inst.id
                    break

        self._rebuild_cards()

    # ── Load / Save ───────────────────────────────────────────────────────────

    def _load_saved(self) -> None:
        cfg = self.app.load_config()
        mt5 = cfg.get("mt5", {})
        self._var_login.set(str(mt5.get("login", "")))
        self._var_password.set(str(mt5.get("password", "")))
        self._var_server.set(str(mt5.get("server", "")))
        self._var_path.set(str(mt5.get("path", "")))

    def _save(self) -> None:
        login_str = self._var_login.get().strip()
        password  = self._var_password.get()
        server    = self._var_server.get().strip()
        path      = self._var_path.get().strip()

        if not server:
            self._lbl_save_status.configure(
                text="⚠  Server name is required", text_color=YELLOW,
            )
            return
        if not login_str:
            self._lbl_save_status.configure(
                text="⚠  Account login is required", text_color=YELLOW,
            )
            return

        try:
            login_int = int(login_str)
        except ValueError:
            self._lbl_save_status.configure(
                text="⚠  Login must be a number", text_color=YELLOW,
            )
            return

        try:
            cfg = self.app.load_config()
            cfg.setdefault("mt5", {}).update({
                "login":    login_int,
                "password": password,
                "server":   server,
                "path":     path,
            })
            self.app.save_config(cfg)
        except Exception as exc:
            self._lbl_save_status.configure(
                text=f"⚠  Save failed: {exc}", text_color=RED,
            )
            return

        self._lbl_save_status.configure(
            text="✓  Saved — restarting AQ Agent…", text_color=GREEN,
        )
        threading.Thread(target=self._delayed_restart, daemon=True).start()

    def _delayed_restart(self) -> None:
        import time
        time.sleep(0.4)
        self.app.restart_with_new_config()

    # ── Advanced toggle ───────────────────────────────────────────────────────

    def _toggle_advanced(self) -> None:
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self._btn_advanced.configure(text="▼  Advanced details")
            self._advanced_frame.pack(fill="x", pady=(0, 16))
        else:
            self._btn_advanced.configure(text="▶  Advanced details")
            self._advanced_frame.pack_forget()

    def _browse_path(self) -> None:
        path = filedialog.askopenfilename(
            title="Select MT5/MT4 terminal executable",
            filetypes=[
                ("MT5/MT4 executable", "terminal64.exe"),
                ("MT4 executable",     "terminal.exe"),
                ("All executables",    "*.exe"),
            ],
        )
        if path:
            self._var_path.set(path.replace("/", "\\"))
            # Try to match to a detected install
            for inst in self._installs:
                if inst.exe_path.lower() == path.lower():
                    self._select(inst.id, inst.exe_path)
                    return
            # Unknown path — deselect cards
            self._selected_id = None
            self._update_card_selection()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cred_row(
    parent: tk.Widget,
    label: str,
    var: tk.StringVar,
    masked: bool,
) -> None:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=4)
    ctk.CTkLabel(
        row, text=label, width=100, anchor="w",
        font=ctk.CTkFont(size=12), text_color=TEXT,
    ).pack(side="left")
    ctk.CTkEntry(
        row, textvariable=var, width=300,
        show="●" if masked else "",
        font=ctk.CTkFont(family="Consolas", size=12),
    ).pack(side="left", padx=(8, 0))
