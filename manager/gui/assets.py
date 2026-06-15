"""
manager/gui/assets.py — Icon and image loading helpers for the Apex GUI.

Locates assets in both the development layout (manager/gui/assets/)
and the PyInstaller onedir bundle (_MEIPASS/manager/gui/assets/).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Tuple

import customtkinter as ctk

# ── Asset path resolver ───────────────────────────────────────────────────────

def _assets_dir() -> Path:
    """Return the assets directory for both dev and packaged environments."""
    # 1. PyInstaller bundle: _MEIPASS/manager/gui/assets
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "manager" / "gui" / "assets"
        if p.exists():
            return p

    # 2. Next to this file: manager/gui/assets/
    here = Path(__file__).resolve().parent / "assets"
    if here.exists():
        return here

    # 3. Walk up from executable looking for manager/gui/assets
    exe_dir = Path(sys.executable).parent
    for depth in range(5):
        candidate = exe_dir
        for _ in range(depth):
            candidate = candidate.parent
        p = candidate / "manager" / "gui" / "assets"
        if p.exists():
            return p

    return here  # fallback even if non-existent


def asset_path(filename: str) -> Optional[Path]:
    p = _assets_dir() / filename
    return p if p.exists() else None


# ── Logo helpers ──────────────────────────────────────────────────────────────

def load_logo_image(
    size: Tuple[int, int] = (32, 32),
) -> Optional[ctk.CTkImage]:
    """
    Return a CTkImage of the Apex logo scaled to *size* (width, height).
    Returns None if PIL is unavailable or the file cannot be found.
    """
    path = asset_path("icon.png")
    if path is None:
        return None
    try:
        from PIL import Image
        img = Image.open(path).convert("RGBA")
        return ctk.CTkImage(light_image=img, dark_image=img, size=size)
    except Exception:
        return None


def set_window_icon(window: "ctk.CTk") -> None:  # type: ignore[name-defined]
    """
    Set the title-bar and taskbar icon for *window*.

    Strategy (Windows):
      1. wm_iconbitmap with icon.ico  — sets title-bar + taskbar reliably
      2. iconphoto with icon.png      — fallback / also updates alt-tab thumbnail

    Safe to call on non-Windows (iconbitmap is skipped, iconphoto still runs).
    """
    # ── 1. ICO path (preferred on Windows) ────────────────────────────────────
    ico_path = asset_path("icon.ico")
    if ico_path and sys.platform == "win32":
        try:
            window.iconbitmap(str(ico_path))
        except Exception:
            pass

    # ── 2. PNG via iconphoto (cross-platform, also updates alt-tab) ───────────
    png_path = asset_path("icon.png")
    if png_path is None:
        return
    try:
        from PIL import Image, ImageTk
        img   = Image.open(png_path).convert("RGBA")
        # Provide both 32×32 and 64×64 for high-DPI displays
        photo32 = ImageTk.PhotoImage(img.resize((32, 32)))
        photo64 = ImageTk.PhotoImage(img.resize((64, 64)))
        # Keep references so Python doesn't GC the images
        window._icon_photos = (photo32, photo64)  # type: ignore[attr-defined]
        window.iconphoto(True, photo64, photo32)
    except Exception:
        pass
