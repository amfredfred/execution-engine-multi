"""
src/gui/app.py — Apex Quantel Engine Manager (production build)

Flow
────
  1. Window opens
  2. ConfigManager checks if setup is complete
  3a. Incomplete → OnboardingWizard shown full-screen
  3b. Complete   → Main dashboard shown (sidebar + pages)
  4. Service poll + WS client run in background threads
  5. AppState is the single source of truth; pages subscribe to events

Nav pages (in order)
────────────────────
  Home      — Readiness checklist + engine status + account summary
  Engine    — Start / Stop / Reinstall
  Platform  — MT5 terminal selection + credentials
  Risk      — Risk profile settings
  Activity  — Live events log
  Settings  — Startup options, open folders, re-run setup
  Advanced  — Technical / developer settings
"""
from __future__ import annotations

import queue as _queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import customtkinter as ctk
import yaml

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

from src.gui.theme import (  # noqa: E402
    GREEN, RED, YELLOW, MUTED,
    TEXT, BASE, SURFACE, SURFACE_RAISED,
    LINE, LINE_STRONG,
    NAV_HOVER, NAV_ACTIVE_BG,
)
from src.gui.config_manager  import ConfigManager
from src.gui.state           import AppState, EngineLifecycle
from src.gui.installer       import InstallerService
from src.gui.components      import EngineStatusBadge
from src.gui.assets          import load_logo_image, set_window_icon
from src.gui.manager_state   import ManagerAppState
from src.gui.manager_client  import ManagerClient


_NAV_PAGES = ["Agents", "Manager", "Home", "Engine", "Platform", "Risk", "Activity", "Settings", "Advanced"]
_NAV_ICONS = {
    "Agents":    "⬡",
    "Manager":   "⚙",
    "Home":      "⬡",
    "Engine":    "⚡",
    "Platform":  "⬡",
    "Risk":      "⚖",
    "Activity":  "📋",
    "Settings":  "⚙",
    "Advanced":  "🔧",
}
# Display labels shown in the sidebar (internal page keys stay unchanged)
_NAV_LABELS = {
    "Engine":  "AQ Agent",
    "Manager": "AQ Manager",
}

# Pages only shown when viewing a specific agent (not in fleet mode)
_AGENT_ONLY_PAGES = {"Home", "Engine", "Platform", "Risk", "Activity", "Settings", "Advanced"}


# ── Main application window ───────────────────────────────────────────────────

