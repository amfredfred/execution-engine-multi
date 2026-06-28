"""
src/gui/onboarding.py — First-run setup wizard (multi-agent edition).

The wizard is presented as a centered card (not full-screen).
On completion it calls on_complete() so app.py transitions to the fleet dashboard.

Steps
-----
1  Welcome          — what Apex Multi-Agent does
2  License Key      — activation key + verify → shows available pairs + slots
3  Install Manager  — register the AQ Manager scheduled task + wait for it to start
4  Finish           — summary + "Open Agents" CTA
"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk

from manager.gui.theme import (
    GREEN, RED, YELLOW, MUTED, TEXT, TEXT_SOFT,
    SURFACE, SURFACE_RAISED, BASE, LINE, LINE_STRONG,
    SUCCESS_BG, SUCCESS_BORDER,
    DANGER_BG, DANGER_BORDER,
    WARNING_BG, WARNING_BORDER,
    INFO_BG, INFO_BORDER, INFO,
)
from manager.gui.components import (
    ActionBanner, PrimaryButton, SectionCard, labeled_field,
)

if TYPE_CHECKING:
    from manager.gui.config_manager import ConfigManager
    from manager.gui.installer import InstallerService

# Path written by the Manager on first successful start
_TOKEN_PATH = (
    Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
    / "Apex Quantel" / "Multi" / "manager" / "api_token.txt"
)

_TOTAL_STEPS  = 4
_CARD_WIDTH   = 840
_STEP_H       = 420

_CARD_HEIGHT  = 4 + 52 + 1 + _STEP_H + 1 + 60  # = 538


# ── Wizard shell ──────────────────────────────────────────────────────────────

class OnboardingWizard(ctk.CTkFrame):
    """
    Full-window frame containing a centered card.
    Call .start() to show step 1.
    """

    def __init__(
        self,
        parent: tk.Widget,
        config: "ConfigManager",
        installer: "InstallerService",
        on_complete: Callable,
    ) -> None:
        super().__init__(parent, fg_color=BASE, corner_radius=0)
        self._cfg       = config
        self._installer = installer
        self._done_cb   = on_complete
        self._step      = 0
        self._step_frames: list[_WizardStep] = []
        self._current_frame: Optional[_WizardStep] = None
        self._data: dict = {}

        self._card = ctk.CTkFrame(
            self,
            width=640,
            height=_CARD_HEIGHT,
            fg_color=SURFACE_RAISED,
            corner_radius=10,
            border_width=1,
            border_color=LINE_STRONG,
        )
        self._card.pack_propagate(False)
        self._card.place(relx=0.5, rely=0.5, anchor="center")
        self.bind("<Configure>", self._on_outer_resize)

        self._build_chrome()
        self._build_steps()

    def _on_outer_resize(self, event: tk.Event) -> None:
        w = min(_CARD_WIDTH, max(560, event.width - 80))
        self._card.configure(width=w)

    def start(self) -> None:
        self._goto(0)

    # ── Chrome ────────────────────────────────────────────────────────────────

    def _build_chrome(self) -> None:
        card = self._card

        # Progress bar (top accent)
        top = ctk.CTkFrame(card, height=4, fg_color=BASE, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        self._progress_bar = ctk.CTkFrame(top, height=4, fg_color=GREEN, corner_radius=0)
        self._progress_bar.place(x=0, y=0, relheight=1.0, relwidth=0.0)

        # Header
        hdr = ctk.CTkFrame(card, height=52, fg_color="transparent", corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkFrame(hdr, width=3, fg_color=GREEN, corner_radius=0).pack(
            side="left", fill="y",
        )
        self._hdr_title = ctk.CTkLabel(
            hdr, text="Setup",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT,
        )
        self._hdr_title.pack(side="left", padx=14)
        self._step_lbl = ctk.CTkLabel(
            hdr, text=f"Step 1 of {_TOTAL_STEPS}",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self._step_lbl.pack(side="right", padx=16)

        ctk.CTkFrame(card, height=1, fg_color=LINE, corner_radius=0).pack(fill="x")

        # Content area
        self._content = ctk.CTkFrame(card, fg_color="transparent", height=_STEP_H)
        self._content.pack(fill="x")
        self._content.pack_propagate(False)

        ctk.CTkFrame(card, height=1, fg_color=LINE, corner_radius=0).pack(fill="x")

        # Footer
        footer = ctk.CTkFrame(card, height=60, fg_color="transparent", corner_radius=0)
        footer.pack(fill="x")
        footer.pack_propagate(False)

        btn_area = ctk.CTkFrame(footer, fg_color="transparent")
        btn_area.pack(fill="both", expand=True, padx=20)

        self._btn_back = ctk.CTkButton(
            btn_area, text="← Back", width=100, height=34,
            font=ctk.CTkFont(size=12),
            fg_color="transparent", hover_color=LINE_STRONG,
            border_width=1, border_color=LINE,
            text_color=MUTED,
            command=self._back,
        )
        self._btn_back.pack(side="left", pady=12)

        self._btn_next = PrimaryButton(
            btn_area, text="Continue →", width=140, height=34, tone="good",
            command=self._next,
        )
        self._btn_next.pack(side="right", pady=12)

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _build_steps(self) -> None:
        self._step_frames = [
            _StepWelcome(self._content, self),
            _StepActivation(self._content, self),
            _StepInstallManager(self._content, self, self._installer),
            _StepFinish(self._content, self),
        ]

    # ── Navigation ────────────────────────────────────────────────────────────

    def _goto(self, idx: int) -> None:
        if self._current_frame:
            self._current_frame.pack_forget()

        self._step = idx
        frame = self._step_frames[idx]
        frame.pack(fill="both", expand=True)
        frame.on_enter(self._cfg.load(), self._data)
        self._current_frame = frame

        step_num = idx + 1
        self._step_lbl.configure(text=f"Step {step_num} of {_TOTAL_STEPS}")
        self._hdr_title.configure(text=frame.title)
        self._progress_bar.place(
            x=0, y=0, relheight=1.0, relwidth=step_num / _TOTAL_STEPS,
        )
        self._btn_back.configure(state="normal" if idx > 0 else "disabled")

        if idx == _TOTAL_STEPS - 1:
            self._btn_next.configure(text="Finish  ✓", state="normal")
        else:
            self._btn_next.configure(text="Continue →")

    def _next(self) -> None:
        frame = self._step_frames[self._step]
        ok, _ = frame.validate_and_save(self._cfg, self._data)
        if not ok:
            return
        if self._step == _TOTAL_STEPS - 1:
            self._finish()
        else:
            self._goto(self._step + 1)

    def _back(self) -> None:
        if self._step > 0:
            self._goto(self._step - 1)

    def _finish(self) -> None:
        try:
            self._done_cb()
        except Exception:
            pass

    def navigate_to_step(self, idx: int) -> None:
        self._goto(idx)

    def open_dashboard(self) -> None:
        webbrowser.open(self._cfg.dashboard_url())


# ── Base step ─────────────────────────────────────────────────────────────────

class _WizardStep(ctk.CTkScrollableFrame):
    title: str = "Setup"

    def __init__(self, parent: tk.Widget, wizard: OnboardingWizard) -> None:
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        pass

    def on_enter(self, cfg: dict, data: dict) -> None:
        pass

    def validate_and_save(
        self,
        config: "ConfigManager",
        data: dict,
    ) -> tuple[bool, str]:
        return True, ""


# ── Step 1 — Welcome ──────────────────────────────────────────────────────────

class _StepWelcome(_WizardStep):
    title = "Welcome to Apex Quantel"

    def _build(self) -> None:
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="x", padx=48, pady=(24, 16))

        try:
            from manager.gui.assets import load_logo_image
            logo_img = load_logo_image(size=(56, 56))
        except Exception:
            logo_img = None

        if logo_img:
            ctk.CTkLabel(outer, image=logo_img, text="",
                         fg_color="transparent").pack(pady=(0, 8))
        else:
            ctk.CTkLabel(outer, text="⚡",
                         font=ctk.CTkFont(size=52), text_color=GREEN).pack(pady=(0, 8))

        ctk.CTkLabel(
            outer, text="Apex Quantel",
            font=ctk.CTkFont(size=26, weight="bold"), text_color=TEXT,
        ).pack()

        ctk.CTkLabel(
            outer, text="Multi-account automated trading infrastructure",
            font=ctk.CTkFont(size=13), text_color=MUTED,
        ).pack(pady=(4, 20))

        for icon, heading, body in [
            ("⚡", "AQ Manager",
             "The AQ Manager runs as a background task and orchestrates all your "
             "MT5 trading accounts. It connects to the gateway once and routes "
             "signals to each account simultaneously."),
            ("🖥️", "This control panel",
             "This app lets you provision and monitor multiple MT5 accounts from "
             "one place. Each account runs its own isolated trading engine with "
             "its own risk settings."),
            ("📋", "First-time setup",
             "This short wizard will verify your license key and register the "
             "AQ Manager service. Then you add your MT5 accounts one by one "
             "from the Agents fleet view."),
        ]:
            card = ctk.CTkFrame(
                outer, corner_radius=8,
                fg_color=BASE, border_width=1, border_color=LINE,
            )
            card.pack(fill="x", pady=4)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(padx=14, pady=10, fill="x")
            ctk.CTkLabel(
                row, text=icon, font=ctk.CTkFont(size=18), width=32,
            ).pack(side="left", anchor="n", pady=2)
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True, padx=(10, 0))
            ctk.CTkLabel(
                col, text=heading, anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT,
            ).pack(anchor="w")
            ctk.CTkLabel(
                col, text=body, anchor="w",
                font=ctk.CTkFont(size=11), text_color=MUTED,
                justify="left", wraplength=580,
            ).pack(anchor="w", pady=(2, 0))


# ── Step 2 — License Key ──────────────────────────────────────────────────────

_DEFAULT_GW_WS_URL = "ws://localhost:4000/engine"

_SYMBOL_LABELS: dict[str, str] = {
    "XAUUSD": "Gold / US Dollar",
    "XAGUSD": "Silver / US Dollar",
    "EURUSD": "Euro / US Dollar",
    "GBPUSD": "British Pound / Dollar",
    "USDJPY": "US Dollar / Japanese Yen",
    "USDCHF": "US Dollar / Swiss Franc",
    "AUDUSD": "Australian Dollar / USD",
    "USDCAD": "US Dollar / Canadian Dollar",
    "NZDUSD": "New Zealand Dollar / USD",
    "US100":  "Nasdaq 100",
    "US500":  "S&P 500",
    "US30":   "Dow Jones 30",
    "BTCUSD": "Bitcoin / US Dollar",
    "ETHUSD": "Ethereum / US Dollar",
}


class _StepActivation(_WizardStep):
    title = "License Key"

    def _build(self) -> None:
        self._state: str = "idle"
        self._preflight: dict | None = None
        self._gateway_ws_url: str = ""

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=32, pady=(16, 0))
        self._f = f

        # Hero
        hero = ctk.CTkFrame(f, fg_color="transparent")
        hero.pack(fill="x", pady=(0, 18))
        ctk.CTkLabel(hero, text="🔑", font=ctk.CTkFont(size=32)).pack(anchor="w")
        ctk.CTkLabel(
            hero, text="Activate your license",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=TEXT, anchor="w",
        ).pack(anchor="w", pady=(4, 2))
        ctk.CTkLabel(
            hero,
            text="Paste the key from your Apex Quantel dashboard. "
                 "One key covers all your managed MT5 accounts.",
            font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w",
            wraplength=680, justify="left",
        ).pack(anchor="w")

        # Key entry card
        key_wrap = ctk.CTkFrame(
            f, fg_color=SURFACE_RAISED, corner_radius=10,
            border_width=1, border_color=LINE_STRONG,
        )
        key_wrap.pack(fill="x", pady=(0, 4))
        key_inner = ctk.CTkFrame(key_wrap, fg_color="transparent")
        key_inner.pack(fill="x", padx=14, pady=10)

        ctk.CTkLabel(
            key_inner, text="LICENSE KEY",
            font=ctk.CTkFont(size=10, weight="bold"), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(0, 6))

        entry_row = ctk.CTkFrame(key_inner, fg_color="transparent")
        entry_row.pack(fill="x")
        self._var_key = tk.StringVar()
        self._key_entry = ctk.CTkEntry(
            entry_row, textvariable=self._var_key, show="●",
            font=ctk.CTkFont(family="Consolas", size=13),
            placeholder_text="Paste your license key here",
            height=44, corner_radius=8,
        )
        self._key_entry.pack(side="left", fill="x", expand=True)
        self._btn_verify = PrimaryButton(
            entry_row, text="Verify Key", tone="info",
            width=120, height=44,
            command=self._verify,
        )
        self._btn_verify.pack(side="left", padx=(10, 0))

        # Dashboard link
        dash_row = ctk.CTkFrame(f, fg_color="transparent")
        dash_row.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(
            dash_row, text="Don't have a key?",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        ).pack(side="left")
        ctk.CTkButton(
            dash_row, text="Open Web Dashboard →",
            font=ctk.CTkFont(size=11), text_color=INFO,
            fg_color="transparent", hover_color=LINE_STRONG,
            height=24, width=0,
            command=self.wizard.open_dashboard,
        ).pack(side="left", padx=(6, 0))

        # Verifying spinner label
        self._lbl_checking = ctk.CTkLabel(
            f, text="Verifying…",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )

        # Result card
        self._result_card = ctk.CTkFrame(
            f, corner_radius=8, border_width=1,
            fg_color=SUCCESS_BG, border_color=SUCCESS_BORDER,
        )
        _rc_inner = ctk.CTkFrame(self._result_card, fg_color="transparent")
        _rc_inner.pack(padx=14, pady=12, fill="x")
        self._lbl_result_main = ctk.CTkLabel(
            _rc_inner, text="",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=GREEN, anchor="w",
        )
        self._lbl_result_main.pack(anchor="w")
        self._lbl_result_detail = ctk.CTkLabel(
            _rc_inner, text="",
            font=ctk.CTkFont(size=11), text_color=TEXT_SOFT, anchor="w",
        )
        self._lbl_result_detail.pack(anchor="w", pady=(3, 0))

        # Advanced override
        self._adv_visible = False
        self._adv_toggle_btn = ctk.CTkButton(
            f, text="▶  Advanced (connection server)",
            anchor="w", height=26, width=240,
            fg_color="transparent", hover_color=LINE_STRONG,
            text_color=MUTED, font=ctk.CTkFont(size=11),
            command=self._toggle_adv,
        )
        self._adv_toggle_btn.pack(anchor="w", pady=(6, 0))

        self._adv_frame = ctk.CTkFrame(
            f, fg_color=BASE, corner_radius=6,
            border_width=1, border_color=LINE,
        )
        adv_inner = ctk.CTkFrame(self._adv_frame, fg_color="transparent")
        adv_inner.pack(padx=12, pady=10, fill="x")
        ctk.CTkLabel(
            adv_inner,
            text="Override the gateway WebSocket URL. Leave blank to use the default.",
            font=ctk.CTkFont(size=11), text_color=MUTED,
            wraplength=600, justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._var_url = tk.StringVar()
        ctk.CTkEntry(
            adv_inner, textvariable=self._var_url,
            font=ctk.CTkFont(family="Consolas", size=11), height=32,
            placeholder_text=_DEFAULT_GW_WS_URL,
        ).pack(fill="x")

        self._banner = ActionBanner(f)
        self._banner.pack(fill="x", pady=(8, 0))
        self._banner.hide()

        # Pairs info panel (shown after verify — informational only)
        self._pairs_frame = ctk.CTkFrame(f, fg_color="transparent")

    def on_enter(self, cfg: dict, data: dict) -> None:
        gw = cfg.get("gateway", {})
        self._gateway_ws_url = str(gw.get("ws_url") or _DEFAULT_GW_WS_URL)
        self._var_url.set(self._gateway_ws_url if self._gateway_ws_url != _DEFAULT_GW_WS_URL else "")
        key = str(gw.get("activation_key", ""))
        self._var_key.set(key)
        cached = data.get("_preflight")
        if cached and data.get("_preflight_key") == key and key:
            self._preflight = cached
            self._show_verified(cached)
        else:
            self._set_state("idle")

    def _toggle_adv(self) -> None:
        self._adv_visible = not self._adv_visible
        if self._adv_visible:
            self._adv_toggle_btn.configure(text="▼  Advanced (connection server)")
            self._adv_frame.pack(fill="x", pady=(4, 0))
        else:
            self._adv_toggle_btn.configure(text="▶  Advanced (connection server)")
            self._adv_frame.pack_forget()

    def _verify(self) -> None:
        key = self._var_key.get().strip()
        if not key:
            self._banner.show("Enter your license key first.", "warn")
            return
        if len(key) < 16:
            self._banner.show("Key looks too short — check you copied it correctly.", "warn")
            return
        self._banner.hide()
        self._set_state("checking")

        url_override = self._var_url.get().strip()
        ws_url = url_override if url_override else self._gateway_ws_url

        def _do() -> None:
            try:
                result = _http_preflight(ws_url, key)
                self.after(0, lambda: self._on_preflight_ok(result))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._on_preflight_err(e))

        threading.Thread(target=_do, daemon=True).start()

    def _on_preflight_ok(self, result: dict) -> None:
        if not result.get("valid"):
            status = result.get("status", "")
            if status == "suspended":
                msg = "This license has been suspended. Contact support to reactivate."
            elif status == "expired":
                msg = "This license has expired. Renew it from the web dashboard."
            else:
                msg = "Key not recognised — check you copied it correctly and try again."
            self._set_state("error")
            self._banner.show(msg, "danger")
            return
        self._preflight = result
        self._show_verified(result)

    def _on_preflight_err(self, error: str) -> None:
        self._set_state("error")
        err_lower = error.lower()
        if "403" in error or "forbidden" in err_lower:
            msg = (
                "Gateway refused the request (403).\n"
                "If you're testing locally, expand Advanced below and set the "
                "connection server to ws://localhost:4000/engine."
            )
        elif "404" in error or "not found" in err_lower:
            msg = (
                "Gateway endpoint not found (404).\n"
                "Check the connection server URL under Advanced below."
            )
        elif "timed out" in err_lower or "timeout" in err_lower:
            msg = "Connection timed out — check your internet connection and try again."
        elif "rate limit" in err_lower or "429" in error:
            msg = (
                "Rate limit exceeded — too many verification attempts. "
                "Wait a few minutes then try again."
            )
        else:
            msg = (
                f"Could not reach the gateway: {error}\n"
                "If testing locally, expand Advanced below and set the gateway URL."
            )
        self._banner.show(msg, "danger")

    def _show_verified(self, result: dict) -> None:
        avail = result.get("available_devices", 1)
        if avail == 0:
            card_bg, card_border = WARNING_BG, WARNING_BORDER
            main_color = YELLOW
            main_text  = "⚠  Agent slot limit reached"
        else:
            card_bg, card_border = SUCCESS_BG, SUCCESS_BORDER
            main_color = GREEN
            main_text  = "✓  License active"

        self._result_card.configure(fg_color=card_bg, border_color=card_border)
        self._lbl_result_main.configure(text=main_text, text_color=main_color)

        parts: list[str] = []
        if result.get("expires_at"):
            try:
                from datetime import datetime
                raw = result["expires_at"].replace("Z", "+00:00")
                dt  = datetime.fromisoformat(raw)
                parts.append(f"Expires {dt.strftime('%d %b %Y')}")
            except Exception:
                pass
        used  = result.get("used_devices", 0)
        max_d = result.get("max_devices", 0)
        if max_d:
            parts.append(f"{used} of {max_d} agent slot{'s' if max_d != 1 else ''} used")
        self._lbl_result_detail.configure(
            text="  ·  ".join(parts) if parts else "",
        )

        symbols = result.get("symbols") or ["XAUUSD"]
        self._set_state("verified")
        self._build_pairs_chips(symbols)

    def _build_pairs_chips(self, symbols: list[str]) -> None:
        """Show available pairs as informational chips (not selectable here — chosen per-agent)."""
        for w in self._pairs_frame.winfo_children():
            w.destroy()

        ctk.CTkLabel(
            self._pairs_frame,
            text="PAIRS AVAILABLE ON THIS LICENSE",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(16, 4))

        pairs_row = ctk.CTkFrame(self._pairs_frame, fg_color="transparent")
        pairs_row.pack(fill="x")
        for sym in symbols:
            chip = ctk.CTkFrame(
                pairs_row, fg_color=INFO_BG, corner_radius=4,
                border_width=1, border_color=INFO_BORDER,
            )
            chip.pack(side="left", padx=(0, 4), pady=2)
            ctk.CTkLabel(
                chip, text=sym,
                font=ctk.CTkFont(size=11, weight="bold"), text_color=INFO,
            ).pack(padx=8, pady=3)

        ctk.CTkLabel(
            self._pairs_frame,
            text="You will choose which pairs each agent trades when adding agents.",
            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(6, 0))

        self._pairs_frame.pack(fill="x")

    def _set_state(self, state: str) -> None:
        self._state = state
        self._lbl_checking.pack_forget()
        self._result_card.pack_forget()
        self._pairs_frame.pack_forget()

        if state == "idle":
            self._key_entry.configure(state="normal")
            self._btn_verify.configure(state="normal", text="Verify Key")

        elif state == "checking":
            self._lbl_checking.pack(anchor="w", pady=(8, 0))
            self._key_entry.configure(state="disabled")
            self._btn_verify.configure(state="disabled", text="Checking…")

        elif state == "verified":
            self._result_card.pack(fill="x", pady=(8, 0))
            self._key_entry.configure(state="normal")
            self._btn_verify.configure(state="normal", text="Re-verify")

        elif state == "error":
            self._key_entry.configure(state="normal")
            self._btn_verify.configure(state="normal", text="Verify Key")

    def validate_and_save(self, config: "ConfigManager", data: dict) -> tuple:
        if self._state != "verified":
            self._banner.show(
                "Click Verify Key to check your license before continuing.", "warn",
            )
            return False, ""

        key = self._var_key.get().strip()
        url_override = self._var_url.get().strip()

        # Save activation key to config.yaml so the Manager migration can read it
        # on first start and store it in DPAPI. Also persist available symbols
        # as a fallback for AddAgentPage when the Manager isn't running yet.
        effective_ws_url = url_override if url_override else self._gateway_ws_url
        updates: dict = {"activation_key": key, "ws_url": effective_ws_url}
        if self._preflight:
            updates["symbols"] = self._preflight.get("symbols") or ["XAUUSD"]

        err = config.update("gateway", updates)
        if err:
            self._banner.show(err, "danger")
            return False, err

        data["_preflight"]     = self._preflight
        data["_preflight_key"] = key
        self._banner.hide()
        return True, ""


# ── Step 3 — Install Manager ──────────────────────────────────────────────────

class _StepInstallManager(_WizardStep):
    title = "Install AQ Manager"

    def __init__(
        self,
        parent: tk.Widget,
        wizard: OnboardingWizard,
        installer: "InstallerService",
    ) -> None:
        self._installer  = installer
        self._poll_job: str | None = None
        super().__init__(parent, wizard)

    def _build(self) -> None:
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="x", padx=40, pady=(20, 0))

        ctk.CTkLabel(
            f,
            text="AQ Manager runs as a Windows background task and orchestrates "
                 "all your MT5 accounts. It starts automatically 20 seconds after "
                 "you log in, even before this control panel opens.",
            font=ctk.CTkFont(size=13), text_color=TEXT_SOFT,
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, 16))

        status_card = SectionCard(f)
        status_card.pack(fill="x", pady=(0, 14))

        self._status_lbl = ctk.CTkLabel(
            status_card.body, text="Checking…",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=MUTED,
        )
        self._status_lbl.pack(anchor="w")

        self._status_detail = ctk.CTkLabel(
            status_card.body, text="",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            justify="left",
        )
        self._status_detail.pack(anchor="w", pady=(4, 0))

        self._btn_install = PrimaryButton(
            f, text="Install & Start Manager", tone="good", width=240,
            command=self._install,
        )
        self._btn_install.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(
            f,
            text="Administrator permission is required. "
                 "A Windows security prompt will appear — click Yes to continue.",
            font=ctk.CTkFont(size=11), text_color=MUTED,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self._banner = ActionBanner(f)
        self._banner.pack(fill="x", pady=(10, 0))
        self._banner.hide()

    def on_enter(self, cfg: dict, data: dict) -> None:
        self._check_manager()

    def _check_manager(self) -> None:
        if self._poll_job:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None

        if _TOKEN_PATH.exists():
            self._status_lbl.configure(text="✓  AQ Manager is running", text_color=GREEN)
            self._status_detail.configure(
                text="Manager is active and ready. You can proceed to Finish."
            )
            self._btn_install.configure(text="Reinstall", state="normal")
        else:
            self._status_lbl.configure(text="AQ Manager is not installed yet", text_color=YELLOW)
            self._status_detail.configure(
                text="Click Install & Start Manager below to register "
                     "the Manager as a scheduled task."
            )
            self._btn_install.configure(text="Install & Start Manager", state="normal")

    def _install(self) -> None:
        self._btn_install.configure(state="disabled", text="Installing…")
        self._banner.hide()

        def _on_result(ok: bool, msg: str) -> None:
            def _apply():
                if ok:
                    self._banner.show(msg, "good")
                    self._btn_install.configure(state="disabled", text="Waiting…")
                    self._poll_for_manager(deadline=time.time() + 60)
                else:
                    self._banner.show(msg, "danger")
                    self._btn_install.configure(state="normal", text="Try Again")
            self.after(0, _apply)

        self._installer.on_result = _on_result
        self._installer.install_manager_async()

    def _poll_for_manager(self, deadline: float) -> None:
        """Poll api_token.txt every 2 s until manager is online or deadline passes."""
        if _TOKEN_PATH.exists():
            self._status_lbl.configure(text="✓  AQ Manager is running", text_color=GREEN)
            self._status_detail.configure(text="Manager started and ready.")
            self._btn_install.configure(text="Reinstall", state="normal")
            return

        remaining = int(deadline - time.time())
        if remaining <= 0:
            self._status_detail.configure(
                text="Manager did not start within 60 s. "
                     "Try again or check Windows Task Scheduler."
            )
            self._btn_install.configure(text="Retry", state="normal")
            return

        self._status_lbl.configure(
            text=f"Waiting for Manager to start… ({remaining}s)",
            text_color=MUTED,
        )
        self._poll_job = self.after(2000, lambda: self._poll_for_manager(deadline))

    def validate_and_save(self, config: "ConfigManager", data: dict) -> tuple:
        if not _TOKEN_PATH.exists():
            self._banner.show(
                "The AQ Manager has not started yet. "
                "Click Install & Start Manager and wait for confirmation.",
                "warn",
            )
            return False, ""
        return True, ""


# ── Step 4 — Finish ───────────────────────────────────────────────────────────

class _StepFinish(_WizardStep):
    title = "Setup Complete"

    def _build(self) -> None:
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="x", padx=40, pady=(20, 0))

    def on_enter(self, cfg: dict, data: dict) -> None:
        for w in self._content.winfo_children():
            w.destroy()

        ctk.CTkLabel(
            self._content, text="✓  Setup complete",
            font=ctk.CTkFont(size=22, weight="bold"), text_color=GREEN,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            self._content,
            text="AQ Manager is running. Click Finish to open the Agents dashboard "
                 "where you can add and manage your MT5 accounts.",
            font=ctk.CTkFont(size=13), text_color=TEXT_SOFT,
            justify="left", wraplength=680,
        ).pack(anchor="w", pady=(0, 20))

        gw = cfg.get("gateway", {})

        items = [
            ("License key",     "Verified  ✓" if gw.get("activation_key") else "Not set"),
            ("AQ Manager",      "Running  ✓"  if _TOKEN_PATH.exists() else "Not started"),
            ("Available pairs",  ", ".join(gw.get("symbols", [])) or "—"),
        ]
        card = SectionCard(self._content)
        card.pack(fill="x", pady=(0, 12))
        for label, value in items:
            row = ctk.CTkFrame(card.body, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                row, text=label, width=180, anchor="w",
                font=ctk.CTkFont(size=12), text_color=MUTED,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=value, anchor="w",
                font=ctk.CTkFont(size=12), text_color=TEXT_SOFT,
            ).pack(side="left")

        ctk.CTkLabel(
            self._content,
            text="Next: click '+ Add Agent' in the Agents page to provision "
                 "your first MT5 account.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
            justify="left", wraplength=660,
        ).pack(anchor="w", pady=(8, 0))


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _http_preflight(ws_url: str, activation_key: str) -> dict:
    """
    POST <gateway_http_base>/activation/preflight and return the JSON body.
    Derives the HTTP base URL from the WebSocket URL stored in config.
    Raises RuntimeError with a human-readable message on any failure.
    """
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    url = ws_url.strip()
    if url.startswith("wss://"):
        url = "https://" + url[6:]
    elif url.startswith("ws://"):
        url = "http://" + url[5:]
    parsed    = urlparse(url)
    http_base = f"{parsed.scheme}://{parsed.netloc}"

    payload = json.dumps({"activation_key": activation_key}).encode()
    req = urllib.request.Request(
        f"{http_base}/activation/preflight",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "AQAgent/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            msg  = body.get("message", str(exc))
        except Exception:
            msg = str(exc)
        raise RuntimeError(msg) from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
