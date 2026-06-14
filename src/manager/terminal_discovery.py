"""
manager/terminal_discovery.py — Scan for installed MT5 terminals and classify their state.

All terminals are always returned — never hidden — with state labels:
  available          | managed_running | managed_stopped | running_unmanaged
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from src.manager.models import AgentStatus, TerminalInfo
from src.manager.registry import AgentRegistry

logger = logging.getLogger(__name__)

_COMMON_PATHS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
]
_MT5_EXE = "terminal64.exe"
_APPDATA_MT5 = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"


class TerminalDiscovery:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    def scan(self) -> list[TerminalInfo]:
        found: dict[str, str] = {}   # path → display name

        # 1. Windows registry scan
        for path, name in _scan_registry():
            found[path] = name

        # 2. Common install paths
        for base in _COMMON_PATHS:
            for path, name in _scan_directory(base):
                found.setdefault(path, name)

        # 3. Roaming AppData portable installs
        if _APPDATA_MT5.exists():
            for path, name in _scan_directory(str(_APPDATA_MT5)):
                found.setdefault(path, name)

        leases   = {l.terminal_path: l for l in self._registry.list_terminal_leases()}
        agents   = {a.agent_id: a for a in self._registry.list_agents()}
        running  = _get_running_terminal_pids()

        results: list[TerminalInfo] = []
        for exe_path, display_name in found.items():
            lease = leases.get(exe_path)
            if lease:
                agent = agents.get(lease.agent_id)
                if agent and agent.status == AgentStatus.RUNNING:
                    state = "managed_running"
                else:
                    state = "managed_stopped"
                results.append(TerminalInfo(
                    path=exe_path,
                    name=display_name,
                    state=state,
                    leased_to=lease.agent_id,
                ))
            else:
                # Check if the exe's directory has a running process
                exe_lower = exe_path.lower()
                is_running = any(exe_lower in (p or "").lower() for p in running)
                state = "running_unmanaged" if is_running else "available"
                results.append(TerminalInfo(
                    path=exe_path,
                    name=display_name,
                    state=state,
                    leased_to=None,
                ))

        return sorted(results, key=lambda t: t.path)


# ── Registry scan ──────────────────────────────────────────────────────────────

def _scan_registry() -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    try:
        import winreg
        hives = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\MetaQuotes Software\Terminal"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\MetaQuotes Software\Terminal"),
        ]
        for hive, key_path in hives:
            try:
                with winreg.OpenKey(hive, key_path) as root:
                    i = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(root, i)
                            i += 1
                            with winreg.OpenKey(root, sub_name) as sub:
                                try:
                                    install_dir, _ = winreg.QueryValueEx(sub, "InstallPath")
                                    exe = str(Path(install_dir) / _MT5_EXE)
                                    if Path(exe).exists():
                                        results.append((exe, Path(install_dir).name))
                                except FileNotFoundError:
                                    pass
                        except OSError:
                            break
            except OSError:
                pass
    except ImportError:
        pass   # non-Windows
    return results


def _scan_directory(base: str) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    try:
        base_path = Path(base)
        if not base_path.exists():
            return []
        for child in base_path.iterdir():
            if not child.is_dir():
                continue
            name_lower = child.name.lower()
            if "metatrader" in name_lower or "mt5" in name_lower or "mt4" in name_lower:
                exe = child / _MT5_EXE
                if exe.exists():
                    results.append((str(exe), child.name))
    except PermissionError:
        pass
    return results


def _get_running_terminal_pids() -> list[str]:
    """Return list of full exe paths of running terminal64.exe processes."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l.strip().strip('"') for l in result.stdout.splitlines() if l.strip()]
        return lines
    except Exception:
        return []
