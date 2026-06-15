"""Apex Quantel manager control-plane GUI."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

import customtkinter as ctk

from src.gui.assets import load_logo_image, set_window_icon
from src.gui.config_manager import ConfigManager
from src.gui.installer import InstallerService
from src.gui.manager_client import ManagerClient, _TOKEN_PATH
from src.gui.manager_state import ManagerAppState
from src.gui.theme import BASE, GREEN, LINE, MUTED, NAV_ACTIVE_BG, NAV_HOVER, SURFACE

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class ApexTraderGUI(ctk.CTk):
    def __init__(self, config_path: str | None = None) -> None:
        super().__init__()
        self.config = ConfigManager(config_path)
        self.installer = InstallerService()
        self.manager_state = ManagerAppState()
        self._queue: queue.Queue[dict] = queue.Queue()
        self._manager_online = False
        self._last_manager_contact = 0.0
        self.manager_client = ManagerClient(
            on_agents=lambda value: self._queue.put({"type": "agents", "payload": value}),
            on_error=lambda error: self._queue.put({"type": "offline", "payload": error}),
        )
        self.manager_client.start()
        self._build_window()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        self.after(5000, self._check_manager)
        set_window_icon(self)

    def _build_window(self) -> None:
        self.title("AQ Agent")
        self.geometry("1200x760")
        self.minsize(900, 580)
        self._root = ctk.CTkFrame(self, fg_color=BASE, corner_radius=0)
        self._root.pack(fill="both", expand=True)
        if _TOKEN_PATH.exists():
            self._show_control_plane()
        else:
            self._show_onboarding()

    def _show_onboarding(self) -> None:
        self._clear_root()
        from src.gui.onboarding import OnboardingWizard

        wizard = OnboardingWizard(
            self._root,
            config=self.config,
            installer=self.installer,
            on_complete=self._show_control_plane,
        )
        wizard.pack(fill="both", expand=True)
        wizard.start()

    def _show_control_plane(self) -> None:
        self._clear_root()
        sidebar = ctk.CTkFrame(self._root, width=250, fg_color=SURFACE, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        content = ctk.CTkFrame(self._root, fg_color=BASE, corner_radius=0)
        content.pack(side="left", fill="both", expand=True)
        logo = load_logo_image((26, 26))
        ctk.CTkLabel(sidebar, text="  Apex Quantel", image=logo, compound="left").pack(
            fill="x", padx=18, pady=(24, 28),
        )
        self._logo = logo
        self._nav_buttons = {}
        self._pages = {}
        from src.gui.pages.agents import AddAgentPage, AgentsPage
        from src.gui.pages.manager import ManagerPage

        self._pages["Agents"] = AgentsPage(content, self)
        self._pages["Manager"] = ManagerPage(content, self)
        self._pages["AddAgent"] = AddAgentPage(content, self)
        for name in ("Agents", "Manager"):
            button = ctk.CTkButton(
                sidebar,
                text=name,
                anchor="w",
                fg_color="transparent",
                hover_color=NAV_HOVER,
                text_color=MUTED,
                command=lambda page=name: self.navigate(page),
            )
            button.pack(fill="x", padx=10, pady=3)
            self._nav_buttons[name] = button
        self.navigate("Agents")

    def navigate(self, page: str) -> None:
        if page == "__setup__":
            self._show_onboarding()
            return
        selected = self._pages.get(page)
        if not selected:
            return
        for name, frame in self._pages.items():
            if frame is selected:
                frame.pack(fill="both", expand=True)
                callback = getattr(frame, "on_navigate_to", None)
                if callback:
                    callback()
            else:
                frame.pack_forget()
        for name, button in self._nav_buttons.items():
            active = name == page
            button.configure(
                fg_color=NAV_ACTIVE_BG if active else "transparent",
                text_color=GREEN if active else MUTED,
            )

    def select_agent(self, engine_id: str, monitoring_port: int) -> None:
        self.manager_state.select_agent(engine_id, monitoring_port)

    def restart_manager(self, on_done=None) -> None:
        def run() -> None:
            ok, _ = self.installer.start_manager_task()
            if not ok:
                self.installer.install_manager_async()
            if on_done:
                self.after(0, lambda: on_done(ok))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                message = self._queue.get_nowait()
                if message["type"] == "agents":
                    self.manager_state.apply(message["payload"])
                    self._manager_online = True
                    self._last_manager_contact = time.time()
                    self._set_manager_online(True)
                elif message["type"] == "offline":
                    self._manager_online = False
                    self._set_manager_online(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _check_manager(self) -> None:
        if self._manager_online and time.time() - self._last_manager_contact > 9:
            self._manager_online = False
            self._set_manager_online(False)
        self.after(5000, self._check_manager)

    def _set_manager_online(self, online: bool) -> None:
        page = getattr(self, "_pages", {}).get("Agents")
        if page and hasattr(page, "set_manager_online"):
            page.set_manager_online(online)

    def _clear_root(self) -> None:
        for child in self._root.winfo_children():
            child.destroy()

    def _close(self) -> None:
        self.manager_client.stop()
        self.destroy()


def resolve_config_path(argv: list[str]) -> str | None:
    return next((arg for arg in argv[1:] if not arg.startswith("-")), None)
