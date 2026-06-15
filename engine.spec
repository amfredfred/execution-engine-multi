# engine.spec — PyInstaller build spec for the Apex Quantel execution engine.
#
# Build command (from execution-engine/ dir):
#   pyinstaller engine.spec --clean --noconfirm
#
# Or via the build pipeline:
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1 -Clean
#
# Output: dist\apex-quant-trader-agent\apex-quant-trader-agent.exe  (onedir — see below)
#
# Why --onedir, not --onefile?
#   MetaTrader5 loads the Python-MT5 bridge DLL (MetaTrader5.pyd + Python-3xx.dll)
#   from the directory it was built against at runtime.  --onefile extracts to a
#   random %TEMP% path on every launch, causing DLL resolution to fail silently.
#   --onedir keeps all binaries in a stable dist/ folder so MT5 always finds them.
#
# Default launch shows the control-plane GUI. The manager launches isolated
# engine workers with --agent.

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all  # noqa: F401

block_cipher = None

# ---------------------------------------------------------------------------
# Hidden imports — packages whose sub-modules aren't auto-discovered because
# they rely on runtime __import__, C-extension entry points, or lazy loading.
# ---------------------------------------------------------------------------
hidden_imports = [
    # ── MetaTrader5 ──────────────────────────────────────────────────────────
    "MetaTrader5",

    # ── websocket-client ────────────────────────────────────────────────────
    "websocket",
    "websocket._app",
    "websocket._abnf",
    "websocket._core",
    "websocket._exceptions",
    "websocket._handshake",
    "websocket._http",
    "websocket._logging",
    "websocket._socket",
    "websocket._ssl_compat",
    "websocket._utils",

    # ── websockets (async, UIBridge server) ─────────────────────────────────
    "websockets",
    "websockets.legacy",
    "websockets.legacy.client",
    "websockets.legacy.server",

    # ── PyYAML ───────────────────────────────────────────────────────────────
    "yaml",
    "_yaml",

    # ── python-dotenv ────────────────────────────────────────────────────────
    "dotenv",

    # ── zoneinfo + tzdata ────────────────────────────────────────────────────
    "zoneinfo",
    "zoneinfo._czoneinfo",
    "tzdata",
    "tzdata.zoneinfo",

    # ── sqlite3 ──────────────────────────────────────────────────────────────
    "sqlite3",
    "_sqlite3",

    # ── ssl / certifi ────────────────────────────────────────────────────────
    "ssl",
    "_ssl",
    "certifi",

    # ── numpy (required by MetaTrader5) ─────────────────────────────────────
    "numpy",
    "numpy._core",
    "numpy._core.multiarray",
    "numpy._core._multiarray_umath",
    "numpy.core",
    "numpy.core.multiarray",

    # ── tkinter (stdlib GUI toolkit) ────────────────────────────────────────
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "_tkinter",
]

# Collect every submodule in src (engine business logic) and manager (orchestration + GUI)
hidden_imports += collect_submodules("src")
hidden_imports += collect_submodules("manager")

# Explicitly add pages that collect_submodules misses (import-time side-effects
# or circular refs prevent auto-discovery at spec build time)
hidden_imports += [
    "manager.gui.pages.agents",
    "manager.gui.pages.manager",
    "manager.gui.pages.agent_dashboard",
    "manager.gui.pages.settings",
    "manager.gui.pages.risk",
    "manager.gui.pages.logs",
    "manager.gui.pages.activity",
]

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
datas = [
    # Version file — used by the auto-updater script and GUI header
    ("version.txt", "."),
    # GUI icons — loaded by manager/gui/assets.py for sidebar logo + window icon
    ("manager/gui/assets/icon.png", "manager/gui/assets"),
    ("manager/gui/assets/icon.ico", "manager/gui/assets"),
    # Clean default config — build.ps1 copies config.example.yaml here after packaging
    # so we never accidentally ship a developer config that contains credentials.
    # If you run pyinstaller directly (not via build.ps1) this file will be absent from
    # _internal/; the GUI falls back to %ProgramData%\Apex Quantel\config.yaml which it
    # creates on first save — that's the correct first-run experience.
]

# Include the full tzdata IANA timezone database
datas += collect_data_files("tzdata")

# CustomTkinter themes are runtime data; package hooks handle its imports and
# the binary dependencies for NumPy and Pillow.
datas += collect_data_files("customtkinter")

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
# sys.base_prefix = the base Python install dir (e.g. C:\Python312).
# Adding it to pathex lets PyInstaller's bootloader find python312.dll when
# running from a venv, where the DLL lives in the base install rather than
# the venv Scripts\ folder.
_base_python_dir = sys.base_prefix

a = Analysis(
    ["src/__main__.py"],
    pathex=[".", _base_python_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Development tools
        "pytest",
        "pytest_asyncio",
        "ruff",
        "mypy",
        "pre_commit",
        "pip",
        "setuptools",
        "wheel",
        "hatch",
        "hatchling",
        # Heavy unused packages (numpy is kept — MT5 requires it)
        "pandas",
        "matplotlib",
        "scipy",
        "IPython",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE
#   console=False  — no terminal window; NSSM captures stdout/stderr via its
#                    own pipe redirect even for Windows-subsystem (GUI) exes.
#   uac_admin=False — manager, GUI, and workers run as the configured
#                     unprivileged runtime identity.
# ---------------------------------------------------------------------------
_icon = None
if sys.platform == "win32":
    _icon_path = os.path.join("installer", "assets", "icon.ico")
    if os.path.exists(_icon_path):
        _icon = _icon_path

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="apex-quant-trader-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can corrupt MT5 DLL loading — leave disabled
    console=False,      # no terminal window; manager/worker log to files
    uac_admin=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

# ---------------------------------------------------------------------------
# COLLECT — produces dist/apex-quant-trader-agent/ (onedir layout)
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="apex-quant-trader-agent",
)
