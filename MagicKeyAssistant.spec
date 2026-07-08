# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — Magic Key Assistant tray controller
======================================================

Builds ``MagicKeyAssistant.exe`` — a windowed (no-console) executable
that shows a system-tray icon and manages the bot subprocess.

Usage:
    pip install pyinstaller
    pyinstaller MagicKeyAssistant.spec

The resulting exe lands in ``dist/MagicKeyAssistant/``.
Copy it into the repo root alongside ``LeisureLLM/`` and the ``.venv/``
for a working installation, or use the Inno Setup script to produce a
proper Windows installer.
"""

import os
from pathlib import Path

ROOT = os.path.abspath(".")
icon_path = os.path.join(ROOT, "MTRMK-Assistant-Icon.ico")

a = Analysis(
    ["tray.py"],
    pathex=[ROOT],
    binaries=[],
    # Ship the icon alongside the exe so the tray can load it at runtime
    datas=[
        (icon_path, "."),
    ],
    hiddenimports=[
        "pystray",
        "pystray._win32",       # Windows backend
        "pystray._darwin",      # macOS backend
        "pystray._xorg",        # Linux/X11 backend
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # These are heavy and only needed by the bot process (runs via
        # the venv), not by the tray controller.
        "discord",
        "langchain",
        "chromadb",
        "fastapi",
        "uvicorn",
        "pandas",
        "numpy",
        "torch",
        "transformers",
        "tensorflow",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MagicKeyAssistant",
    icon=icon_path,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # `windowed=True` → no console window
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Version info (shows in file properties on Windows)
    version="version_info.py",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MagicKeyAssistant",
)
