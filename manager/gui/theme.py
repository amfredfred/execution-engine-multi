"""
src/gui/theme.py — Shared design tokens and widget factories.

Mirrors the customer dashboard globals.css colour system exactly so the
desktop GUI and the web dashboard share the same visual language.
"""
from __future__ import annotations

import tkinter as tk
from typing import ClassVar, Literal

import customtkinter as ctk

# ── Brand / tone ──────────────────────────────────────────────────────────────
GREEN   = "#3ddc97"   # success  (dashboard --success)
RED     = "#f43f5e"   # danger   (dashboard --danger)
YELLOW  = "#f5b942"   # warning  (dashboard --warning)
INFO    = "#8ab4ff"   # info     (dashboard --info)

# ── Text ──────────────────────────────────────────────────────────────────────
TEXT      = "#f2f4f7"   # dashboard --text
TEXT_SOFT = "#c9ced6"   # dashboard --text-soft
MUTED     = "#818891"   # dashboard --muted

# ── Surfaces ──────────────────────────────────────────────────────────────────
BASE           = "#08090b"   # dashboard --base  (window chrome)
SURFACE        = "#0d1015"   # dashboard --surface  (content area)
SURFACE_RAISED = "#121620"   # dashboard --surface-raised  (cards)

# ── Borders (rgba blended onto SURFACE) ──────────────────────────────────────
LINE        = "#191c22"   # ≈ rgba(255,255,255,.08)
LINE_STRONG = "#252932"   # ≈ rgba(255,255,255,.15)

# ── Tone tint backgrounds (rgba blended onto SURFACE) ─────────────────────────
SUCCESS_BG     = "#0d2318"
SUCCESS_BORDER = "#1d4530"
WARNING_BG     = "#1d1808"
WARNING_BORDER = "#382d14"
DANGER_BG      = "#1d0d10"
DANGER_BORDER  = "#38141e"
INFO_BG        = "#0d1520"
INFO_BORDER    = "#1d2c42"

# ── Sidebar ───────────────────────────────────────────────────────────────────
SIDEBAR       = BASE
NAV_HOVER     = "#111418"
NAV_ACTIVE_BG = SUCCESS_BG   # rgba(61,220,151,.08) tint

# ── Type alias ────────────────────────────────────────────────────────────────
Tone = Literal["normal", "good", "warn", "danger", "info"]

_TONE_COLOR = {
    "good":   GREEN,
    "warn":   YELLOW,
    "danger": RED,
    "info":   INFO,
    "normal": "transparent",
}
_TONE_TEXT = {
    "good":   GREEN,
    "warn":   YELLOW,
    "danger": RED,
    "info":   INFO,
    "normal": TEXT,
}


# ── Shared widget factories ───────────────────────────────────────────────────

def section_rule(parent: tk.Widget, text: str) -> ctk.CTkFrame:
    """
    Dashboard-style section header:  ▌ SECTION TITLE ────────────────
    A 3×14 green accent bar, uppercase muted label, then a horizontal rule.
    """
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(fill="x", pady=(16, 8))

    ctk.CTkFrame(
        frame, width=3, height=14, fg_color=GREEN, corner_radius=1,
    ).pack(side="left", padx=(0, 8))

    ctk.CTkLabel(
        frame,
        text=text.upper(),
        font=ctk.CTkFont(size=10, weight="bold"),
        text_color=MUTED,
        anchor="w",
    ).pack(side="left")

    ctk.CTkFrame(
        frame, height=1, fg_color=LINE,
    ).pack(side="left", fill="x", expand=True, padx=(10, 0))

    return frame


def page_header(
    parent: tk.Widget,
    title: str,
    subtitle: str = "",
) -> ctk.CTkFrame:
    """
    Standard page top bar:  title (+ optional subtitle) on the left.
    The frame is already pack()ed into parent; returns it for optional
    further configuration.
    """
    height = 62 if subtitle else 52
    hdr = ctk.CTkFrame(parent, height=height, fg_color=SURFACE_RAISED, corner_radius=0)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)

    # Left accent strip
    ctk.CTkFrame(hdr, width=3, fg_color=GREEN, corner_radius=0).pack(
        side="left", fill="y",
    )

    title_col = ctk.CTkFrame(hdr, fg_color="transparent")
    title_col.pack(side="left", padx=(14, 0))

    ctk.CTkLabel(
        title_col,
        text=title,
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color=TEXT,
        anchor="w",
    ).pack(anchor="w")

    if subtitle:
        ctk.CTkLabel(
            title_col,
            text=subtitle,
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            anchor="w",
        ).pack(anchor="w")

    return hdr


