"""
src/gui/pages/activity.py — Fleet activity and manager log.

Two tabs:
  • Events   — agent state-change events derived from manager_state polls
  • Raw Logs — tail of the manager log file
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

from manager.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    BASE, SURFACE, SURFACE_RAISED, LINE,
    SUCCESS_BG, DANGER_BG, WARNING_BG, INFO_BG,
    SUCCESS_BORDER, DANGER_BORDER, WARNING_BORDER, INFO_BORDER,
    page_header,
)

if TYPE_CHECKING:
    from manager.gui.app import ApexTraderGUI
    from manager.gui.manager_state import AgentCardState

_MAX_EVENTS  = 200
_MAX_LINES   = 2000
_LOG_PRELOAD = 200

_EVENT_META = {
    "agent.started":      ("▶",  SUCCESS_BG, SUCCESS_BORDER, GREEN),
    "agent.stopped":      ("■",  SURFACE_RAISED, LINE,        MUTED),
    "agent.crash":        ("⛔", DANGER_BG,  DANGER_BORDER,  RED),
    "agent.provisioned":  ("✚",  INFO_BG,    INFO_BORDER,    INFO),
    "agent.removed":      ("✕",  WARNING_BG, WARNING_BORDER, YELLOW),
    "manager.online":     ("🔗", SUCCESS_BG, SUCCESS_BORDER, GREEN),
    "manager.offline":    ("🔌", DANGER_BG,  DANGER_BORDER,  RED),
}


class ActivityPage(ctk.CTkFrame):

    def __init__(self, parent: tk.Widget, app: "ApexTraderGUI") -> None:
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.app = app
        self._events: deque[ctk.CTkFrame] = deque(maxlen=_MAX_EVENTS)
        self._autoscroll = True
        self._active_tab = "events"
        self._prev_statuses: dict[str, str] = {}
        self._build()
        self._start_log_tail()
        self.app.manager_state.subscribe("agents", self._on_agents_updated)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        page_header(self, "Activity", "Fleet events and manager log output")

        tab_bar = ctk.CTkFrame(self, fg_color=SURFACE_RAISED, corner_radius=0, height=40)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)

        self._tab_events = ctk.CTkButton(
            tab_bar, text="Events", width=120, height=38, corner_radius=0,
            fg_color=SUCCESS_BG, text_color=GREEN,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._show_tab("events"),
        )
        self._tab_events.pack(side="left")

        self._tab_logs = ctk.CTkButton(
            tab_bar, text="Raw Logs", width=120, height=38, corner_radius=0,
            fg_color="transparent", text_color=MUTED,
            font=ctk.CTkFont(size=13),
            command=lambda: self._show_tab("logs"),
        )
        self._tab_logs.pack(side="left")

        ctk.CTkButton(
            tab_bar, text="Clear", width=80, height=30,
            fg_color="transparent", hover_color=LINE,
            border_width=1, border_color=LINE, text_color=MUTED,
            font=ctk.CTkFont(size=11), command=self._clear,
        ).pack(side="right", padx=12, pady=4)

        self._autoscroll_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab_bar, text="Auto-scroll", variable=self._autoscroll_var,
            font=ctk.CTkFont(size=11), text_color=MUTED,
            checkbox_width=16, checkbox_height=16,
            command=lambda: setattr(self, "_autoscroll", self._autoscroll_var.get()),
        ).pack(side="right", padx=4)

        content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        content.pack(fill="both", expand=True)

        self._events_frame = ctk.CTkScrollableFrame(content, fg_color=BASE, corner_radius=0)
        self._events_frame.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

        self._no_events_lbl = ctk.CTkLabel(
            self._events_frame,
            text="No events yet — fleet state changes will appear here.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self._no_events_lbl.pack(pady=32)

        self._logs_frame = ctk.CTkFrame(content, fg_color=BASE, corner_radius=0)
        self._logs_frame.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

        self._log_box = ctk.CTkTextbox(
            self._logs_frame,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#060810", text_color=TEXT_SOFT,
            corner_radius=0, wrap="none", state="disabled",
        )
        self._log_box.pack(fill="both", expand=True)

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

    # ── Event rendering ───────────────────────────────────────────────────────

    def _on_agents_updated(self, agents: list["AgentCardState"]) -> None:
        current = {a.agent_id: a.status for a in agents}
        for agent_id, status in current.items():
            prev = self._prev_statuses.get(agent_id)
            if prev is None:
                self.after(0, lambda aid=agent_id, s=status: self._emit_event(
                    "agent.provisioned", f"{aid}  →  {s}", aid,
                ))
            elif prev != status:
                event_type = _status_to_event(prev, status)
                self.after(0, lambda aid=agent_id, s=status, et=event_type: self._emit_event(
                    et, f"{aid}  →  {s}", aid,
                ))
        for agent_id in list(self._prev_statuses):
            if agent_id not in current:
                self.after(0, lambda aid=agent_id: self._emit_event(
                    "agent.removed", f"{aid}  removed", aid,
                ))
        self._prev_statuses = current

    def _emit_event(self, event_type: str, summary: str, agent_id: str = "") -> None:
        meta = _EVENT_META.get(event_type, ("•", SURFACE_RAISED, LINE, MUTED))
        icon, bg, border, color = meta

        if self._no_events_lbl.winfo_ismapped():
            self._no_events_lbl.pack_forget()

        ts   = time.strftime("%H:%M:%S")
        card = ctk.CTkFrame(
            self._events_frame, fg_color=bg,
            border_width=1, border_color=border, corner_radius=6,
        )
        card.pack(fill="x", padx=8, pady=3)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=7)

        ctk.CTkLabel(row, text=icon, width=28,
                     font=ctk.CTkFont(size=14), text_color=color).pack(side="left")
        ctk.CTkLabel(row, text=event_type.replace(".", " → "),
                     font=ctk.CTkFont(size=12, weight="bold"), text_color=color,
                     anchor="w").pack(side="left", padx=(4, 8))
        if summary:
            ctk.CTkLabel(row, text=summary,
                         font=ctk.CTkFont(size=11), text_color=TEXT_SOFT,
                         anchor="w").pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(row, text=ts,
                     font=ctk.CTkFont(family="Consolas", size=10),
                     text_color=MUTED).pack(side="right")

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

    # ── Manager connect/disconnect callbacks ──────────────────────────────────

    def on_manager_online(self) -> None:
        self._emit_event("manager.online", "Manager came online")

    def on_manager_offline(self) -> None:
        self._emit_event("manager.offline", "Manager offline")

    # ── Log file tail ─────────────────────────────────────────────────────────

    def _start_log_tail(self) -> None:
        threading.Thread(target=self._tail_log, daemon=True).start()

    def _tail_log(self) -> None:
        log_path = self._find_log()
        while log_path is None:
            time.sleep(5.0)
            log_path = self._find_log()

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
                preload = all_lines[-_LOG_PRELOAD:] if len(all_lines) > _LOG_PRELOAD else all_lines
                if preload:
                    batch = "".join(preload)
                    self.after(0, lambda b=batch: self._append_log_batch(b))
                fh.seek(0, os.SEEK_END)
                while True:
                    line = fh.readline()
                    if line:
                        self.after(0, lambda l=line.rstrip(): self._append_log_line(l))
                    else:
                        time.sleep(0.5)
        except Exception:
            pass

    def _find_log(self) -> Path | None:
        from manager.gui.config_manager import ConfigManager
        mgr_logs = ConfigManager.programdata_manager_logs_path()
        for name in ("manager.log", "stdout.log", "apex.log"):
            p = mgr_logs / name
            if p.exists():
                return p
        return None

    def _append_log_batch(self, text: str) -> None:
        inner: tk.Text = self._log_box._textbox  # type: ignore[attr-defined]
        inner.configure(state="normal")
        for line in text.splitlines():
            inner.insert("end", line + "\n", _log_level(line))
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log_level(line: str) -> str:
    for lvl in ("CRITICAL", "ERROR", "WARNING", "DEBUG", "INFO"):
        if lvl in line:
            return lvl
    return "INFO"


def _status_to_event(prev: str, curr: str) -> str:
    if curr == "RUNNING":
        return "agent.started"
    if curr in ("STOPPED", "PROVISIONED"):
        return "agent.stopped"
    if curr == "CRASH_LOOP":
        return "agent.crash"
    if curr == "ERROR":
        return "agent.crash"
    return "agent.stopped"
