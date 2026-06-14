"""
src/gui/pages/activity.py — Activity / Event log

Two tabs:
  • Events   — structured trade/signal events with icons (capped at _MAX_EVENTS)
  • Raw logs — tail of the engine log file, pre-seeded with recent history
"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from src.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE, SURFACE_RAISED, LINE, LINE_STRONG,
    SUCCESS_BG, DANGER_BG, WARNING_BG, INFO_BG,
    SUCCESS_BORDER, DANGER_BORDER, WARNING_BORDER, INFO_BORDER,
    section_rule, page_header,
)
from src.gui.components import SectionCard, PrimaryButton

if TYPE_CHECKING:
    from src.gui.app import ApexTraderGUI

_MAX_EVENTS   = 200   # max event cards kept in the DOM
_MAX_LINES    = 2000  # max log lines kept in the textbox
_LOG_PRELOAD  = 200   # lines to show from the end of the log on open
_TAIL_RETRY_S = 5.0   # seconds between log-file retry attempts

_EVENT_META = {
    # (icon, bg, border, text_color)
    "trade.opened":      ("📈", SUCCESS_BG,    SUCCESS_BORDER,  GREEN),
    "trade.tp1_hit":     ("✓",  SUCCESS_BG,    SUCCESS_BORDER,  GREEN),
    "trade.tp2_hit":     ("✓✓", SUCCESS_BG,    SUCCESS_BORDER,  GREEN),
    "trade.sl_hit":      ("✕",  DANGER_BG,     DANGER_BORDER,   RED),
    "trade.closed":      ("■",  SURFACE_RAISED, LINE,            MUTED),
    "trade.invalidated": ("⊘",  WARNING_BG,    WARNING_BORDER,  YELLOW),
    "trade.expired":     ("⏱",  SURFACE_RAISED, LINE,            MUTED),
    "trade.error":       ("⛔", DANGER_BG,     DANGER_BORDER,   RED),
    "signal.received":   ("📡", INFO_BG,       INFO_BORDER,     INFO),
    "signal.triggered":  ("🎯", INFO_BG,       INFO_BORDER,     INFO),
    "risk.approved":     ("✅", SUCCESS_BG,    SUCCESS_BORDER,  GREEN),
    "risk.rejected":     ("🚫", WARNING_BG,    WARNING_BORDER,  YELLOW),
    "ws_connected":      ("🔗", SUCCESS_BG,    SUCCESS_BORDER,  GREEN),
    "ws_disconnected":   ("🔌", DANGER_BG,     DANGER_BORDER,   RED),
    "mt5_error":         ("⚠",  DANGER_BG,     DANGER_BORDER,   RED),
}


class ActivityPage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        # _events stores the actual CTkFrame widgets so we can destroy old ones
        self._events: deque[ctk.CTkFrame] = deque(maxlen=_MAX_EVENTS)
        self._autoscroll = True
        self._active_tab = "events"
        self._build()
        self._start_log_tail()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Activity", "Engine events and log output")

        # Tab bar
        tab_bar = ctk.CTkFrame(self, fg_color=SURFACE_RAISED, corner_radius=0, height=40)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)

        self._tab_events = ctk.CTkButton(
            tab_bar, text="Events", width=120, height=38,
            corner_radius=0,
            fg_color=SUCCESS_BG, text_color=GREEN,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._show_tab("events"),
        )
        self._tab_events.pack(side="left")

        self._tab_logs = ctk.CTkButton(
            tab_bar, text="Raw Logs", width=120, height=38,
            corner_radius=0,
            fg_color="transparent", text_color=MUTED,
            font=ctk.CTkFont(size=13),
            command=lambda: self._show_tab("logs"),
        )
        self._tab_logs.pack(side="left")

        ctk.CTkButton(
            tab_bar, text="Clear", width=80, height=30,
            fg_color="transparent", hover_color=LINE,
            border_width=1, border_color=LINE,
            text_color=MUTED, font=ctk.CTkFont(size=11),
            command=self._clear,
        ).pack(side="right", padx=12, pady=4)

        self._autoscroll_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab_bar, text="Auto-scroll",
            variable=self._autoscroll_var,
            font=ctk.CTkFont(size=11), text_color=MUTED,
            checkbox_width=16, checkbox_height=16,
            command=lambda: setattr(self, "_autoscroll", self._autoscroll_var.get()),
        ).pack(side="right", padx=4)

        # Stacked content area
        content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        content.pack(fill="both", expand=True)

        # Events tab
        self._events_frame = ctk.CTkScrollableFrame(
            content, fg_color=BASE, corner_radius=0,
        )
        self._events_frame.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

        self._no_events_lbl = ctk.CTkLabel(
            self._events_frame,
            text="No events yet — AQ Agent events will appear here once running.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._no_events_lbl.pack(pady=32)

        # Logs tab
        self._logs_frame = ctk.CTkFrame(content, fg_color=BASE, corner_radius=0)
        self._logs_frame.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

        self._log_box = ctk.CTkTextbox(
            self._logs_frame,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#060810", text_color=TEXT_SOFT,
            corner_radius=0, wrap="none",
            state="disabled",
        )
        self._log_box.pack(fill="both", expand=True)

        # Colour tags for log levels — must be set on the inner tk.Text widget
        inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
        inner.tag_config("DEBUG",    foreground=MUTED)
        inner.tag_config("INFO",     foreground=TEXT_SOFT)
        inner.tag_config("WARNING",  foreground=YELLOW)
        inner.tag_config("ERROR",    foreground=RED)
        inner.tag_config("CRITICAL", foreground=RED)

        self._show_tab("events")

    def _show_tab(self, tab: str) -> None:
        self._active_tab = tab
        if tab == "events":
            self._events_frame.lift()
            self._tab_events.configure(fg_color=SUCCESS_BG, text_color=GREEN,
                                        font=ctk.CTkFont(size=13, weight="bold"))
            self._tab_logs.configure(fg_color="transparent", text_color=MUTED,
                                      font=ctk.CTkFont(size=13))
        else:
            self._logs_frame.lift()
            self._tab_logs.configure(fg_color=SUCCESS_BG, text_color=GREEN,
                                      font=ctk.CTkFont(size=13, weight="bold"))
            self._tab_events.configure(fg_color="transparent", text_color=MUTED,
                                        font=ctk.CTkFont(size=13))

    # ── Event rendering ────────────────────────────────────────────────────────

    def _add_event(self, event_type: str, payload: dict) -> None:
        meta = _EVENT_META.get(event_type, ("•", SURFACE_RAISED, LINE, MUTED))
        icon, bg, border, color = meta

        # Hide placeholder
        if self._no_events_lbl.winfo_ismapped():
            self._no_events_lbl.pack_forget()

        ts   = time.strftime("%H:%M:%S")
        card = ctk.CTkFrame(
            self._events_frame,
            fg_color=bg, border_width=1, border_color=border, corner_radius=6,
        )
        card.pack(fill="x", padx=8, pady=3)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=7)

        ctk.CTkLabel(
            row, text=icon, width=28,
            font=ctk.CTkFont(size=14), text_color=color,
        ).pack(side="left")

        ctk.CTkLabel(
            row, text=event_type.replace(".", " → "),
            font=ctk.CTkFont(size=12, weight="bold"), text_color=color, anchor="w",
        ).pack(side="left", padx=(4, 8))

        summary = _summarise(event_type, payload)
        if summary:
            ctk.CTkLabel(
                row, text=summary,
                font=ctk.CTkFont(size=11), text_color=TEXT_SOFT, anchor="w",
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            row, text=ts,
            font=ctk.CTkFont(family="Consolas", size=10), text_color=MUTED,
        ).pack(side="right")

        # Enforce widget cap: destroy the oldest card if deque is full
        if len(self._events) == self._events.maxlen:
            oldest = self._events[0]
            if oldest.winfo_exists():
                oldest.destroy()

        self._events.append(card)

        if self._autoscroll:
            self.after(50, self._scroll_events_to_bottom)

    def _scroll_events_to_bottom(self) -> None:
        try:
            self._events_frame._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    # ── Log file tail ──────────────────────────────────────────────────────────

    def _start_log_tail(self) -> None:
        threading.Thread(target=self._tail_log, daemon=True).start()

    def _tail_log(self) -> None:
        """
        Background thread: waits for the log file to appear (retrying every
        _TAIL_RETRY_S seconds), pre-loads the last _LOG_PRELOAD lines, then
        tails new output indefinitely.
        """
        while True:
            log_path = self._find_log()
            if log_path:
                break
            time.sleep(_TAIL_RETRY_S)

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                # Pre-load last N lines so the tab isn't empty on open
                all_lines = fh.readlines()
                preload   = all_lines[-_LOG_PRELOAD:] if len(all_lines) > _LOG_PRELOAD else all_lines
                if preload:
                    batch = "".join(preload)
                    self.after(0, lambda b=batch: self._append_log_batch(b))

                # Tail from here
                fh.seek(0, os.SEEK_END)
                while True:
                    line = fh.readline()
                    if line:
                        self.after(0, lambda l=line.rstrip(): self._append_log_line(l))
                    else:
                        time.sleep(0.4)
        except Exception:
            pass

    def _find_log(self) -> Path | None:
        from src.gui.config_manager import ConfigManager
        prog_logs = ConfigManager.programdata_logs_path()
        candidates = [
            prog_logs / "engine.log",
            prog_logs / "stdout.log",   # NSSM stdout capture
            prog_logs / "apex.log",
        ]
        import sys
        exe_dir = Path(sys.executable).parent
        candidates += [
            exe_dir / "logs" / "engine.log",
            exe_dir / "logs" / "stdout.log",   # NSSM stdout capture
            exe_dir / "data" / "engine.log",
        ]
        # Also check adjacent to config.yaml
        try:
            cfg_logs = ConfigManager().path.parent / "logs"
            candidates += [
                cfg_logs / "engine.log",
                cfg_logs / "stdout.log",
            ]
        except Exception:
            pass
        for p in candidates:
            if p.exists():
                return p
        return None

    def _append_log_batch(self, text: str) -> None:
        """Insert a multi-line preload block, then trim to _MAX_LINES."""
        inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
        inner.configure(state="normal")
        for line in text.splitlines():
            level = _log_level(line)
            inner.insert("end", line + "\n", level)
        # Trim to _MAX_LINES
        line_count = int(inner.index("end-1c").split(".")[0])
        if line_count > _MAX_LINES:
            inner.delete("1.0", f"{line_count - _MAX_LINES}.0")
        inner.configure(state="disabled")
        if self._autoscroll:
            inner.see("end")

    def _append_log_line(self, line: str) -> None:
        inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
        inner.configure(state="normal")
        inner.insert("end", line + "\n", _log_level(line))
        # Trim oldest line if over cap
        line_count = int(inner.index("end-1c").split(".")[0])
        if line_count > _MAX_LINES:
            inner.delete("1.0", "2.0")
        inner.configure(state="disabled")
        if self._autoscroll:
            inner.see("end")

    def _clear(self) -> None:
        if self._active_tab == "events":
            for w in list(self._events_frame.winfo_children()):
                if w is not self._no_events_lbl:
                    w.destroy()
            self._events.clear()
            self._no_events_lbl.pack(pady=32)
        else:
            inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
            inner.configure(state="normal")
            inner.delete("1.0", "end")
            inner.configure(state="disabled")

    # ── Broadcast callbacks ────────────────────────────────────────────────────

    def on_trade_event(self, event_type: str, payload: dict) -> None:
        self._add_event(event_type, payload)

    def on_signal_event(self, event_type: str, payload: dict) -> None:
        self._add_event(event_type, payload)

    def on_ws_connected(self) -> None:
        self._add_event("ws_connected", {})

    def on_ws_disconnected(self) -> None:
        self._add_event("ws_disconnected", {})

    def on_mt5_error(self, message: str) -> None:
        self._add_event("mt5_error", {"message": message})

    def on_engine_status(self, status: str, detail=None) -> None:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log_level(line: str) -> str:
    for lvl in ("CRITICAL", "ERROR", "WARNING", "DEBUG", "INFO"):
        if lvl in line:
            return lvl
    return "INFO"


def _summarise(event_type: str, payload: dict) -> str:
    if not payload:
        return ""
    if event_type == "trade.opened":
        sym  = payload.get("symbol", "")
        side = payload.get("direction") or payload.get("side", "")
        lots = payload.get("volume") or payload.get("lots", "")
        return f"{sym}  {side}  {lots} lots" if sym else ""
    if event_type in ("trade.sl_hit", "trade.tp1_hit", "trade.tp2_hit"):
        sym = payload.get("symbol", payload.get("trade_id", ""))
        pnl = payload.get("pnl") or payload.get("profit")
        return f"{sym}  P&L: {pnl:+.2f}" if pnl is not None else str(sym)
    if event_type == "trade.error":
        sym    = payload.get("symbol", "")
        reason = payload.get("reason", "")
        msg    = payload.get("message", "")
        if reason == "AUTOTRADING_DISABLED":
            return f"{sym}  ⚠ AutoTrading is DISABLED in MT5 — no order sent"
        return f"{sym}  {(msg or reason)[:90]}"
    if event_type == "mt5_error":
        return payload.get("message", "")[:80]
    if event_type in ("signal.received", "signal.triggered"):
        sym  = payload.get("symbol", "")
        side = payload.get("direction") or payload.get("side", "")
        return f"{sym} {side}".strip()
    if event_type == "risk.rejected":
        sym    = payload.get("symbol", "")
        reason = payload.get("reason", "")
        return f"{sym}  {reason}" if reason else str(sym)
    return ""