class ApexTraderGUI(ctk.CTk):
    """
    Root window.  Owns AppState, ConfigManager, InstallerService, and all pages.

    Pages access shared services through:
        self.app.app_state       — AppState (pub/sub, engine lifecycle, account)
        self.app.config      — ConfigManager (load, save, validate)
        self.app.installer   — InstallerService (install/uninstall)
        self.app.svc         — ServiceController (start/stop/restart)
        self.app.ws          — WSClient (send WebSocket commands)
        self.app.navigate(p) — switch to page
    """

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__()

        # Foundation services
        self.config     = ConfigManager(config_path)
        self.app_state  = AppState()
        self.installer  = InstallerService()

        self._msg_queue: _queue.Queue[dict] = _queue.Queue()
        self._fleet_mode: bool = True   # True = showing Agents fleet, False = drilling into an agent

        from src.gui.service_controller import ServiceController
        from src.gui.ws_client import WSClient

        self.svc = ServiceController()
        self.svc.on_status_change = self._on_svc_status_change

        port = self.config.get("engine", "monitoring_port") or 8080
        self.ws = WSClient(
            url=f"ws://localhost:{port}",
            on_message=lambda msg: self._msg_queue.put(msg),
            on_connect=lambda: self._msg_queue.put({"type": "_ws_connected"}),
            on_disconnect=lambda: self._msg_queue.put({"type": "_ws_disconnected"}),
        )

        # Manager fleet state + HTTP poller
        self._manager_online: bool = False
        self._last_manager_contact: float = 0.0
        self._manager_license_sync_attempted: bool = False
        self.manager_state  = ManagerAppState()
        self.manager_client = ManagerClient(
            on_agents=lambda data: self._msg_queue.put({"type": "_manager_agents", "payload": data}),
            on_error =lambda err:  self._msg_queue.put({"type": "_manager_offline"}),
        )
        self.manager_client.start()
        self.after(6000, self._check_manager_heartbeat)

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(1000, self._tick_heartbeat)

        # Set window icon (title-bar + taskbar)
        set_window_icon(self)

        self.ws.start()
        # Kick off the first service-status check quickly — users shouldn't
        # sit on "Checking…" longer than necessary.
        self.after(80, self._refresh_service_status)

        # Safety: if service query hasn't resolved CHECKING in 6 s, force UNKNOWN
        self.after(6000, self._checking_timeout)

        self.lift()
        self.focus_force()

    # ── Window construction ───────────────────────────────────────────────────

    def _build_layout(self) -> None:
        self.title("AQ Agent")

        # Size — cap at screen dimensions so the window is never larger than
        # the monitor, then center it explicitly so it's never off-screen.
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w = min(1200, sw - 80)
        win_h = min(760,  sh - 80)
        x = max(0, (sw - win_w) // 2)
        y = max(0, (sh - win_h) // 2)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(min(900, sw), min(580, sh))

        # Root is a single frame that swaps between onboarding and main
        self._root_frame = ctk.CTkFrame(self, fg_color=BASE, corner_radius=0)
        self._root_frame.pack(fill="both", expand=True)

        if self._is_manager_setup_complete():
            self.app_state.mark_setup_complete(True)
            self._show_main_ui()
        else:
            self._show_onboarding()

    # ── Setup check ───────────────────────────────────────────────────────────

    def _is_manager_setup_complete(self) -> bool:
        """Setup is complete when the Manager has run at least once (api_token.txt exists)."""
        from src.gui.manager_client import _TOKEN_PATH
        return _TOKEN_PATH.exists()

    # ── Onboarding ────────────────────────────────────────────────────────────

    def _show_onboarding(self) -> None:
        for w in self._root_frame.winfo_children():
            w.destroy()
        from src.gui.onboarding import OnboardingWizard
        wiz = OnboardingWizard(
            self._root_frame,
            config=self.config,
            installer=self.installer,
            on_complete=self._on_onboarding_complete,
        )
        wiz.pack(fill="both", expand=True)
        wiz.start()

    def _on_onboarding_complete(self) -> None:
        self.app_state.mark_setup_complete(True)
        self._show_main_ui()
        # Start manager polling now that setup is done
        if not self.manager_client._thread or not self.manager_client._thread.is_alive():
            self.manager_client.start()

    # ── Main dashboard ────────────────────────────────────────────────────────

    def _show_main_ui(self) -> None:
        for w in self._root_frame.winfo_children():
            w.destroy()

        self._root_frame.grid_rowconfigure(0, weight=1)
        self._root_frame.grid_columnconfigure(0, weight=0)
        self._root_frame.grid_columnconfigure(1, weight=1)

        # Sidebar
        self._sidebar = ctk.CTkFrame(
            self._root_frame, width=200, corner_radius=0, fg_color=BASE,
        )
        self._sidebar.grid(row=0, column=0, sticky="nsew")
        self._sidebar.grid_propagate(False)

        # Content area
        self._content = ctk.CTkFrame(
            self._root_frame, corner_radius=0, fg_color=SURFACE,
        )
        self._content.grid(row=0, column=1, sticky="nsew")
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

        self._build_sidebar()
        self._build_pages()
        self._show_page("Agents")

    def _build_sidebar(self) -> None:
        # Logo
        logo = ctk.CTkFrame(self._sidebar, fg_color="transparent", height=66)
        logo.pack(fill="x")
        logo.pack_propagate(False)

        logo_img = load_logo_image(size=(28, 28))
        if logo_img:
            ctk.CTkLabel(
                logo, image=logo_img, text="  Apex Quantel",
                compound="left",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=GREEN,
            ).place(relx=0.5, rely=0.62, anchor="center")
        else:
            ctk.CTkLabel(
                logo,
                text="⚡  Apex Quantel",
                font=ctk.CTkFont(size=15, weight="bold"),
                text_color=GREEN,
            ).place(relx=0.5, rely=0.62, anchor="center")

        version = _read_version()
        ctk.CTkLabel(
            self._sidebar, text=f"v{version}",
            font=ctk.CTkFont(size=10), text_color=MUTED,
        ).pack(pady=(0, 4))

        _divider(self._sidebar)

        # "← Fleet" back button (only shown when viewing an agent)
        self._fleet_btn = ctk.CTkButton(
            self._sidebar,
            text="← Fleet",
            anchor="w",
            height=34, corner_radius=6,
            fg_color="transparent",
            hover_color=NAV_HOVER,
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            command=self._show_fleet,
        )
        # Not packed yet — shown only in agent mode

        # Nav buttons
        self._nav_btns: dict[str, ctk.CTkButton] = {}
        for page in _NAV_PAGES:
            icon  = _NAV_ICONS.get(page, "")
            label = _NAV_LABELS.get(page, page)
            btn = ctk.CTkButton(
                self._sidebar,
                text=f"  {icon}  {label}",
                anchor="w",
                height=38, corner_radius=6,
                fg_color="transparent",
                hover_color=NAV_HOVER,
                text_color=MUTED,
                font=ctk.CTkFont(size=13),
                command=lambda p=page: self._show_page(p),
            )
            btn.pack(fill="x", padx=8, pady=2)
            self._nav_btns[page] = btn
            # Agent-only pages start hidden in fleet mode
            if page in _AGENT_ONLY_PAGES:
                btn.pack_forget()

        # Spacer
        ctk.CTkFrame(self._sidebar, fg_color="transparent").pack(
            fill="both", expand=True,
        )

        # Manager restart button — shown when manager is offline
        from src.gui.theme import DANGER_BG, DANGER_BORDER, RED
        self._start_mgr_btn = ctk.CTkButton(
            self._sidebar,
            text="▶  Start Manager",
            height=30,
            fg_color=DANGER_BG, hover_color=DANGER_BORDER, text_color=RED,
            border_width=1, border_color=DANGER_BORDER, corner_radius=4,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_sidebar_start_manager,
        )
        # Hidden until manager goes offline
        self._start_mgr_btn.pack_forget()

        # Engine status badge at bottom
        _divider(self._sidebar)
        self._engine_badge = EngineStatusBadge(self._sidebar)
        self._engine_badge.pack(pady=(6, 14), padx=12, fill="x")
        # Subscribe to engine lifecycle changes
        self.app_state.subscribe("engine", self._on_lifecycle_changed)

    def _build_pages(self) -> None:
        from src.gui.pages.home     import HomePage
        from src.gui.pages.engine   import EnginePage
        from src.gui.pages.platform import PlatformPage
        from src.gui.pages.risk     import RiskPage
        from src.gui.pages.activity import ActivityPage
        from src.gui.pages.settings import SettingsPage
        from src.gui.pages.advanced import AdvancedPage
        from src.gui.pages.agents   import AgentsPage, AddAgentPage
        from src.gui.pages.manager  import ManagerPage

        self._pages: dict[str, ctk.CTkFrame] = {
            "Agents":   AgentsPage(self._content, self),
            "Manager":  ManagerPage(self._content, self),
            "AddAgent": AddAgentPage(self._content, self),
            "Home":     HomePage(self._content, self),
            "Engine":   EnginePage(self._content, self),
            "Platform": PlatformPage(self._content, self),
            "Risk":     RiskPage(self._content, self),
            "Activity": ActivityPage(self._content, self),
            "Settings": SettingsPage(self._content, self),
            "Advanced": AdvancedPage(self._content, self),
        }

    # ── Navigation ────────────────────────────────────────────────────────────

    def _show_page(self, name: str) -> None:
        if not hasattr(self, "_pages"):
            return
        for pname, page in self._pages.items():
            if pname == name:
                page.grid(row=0, column=0, sticky="nsew")
                cb = getattr(page, "on_navigate_to", None)
                if callable(cb):
                    cb()
            else:
                page.grid_remove()

        for bname, btn in self._nav_btns.items():
            active = bname == name
            btn.configure(
                fg_color=NAV_ACTIVE_BG if active else "transparent",
                text_color=GREEN if active else MUTED,
                border_width=1 if active else 0,
                border_color=LINE_STRONG if active else BASE,
            )

    def navigate(self, page: str) -> None:
        """Called by pages and onboarding to switch pages."""
        if page == "__setup__":
            self._show_onboarding()
        elif page == "__dashboard__":
            import webbrowser
            webbrowser.open(self.config.dashboard_url())
        else:
            self._show_page(page)

    # ── Agent drill-in / fleet switch ─────────────────────────────────────────

    def select_agent(self, agent_id: str, monitoring_port: int) -> None:
        """Switch the GUI into a single agent's management panel."""
        from src.gui.ws_client import WSClient

        self._fleet_mode = False
        self.manager_state.select_agent(agent_id, monitoring_port)

        # Reconnect WSClient to the agent's UIBridge port
        self.ws.stop()
        self.ws = WSClient(
            url=f"ws://localhost:{monitoring_port}",
            on_message=lambda msg: self._msg_queue.put(msg),
            on_connect=lambda: self._msg_queue.put({"type": "_ws_connected"}),
            on_disconnect=lambda: self._msg_queue.put({"type": "_ws_disconnected"}),
        )
        self.ws.start()

        # Sidebar: show "← Fleet" button, hide Agents button, show agent pages
        # Manager page stays visible in all modes.
        if hasattr(self, "_fleet_btn"):
            self._fleet_btn.pack(fill="x", padx=8, pady=(0, 4))
        for page, btn in self._nav_btns.items():
            if page == "Agents":
                btn.pack_forget()
            else:
                btn.pack(fill="x", padx=8, pady=2)

        self._show_page("Home")

    def _show_fleet(self) -> None:
        """Return to the fleet view — disconnect agent WSClient."""
        self._fleet_mode = True
        self.manager_state.clear_selection()

        self.ws.stop()

        # Sidebar: hide "← Fleet" button, show Agents + Manager, hide agent-only pages
        if hasattr(self, "_fleet_btn"):
            self._fleet_btn.pack_forget()
        for page, btn in self._nav_btns.items():
            if page in ("Agents", "Manager"):
                btn.pack(fill="x", padx=8, pady=2)
            elif page in _AGENT_ONLY_PAGES:
                btn.pack_forget()

        self._show_page("Agents")

    # ── Lifecycle badge ───────────────────────────────────────────────────────

    def _on_lifecycle_changed(self, lifecycle: EngineLifecycle = None, **_) -> None:
        # In multi-agent mode the badge is owned by _apply_manager_online;
        # only update via lifecycle if manager tracking hasn't kicked in yet.
        if lifecycle and hasattr(self, "_engine_badge") and not self._manager_online:
            self._engine_badge.update(lifecycle)

    # ── Manager reachability ──────────────────────────────────────────────────

    def _apply_manager_online(self, online: bool) -> None:
        if hasattr(self, "_engine_badge"):
            self._engine_badge.set_manager_status(online)
        if hasattr(self, "_start_mgr_btn"):
            if online:
                self._start_mgr_btn.pack_forget()
            else:
                self._start_mgr_btn.pack(fill="x", padx=8, pady=(0, 4),
                                          before=self._engine_badge)
        if hasattr(self, "_pages"):
            agents_page = self._pages.get("Agents")
            if agents_page and hasattr(agents_page, "set_manager_online"):
                agents_page.set_manager_online(online)

    def _on_sidebar_start_manager(self) -> None:
        self._start_mgr_btn.configure(state="disabled", text="Starting…")
        def _done(ok: bool) -> None:
            def _apply():
                self._start_mgr_btn.configure(
                    state="normal",
                    text="▶  Start Manager" if ok else "▶  Retry",
                )
            self.after(0, _apply)
        self.restart_manager(on_done=_done)

    def _check_manager_heartbeat(self) -> None:
        import time as _time
        if self._manager_online and (_time.time() - self._last_manager_contact) > 9:
            self._manager_online = False
            self._apply_manager_online(False)
        # Also apply offline if we've never seen the manager and enough time has passed
        elif not self._manager_online and not getattr(self, "_manager_offline_shown", False):
            self._manager_offline_shown = True
            self._apply_manager_online(False)
        self.after(5000, self._check_manager_heartbeat)

    def restart_manager(self, on_done=None) -> None:
        """Try schtasks /Run first; fall back to install_manager.ps1 elevated."""
        import subprocess
        from src.gui.installer import InstallerService

        def _run():
            import time as _time
            # 1. Try running the already-registered scheduled task (non-elevated).
            result = subprocess.run(
                ["schtasks", "/Run", "/TN", r"\Apex Quantel\AQ Manager"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                # Wait up to 30 s for api_token.txt to appear.
                from src.gui.manager_client import _TOKEN_PATH
                deadline = _time.time() + 30
                while _time.time() < deadline:
                    if _TOKEN_PATH.exists():
                        if on_done:
                            self.after(0, lambda: on_done(True))
                        return
                    _time.sleep(1)

            # 2. Task not registered or timed out — run the installer PS1.
            svc = InstallerService()
            def _notify(ok: bool, msg: str) -> None:
                if on_done:
                    self.after(0, lambda: on_done(ok))
            svc.on_result = _notify
            svc.install_manager_async()

        threading.Thread(target=_run, daemon=True).start()

    # ── Service status ────────────────────────────────────────────────────────

    def _on_svc_status_change(self, status: str, detail: str | None) -> None:
        self.after(0, lambda: self._apply_svc_status(status, detail))

    def _apply_svc_status(self, status: str, detail: str | None) -> None:
        setup_complete = self.config.is_setup_complete()
        self.app_state.update_service_status(status, setup_complete, detail)
        # Legacy broadcast so old-style pages (engine.py etc.) still work
        if hasattr(self, "_pages"):
            _broadcast(self._pages, "on_engine_status", status, detail)

    def _refresh_service_status(self) -> None:
        def _check():
            try:
                status = self.svc.query()
            except Exception:
                # sc.exe unavailable or access denied — report as unknown so
                # the UI escapes "Checking…" rather than staying there forever.
                from src.gui.service_controller import ServiceStatus
                status = ServiceStatus.UNKNOWN
            self.after(0, lambda: self._apply_svc_status(status, None))
        threading.Thread(target=_check, daemon=True).start()
        self.after(5000, self._refresh_service_status)

    # ── WebSocket event dispatch ──────────────────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                self._dispatch(msg)
        except _queue.Empty:
            pass
        self.after(100, self._poll)

    def _dispatch(self, msg: dict) -> None:
        t       = msg.get("type", "")
        payload = msg.get("payload", {})

        if t == "_manager_agents":
            self.manager_state.apply(payload)
            import time as _time
            self._last_manager_contact = _time.time()
            if not self._manager_online:
                self._manager_online = True
                self._apply_manager_online(True)
                self._sync_manager_license()
            return

        if t == "_manager_offline":
            # Apply on every offline signal until manager comes online —
            # ensures the initial offline state is shown even on first load.
            if self._manager_online or not getattr(self, "_manager_offline_shown", False):
                self._manager_online = False
                self._manager_offline_shown = True
                self._apply_manager_online(False)
            return

        if t == "_ws_connected":
            self.app_state.on_ws_connected()
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_ws_connected")

        elif t == "_ws_disconnected":
            self.app_state.on_ws_disconnected()
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_ws_disconnected")

        elif t == "STATE_SNAPSHOT":
            self.app_state.on_heartbeat()
            self.app_state.apply_snapshot(payload)
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_snapshot", payload)

        elif t == "METRICS_UPDATE":
            self.app_state.on_heartbeat()
            self.app_state.apply_metrics(payload)
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_metrics", payload)

        elif t == "mt5.error":
            msg_text = payload.get("message", "MT5 connection failed")
            self.app_state.on_mt5_error(msg_text)
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_mt5_error", msg_text)

        elif t in (
            "trade.opened", "trade.tp1_hit",
            "trade.tp2_hit", "trade.sl_hit", "trade.closed",
            "trade.error",
        ):
            self.app_state.on_trade_event(t, payload)
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_trade_event", t, payload)

        elif t in (
            "signal.received", "signal.triggered",
            "risk.approved",   "risk.rejected",
        ):
            if hasattr(self, "_pages"):
                _broadcast(self._pages, "on_signal_event", t, payload)

    def _sync_manager_license(self) -> None:
        """Copy the GUI-configured key into the manager's encrypted store once."""
        if self._manager_license_sync_attempted:
            return
        self._manager_license_sync_attempted = True

        key = str(self.config.get("gateway", "activation_key") or "").strip()
        if not key:
            return

        def _on_info(info: dict) -> None:
            if info.get("configured"):
                return
            self.manager_client.set_license_key(
                key,
                lambda result: logger.info(
                    "Manager license sync %s",
                    "completed" if result.get("valid") else "failed",
                ),
            )

        self.manager_client.get_license_info(_on_info)

    def _tick_heartbeat(self) -> None:
        self.app_state.tick_heartbeat()
        self.after(1000, self._tick_heartbeat)

    def _checking_timeout(self) -> None:
        """
        Hard fallback: if the CHECKING state was never resolved by the service
        poll (e.g. sc.exe timed out), force a transition to UNKNOWN so the
        home page never stays stuck on "Checking engine status…" forever.
        """
        self.app_state.force_out_of_checking()

    # ── Legacy compatibility helpers ──────────────────────────────────────────

    def load_config(self) -> dict:
        """Backward-compat shim for old pages."""
        return self.config.load(force=True)

    def save_config(self, cfg: dict) -> None:
        """Backward-compat shim for old pages."""
        self.config.save(cfg)

    def restart_with_new_config(self) -> None:
        """Backward-compat shim for old pages that call this after saving config."""
        import threading
        threading.Thread(target=self.svc.restart, daemon=True).start()

    def send_command(self, cmd_type: str, payload: dict | None = None) -> None:
        self.ws.send(cmd_type, payload or {})

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        self.ws.stop()
        self.manager_client.stop()
        self.destroy()


# ── Module-level helpers ──────────────────────────────────────────────────────

def _broadcast(pages: dict, method: str, *args: Any) -> None:
    for page in pages.values():
        cb = getattr(page, method, None)
        if callable(cb):
            try:
                cb(*args)
            except Exception:
                pass


def _divider(parent: ctk.CTkFrame) -> None:
    ctk.CTkFrame(parent, height=1, fg_color=LINE).pack(fill="x", padx=12, pady=6)


def _read_version() -> str:
    for candidate in (
        Path("version.txt"),
        Path(sys.executable).parent / "version.txt",
        Path(__file__).resolve().parent.parent.parent / "version.txt",
    ):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return "?"


def resolve_config_path(argv: list[str]) -> str:
    """
    Locate config.yaml in priority order:
    1. Explicit CLI argument ending in .yaml
    2. %ProgramData% / Apex Quantel / config.yaml
    3. Next to the exe / walk up 3 levels
    4. sys._MEIPASS
    5. CWD
    6. Walk up from __file__
    7. Fallback "config.yaml"
    """
    for arg in argv[1:]:
        if not arg.startswith("-") and arg.endswith(".yaml"):
            return arg

    # Delegate entirely to ConfigManager which has the same logic
    from src.gui.config_manager import ConfigManager
    return str(ConfigManager().path)
