"""
src/gui/pages/logs.py — Manager log viewer.

Reads the manager log from:
  C:\\ProgramData\\Apex Quantel\\manager\\logs\\manager.log

Tails new lines every 2 seconds while the page is visible.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from manager.gui.theme import (
    GREEN, RED, YELLOW, MUTED,
    SURFACE_RAISED, LINE,
    page_header,
)

if TYPE_CHECKING:
    from manager.gui.app import ApexTraderGUI

_LEVEL_COLOURS = {
    "DEBUG":    MUTED,
    "INFO":     "#c9ced6",
    "WARNING":  YELLOW,
    "ERROR":    RED,
    "CRITICAL": "#ff1a2e",
}
_MAX_LINES = 2000


class LogsPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.app = app
        self._auto_scroll = True
        self._last_pos: int = 0
        self._log_path: Path | None = None
        self._build()
        self.after(200, self._detect_log_file)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Logs", "Manager log output")

        toolbar = ctk.CTkFrame(self, fg_color=SURFACE_RAISED, height=44,
                               corner_radius=0, border_width=0)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        self._auto_scroll_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            toolbar, text="Auto-scroll",
            variable=self._auto_scroll_var,
            command=lambda: setattr(self, "_auto_scroll", self._auto_scroll_var.get()),
            width=110,
        ).pack(side="left", padx=(12, 4), pady=8)

        ctk.CTkButton(toolbar, text="Clear", width=70, height=28,
                      command=self._clear).pack(side="left", padx=4)
        ctk.CTkButton(toolbar, text="↺  Refresh", width=90, height=28,
                      command=self._reload_file).pack(side="left", padx=4)

        ctk.CTkLabel(toolbar, text="Level:", font=ctk.CTkFont(size=11),
                     text_color=MUTED).pack(side="left", padx=(14, 4))
        self._level_var = tk.StringVar(value="INFO")
        ctk.CTkOptionMenu(toolbar, values=["DEBUG", "INFO", "WARNING", "ERROR"],
                          variable=self._level_var, width=100, height=28).pack(side="left")

        self._lbl_source = ctk.CTkLabel(toolbar, text="",
                                         font=ctk.CTkFont(size=10), text_color=MUTED)
        self._lbl_source.pack(side="right", padx=12)

        ctk.CTkFrame(self, height=1, fg_color=LINE, corner_radius=0).pack(fill="x")

        self._text = ctk.CTkTextbox(
            self, wrap="none",
            font=ctk.CTkFont(family="Consolas", size=11),
            activate_scrollbars=True, fg_color="#080a0d",
        )
        self._text.pack(fill="both", expand=True)
        self._text.configure(state="disabled")

        tw = self._text._textbox
        for level, colour in _LEVEL_COLOURS.items():
            tw.tag_configure(level, foreground=colour)

        self._tail_loop()

    # ── Log file detection ────────────────────────────────────────────────────

    def _detect_log_file(self) -> None:
        from manager.gui.config_manager import ConfigManager
        mgr_logs = ConfigManager.programdata_manager_logs_path()
        candidates = [
            mgr_logs / "manager.log",
            mgr_logs / "stdout.log",
            mgr_logs / "apex.log",
        ]
        for c in candidates:
            if c.exists():
                self._log_path = c
                self._lbl_source.configure(text=f"  {c.name}")
                self._reload_file()
                return
        self._lbl_source.configure(text="  No log file found yet")
        self._append_line("INFO",
            f"Manager log not found at {mgr_logs}. "
            "Start AQ Manager to generate logs.")

    # ── Tail loop ─────────────────────────────────────────────────────────────

    def _tail_loop(self) -> None:
        if self._log_path:
            try:
                size = os.path.getsize(self._log_path)
                if size > self._last_pos:
                    self._read_new_lines()
            except OSError:
                pass
        self.after(2000, self._tail_loop)

    def on_navigate_to(self) -> None:
        if not self._log_path:
            self.after(200, self._detect_log_file)
        else:
            self._reload_file()

    def _reload_file(self) -> None:
        self._clear()
        self._last_pos = 0
        if self._log_path:
            self._read_new_lines()

    def _read_new_lines(self) -> None:
        if not self._log_path:
            return

        def _read():
            lines = []
            try:
                with open(self._log_path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(self._last_pos)
                    chunk = fh.read()
                    new_pos = fh.tell()
                lines = chunk.splitlines()
            except OSError:
                new_pos = self._last_pos
            self.after(0, lambda: self._append_lines(lines, new_pos))

        threading.Thread(target=_read, daemon=True).start()

    def _append_lines(self, lines: list[str], new_pos: int) -> None:
        self._last_pos = new_pos
        tw = self._text._textbox
        self._text.configure(state="normal")
        for raw in lines:
            level = self._detect_level(raw)
            if not self._level_visible(level):
                continue
            tw.insert("end", raw + "\n", level)
        line_count = int(tw.index("end-1c").split(".")[0])
        if line_count > _MAX_LINES:
            tw.delete("1.0", f"{line_count - _MAX_LINES}.0")
        self._text.configure(state="disabled")
        if self._auto_scroll and lines:
            tw.see("end")

    def _append_line(self, level: str, msg: str) -> None:
        tw = self._text._textbox
        self._text.configure(state="normal")
        tw.insert("end", msg + "\n", level)
        self._text.configure(state="disabled")
        if self._auto_scroll:
            tw.see("end")

    def _clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("0.0", "end")
        self._text.configure(state="disabled")

    @staticmethod
    def _detect_level(line: str) -> str:
        upper = line.upper()
        for lvl in ("CRITICAL", "ERROR", "WARNING", "DEBUG"):
            if lvl in upper:
                return lvl
        return "INFO"

    def _level_visible(self, level: str) -> bool:
        _RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        min_rank = _RANK.get(self._level_var.get(), 0)
        return _RANK.get(level, 0) >= min_rank