class KpiCard(ctk.CTkFrame):
    """
    Dashboard-style KPI card.

        ┌─────────────────────────────┐
        │ ▬ (2 px tone accent)        │
        │  LABEL          (10 px)     │
        │  123,456.78     (18 px mono)│
        │  detail text    (11 px muted│
        └─────────────────────────────┘
    """

    def __init__(
        self,
        parent: tk.Widget,
        label: str,
        value: str = "--",
        tone: Tone = "normal",
        detail: str = "",
        **kwargs,
    ) -> None:
        accent = _TONE_COLOR.get(tone, "transparent")
        super().__init__(
            parent,
            corner_radius=8,
            fg_color=SURFACE_RAISED,
            border_width=1,
            border_color=LINE,
            **kwargs,
        )

        # 2 px top accent bar
        self._accent_bar = ctk.CTkFrame(
            self, height=2, fg_color=accent, corner_radius=0,
        )
        self._accent_bar.pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=13, pady=(10, 12))

        ctk.CTkLabel(
            body,
            text=label.upper(),
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=MUTED,
            anchor="w",
        ).pack(anchor="w")

        self._val_lbl = ctk.CTkLabel(
            body,
            text=value,
            font=ctk.CTkFont(family="Consolas", size=18, weight="bold"),
            text_color=_TONE_TEXT.get(tone, TEXT),
            anchor="w",
        )
        self._val_lbl.pack(anchor="w", pady=(6, 0))

        if detail:
            self._detail_lbl = ctk.CTkLabel(
                body,
                text=detail,
                font=ctk.CTkFont(size=11),
                text_color=MUTED,
                anchor="w",
            )
            self._detail_lbl.pack(anchor="w", pady=(3, 0))
        else:
            self._detail_lbl = None

    def set(self, value: str, tone: Tone = "normal", detail: str | None = None) -> None:
        accent = _TONE_COLOR.get(tone, "transparent")
        self._accent_bar.configure(fg_color=accent if accent != "transparent" else LINE)
        self._val_lbl.configure(
            text=value,
            text_color=_TONE_TEXT.get(tone, TEXT),
        )
        if detail is not None and self._detail_lbl is not None:
            self._detail_lbl.configure(text=detail)


class Badge(ctk.CTkFrame):
    """
    Inline status badge:  ● RUNNING  /  ⚠ WARN  etc.
    tone: good | warn | danger | info | normal
    """

    _BG: ClassVar[dict[str, str]] = {
        "good":   SUCCESS_BG,
        "warn":   WARNING_BG,
        "danger": DANGER_BG,
        "info":   INFO_BG,
        "normal": SURFACE_RAISED,
    }
    _BORDER: ClassVar[dict[str, str]] = {
        "good":   SUCCESS_BORDER,
        "warn":   WARNING_BORDER,
        "danger": DANGER_BORDER,
        "info":   INFO_BORDER,
        "normal": LINE_STRONG,
    }

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        tone: Tone = "normal",
        **kwargs,
    ) -> None:
        bg     = self._BG.get(tone, SURFACE_RAISED)
        border = self._BORDER.get(tone, LINE_STRONG)
        color  = _TONE_TEXT.get(tone, MUTED)
        super().__init__(
            parent,
            corner_radius=4,
            fg_color=bg,
            border_width=1,
            border_color=border,
            **kwargs,
        )
        self._lbl = ctk.CTkLabel(
            self,
            text=text,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=color,
        )
        self._lbl.pack(padx=8, pady=2)

    def set(self, text: str, tone: Tone = "normal") -> None:
        bg     = self._BG.get(tone, SURFACE_RAISED)
        border = self._BORDER.get(tone, LINE_STRONG)
        color  = _TONE_TEXT.get(tone, MUTED)
        self.configure(fg_color=bg, border_color=border)
        self._lbl.configure(text=text, text_color=color)


def info_row(
    parent: tk.Widget,
    label: str,
    value: str,
    label_width: int = 180,
) -> None:
    """Key → value row used in technical detail cards."""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=3)
    ctk.CTkLabel(
        row,
        text=label,
        width=label_width,
        anchor="w",
        font=ctk.CTkFont(size=12),
        text_color=MUTED,
    ).pack(side="left")
    ctk.CTkLabel(
        row,
        text=value,
        anchor="w",
        font=ctk.CTkFont(family="Consolas", size=12),
        text_color=TEXT_SOFT,
    ).pack(side="left")
