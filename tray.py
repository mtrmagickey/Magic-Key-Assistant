#!/usr/bin/env python3
"""
Magic Key Assistant — System-Tray Controller
=============================================

Runs the bot as a background process and provides a system-tray icon
with a right-click menu for everyday management.

Usage (development)
-------------------
    pythonw tray.py            # Windows — no terminal window
    python  tray.py            # any OS   — with terminal for debugging

When packaged via PyInstaller the resulting ``MagicKeyAssistant.exe``
behaves identically but without requiring a Python installation.

Tray menu
---------
    Open Console   – opens http://localhost:8000 in the default browser
    ─────────────
    Start          – start the bot subprocess
    Stop           – stop the bot subprocess
    Restart        – stop then start
    ─────────────
    Run Setup      – open the first-run launcher (bootstrap + wizard)
    About          – version tooltip
    Quit           – stop the bot and exit
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — work correctly both when running from source and when frozen
# by PyInstaller (sys._MEIPASS points to the extracted bundle).
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle
    # The exe lives in {app}\MagicKeyAssistant\ but the project root
    # (launcher.py, LeisureLLM/, .venv/) is one level up at {app}\.
    BUNDLE_DIR = Path(sys._MEIPASS)         # type: ignore[attr-defined]
    ROOT = Path(sys.executable).parent.parent.resolve()
else:
    ROOT = Path(__file__).parent.resolve()
    BUNDLE_DIR = ROOT

LEISURELLM = ROOT / "LeisureLLM"
IS_WIN = sys.platform == "win32"
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")
ICON_PATH = BUNDLE_DIR / "MTRMK-Assistant-Icon.ico"

APP_NAME = "Magic Key Assistant"
ADMIN_URL = "http://localhost:8000"

# ---------------------------------------------------------------------------
# Logging — write to a file so we have diagnostics even without a console
# ---------------------------------------------------------------------------
LOG_PATH = ROOT / "tray.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tray")

# Also echo to stderr if we have a console (development mode)
if sys.stderr and hasattr(sys.stderr, "write"):
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))
    log.addHandler(_sh)


# ═══════════════════════════════════════════════════════════════════════════════
# Bot process management
# ═══════════════════════════════════════════════════════════════════════════════

class BotController:
    """Manages the lifecycle of the bot subprocess."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # -- Status ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def status_text(self) -> str:
        return "Running" if self.running else "Stopped"

    # -- Actions --------------------------------------------------------------

    def start(self) -> bool:
        """Start the bot.  Returns True on success."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                log.info("Bot already running (pid %d)", self._proc.pid)
                return True

            if not _venv_healthy():
                log.error("Virtual-env broken or missing at %s", VENV)
                return False

            env = self._build_env()

            try:
                # CREATE_NO_WINDOW keeps the bot from spawning a console on
                # Windows when the tray app itself is a windowed exe.
                kwargs: dict = {}
                if IS_WIN:
                    kwargs["creationflags"] = (
                        subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                    )

                self._proc = subprocess.Popen(
                    [str(VENV_PYTHON), str(LEISURELLM / "leisureLLM.py")],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **kwargs,
                )
                log.info("Bot started (pid %d)", self._proc.pid)
                return True
            except Exception:
                log.exception("Failed to start bot")
                return False

    def stop(self, timeout: float = 8) -> None:
        """Gracefully terminate the bot."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            log.info("Stopping bot (pid %d) …", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("Bot did not stop in time — killing")
                self._proc.kill()
            self._proc = None
            log.info("Bot stopped")

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _build_env() -> dict[str, str]:
        """Load .env and merge with the current environment."""
        env = os.environ.copy()
        # Suppress the in-process browser-open; the tray handles that.
        env["NO_BROWSER"] = "1"
        env_path = LEISURELLM / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        return env


# ═══════════════════════════════════════════════════════════════════════════════
# System-tray icon
# ═══════════════════════════════════════════════════════════════════════════════

def _load_icon():
    """Return a PIL Image for the tray icon."""
    from PIL import Image

    if ICON_PATH.exists():
        try:
            return Image.open(str(ICON_PATH))
        except Exception:
            log.warning("Could not load %s — using fallback", ICON_PATH)

    # Fallback: a simple 64×64 green circle
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(43, 162, 133, 255))
    return img


