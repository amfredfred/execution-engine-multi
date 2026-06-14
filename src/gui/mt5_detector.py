"""
src/gui/mt5_detector.py

Scans common Windows locations for MetaTrader 4/5 installations.
Returns readable broker names instead of raw executable paths.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MT5Install:
    id: str           # unique slug, e.g. "fbs-mt5"
    name: str         # display name, e.g. "FBS MetaTrader 5"
    broker: str       # e.g. "FBS"
    platform: str     # "mt5" | "mt4"
    exe_path: str     # full path to terminal64.exe or terminal.exe
    is_available: bool = True


def _parse_folder(folder_name: str) -> tuple[str, str]:
    """
    Given a folder name like 'FBS MetaTrader 5', return (broker, platform).
    Strips MetaTrader/MT4/MT5 tokens; what remains is the broker name.
    """
    name = folder_name.strip()
    platform = "mt5"

    mt5_patterns = [r"\bMetaTrader\s*5\b", r"\bMT5\b", r"\bMeta\s*Trader\s*5\b"]
    mt4_patterns = [r"\bMetaTrader\s*4\b", r"\bMT4\b"]

    for pat in mt5_patterns:
        if re.search(pat, name, re.IGNORECASE):
            platform = "mt5"
            name = re.sub(pat, "", name, flags=re.IGNORECASE).strip(" -_")
            break
    else:
        for pat in mt4_patterns:
            if re.search(pat, name, re.IGNORECASE):
                platform = "mt4"
                name = re.sub(pat, "", name, flags=re.IGNORECASE).strip(" -_")
                break

    broker = name if name else "MetaQuotes"
    return broker, platform


def _make_slug(broker: str, platform: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{broker}-{platform}".lower()).strip("-")


def detect_installs() -> list[MT5Install]:
    """
    Scan common Windows install locations for MT4/MT5 terminals.
    Returns a list of MT5Install objects sorted by display name.
    """
    search_roots: list[Path] = []

    # Standard Program Files locations
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        p = os.environ.get(env_var)
        if p:
            search_roots.append(Path(p))

    # Per-user install locations
    for env_var in ("LOCALAPPDATA", "APPDATA"):
        p = os.environ.get(env_var)
        if p:
            search_roots.append(Path(p) / "Programs")
            search_roots.append(Path(p))

    seen_paths: set[str] = set()
    results: list[MT5Install] = []

    for root in search_roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except (PermissionError, OSError):
            continue

        for entry in entries:
            if not entry.is_dir():
                continue
            folder_name = entry.name
            # Fast filter: only folders mentioning MetaTrader/MT4/MT5
            if not re.search(r"(metatrader|mt[45])", folder_name, re.IGNORECASE):
                continue

            # Prefer terminal64.exe (64-bit MT5/MT4) over terminal.exe (32-bit)
            for exe_name in ("terminal64.exe", "terminal.exe"):
                exe_path = entry / exe_name
                if not exe_path.exists():
                    continue
                key = str(exe_path).lower()
                if key in seen_paths:
                    break
                seen_paths.add(key)

                broker, platform = _parse_folder(folder_name)
                base_slug = _make_slug(broker, platform)
                slug = base_slug
                n = 2
                while any(r.id == slug for r in results):
                    slug = f"{base_slug}-{n}"
                    n += 1

                platform_label = "MetaTrader 5" if platform == "mt5" else "MetaTrader 4"
                display_name = (
                    f"{broker} {platform_label}"
                    if broker not in ("MetaQuotes", "")
                    else platform_label
                )

                results.append(MT5Install(
                    id=slug,
                    name=display_name,
                    broker=broker,
                    platform=platform,
                    exe_path=str(exe_path),
                    is_available=True,
                ))
                break  # found exe for this folder

    results.sort(key=lambda x: x.name.lower())
    return results
