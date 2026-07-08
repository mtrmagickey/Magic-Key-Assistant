# Windows Installer Build Guide

> Release-engineering guide for producing the Windows tray app and installer.

## Output

The Windows packaging flow produces a user-installable setup executable named like:

`MagicKeyAssistant-Setup-<version>.exe`

Avoid hardcoding a release number in this document. The installer filename should track the current application version.

## Packaging Shape

The packaged Windows distribution has two layers:

- a lightweight tray executable that starts, stops, and monitors the app
- the full Python application environment created during setup or first run

That keeps the tray bundle small while leaving the heavier runtime in the managed environment it launches.

## Prerequisites

You need:

- Python 3.12+
- PyInstaller
- Inno Setup
- the project virtual environment prepared in the repository

## Build The Tray Executable

From the repository root:

```powershell
.\.venv\Scripts\pip.exe install pystray Pillow
.\.venv\Scripts\pyinstaller.exe MagicKeyAssistant.spec
```

Expected result:

- `dist/MagicKeyAssistant/MagicKeyAssistant.exe`

Smoke-test it before building the installer:

```powershell
.\dist\MagicKeyAssistant\MagicKeyAssistant.exe
```

## Build The Installer

Compile [installer.iss](../../installer.iss) with Inno Setup.

From PowerShell:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

Expected result:

- `Output/MagicKeyAssistant-Setup-<version>.exe`

## What The Installer Should Include

The installer should place the user-facing essentials alongside the app:

- the tray executable and its support files
- the launcher and start scripts
- the core user docs: README, Getting Started, Installation
- the icon and uninstall entry

## Expected User Experience

After install, the user should have:

- a Start Menu shortcut
- an optional desktop shortcut if selected during install
- a tray icon for start, stop, restart, setup, and console launch
- a first-run flow that creates the virtual environment and opens the setup experience

## Version Bump Checklist

Before cutting a release, keep these aligned:

- [pyproject.toml](../../pyproject.toml)
- [version_info.py](../../version_info.py)
- [installer.iss](../../installer.iss)
- any user-visible version labels in tray or launcher surfaces

## Related Docs

- [Development And Build Guide](development-build.md)
- [Installation](../../INSTALLATION.md)
- [Getting Started](../../GET_STARTED.md)