def _venv_healthy() -> bool:
    """Return True only if the venv python AND pyvenv.cfg both exist."""
    return VENV_PYTHON.exists() and (VENV / "pyvenv.cfg").exists()


def _needs_setup() -> bool:
    """True if first-run setup has not been completed."""
    return not (LEISURELLM / "config" / ".setup_complete").exists() or not _venv_healthy()


def _find_compatible_python() -> list[str]:
    """Locate a Python 3.10–3.13 for running the launcher.

    Returns a *list* of command tokens (e.g. ``["py", "-3.13"]`` or
    ``["C:\\...\\python.exe"]``).  Falls back to ``["python"]``.
    """
    import shutil as _shutil

    min_minor, max_minor = 10, 13

    # py launcher (Windows)
    if IS_WIN:
        for minor in range(max_minor, min_minor - 1, -1):
            try:
                result = subprocess.run(
                    ["py", f"-3.{minor}", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return ["py", f"-3.{minor}"]
            except Exception:
                continue

    # Well-known Windows install directories
    if IS_WIN:
        for minor in range(max_minor, min_minor - 1, -1):
            for base in (
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / f"Python3{minor}",
                Path(os.environ.get("ProgramFiles", "")) / f"Python3{minor}",
            ):
                exe = base / "python.exe"
                if exe.is_file():
                    return [str(exe)]

    # Versioned names on PATH
    for minor in range(max_minor, min_minor - 1, -1):
        name = f"python3.{minor}" + (".exe" if IS_WIN else "")
        if _shutil.which(name):
            return [name]

    return ["python"]


# Track whether we've already launched setup in this tray session
_setup_launched = False


def _run_setup() -> None:
    """Open a terminal and run the first-run launcher."""
    global _setup_launched
    if _setup_launched:
        log.info("Setup already launched in this session — skipping")
        return
    launcher = ROOT / "launcher.py"
    if not launcher.exists():
        log.error("launcher.py not found at %s", launcher)
        return
    # Check if a launcher is already running (lockfile exists with a live PID)
    lock_file = ROOT / ".launcher.lock"
    if lock_file.exists():
        try:
            pid_str = lock_file.read_text(encoding="utf-8").strip()
            pid = int(pid_str)
            # Check if that PID is still alive using GetExitCodeProcess.
            # OpenProcess alone is unreliable — it can return a handle for
            # dead/zombie processes whose handles haven't been released.
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                STILL_ACTIVE = 259
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == STILL_ACTIVE:
                    kernel32.CloseHandle(handle)
                    log.info("Launcher already running (PID %d) — skipping", pid)
                    return
                kernel32.CloseHandle(handle)
                log.info("Lockfile PID %d is dead (exit %d) — proceeding with setup", pid, exit_code.value)
        except Exception:
            pass  # stale lockfile, proceed

    _setup_launched = True
    log.info("Running setup: launcher=%s  cwd=%s", launcher, ROOT)

    if IS_WIN:
        # Find a compatible Python (3.10–3.13); bare "python" on PATH may
        # be 3.14+ which breaks chromadb / pydantic v1.
        if getattr(sys, "frozen", False):
            python_cmd = _find_compatible_python()  # returns list, e.g. ["py", "-3.13"]
        else:
            python_cmd = [sys.executable]
        # Launch the launcher directly with its own console window.
        # Using CREATE_NEW_CONSOLE avoids the stdin-inheritance deadlock
        # that cmd /k caused (pip's child resolver froze on shared stdin).
        subprocess.Popen(
            [*python_cmd, str(launcher)],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        # Best-effort: try common terminal emulators
        for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
            try:
                subprocess.Popen([term, "-e", f"python3 {launcher}"], cwd=str(ROOT))
                return
            except FileNotFoundError:
                continue
        # Last resort: just run in foreground
        subprocess.Popen(["python3", str(launcher)], cwd=str(ROOT))


def _admin_is_up() -> bool:
    """Quick check whether the admin console is responding."""
    import urllib.request
    try:
        urllib.request.urlopen(f"{ADMIN_URL}/api/v1/auth/status", timeout=2)
        return True
    except Exception:
        return False


def _find_browser() -> str | None:
    """Locate Edge or Chrome, checking PATH then well-known install dirs."""
    import shutil as _sh

    for name in ("msedge", "chrome"):
        p = _sh.which(name)
        if p:
            return p
    if IS_WIN:
        # Check all locations for Edge first (preferred), then Chrome
        _dirs = [os.environ.get(v, "") for v in
                 ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for rel in (
            os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join("Google", "Chrome", "Application", "chrome.exe"),
        ):
            for base in _dirs:
                if base:
                    candidate = os.path.join(base, rel)
                    if os.path.isfile(candidate):
                        return candidate
    return None


def _open_console() -> None:
    """Open the admin console in app mode (standalone window, no browser chrome).

    Tries Edge first (present on all Windows 10/11), then Chrome, then
    falls back to the default browser.
    """
    exe = _find_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={ADMIN_URL}"])
            log.info("Opened console with %s", exe)
            return
        except Exception:
            log.warning("Failed to launch %s — falling back", exe, exc_info=True)

    # Fallback: regular browser tab
    log.info("Using webbrowser.open() fallback for %s", ADMIN_URL)
    webbrowser.open(ADMIN_URL)


def _wait_then_open_console(bot: BotController, delay: float = 4) -> None:
    """Wait for the admin console to come up, then open the browser."""
    def _worker():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _admin_is_up():
                _open_console()
                return
            time.sleep(delay)
        log.warning("Admin console never responded — skipping browser open")
    threading.Thread(target=_worker, daemon=True).start()


def run_tray() -> None:
    """Entry point: start bot + tray icon."""
    import pystray
    from pystray import MenuItem as Item

    bot = BotController()
    icon_image = _load_icon()

    # ── Menu callbacks (run on the pystray background thread) ────────────

    def on_open_console(icon, item):
        if bot.running and _admin_is_up():
            _open_console()
        elif bot.running:
            _wait_then_open_console(bot)
        else:
            log.info("Bot not running — starting first, then opening console")
            bot.start()
            _wait_then_open_console(bot)

    def on_start(icon, item):
        if not bot.running:
            bot.start()
            _wait_then_open_console(bot)
            _update_tooltip(icon, bot)

    def on_stop(icon, item):
        bot.stop()
        _update_tooltip(icon, bot)

    def on_restart(icon, item):
        bot.restart()
        _wait_then_open_console(bot)
        _update_tooltip(icon, bot)

    def on_setup(icon, item):
        _run_setup()

    def on_quit(icon, item):
        log.info("Quit requested")
        bot.stop()
        icon.stop()

    # ── Build menu ──────────────────────────────────────────────────────

    menu = pystray.Menu(
        Item("Open Console", on_open_console, default=True),
        pystray.Menu.SEPARATOR,
        Item("Start", on_start, enabled=lambda item: not bot.running),
        Item("Stop", on_stop, enabled=lambda item: bot.running),
        Item("Restart", on_restart, enabled=lambda item: bot.running),
        pystray.Menu.SEPARATOR,
        Item("Run Setup …", on_setup),
        Item("v0.8.0", None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item("Quit", on_quit),
    )

    def _update_tooltip(icon, bot):
        icon.title = f"{APP_NAME} — {bot.status_text}"

    # ── Create and run ──────────────────────────────────────────────────

    icon = pystray.Icon(
        name="magic_key_assistant",
        icon=icon_image,
        title=f"{APP_NAME} — Starting …",
        menu=menu,
    )

    # Auto-start the bot (unless first-run hasn't happened yet)
    def _on_setup_ready(icon):
        if _needs_setup():
            icon.title = f"{APP_NAME} — Setup required"
            log.info("First-run setup not complete — auto-launching setup")
            _run_setup()
        else:
            bot.start()
            _wait_then_open_console(bot)
            _update_tooltip(icon, bot)

    # Periodic tooltip updater
    def _tooltip_loop(icon):
        _on_setup_ready(icon)
        while icon.visible:
            _update_tooltip(icon, bot)
            time.sleep(5)

    threading.Thread(target=_tooltip_loop, args=(icon,), daemon=True).start()

    log.info("Starting system-tray icon")
    icon.run()
    log.info("Tray icon exited")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        run_tray()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error in tray app")
        sys.exit(1)
