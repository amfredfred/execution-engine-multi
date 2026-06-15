"""
src/gui/components.py — Reusable UI building blocks.

All widgets use the shared theme palette.  Import from here instead of
building one-off frames in each page.

Exports
-------
StatusCard          — Titled card with a tone-coloured header strip
ReadinessRow        — Single checklist row (done/pending/error)
ReadinessPanel      — Full readiness checklist
ActionBanner        — Inline error / warning / success banner
PrimaryButton       — Large CTA button
SectionCard         — Borderless content group
InfoTable           — Key/value table (plain rows)
RevealEntry         — Password entry with show/hide toggle
EngineStatusBadge   — Compact pill showing EngineLifecycle
"""
from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Callable, Optional, Protocol

import customtkinter as ctk

from manager.gui.theme import (
    GREEN, RED, YELLOW, INFO, MUTED, TEXT, TEXT_SOFT,
    SURFACE_RAISED, BASE, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER,
    WARNING_BG, WARNING_BORDER,
    DANGER_BG, DANGER_BORDER,
    INFO_BG, INFO_BORDER,
    Tone,
)

if TYPE_CHECKING:
    class EngineLifecycle(Protocol):
        color_key: str
        label: str

# ── Colour helpers ────────────────────────────────────────────────────────────

_TONE_BG     = {"good": SUCCESS_BG,  "warn": WARNING_BG,  "danger": DANGER_BG,  "info": INFO_BG,  "normal": SURFACE_RAISED}
_TONE_BORDER = {"good": SUCCESS_BORDER, "warn": WARNING_BORDER, "danger": DANGER_BORDER, "info": INFO_BORDER, "normal": LINE}
_TONE_TEXT   = {"good": GREEN, "warn": YELLOW, "danger": RED, "info": INFO, "normal": TEXT}
_TONE_ACCENT = {"good": GREEN, "warn": YELLOW, "danger": RED, "info": INFO, "normal": LINE_STRONG}


# ── StatusCard ────────────────────────────────────────────────────────────────

class StatusCard(ctk.CTkFrame):
    """
    Card with a 2 px top accent bar.

        ▬▬▬▬▬▬▬▬ (tone colour)
        TITLE          badge
        body content
    """

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        tone: Tone = "normal",
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            corner_radius=8,
            fg_color=SURFACE_RAISED,
            border_width=1,
            border_color=LINE,
            **kwargs,
        )
        accent = _TONE_ACCENT.get(tone, LINE_STRONG)
        self._accent_bar = ctk.CTkFrame(self, height=2, fg_color=accent, corner_radius=0)
        self._accent_bar.pack(fill="x")

        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x", padx=16, pady=(10, 0))

        self._title_lbl = ctk.CTkLabel(
            self._header, text=title.upper(),
            font=ctk.CTkFont(size=10, weight="bold"), text_color=MUTED, anchor="w",
        )
        self._title_lbl.pack(side="left")

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=16, pady=(8, 14))

    def set_tone(self, tone: Tone) -> None:
        accent = _TONE_ACCENT.get(tone, LINE_STRONG)
        self._accent_bar.configure(fg_color=accent)

    def set_badge(self, text: str, tone: Tone) -> None:
        if not hasattr(self, "_badge"):
            self._badge = ctk.CTkLabel(
                self._header, text="",
                font=ctk.CTkFont(size=10, weight="bold"),
                corner_radius=4,
            )
            self._badge.pack(side="right")
        bg  = _TONE_BG.get(tone, SURFACE_RAISED)
        fg  = _TONE_TEXT.get(tone, MUTED)
        self._badge.configure(
            text=f"  {text}  ",
            text_color=fg,
            fg_color=bg,
        )


# ── ActionBanner ──────────────────────────────────────────────────────────────

