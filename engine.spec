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
# GUI mode:
#   Default launch shows the CustomTkinter desktop app.
#   Pass --headless for NSSM service mode (no window).

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

# Collect every submodule in our src package
hidden_imports += collect_submodules("src")

# Collect all numpy submodules (covers _core, linalg, fft, random, etc.)
hidden_imports += collect_submodules("numpy")

# ---------------------------------------------------------------------------
# customtkinter — use collect_all for complete coverage
#   (hidden imports + data files + binaries in one call)
# ---------------------------------------------------------------------------
_ctk_data, _ctk_bin, _ctk_hi = collect_all("customtkinter")
hidden_imports += _ctk_hi

_dk_data, _dk_bin, _dk_hi = collect_all("darkdetect")
hidden_imports += _dk_hi

_pil_data, _pil_bin, _pil_hi = collect_all("PIL")
hidden_imports += _pil_hi

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
datas = [
    # Version file — used by the auto-updater script and GUI header
    ("version.txt", "."),
    # Default config — placed next to the exe so the GUI finds it on first launch
    ("config.yaml",  "."),
    # GUI icons — loaded by src/gui/assets.py for sidebar logo + window icon
    ("src/gui/assets/icon.png", "src/gui/assets"),
    ("src/gui/assets/icon.ico", "src/gui/assets"),
]

# Include the full tzdata IANA timezone database
datas += collect_data_files("tzdata")

# numpy data files (.pyd C extensions, .pyi stubs, etc.)
datas += collect_data_files("numpy")

# customtkinter / darkdetect / Pillow data files
datas += _ctk_data
datas += _dk_data
datas += _pil_data

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
    binaries=_ctk_bin + _dk_bin + _pil_bin,
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
#   uac_admin=True — embeds requireAdministrator manifest so Windows always
#                    elevates via UAC; needed so sc.exe start/stop work from
#                    the GUI control panel without extra prompts.
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
    console=False,      # no terminal window for GUI mode
    uac_admin=True,     # requireAdministrator — needed for sc.exe service control
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