class ActionBanner(ctk.CTkFrame):
    """
    Inline banner for errors, warnings, and success messages.

    Usage:
        banner = ActionBanner(parent)
        banner.pack(fill="x", padx=24)      # caller manages geometry
        banner.hide()                        # starts hidden

        banner.show("Saved!", "good", auto_dismiss_after_ms=3000)
        banner.hide()
    """

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._inner: Optional[ctk.CTkFrame] = None
        self._dismiss_job: Optional[str]    = None
        self._pack_kw: dict                 = {"fill": "x"}

    # Override pack so we can replay the original kwargs on show()
    def pack(self, **kwargs) -> None:
        self._pack_kw = dict(kwargs)
        super().pack(**kwargs)

    def show(
        self,
        message: str,
        tone: Tone = "danger",
        auto_dismiss_after_ms: int = 0,
    ) -> None:
        # Cancel any pending auto-dismiss
        if self._dismiss_job:
            try:
                self.after_cancel(self._dismiss_job)
            except Exception:
                pass
            self._dismiss_job = None

        if self._inner:
            self._inner.destroy()
            self._inner = None

        bg   = _TONE_BG.get(tone, DANGER_BG)
        brd  = _TONE_BORDER.get(tone, DANGER_BORDER)
        fg   = _TONE_TEXT.get(tone, RED)
        icon = {"good": "✓", "warn": "⚠", "danger": "✕", "info": "ℹ", "normal": "•"}.get(tone, "•")

        self._inner = ctk.CTkFrame(
            self, fg_color=bg, border_width=1, border_color=brd, corner_radius=6,
        )
        self._inner.pack(fill="x")

        row = ctk.CTkFrame(self._inner, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=7)

        ctk.CTkLabel(
            row, text=icon, width=18,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=fg,
        ).pack(side="left")

        # Responsive wraplength — updated on resize via <Configure>
        msg_lbl = ctk.CTkLabel(
            row, text=message,
            font=ctk.CTkFont(size=12), text_color=fg,
            anchor="w", justify="left",
            wraplength=400,                 # sensible default before first resize
        )
        msg_lbl.pack(side="left", fill="x", expand=True, padx=(8, 0))

        def _sync_wrap(event, lbl=msg_lbl):
            avail = event.width - 60        # subtract icon + padding
            if avail > 60:
                lbl.configure(wraplength=avail)

        row.bind("<Configure>", _sync_wrap)

        # Re-pack with original kwargs so padx/pady are preserved
        super().pack(**self._pack_kw)

        if auto_dismiss_after_ms > 0:
            self._dismiss_job = self.after(auto_dismiss_after_ms, self.hide)

    def hide(self) -> None:
        if self._dismiss_job:
            try:
                self.after_cancel(self._dismiss_job)
            except Exception:
                pass
            self._dismiss_job = None
        if self._inner:
            self._inner.destroy()
            self._inner = None
        self.pack_forget()


# ── ReadinessRow ──────────────────────────────────────────────────────────────

class ReadinessRow(ctk.CTkFrame):
    """
    Single row in a setup/readiness checklist.

        ● / ✓   Title                    [Action]
                Detail text
    """

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        detail: str,
        done: bool = False,
        action_label: Optional[str] = None,
        action_fn: Optional[Callable] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            fg_color=SURFACE_RAISED,
            border_width=1,
            border_color=LINE,
            corner_radius=6,
            **kwargs,
        )
        self._build(title, detail, done, action_label, action_fn)

    def _build(
        self,
        title: str,
        detail: str,
        done: bool,
        action_label: Optional[str],
        action_fn: Optional[Callable],
    ) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)

        dot_color = GREEN if done else MUTED
        dot_text  = "✓" if done else "○"
        ctk.CTkLabel(
            row, text=dot_text, width=20,
            font=ctk.CTkFont(size=14, weight="bold"), text_color=dot_color,
        ).pack(side="left", anchor="n", pady=2)

        text_col = ctk.CTkFrame(row, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True, padx=(8, 8))

        ctk.CTkLabel(
            text_col, text=title, anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT if done else TEXT_SOFT,
        ).pack(anchor="w")

        if detail:
            ctk.CTkLabel(
                text_col, text=detail, anchor="w",
                font=ctk.CTkFont(size=11), text_color=MUTED,
                justify="left",
            ).pack(anchor="w", pady=(2, 0))

        if action_label and action_fn and not done:
            ctk.CTkButton(
                row, text=action_label, width=120, height=28,
                font=ctk.CTkFont(size=11),
                fg_color=SUCCESS_BG, hover_color=SUCCESS_BORDER,
                border_width=1, border_color=SUCCESS_BORDER,
                text_color=GREEN,
                command=action_fn,
            ).pack(side="right", anchor="n", pady=2)


# ── ReadinessPanel ────────────────────────────────────────────────────────────

class ReadinessPanel(ctk.CTkFrame):
    """
    A labelled panel of ReadinessRow items rendered from a list.
    Call .refresh(issues, all_items) to repopulate.

    Uses signature-based diffing: the panel is only rebuilt when the data
    actually changes, preventing flicker on every service-poll tick.
    """

    def __init__(
        self,
        parent: tk.Widget,
        navigate_fn: Optional[Callable] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._navigate  = navigate_fn
        self._last_sig: Optional[tuple] = None   # signature of last render

    def refresh(
        self,
        issues: list,
        all_items: Optional[list] = None,
    ) -> None:
        """
        issues     — list of ReadinessIssue (blocking items)
        all_items  — optional list of (key, label, detail, done, action_label,
                     action_page) for complete checklist view
        """
        items = all_items or []

        # ── Signature check — skip rebuild if nothing changed ─────────────────
        sig = tuple(
            (key, label, detail, bool(done), action_label, action_page)
            for key, label, detail, done, action_label, action_page in items
        )
        if sig and sig == self._last_sig:
            return
        self._last_sig = sig

        # ── Full rebuild (only runs when data actually changed) ───────────────
        for w in self.winfo_children():
            w.destroy()

        if not items and not issues:
            ctk.CTkLabel(
                self, text="✓  All systems ready",
                font=ctk.CTkFont(size=13, weight="bold"), text_color=GREEN,
            ).pack(anchor="w", pady=4)
            return

        for item in items:
            key, label, detail, done, action_label, action_page = item
            fn = (
                (lambda p=action_page: self._navigate(p))
                if action_page and self._navigate and not done
                else None
            )
            ReadinessRow(
                self, title=label, detail=detail, done=done,
                action_label=action_label, action_fn=fn,
            ).pack(fill="x", pady=3)

        if not items and issues:
            for issue in issues:
                fn = (
                    (lambda p=issue.action_page: self._navigate(p))
                    if issue.action_page and self._navigate
                    else issue.action_fn
                )
                ReadinessRow(
                    self,
                    title=issue.title, detail=issue.detail,
                    done=False,
                    action_label=issue.action_label, action_fn=fn,
                ).pack(fill="x", pady=3)


# ── PrimaryButton ─────────────────────────────────────────────────────────────

class PrimaryButton(ctk.CTkButton):
    """Large CTA button with consistent tone styling."""

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Optional[Callable] = None,
        tone: Tone = "good",
        width: int = 200,
        height: int = 44,
        **kwargs,
    ) -> None:
        bg  = _TONE_BG.get(tone, SUCCESS_BG)
        brd = _TONE_BORDER.get(tone, SUCCESS_BORDER)
        fg  = _TONE_TEXT.get(tone, GREEN)
        super().__init__(
            parent,
            text=text,
            width=width, height=height,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=bg,
            hover_color=brd,
            border_width=1, border_color=brd,
            text_color=fg,
            command=command,
            **kwargs,
        )


# ── SectionCard ───────────────────────────────────────────────────────────────

class SectionCard(ctk.CTkFrame):
    """Simple bordered card.  Use .body to pack children into."""

    def __init__(
        self,
        parent: tk.Widget,
        padx: int = 20,
        pady: int = 16,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            corner_radius=8,
            fg_color=SURFACE_RAISED,
            border_width=1,
            border_color=LINE,
            **kwargs,
        )
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=padx, pady=pady)


# ── InfoTable ─────────────────────────────────────────────────────────────────

class InfoTable(ctk.CTkFrame):
    """
    Key/value rows for read-only display (technical detail cards).
    """

    def __init__(self, parent: tk.Widget, label_width: int = 180, **kwargs) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._lw = label_width

    def add_row(self, label: str, value: str) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(
            row, text=label, width=self._lw, anchor="w",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        ).pack(side="left")
        ctk.CTkLabel(
            row, text=value, anchor="w",
            font=ctk.CTkFont(family="Consolas", size=12), text_color=TEXT_SOFT,
        ).pack(side="left")


# ── RevealEntry ───────────────────────────────────────────────────────────────

class RevealEntry(ctk.CTkFrame):
    """Password/secret entry with a reveal toggle button."""

    def __init__(
        self,
        parent: tk.Widget,
        textvariable: Optional[tk.StringVar] = None,
        width: int = 300,
        placeholder: str = "",
        **kwargs,
    ) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._var     = textvariable or tk.StringVar()
        self._revealed = False

        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._var,
            width=width,
            show="●",
            font=ctk.CTkFont(family="Consolas", size=12),
            placeholder_text=placeholder,
        )
        self._entry.pack(side="left")

        self._btn = ctk.CTkButton(
            self,
            text="Show",
            width=52, height=28,
            font=ctk.CTkFont(size=11),
            fg_color=BASE, hover_color=LINE_STRONG,
            border_width=1, border_color=LINE,
            text_color=MUTED,
            command=self._toggle,
        )
        self._btn.pack(side="left", padx=(6, 0))

    def _toggle(self) -> None:
        self._revealed = not self._revealed
        self._entry.configure(show="" if self._revealed else "●")
        self._btn.configure(text="Hide" if self._revealed else "Show")

    @property
    def variable(self) -> tk.StringVar:
        return self._var

    def get(self) -> str:
        return self._var.get()

    def set(self, value: str) -> None:
        self._var.set(value)


# ── EngineStatusBadge ─────────────────────────────────────────────────────────

class EngineStatusBadge(ctk.CTkFrame):
    """Compact status pill: ● Running / ● Stopped / etc."""

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(
            parent,
            corner_radius=4,
            fg_color=SURFACE_RAISED,
            border_width=1,
            border_color=LINE,
            **kwargs,
        )
        self._lbl = ctk.CTkLabel(
            self,
            text="●  Unknown",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=MUTED,
        )
        self._lbl.pack(padx=10, pady=4)

    def update(self, lifecycle: "EngineLifecycle") -> None:
        from manager.gui.theme import GREEN, RED, YELLOW, MUTED
        color = {
            "good":   GREEN,
            "warn":   YELLOW,
            "danger": RED,
            "normal": MUTED,
        }.get(lifecycle.color_key, MUTED)
        self._lbl.configure(text=f"●  {lifecycle.label}", text_color=color)
        bg  = _TONE_BG.get(lifecycle.color_key, SURFACE_RAISED)
        brd = _TONE_BORDER.get(lifecycle.color_key, LINE)
        self.configure(fg_color=bg, border_color=brd)

    def set_manager_status(self, online: bool) -> None:
        """Override badge to show manager connectivity instead of legacy service status."""
        from manager.gui.theme import GREEN, RED, SUCCESS_BG, SUCCESS_BORDER, DANGER_BG, DANGER_BORDER
        if online:
            self._lbl.configure(text="●  Manager running", text_color=GREEN)
            self.configure(fg_color=SUCCESS_BG, border_color=SUCCESS_BORDER)
        else:
            self._lbl.configure(text="○  Manager offline", text_color=RED)
            self.configure(fg_color=DANGER_BG, border_color=DANGER_BORDER)


# ── LabeledField ─────────────────────────────────────────────────────────────

def labeled_field(
    parent: tk.Widget,
    label: str,
    variable: tk.StringVar,
    masked: bool = False,
    width: int = 300,
    placeholder: str = "",
    hint: str = "",
    label_width: int = 140,
) -> ctk.CTkFrame | RevealEntry:
    """
    Returns a row frame containing label + entry (or RevealEntry if masked).
    Already packed into parent.
    """
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=5)

    ctk.CTkLabel(
        row, text=label, width=label_width, anchor="w",
        font=ctk.CTkFont(size=12), text_color=TEXT,
    ).pack(side="left")

    if masked:
        widget = RevealEntry(row, textvariable=variable, width=width, placeholder=placeholder)
        widget.pack(side="left")
    else:
        widget = ctk.CTkEntry(
            row, textvariable=variable, width=width,
            font=ctk.CTkFont(family="Consolas", size=12),
            placeholder_text=placeholder,
        )
        widget.pack(side="left")

    if hint:
        ctk.CTkLabel(
            row, text=hint, anchor="w",
            font=ctk.CTkFont(size=10), text_color=MUTED,
        ).pack(side="left", padx=(8, 0))

    return widget
