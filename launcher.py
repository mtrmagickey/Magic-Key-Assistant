#!/usr/bin/env python3
"""
Magic Key Assistant — One-Command Launcher
═══════════════════════════════════════════

Bootstraps the entire system and opens the admin setup wizard in your
browser.  Requires only Python 3.10+ (no pip packages needed to run
this script — it installs everything for you).

Usage
-----
    python launcher.py

What it does
------------
1. Checks your Python version
2. Creates a virtual environment (.venv/)
3. Installs all pip dependencies
4. Runs SQLite database migrations
5. Starts the admin console on http://localhost:8000
6. Opens the setup wizard in your default browser

A real-time progress page is served at http://localhost:9000 while the
setup runs, matching the admin console's glassmorphism dark theme.
"""

from __future__ import annotations

import atexit
import http.server
import json
import os
import platform
import socket
import sqlite3
import subprocess
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

def _find_browser() -> str | None:
    """Locate Edge or Chrome, checking PATH then well-known install dirs."""
    for name in ("msedge", "chrome"):
        p = shutil.which(name)
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


def _open_app_window(url: str) -> None:
    """Open *url* in a standalone app-mode browser window (no tabs/address bar).

    Tries Edge then Chrome; falls back to a regular browser tab.
    """
    exe = _find_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}"])
            return
        except Exception:
            pass
    webbrowser.open(url)


# ── Paths ─────────────────────────────────────────────────────────────────────
IS_FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).parent.resolve() if IS_FROZEN else Path(__file__).parent.resolve()
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT)).resolve() if IS_FROZEN else ROOT
VENV = ROOT / ".venv"
LEISURELLM = ROOT / "LeisureLLM"
DB_PATH = LEISURELLM / "assistant.db"
MIGRATIONS = LEISURELLM / "migrations"
REQUIREMENTS = LEISURELLM / "requirements.txt"
WHEELHOUSE = LEISURELLM / "wheels"

IS_WIN = platform.system() == "Windows"
VENV_PYTHON = VENV / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")
VENV_PIP = VENV / ("Scripts" if IS_WIN else "bin") / ("pip.exe" if IS_WIN else "pip")
LOG_FILE = ROOT / "launcher.log"
PIP_LOG = ROOT / "pip_install.log"
LOCK_FILE = ROOT / ".launcher.lock"


def _stage_portable_payload() -> None:
    """Copy bundled app files next to a frozen exe on first run."""
    if not IS_FROZEN:
        return

    source = BUNDLE_ROOT / "LeisureLLM"
    if not source.exists():
        return

    if LEISURELLM.exists() and REQUIREMENTS.exists():
        return

    if LEISURELLM.exists():
        shutil.rmtree(LEISURELLM, ignore_errors=True)

    shutil.copytree(source, LEISURELLM)


def _exit_error(code: int = 1) -> None:
    """Exit with *code*, pausing first so the user can read the output.

    When the launcher runs in its own console window (CREATE_NEW_CONSOLE)
    the window would vanish instantly on exit. Pause to keep it visible.
    """
    print()
    print("  Press Enter to close this window …")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)


# ── Singleton lock ─────────────────────────────────────────────────────────────
_lock_fh = None  # file handle kept open for lifetime of process


def _acquire_singleton_lock() -> bool:
    """Try to acquire an exclusive lock on .launcher.lock.

    Returns True if we got the lock (we are the only instance).
    Returns False if another launcher already holds it.
    """
    global _lock_fh
    try:
        _lock_fh = open(LOCK_FILE, "w", encoding="utf-8")
        if IS_WIN:
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Write our PID so we can diagnose stale locks
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except (OSError, IOError):
        # Lock already held by another process
        if _lock_fh:
            _lock_fh.close()
            _lock_fh = None
        return False


def _release_singleton_lock() -> None:
    global _lock_fh
    if _lock_fh:
        try:
            if IS_WIN:
                import msvcrt
                _lock_fh.seek(0)
                msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            _lock_fh.close()
        except Exception:
            pass
        _lock_fh = None
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _kill_stale_pips() -> None:
    """Kill any orphaned pip processes targeting our .venv.

    When a previous launcher crashed, it may have left pip processes
    running that hold locks on site-packages, blocking new installs.
    """
    if not IS_WIN:
        return
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe'", "get",
             "processid,commandline", "/format:csv"],
            capture_output=True, text=True, timeout=10,
        )
        venv_str = str(VENV).lower()
        my_pid = os.getpid()
        for line in result.stdout.splitlines():
            low = line.lower()
            if "pip install" in low and venv_str in low:
                # Extract PID (last CSV field)
                parts = line.strip().split(",")
                try:
                    pid = int(parts[-1])
                except (ValueError, IndexError):
                    continue
                if pid != my_pid:
                    _logf(f"ORPHAN: killing stale pip PID={pid}")
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=5)
    except Exception as e:
        _logf(f"ORPHAN cleanup error: {e}")

# ── Persistent log file ───────────────────────────────────────────────────────
_log_file_lock = threading.Lock()
_log_lines: list[str] = []  # kept in-memory for the debug panel

def _logf(msg: str) -> None:
    """Write a timestamped line to launcher.log AND to stdout."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"  {line}")
    with _log_file_lock:
        _log_lines.append(line)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── Shared state (read by HTTP handler, written by setup thread) ──────────────
STATUS: dict = {
    "steps": [
        {"id": "python", "label": "Python", "detail": "", "status": "pending", "elapsed": 0},
        {"id": "venv", "label": "Virtual environment", "detail": "", "status": "pending", "elapsed": 0},
        {"id": "deps", "label": "Dependencies", "detail": "", "status": "pending", "elapsed": 0},
        {"id": "db", "label": "Database", "detail": "", "status": "pending", "elapsed": 0},
        {"id": "server", "label": "Admin console", "detail": "", "status": "pending", "elapsed": 0},
    ],
    "complete": False,
    "error": None,
    "redirect": None,
    # Sub-progress for the dependency install step
    "deps_progress": {
        "installed": 0,
        "total": 0,
        "packages": [],  # [{"name": "chromadb", "status": "pending|active|done"}]
    },
    # Debug log tail (last 80 lines, served to the browser)
    "debug_log": [],
    # Live pip output (last 30 lines)
    "pip_output": [],
}
_lock = threading.Lock()
_step_start_times: dict[int, float] = {}


def _update(idx: int, status: str, detail: str = "") -> None:
    with _lock:
        STATUS["steps"][idx]["status"] = status
        if detail:
            STATUS["steps"][idx]["detail"] = detail
        # Track elapsed time
        if status == "active" and idx not in _step_start_times:
            _step_start_times[idx] = time.time()
        if idx in _step_start_times:
            STATUS["steps"][idx]["elapsed"] = round(time.time() - _step_start_times[idx], 1)
        if status in ("done", "error"):
            _step_start_times.pop(idx, None)
        # Push last 80 log lines into status for the debug panel
        with _log_file_lock:
            STATUS["debug_log"] = _log_lines[-80:]


def _set_error(msg: str) -> None:
    with _lock:
        STATUS["error"] = msg


def _add_pip_line(line: str) -> None:
    """Append a line from pip output to the live tail."""
    with _lock:
        STATUS["pip_output"].append(line)
        if len(STATUS["pip_output"]) > 30:
            STATUS["pip_output"] = STATUS["pip_output"][-30:]


def _reset_status() -> None:
    """Reset STATUS to initial state (used by the retry endpoint)."""
    with _lock:
        for s in STATUS["steps"]:
            s["status"] = "pending"
            s["detail"] = ""
            s["elapsed"] = 0
        STATUS["complete"] = False
        STATUS["error"] = None
        STATUS["redirect"] = None
        STATUS["deps_progress"] = {
            "installed": 0,
            "total": 0,
            "packages": [],
        }
        STATUS["pip_output"] = []
        _step_start_times.clear()
    _logf("STATUS reset — retrying setup")


# Guard to prevent concurrent setup runs
_setup_running = threading.Event()


def _set_redirect(url: str) -> None:
    with _lock:
        STATUS["complete"] = True
        STATUS["redirect"] = url


# ── Terminal logger ───────────────────────────────────────────────────────────
_ICONS = {"done": "✓", "error": "✗", "active": "⏳", "pending": "○"}


def _log(idx: int, status: str, label: str, detail: str = "") -> None:
    icon = _ICONS.get(status, "?")
    suffix = f" — {detail}" if detail else ""
    _logf(f"[{idx + 1}/5] {icon} {label}{suffix}")


# ── Compatible Python version range ───────────────────────────────────────────
# chromadb and several other deps are not yet compatible with Python 3.14+.
# We require 3.10 ≤ version ≤ 3.13.
MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 13)


def _find_compatible_python() -> str | None:
    """Locate a Python 3.10–3.13 interpreter on this system.

    Search order:
    1. The Python running this script (``sys.executable``).
    2. The ``py`` launcher (``py -3.13``, ``py -3.12``, …).
    3. Well-known Windows install locations.
    4. ``python3.13``, ``python3.12``, … on PATH.
    """
    # 1. Current interpreter
    v = sys.version_info
    if MIN_PYTHON <= (v.major, v.minor) <= MAX_PYTHON:
        return sys.executable

    # 2. Windows py launcher
    if IS_WIN:
        for minor in range(MAX_PYTHON[1], MIN_PYTHON[1] - 1, -1):
            try:
                result = subprocess.run(
                    ["py", f"-3.{minor}", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return f"py|-3.{minor}"  # sentinel: split on |
            except Exception:
                continue

    # 3. Well-known Windows install directories
    if IS_WIN:
        for minor in range(MAX_PYTHON[1], MIN_PYTHON[1] - 1, -1):
            for base in (
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / f"Python3{minor}",
                Path(os.environ.get("ProgramFiles", "")) / f"Python3{minor}",
            ):
                exe = base / "python.exe"
                if exe.is_file():
                    return str(exe)

    # 4. Versioned names on PATH
    for minor in range(MAX_PYTHON[1], MIN_PYTHON[1] - 1, -1):
        name = f"python3.{minor}" + (".exe" if IS_WIN else "")
        found = shutil.which(name)
        if found:
            return found

    return None


def _python_cmd(python_spec: str) -> list[str]:
    """Convert a python spec (path or 'py|-3.X') into a command list."""
    if "|" in python_spec:
        return python_spec.split("|")
    return [python_spec]


# ══════════════════════════════════════════════════════════════════════════════
# Setup steps (run in background thread)
# ══════════════════════════════════════════════════════════════════════════════

def _step_python() -> bool:
    _update(0, "active")
    _logf(f"PYTHON: searching for compatible Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1]} ...")
    _logf(f"PYTHON: sys.executable = {sys.executable}")
    _logf(f"PYTHON: sys.version = {sys.version}")
    _logf(f"PYTHON: platform = {platform.platform()}")
    _logf(f"PYTHON: ROOT = {ROOT}")
    _logf(f"PYTHON: VENV = {VENV}")
    _logf(f"PYTHON: WHEELHOUSE = {WHEELHOUSE}")
    python = _find_compatible_python()
    _logf(f"PYTHON: _find_compatible_python() -> {python!r}")
    if python is None:
        v = sys.version_info
        detail = (
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1]} "
            f"required (found {v.major}.{v.minor}, which is not yet supported by key dependencies)"
        )
        _update(0, "error", detail)
        _log(0, "error", "Python", detail)
        return False

    # Store so _step_venv can reuse it
    global _compat_python
    _compat_python = python

    # Report which python we found
    cmd = _python_cmd(python)
    try:
        result = subprocess.run(
            [*cmd, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
            capture_output=True, text=True, timeout=10,
        )
        ver_str = result.stdout.strip()
    except Exception:
        ver_str = python
    detail = f"Python {ver_str}"
    _update(0, "done", detail)
    _log(0, "done", "Python", detail)
    return True

_compat_python: str | None = None


def _step_venv() -> bool:
    _update(1, "active")
    _logf(f"VENV: checking health — VENV_PYTHON exists={VENV_PYTHON.exists()}, pyvenv.cfg exists={(VENV / 'pyvenv.cfg').exists()}")
    if _venv_healthy():
        _update(1, "done", "Already exists")
        _log(1, "done", "Virtual environment", "already exists")
        return True
    # If the directory exists but is broken (e.g. missing pyvenv.cfg), nuke it
    if VENV.exists():
        _logf(f"VENV: directory exists but unhealthy, contents: {[p.name for p in VENV.iterdir()][:20]}")
        _log(1, "active", "Virtual environment", "corrupted — recreating …")
        _nuke_venv()
        _logf(f"VENV: after nuke, exists={VENV.exists()}")
    try:
        _log(1, "active", "Virtual environment", "creating .venv/ …")
        # Use the compatible Python found by _step_python, not sys.executable
        # (sys.executable may be 3.14+ which breaks chromadb/pydantic v1).
        python_cmd = _python_cmd(_compat_python) if _compat_python else [sys.executable]
        _logf(f"VENV: creating with command: {[*python_cmd, '-m', 'venv', str(VENV)]}")
        result = subprocess.run(
            [*python_cmd, "-m", "venv", str(VENV)],
            capture_output=True,
            text=True,
        )
        _logf(f"VENV: venv creation exit code={result.returncode}, stderr={result.stderr.strip()[:200]}")
        # python -m venv can return 0 yet fail to copy executables (e.g.
        # when locked files from a previous run linger).  Validate the
        # result by checking pyvenv.cfg AND running python --version.
        if not (VENV / "pyvenv.cfg").exists():
            stderr_tail = (result.stderr or "").strip()[-300:]
            detail = f"venv created but pyvenv.cfg missing{': ' + stderr_tail if stderr_tail else ' (locked files?)'}"
            _update(1, "error", detail)
            _log(1, "error", "Virtual environment", detail)
            return False
        if not VENV_PYTHON.exists():
            detail = "venv created but python.exe not found in Scripts/"
            _update(1, "error", detail)
            _log(1, "error", "Virtual environment", detail)
            return False
        # Smoke-test the venv's Python
        smoke = subprocess.run(
            [str(VENV_PYTHON), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if smoke.returncode != 0:
            detail = f"venv python failed (exit {smoke.returncode}): {(smoke.stderr or '').strip()[:200]}"
            _update(1, "error", detail)
            _log(1, "error", "Virtual environment", detail)
            return False
        _update(1, "done", "Created .venv/")
        _log(1, "done", "Virtual environment", "created")
        return True
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "unknown error")[:200]
        _update(1, "error", detail)
        _log(1, "error", "Virtual environment", detail)
        return False
    except Exception as exc:
        detail = str(exc)[:200]
        _update(1, "error", detail)
        _log(1, "error", "Virtual environment", detail)
        return False


def _nuke_venv() -> None:
    """Aggressively remove .venv, handling locked files on Windows."""
    # First attempt: plain rmtree
    shutil.rmtree(VENV, ignore_errors=True)
    if not VENV.exists():
        return
    # Second attempt: on Windows, try renaming first (moves the locks)
    if IS_WIN:
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="_old_venv_", dir=ROOT))
        try:
            VENV.rename(tmp / "old_venv")
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
    # Third attempt: try rmtree one more time after a brief sleep
    if VENV.exists():
        time.sleep(2)
        shutil.rmtree(VENV, ignore_errors=True)
    if VENV.exists():
        # Last resort: use cmd /c rmdir which can sometimes succeed
        if IS_WIN:
            subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", str(VENV)],
                capture_output=True, timeout=15,
            )
            time.sleep(1)


def _parse_requirement_names(path: Path) -> list[str]:
    """Extract top-level package names from a requirements.txt file."""
    import re
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifiers, extras, comments
        # e.g. "chromadb>=0.4.24,<1.5  # Pin" → "chromadb"
        m = re.match(r"([A-Za-z0-9_][A-Za-z0-9._-]*)", line)
        if m:
            names.append(m.group(1).lower().replace("-", "_"))
    return names


def _deps_set_pkg(name: str, status: str) -> None:
    """Update a package's status in deps_progress, adding it if new."""
    with _lock:
        dp = STATUS["deps_progress"]
        norm = name.lower().replace("-", "_").split("[")[0]
        for pkg in dp["packages"]:
            if pkg["name"] == norm:
                if pkg["status"] != "done":
                    pkg["status"] = status
                if status == "done" and pkg.get("_counted") is not True:
                    dp["installed"] += 1
                    pkg["_counted"] = True
                return
        # New package (transitive dep) — add it
        dp["packages"].append({"name": norm, "status": status})
        dp["total"] += 1
        if status == "done":
            dp["installed"] += 1
            dp["packages"][-1]["_counted"] = True


def _preflight_diagnostics() -> list[str]:
    """Run pre-flight checks before pip and return a list of findings."""
    findings: list[str] = []

    # 1. Venv python exists and runs
    _logf("PREFLIGHT: checking venv python …")
    if not VENV_PYTHON.exists():
        findings.append(f"FATAL: venv python not found at {VENV_PYTHON}")
        return findings
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        findings.append(f"venv python: {r.stdout.strip()} (exit {r.returncode})")
        if r.returncode != 0:
            findings.append(f"  stderr: {r.stderr.strip()[:200]}")
    except Exception as e:
        findings.append(f"venv python --version failed: {e}")

    # 2. pip importable
    _logf("PREFLIGHT: checking pip is importable …")
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        findings.append(f"pip version: {r.stdout.strip()} (exit {r.returncode})")
        if r.returncode != 0:
            findings.append(f"  stderr: {r.stderr.strip()[:200]}")
    except Exception as e:
        findings.append(f"pip --version failed: {e}")

    # 3. pyvenv.cfg content
    cfg = VENV / "pyvenv.cfg"
    if cfg.exists():
        try:
            content = cfg.read_text(encoding="utf-8").strip()
            findings.append(f"pyvenv.cfg ({len(content)} bytes): {content[:200]}")
        except Exception as e:
            findings.append(f"pyvenv.cfg read error: {e}")
    else:
        findings.append("FATAL: pyvenv.cfg missing!")

    # 4. Wheelhouse
    if WHEELHOUSE.exists():
        whl_files = list(WHEELHOUSE.rglob("*.whl"))
        total_bytes = sum(f.stat().st_size for f in whl_files)
        findings.append(f"Wheelhouse: {len(whl_files)} wheels, {total_bytes / 1_048_576:.1f} MB at {WHEELHOUSE}")
    else:
        findings.append(f"Wheelhouse: NOT FOUND at {WHEELHOUSE}")

    # 5. Disk space
    try:
        import shutil as _su
        usage = _su.disk_usage(str(ROOT))
        findings.append(f"Disk free: {usage.free / 1_073_741_824:.1f} GB (total {usage.total / 1_073_741_824:.0f} GB)")
    except Exception as e:
        findings.append(f"Disk check failed: {e}")

    # 6. requirements.txt
    if REQUIREMENTS.exists():
        lines = [l.strip() for l in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        findings.append(f"requirements.txt: {len(lines)} entries")
    else:
        findings.append("requirements.txt: NOT FOUND")

    # 7. Existing site-packages
    sp = VENV / "Lib" / "site-packages"
    if sp.exists():
        dirs = [d.name for d in sp.iterdir() if d.is_dir() and not d.name.startswith("_")]
        findings.append(f"site-packages: {len(dirs)} dirs (may include past installs)")
    else:
        findings.append("site-packages: dir not found (fresh venv)")

    for f in findings:
        _logf(f"PREFLIGHT: {f}")
    return findings


def _step_deps() -> bool:
    _update(2, "active", "Running pre-flight checks …")
    _log(2, "active", "Dependencies", "running pre-flight diagnostics …")

    # ── Pre-flight diagnostics ──
    preflight = _preflight_diagnostics()
    fatals = [f for f in preflight if f.startswith("FATAL")]
    if fatals:
        detail = "; ".join(fatals)
        _update(2, "error", detail)
        _log(2, "error", "Dependencies", detail)
        return False

    if not REQUIREMENTS.exists():
        detail = "LeisureLLM/requirements.txt not found"
        _update(2, "error", detail)
        _log(2, "error", "Dependencies", detail)
        return False

    # Pre-populate the package list from requirements.txt
    req_names = _parse_requirement_names(REQUIREMENTS)
    with _lock:
        dp = STATUS["deps_progress"]
        dp["total"] = len(req_names)
        dp["packages"] = [{"name": n, "status": "pending"} for n in req_names]

    try:
        import re as _re

        # Discover wheel and sdist files in the wheelhouse.
        wheel_files = sorted(WHEELHOUSE.glob("*.whl")) if WHEELHOUSE.exists() else []
        sdist_files = sorted(WHEELHOUSE.glob("*.tar.gz")) if WHEELHOUSE.exists() else []
        all_pkg_files = wheel_files + sdist_files

        if not all_pkg_files:
            detail = "No .whl or .tar.gz files found in wheelhouse"
            _logf(f"DEPS: {detail}")
            _update(2, "error", detail)
            _log(2, "error", "Dependencies", detail)
            return False

        _logf(f"DEPS: found {len(wheel_files)} .whl + {len(sdist_files)} .tar.gz in wheelhouse")
        _update(2, "active", f"Installing {len(all_pkg_files)} packages …")

        env = os.environ.copy()
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        env["PIP_NO_INPUT"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        def _pkg_name_from_whl(whl_path: Path) -> str:
            """Extract normalised package name from wheel filename."""
            # Wheel filenames: {name}-{ver}(-{build})?-{pytag}-{abitag}-{plattag}.whl
            return whl_path.name.split("-", 1)[0].lower().replace("_", "-")

        def _run_pip_batch(files: list[Path], label: str, extra_flags: list[str] | None = None,
                           timeout: int = 300) -> tuple[bool, str]:
            """Install a batch of wheel/sdist files via pip.

            Returns (success, combined_output).
            """
            cmd = [
                str(VENV_PYTHON), "-m", "pip", "install",
                "--no-deps",           # ← skip resolver entirely
                "--no-index",
                "--no-input",
                "--progress-bar", "off",
            ]
            if extra_flags:
                cmd.extend(extra_flags)
            cmd.extend(str(f) for f in files)

            _logf(f"DEPS: [{label}] running: {' '.join(cmd[:8])} … ({len(files)} files)")
            _logf(f"DEPS: [{label}] files: {', '.join(f.name for f in files)}")

            last_exc: Exception | None = None
            for attempt in range(1, 3):           # up to 2 attempts
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout,
                        env=env,
                    )
                    last_exc = None
                    break                         # success — exit retry loop
                except subprocess.TimeoutExpired:
                    _logf(f"DEPS: [{label}] TIMEOUT after {timeout}s (attempt {attempt}/2)")
                    last_exc = subprocess.TimeoutExpired(cmd, timeout)
                    if attempt < 2:
                        _logf(f"DEPS: [{label}] retrying …")
                except Exception as exc:
                    _logf(f"DEPS: [{label}] exception: {exc}")
                    return False, str(exc)

            if last_exc is not None:
                return False, f"pip timed out after {timeout}s (2 attempts)"

            out = (result.stdout or "") + (result.stderr or "")
            for line in out.splitlines():
                line = line.strip()
                if line:
                    _add_pip_line(line)
                    print(f"    {line}")

            if result.returncode != 0:
                _logf(f"DEPS: [{label}] FAILED (exit {result.returncode}):\n{out[-800:]}")
                return False, out[-500:]

            _logf(f"DEPS: [{label}] OK (exit 0)")
            return True, out

        # ── Install wheels in batches ──
        BATCH_SIZE = 25
        installed_count = 0
        total = len(all_pkg_files)

        for batch_idx in range(0, len(wheel_files), BATCH_SIZE):
            batch = wheel_files[batch_idx : batch_idx + BATCH_SIZE]
            batch_num = batch_idx // BATCH_SIZE + 1

            # Mark packages in this batch as active
            for whl in batch:
                _deps_set_pkg(_pkg_name_from_whl(whl), "active")

            _update(2, "active", f"Installing batch {batch_num} … ({installed_count}/{total})")

            ok, output = _run_pip_batch(batch, f"wheels batch {batch_num}")
            if not ok:
                detail = f"Wheel batch {batch_num} failed: {output[:300]}"
                _update(2, "error", detail)
                _log(2, "error", "Dependencies", detail)
                return False

            # Mark packages done
            for whl in batch:
                _deps_set_pkg(_pkg_name_from_whl(whl), "done")
            installed_count += len(batch)
            _update(2, "active", f"{installed_count}/{total} packages installed")
            _logf(f"DEPS: progress {installed_count}/{total}")

        # ── Install sdists (need build tools already available from wheels) ──
        for sdist in sdist_files:
            sdist_name = sdist.name.rsplit("-", 1)[0] if "-" in sdist.name else sdist.stem
            _deps_set_pkg(sdist_name.lower().replace("_", "-"), "active")
            _update(2, "active", f"Building {sdist.name} … ({installed_count}/{total})")

            ok, output = _run_pip_batch(
                [sdist],
                f"sdist {sdist.name}",
                extra_flags=["--no-build-isolation"],
                timeout=120,
            )
            if not ok:
                detail = f"sdist {sdist.name} failed: {output[:300]}"
                _update(2, "error", detail)
                _log(2, "error", "Dependencies", detail)
                return False

            _deps_set_pkg(sdist_name.lower().replace("_", "-"), "done")
            installed_count += 1
            _logf(f"DEPS: progress {installed_count}/{total}")

        # ── Mark any remaining packages as done ──
        with _lock:
            dp = STATUS["deps_progress"]
            for pkg in dp["packages"]:
                if pkg["status"] != "done":
                    pkg["status"] = "done"
                    if not pkg.get("_counted"):
                        dp["installed"] += 1
                        pkg["_counted"] = True

        _update(2, "done", f"All {installed_count} packages installed")
        _log(2, "done", "Dependencies", f"all {installed_count} packages installed")
        return True
    except Exception as exc:
        detail = str(exc)[:300]
        _logf(f"DEPS EXCEPTION: {detail}")
        import traceback
        _logf(f"DEPS TRACEBACK: {traceback.format_exc()}")
        _update(2, "error", detail)
        _log(2, "error", "Dependencies", detail)
        return False


def _step_db() -> bool:
    _update(3, "active")
    if DB_PATH.exists():
        _update(3, "done", "Already exists")
        _log(3, "done", "Database", "already exists")
        return True

    migration_files = sorted(MIGRATIONS.glob("*.sqlite.sql")) if MIGRATIONS.is_dir() else []
    if not migration_files:
        _update(3, "done", "No migrations found — will be created on first bot start")
        _log(3, "done", "Database", "no migrations (created on first run)")
        return True

    _log(3, "active", "Database", f"applying {len(migration_files)} migrations …")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        for mf in migration_files:
            sql = mf.read_text(encoding="utf-8")
            conn.executescript(sql)
        conn.commit()
        conn.close()
        detail = f"{len(migration_files)} migrations applied"
        _update(3, "done", detail)
        _log(3, "done", "Database", detail)
        return True
    except Exception as exc:
        detail = str(exc)[:200]
        _update(3, "error", detail)
        _log(3, "error", "Database", detail)
        return False


# Reference to the admin server subprocess (set by _step_server)
_server_proc: subprocess.Popen | None = None
# Collects server output in a background thread for crash diagnostics
_server_output_lines: list[str] = []


def _drain_server_stdout() -> None:
    """Read server stdout line-by-line so the OS pipe buffer never fills.

    On Windows the default pipe buffer is only 4 KB.  If nobody reads it
    the server deadlocks on its next ``print()`` / log call and *all*
    HTTP requests hang — which manifests as "Hardware Profile Offline".
    """
    try:
        assert _server_proc is not None and _server_proc.stdout is not None
        for raw in iter(_server_proc.stdout.readline, b""):
            _server_output_lines.append(raw.decode("utf-8", errors="replace").rstrip())
    except Exception:
        pass


def _step_server() -> bool:
    global _server_proc
    _update(4, "active", "Starting on port 8000 …")
    _log(4, "active", "Admin console", "starting on port 8000 …")

    env = os.environ.copy()
    # Disable auth for first-run convenience; the setup wizard sets keys
    # and the user restarts with auth enabled via StartBot.bat later.
    env["ADMIN_AUTH_DISABLED"] = "1"

    try:
        _server_proc = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "admin.server"],
            cwd=str(LEISURELLM),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        detail = str(exc)[:200]
        _update(4, "error", detail)
        _log(4, "error", "Admin console", detail)
        return False

    # Drain stdout in a background thread to prevent pipe-buffer deadlock
    threading.Thread(target=_drain_server_stdout, daemon=True).start()

    # Poll until the server responds (up to 45 seconds)
    import urllib.request

    for _ in range(45):
        time.sleep(1)
        if _server_proc.poll() is not None:
            # Show last ~500 chars of captured output so the user can diagnose
            tail = "\n".join(_server_output_lines[-30:])
            snippet = tail.strip()[-500:] if tail.strip() else "(no output)"
            detail = f"Server exited (code {_server_proc.returncode}). Output:\n{snippet}"
            _update(4, "error", detail)
            _log(4, "error", "Admin console", detail)
            return False
        try:
            urllib.request.urlopen("http://localhost:8000/api/v1/auth/status", timeout=2)
            _update(4, "done", "Running at localhost:8000")
            _log(4, "done", "Admin console", "running at localhost:8000")
            return True
        except Exception:
            continue

    _update(4, "error", "Server did not respond after 45 seconds")
    _log(4, "error", "Admin console", "no response after 45 s")
    return False


def _run_setup() -> None:
    """Background thread: run all bootstrap steps sequentially."""
    _setup_running.set()
    _logf("="*60)
    _logf("SETUP STARTED")
    _logf(f"CWD: {os.getcwd()}")
    _logf(f"ROOT: {ROOT}")
    _logf(f"PID: {os.getpid()}")
    _logf("="*60)
    try:
        for step_fn in (_step_python, _step_venv, _step_deps, _step_db, _step_server):
            step_name = step_fn.__name__
            _logf(f"STEP: starting {step_name}")
            t0 = time.time()
            ok = step_fn()
            elapsed = time.time() - t0
            _logf(f"STEP: {step_name} {'OK' if ok else 'FAILED'} in {elapsed:.1f}s")
            if not ok:
                _set_error(
                    next(
                        (s["detail"] for s in STATUS["steps"] if s["status"] == "error"),
                        "Unknown error",
                    )
                )
                _logf("SETUP FAILED — see errors above")
                return
            time.sleep(0.3)  # brief pause so the web UI can catch up

        _set_redirect("http://localhost:8000")
        _logf("SETUP COMPLETE — redirecting to admin console")
    finally:
        _setup_running.clear()


def _venv_healthy() -> bool:
    """Return True only if the venv python AND pyvenv.cfg both exist.

    A venv directory that has python.exe but is missing pyvenv.cfg is
    broken — CPython will refuse to start (exit 106).  Detecting this
    lets us recreate the venv automatically instead of crashing.
    """
    return VENV_PYTHON.exists() and (VENV / "pyvenv.cfg").exists()


def _is_setup_complete() -> bool:
    """Return True if the setup wizard has been completed previously."""
    return (LEISURELLM / "config" / ".setup_complete").exists()


# ── Cleanup ───────────────────────────────────────────────────────────────────
def _cleanup() -> None:
    if _server_proc and _server_proc.poll() is None:
        _server_proc.terminate()


atexit.register(_cleanup)


# ══════════════════════════════════════════════════════════════════════════════
# Embedded launcher web server (temporary — port 9000)
# ══════════════════════════════════════════════════════════════════════════════

class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the progress page and status API."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # suppress noisy request logs

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path == "/api/status":
            self._serve_status()
        elif self.path == "/api/log":
            self._serve_log()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/retry":
            self._handle_retry()
        else:
            self.send_error(404)

    def _handle_retry(self) -> None:
        if _setup_running.is_set():
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"reason":"already running"}')
            return
        _reset_status()
        threading.Thread(target=_run_setup, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _serve_html(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_LAUNCHER_HTML.encode("utf-8"))

    def _serve_status(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with _lock:
            # Deep-copy and strip internal keys before serializing
            out = json.loads(json.dumps(STATUS))
            for pkg in out.get("deps_progress", {}).get("packages", []):
                pkg.pop("_counted", None)
            self.wfile.write(json.dumps(out).encode("utf-8"))

    def _serve_log(self) -> None:
        """Serve the full launcher.log file for download."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="launcher.log"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            data = LOG_FILE.read_bytes() if LOG_FILE.exists() else b"(no log file yet)\n"
        except Exception as e:
            data = f"Error reading log: {e}\n".encode("utf-8")
        self.wfile.write(data)


def _find_free_port(start: int = 9000, attempts: int = 20) -> int:
    for port in range(start, start + attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start


# ══════════════════════════════════════════════════════════════════════════════
# Embedded HTML — glassmorphism progress page
# (Defined before main() so it's available when the HTTP handler runs.)
# ══════════════════════════════════════════════════════════════════════════════

_LAUNCHER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Magic Key Assistant — Setup</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAFn0lEQVR4nJ2XOYxbRRjHf9/MvOdjL3uzWQKJEpKQTYggKCQiXAUUIIEUQIIKiYKjo0NUFNBQAQUFNEi0gKBChAYKDkGBABEJEAFyAJtsNgne017b75hB79nefWt7L0ZayzOe+f7/7/8dMyt3vP+6WwgDlAibHs6x9dG23wWjuhc2BP5f4GuTVrj/b0Ac6Nih7CaMpI727jObUmAN9lYJCzmDAgaaUUrIJZMtDLO17RlwoBCEPPDrFEtG8/XB61IP81FMrDsspNfnxJlMvplNh2DFAsY65vOGx3+6zCunfqbqKT4/uIPXHrqVypDPcCNI1UnUda2PnuTr4KqN8XoZRkoo1QJO/jHJ1XGP2eECJ3+f4qN3vuS+M1eo+l4LoPuv1xe2GLFWwjU8xcnfpzlyeRF/0DE5PsDTz9zLqeN7ePL0eZ44Pdn2fAN5ZaMcSM8n2q0YSsK3ZDU7zlY4Eytqi5q5epNvRkt8e3+ZYmTZOVfHs5agkwsd+bOhkM1WgbQzLvFJHHU0j5gKF263fLprhJxA0YenCtOcWhrDCVwcKaR5klTFKvCOB5lhNgTPTh2EzvFwfoZH7wmoOZ1uKUrE6WaFj6+MpXM/tq0QZM+v4ajaVBUkrEWwCAPieHH2Rl69uhsJIrwg4L257Tx5ZSLFkH6ltw4Ls6VWLIJyjiS0b9obORrG3CSLvBwcJpYmg9Zis/aWZe+Vvr8Cbv3SSUxEvqY4U+PtIOT24q2UwyO8WzDsuTRL0/da/rQVa4Em1dDuBVlSywpsNDIHkhKsKsUL31/g8R/+4dJAPrV/nICXRko8/+zdvee7FekkdZub6gfUn4UjVIqRWpM7/p7hmlGoOMbEMVNDigOzNXZVlmgaaWX/Wga7KkOtCe5cGh8toJXgKUVQ9Lnt2iK3zNQZwTAqHqPa5/pZYe9CnTv/mUlDZJxFsk2oX/jdOiEQHNb3qIbggggXRqggJJAaM5V53jg4SlOrNCRWhNjzyFn4UykatYS4h5cQicJWM8wmY5cKphfcEikP7/wFjjcm2WZCSjpgUMcUJUZuEOJ9mgFriR1YJzRimLOa3c1/GT//GzOxz1k9zsK+Q/jarXTkTOxXFJBMw3A29Vz9Pclz0XccvqlAYFt1rbTCy+WII4eLLcWCj2cUzSDG04KNLc0gws8pjG4wfekXXj8bMnv4KF4Y4LI4mdCYDr1OmdZjxaHaFBN783z41SzbRzS+p6hWQ3aMFxge9Pj+9L9M7B9GRAgjSz6n2Xl9kS++vcKBfcNEKG7eWeDYzFU+qYfkfEnV6gZfUaAdl4RLTjkum1Eqlb948Ngw2jcYo6g3LUoLfk5z4th2fCNMX2tQHvEpD/uUhjzuuq2M70naqJrzVc5G4/g5g43DFQ+7VDCdYLQuPkFHIbU9+3jrXJ0TjWm26YiiiRn0wdeWIesYL3k0Isve3QNEkaUROc5N1VlyPtWqYrYhnGmMc27vEfLErXthjWGWO55qqwDktePixFEmoik8V+ODuRK7onmoB+zIOfw6XFxy3Fus8WNjiKZo6tpP1dk/HHFBhvjVG6OsQuJE+7bt5bG6CqRr0RE7RTmu81hpjlxesb/cYJuxhBRZchqjQKxDyRC5qMiJ/DUuJzJ7ihEX8dG88Ec9wiVGE+nT67znaZF+N8srmRpNRBv0HJ81xqjWhe0mxDQHCJxi0DgmrccNLuSq8xhTEV+FQ0xaw4LkKEmBSmRInoTL0q9zLZvVzGT5ily0ms+icnqPqCBpOKRG40DSuK2+WyTtmmkDQ/DFkZP2m6BP5mfnZtVCOwyJ+fStrxJznYMdbzrdpPcaTQA7JDb72Da9DFuPydYN2gHqp2G/zF7ncdHdjvv+byhZEusB9ftpKy+bFVKqL7NlImusZfXtt7cPUN/RNweyhjcy0E1wM3u7IvofOzBbDAZBu9wAAAAASUVORK5CYII=">
<style>
:root{
  --a:239,170,196;--b:126,163,204;--c:131,120,27;
  --ink:43,38,34;--canvas:249,222,162;
}
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  background:#1a1714;color:rgba(var(--canvas),.85);
  min-height:100vh;display:flex;align-items:center;justify-content:center;
}
/* ── Background layer ── */
.bg{position:fixed;inset:0;z-index:0;overflow:hidden}
.bg-grad{
  position:absolute;inset:0;
  background:
    radial-gradient(ellipse at 30% 20%,rgba(var(--a),.10) 0%,transparent 60%),
    radial-gradient(ellipse at 70% 80%,rgba(var(--c),.08) 0%,transparent 55%),
    radial-gradient(ellipse at 50% 50%,rgba(var(--b),.06) 0%,transparent 70%);
}
.orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:0;
  animation:orbFloat 22s ease-in-out infinite,orbFade 22s ease-in-out infinite}
.o1{width:420px;height:420px;background:rgba(var(--a),.18);top:-80px;left:-80px}
.o2{width:340px;height:340px;background:rgba(var(--c),.14);bottom:-60px;right:-60px;animation-delay:-8s}
.o3{width:260px;height:260px;background:rgba(var(--b),.12);top:45%;left:55%;animation-delay:-15s}
@keyframes orbFloat{
  0%,100%{transform:translate(0,0) scale(1)}
  25%{transform:translate(40px,-35px) scale(1.08)}
  50%{transform:translate(-25px,30px) scale(.94)}
  75%{transform:translate(15px,-20px) scale(1.03)}
}
@keyframes orbFade{
  0%,100%{opacity:.5}
  50%{opacity:1}
}
/* Bokeh particles */
.bokeh{position:absolute;border-radius:50%;pointer-events:none;
  animation:bokehDrift 18s ease-in-out infinite,bokehGlow 6s ease-in-out infinite}
.bk1{width:6px;height:6px;background:rgba(var(--a),.35);top:18%;left:72%;animation-delay:0s}
.bk2{width:4px;height:4px;background:rgba(var(--b),.30);top:65%;left:20%;animation-delay:-3s}
.bk3{width:5px;height:5px;background:rgba(var(--canvas),.20);top:35%;left:85%;animation-delay:-7s}
.bk4{width:3px;height:3px;background:rgba(var(--a),.25);top:80%;left:60%;animation-delay:-11s}
.bk5{width:4px;height:4px;background:rgba(var(--b),.22);top:10%;left:40%;animation-delay:-14s}
@keyframes bokehDrift{
  0%,100%{transform:translate(0,0)}
  33%{transform:translate(20px,-15px)}
  66%{transform:translate(-12px,18px)}
}
@keyframes bokehGlow{0%,100%{opacity:.4}50%{opacity:1}}
/* Light ray */
.light-ray{
  position:absolute;top:-30%;left:15%;width:50%;height:160%;
  background:linear-gradient(170deg,rgba(var(--canvas),.03) 0%,transparent 60%);
  transform:rotate(-8deg);pointer-events:none;
}
.noise{
  position:absolute;inset:0;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
  background-repeat:repeat;
}
/* ── Glass card ── */
.card{
  position:relative;z-index:1;
  background:linear-gradient(140deg,
    rgba(var(--canvas),.06) 0%,
    rgba(var(--a),.03) 50%,
    rgba(var(--b),.04) 100%);
  backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);
  border:1px solid rgba(var(--canvas),.10);border-radius:20px;
  width:100%;max-width:480px;margin:24px;padding:36px 32px;
  animation:slideUp .6s ease;
  box-shadow:0 8px 32px rgba(0,0,0,.25),inset 0 1px 0 rgba(var(--canvas),.08);
}
.card::before{
  content:'';position:absolute;top:0;left:20px;right:20px;height:1px;
  background:linear-gradient(90deg,transparent,rgba(var(--canvas),.15),transparent);
  border-radius:1px;
}
.card::after{
  content:'';position:absolute;bottom:0;left:40px;right:40px;height:2px;
  background:linear-gradient(90deg,
    rgba(var(--a),.3),
    rgba(var(--b),.2),
    rgba(var(--c),.3));
  border-radius:2px;
}
@keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.logo{font-size:36px;margin-bottom:4px;line-height:1}
h1{font-size:18px;font-weight:700;color:rgba(var(--canvas),.95);margin-bottom:2px;letter-spacing:-.02em}
.sub{font-size:12px;color:rgba(var(--canvas),.45);margin-bottom:28px}
/* ── Step segments ── */
.prog{display:flex;gap:3px;margin-bottom:28px}
.seg{flex:1;height:3px;border-radius:2px;background:rgba(var(--canvas),.08);transition:all .5s ease}
.seg.done{background:rgba(var(--a),.7)}
.seg.active{background:rgba(var(--a),.7);animation:shimmer 1.5s ease-in-out infinite}
.seg.error{background:#E13943}
@keyframes shimmer{0%,100%{opacity:1}50%{opacity:.4}}
/* ── Step rows ── */
.steps{display:flex;flex-direction:column;gap:2px}
.row{
  display:flex;align-items:center;gap:14px;
  padding:11px 0;border-bottom:1px solid rgba(var(--canvas),.04);transition:opacity .3s;
}
.row:last-child{border-bottom:none}
.dot{
  width:28px;height:28px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:600;flex-shrink:0;
  border:1px solid rgba(var(--canvas),.10);background:rgba(var(--canvas),.03);color:rgba(var(--canvas),.35);
  transition:all .4s ease;
}
.dot.done{background:rgba(var(--a),.15);border-color:rgba(var(--a),.30);color:rgb(var(--a))}
.dot.active{background:rgba(var(--a),.08);border-color:rgba(var(--a),.20);color:rgb(var(--a));
  animation:pulse 2s ease-in-out infinite}
.dot.error{background:rgba(225,57,67,.15);border-color:rgba(225,57,67,.3);color:#E13943}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.1)}}
.info{flex:1;min-width:0}
.lbl{font-size:13px;font-weight:500;color:rgba(var(--canvas),.50);transition:color .3s}
.row.ok .lbl,.row.on .lbl{color:rgba(var(--canvas),.90)}
.dtl{font-size:11px;margin-top:1px;color:rgba(var(--canvas),.30);transition:color .3s}
.dtl.done{color:rgba(var(--a),.65)}
.dtl.active{color:rgba(var(--canvas),.55)}
.dtl.error{color:#E13943}
/* ── Finish state ── */
.fin{text-align:center;padding:16px 0 8px;animation:slideUp .5s ease}
.ring{
  width:52px;height:52px;border-radius:50%;
  background:rgba(var(--a),.12);border:2px solid rgba(var(--a),.30);
  display:inline-flex;align-items:center;justify-content:center;
  font-size:22px;color:rgb(var(--a));margin-bottom:12px;
}
.fin h2{font-size:15px;font-weight:600;color:rgba(var(--canvas),.95);margin-bottom:4px}
.fin p{font-size:11px;color:rgba(var(--canvas),.45)}
.cd{font-variant-numeric:tabular-nums;color:rgb(var(--a))}
/* ── Error & retry ── */
.err{
  background:rgba(225,57,67,.08);border:1px solid rgba(225,57,67,.15);
  border-radius:12px;padding:10px 14px;margin-top:16px;
  font-size:11px;color:#E13943;line-height:1.5;word-break:break-word;
}
.retry-btn{
  display:inline-block;margin-top:10px;padding:8px 22px;
  background:linear-gradient(180deg,rgba(var(--a),.25) 0%,rgba(var(--a),.15) 100%);
  border:1px solid rgba(var(--a),.30);
  border-radius:10px;color:rgb(var(--a));font-size:11px;font-weight:600;
  cursor:pointer;transition:all .2s;position:relative;overflow:hidden;
}
.retry-btn::before{
  content:'';position:absolute;top:0;left:0;right:0;height:50%;
  background:linear-gradient(180deg,rgba(255,255,255,.12),transparent);
  border-radius:10px 10px 0 0;pointer-events:none;
}
.retry-btn:hover{background:linear-gradient(180deg,rgba(var(--a),.35) 0%,rgba(var(--a),.22) 100%);
  box-shadow:0 4px 16px rgba(var(--a),.20)}
.retry-btn:disabled{opacity:.4;cursor:default}
.foot{text-align:center;margin-top:20px;font-size:10px;color:rgba(var(--canvas),.25)}
/* ── Overall progress bar ── */
.progress-outer{margin-bottom:24px}
.progress-track{height:6px;border-radius:3px;background:rgba(var(--canvas),.08);overflow:hidden;position:relative}
.progress-fill{
  height:100%;border-radius:3px;width:0%;position:relative;
  background:linear-gradient(90deg,rgba(var(--a),.8),rgba(var(--a),1));
  transition:width .6s cubic-bezier(.4,0,.2,1);
}
.progress-fill::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,.18) 50%,transparent 100%);animation:bar-shine 2s ease-in-out infinite}
@keyframes bar-shine{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
.progress-label{display:flex;justify-content:space-between;align-items:center;margin-top:6px;font-size:10px;color:rgba(var(--canvas),.40);font-variant-numeric:tabular-nums}
.progress-pct{color:rgb(var(--a));font-weight:600;font-size:12px}
/* ── Dependencies sub-progress ── */
.deps-sub{overflow:hidden;max-height:0;transition:max-height .4s ease;margin-left:42px;margin-top:0}
.deps-sub.open{max-height:600px}
.deps-bar-wrap{height:4px;border-radius:2px;background:rgba(var(--canvas),.08);margin:6px 0 8px;overflow:hidden}
.deps-bar-fill{height:100%;border-radius:2px;background:rgb(var(--a));transition:width .4s ease;width:0}
.deps-count{font-size:10px;color:rgba(var(--canvas),.40);margin-bottom:6px;font-variant-numeric:tabular-nums}
.deps-grid{display:flex;flex-wrap:wrap;gap:3px 8px;max-height:180px;overflow-y:auto;padding-right:4px}
.deps-grid::-webkit-scrollbar{width:3px}
.deps-grid::-webkit-scrollbar-thumb{background:rgba(var(--canvas),.12);border-radius:2px}
.pkg{font-size:10px;line-height:1.6;color:rgba(var(--canvas),.28);white-space:nowrap;transition:color .3s}
.pkg.active{color:rgba(var(--canvas),.55)}
.pkg.done{color:rgba(var(--a),.65)}
.pkg::before{content:'○ ';font-size:8px}
.pkg.active::before{content:'◌ '}
.pkg.done::before{content:'✓ ';color:rgb(var(--a))}
/* ── Debug panel ── */
.debug-toggle{
  display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;
  font-size:10px;color:rgba(var(--canvas),.30);padding:8px 0 4px;transition:color .2s;
}
.debug-toggle:hover{color:rgba(var(--canvas),.55)}
.debug-toggle .arrow{transition:transform .2s;display:inline-block}
.debug-toggle .arrow.open{transform:rotate(90deg)}
.debug-panel{max-height:0;overflow:hidden;transition:max-height .4s ease}
.debug-panel.open{max-height:2000px}
.debug-log{
  background:rgba(var(--ink),.60);border:1px solid rgba(var(--canvas),.06);
  border-radius:8px;padding:8px 10px;margin-top:6px;
  max-height:300px;overflow-y:auto;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;
  font-size:9px;line-height:1.6;color:rgba(var(--canvas),.50);
  white-space:pre-wrap;word-break:break-all;
}
.debug-log::-webkit-scrollbar{width:4px}
.debug-log::-webkit-scrollbar-thumb{background:rgba(var(--canvas),.12);border-radius:2px}
.debug-log .log-err{color:#E13943}
.debug-log .log-warn{color:rgba(var(--canvas),.70)}
.debug-log .log-ok{color:rgb(var(--a))}
.debug-actions{display:flex;gap:8px;margin-top:6px}
.debug-actions a,.debug-actions button{
  font-size:9px;color:rgba(var(--canvas),.30);text-decoration:underline;cursor:pointer;
  background:none;border:none;padding:0;
}
.debug-actions a:hover,.debug-actions button:hover{color:rgba(var(--canvas),.55)}
.pip-output{
  background:rgba(var(--ink),.45);border:1px solid rgba(var(--canvas),.04);
  border-radius:6px;padding:6px 8px;margin-top:6px;
  max-height:150px;overflow-y:auto;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;
  font-size:9px;line-height:1.5;color:rgba(var(--canvas),.40);
  white-space:pre-wrap;word-break:break-all;
}
.pip-output::-webkit-scrollbar{width:3px}
.pip-output::-webkit-scrollbar-thumb{background:rgba(var(--canvas),.08);border-radius:2px}
.elapsed{font-size:9px;color:rgba(var(--canvas),.30);margin-left:4px;font-variant-numeric:tabular-nums}
</style>
</head>
<body>
<div class="bg">
  <div class="bg-grad"></div>
  <div class="orb o1"></div>
  <div class="orb o2"></div>
  <div class="orb o3"></div>
  <div class="bokeh bk1"></div>
  <div class="bokeh bk2"></div>
  <div class="bokeh bk3"></div>
  <div class="bokeh bk4"></div>
  <div class="bokeh bk5"></div>
  <div class="light-ray"></div>
  <div class="noise"></div>
</div>
<div class="card">
  <div class="logo">🔑</div>
  <h1>Magic Key Assistant</h1>
  <p class="sub" id="sub">Preparing your workspace …</p>
  <div class="prog" id="bars">
    <div class="seg"></div><div class="seg"></div>
    <div class="seg"></div><div class="seg"></div>
    <div class="seg"></div>
  </div>
  <div class="progress-outer" id="progressOuter">
    <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
    <div class="progress-label"><span id="progressStep">Starting …</span><span class="progress-pct" id="progressPct">0%</span></div>
  </div>
  <div class="steps" id="steps">
    <div class="row" id="r0"><div class="dot" id="d0">1</div>
      <div class="info"><div class="lbl">Python</div>
      <div class="dtl" id="t0"></div></div></div>
    <div class="row" id="r1"><div class="dot" id="d1">2</div>
      <div class="info"><div class="lbl">Virtual environment</div>
      <div class="dtl" id="t1"></div></div></div>
    <div class="row" id="r2"><div class="dot" id="d2">3</div>
      <div class="info"><div class="lbl">Dependencies</div>
      <div class="dtl" id="t2"></div></div></div>
    <div class="deps-sub" id="depsSub">
      <div class="deps-bar-wrap"><div class="deps-bar-fill" id="depsBar"></div></div>
      <div class="deps-count" id="depsCount"></div>
      <div class="deps-grid" id="depsGrid"></div>
    </div>
    <div class="row" id="r3"><div class="dot" id="d3">4</div>
      <div class="info"><div class="lbl">Database</div>
      <div class="dtl" id="t3"></div></div></div>
    <div class="row" id="r4"><div class="dot" id="d4">5</div>
      <div class="info"><div class="lbl">Admin console</div>
      <div class="dtl" id="t4"></div></div></div>
  </div>
  <div id="fin" class="fin" style="display:none">
    <div class="ring">✓</div>
    <h2>Ready!</h2>
    <p>Opening setup wizard in <span class="cd" id="cd">3</span>s …</p>
  </div>
  <div id="err" class="err" style="display:none">
    <span id="errMsg"></span>
    <br><button class="retry-btn" id="retryBtn" onclick="doRetry()">Retry</button>
  </div>
  <div class="debug-toggle" onclick="toggleDebug()">
    <span class="arrow" id="dbgArrow">▶</span> Diagnostics
  </div>
  <div class="debug-panel" id="debugPanel">
    <div id="pipOut" class="pip-output" style="display:none"></div>
    <div class="debug-log" id="debugLog"></div>
    <div class="debug-actions">
      <a href="/api/log" download="launcher.log">Download full log</a>
      <button onclick="copyLog()">Copy log to clipboard</button>
    </div>
  </div>
  <div class="foot">Press Ctrl+C in the terminal to stop the server</div>
</div>
<script>
let prev='',busy=false,dbgOpen=false;
function toggleDebug(){
  dbgOpen=!dbgOpen;
  document.getElementById('debugPanel').classList.toggle('open',dbgOpen);
  document.getElementById('dbgArrow').classList.toggle('open',dbgOpen);
}
function copyLog(){
  const txt=document.getElementById('debugLog').textContent;
  navigator.clipboard.writeText(txt).then(()=>alert('Log copied!')).catch(()=>{});
}
function fmtElapsed(s){
  if(!s||s<1) return '';
  if(s<60) return Math.round(s)+'s';
  return Math.floor(s/60)+'m '+Math.round(s%60)+'s';
}
function colorLine(line){
  const low=line.toLowerCase();
  if(low.includes('fatal')||low.includes('error')||low.includes('failed')) return 'log-err';
  if(low.includes('warning')||low.includes('warn')) return 'log-warn';
  if(low.includes(' ok ')||low.includes('done')||low.includes('complete')) return 'log-ok';
  return '';
}
async function doRetry(){
  const btn=document.getElementById('retryBtn');
  btn.disabled=true;btn.textContent='Retrying…';
  try{await fetch('/api/retry',{method:'POST'});}catch(e){}
  prev='';
  document.getElementById('err').style.display='none';
  document.getElementById('errMsg').textContent='';
  document.getElementById('sub').textContent='Preparing your workspace …';
  document.getElementById('steps').style.display='';
  document.getElementById('fin').style.display='none';
  document.getElementById('depsGrid').innerHTML='';
  document.getElementById('depsSub').classList.remove('open');
  document.getElementById('progressFill').style.width='0%';
  document.getElementById('progressPct').textContent='0%';
  document.getElementById('progressStep').textContent='Starting …';
  document.getElementById('debugLog').innerHTML='';
  document.getElementById('pipOut').innerHTML='';
  document.getElementById('pipOut').style.display='none';
  btn.disabled=false;btn.textContent='Retry';
}
async function tick(){
  if(busy)return;
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    const s=JSON.stringify(d);
    if(s===prev)return;
    prev=s;
    const bars=[...document.querySelectorAll('.seg')];
    d.steps.forEach((st,i)=>{
      bars[i].className='seg'+(st.status!=='pending'?' '+st.status:'');
      const dot=document.getElementById('d'+i);
      dot.className='dot'+(st.status!=='pending'?' '+st.status:'');
      dot.textContent=st.status==='done'?'✓':st.status==='error'?'✗':st.status==='active'?'◌':(i+1);
      const row=document.getElementById('r'+i);
      row.className='row'+(st.status==='done'?' ok':st.status==='active'?' on':'');
      const txt=document.getElementById('t'+i);
      // Show elapsed time next to detail
      let detail=st.detail||'';
      const el=fmtElapsed(st.elapsed);
      if(el && st.status==='active') detail+=' ('+el+')';
      txt.innerHTML=detail+(el && st.status==='done'?' <span class="elapsed">'+el+'</span>':'');
      txt.className='dtl'+(st.status!=='pending'?' '+st.status:'');
    });
    // ── Deps sub-progress ──
    const dp=d.deps_progress;
    const sub=document.getElementById('depsSub');
    const depsStep=d.steps[2];
    if(dp && dp.total>0 && depsStep.status==='active'){
      sub.classList.add('open');
      const pct=dp.total?Math.round(dp.installed/dp.total*100):0;
      document.getElementById('depsBar').style.width=pct+'%';
      document.getElementById('depsCount').textContent=dp.installed+' / '+dp.total+' packages ready';
      const grid=document.getElementById('depsGrid');
      if(grid.childElementCount!==dp.packages.length){
        grid.innerHTML='';
        dp.packages.forEach(p=>{
          const el=document.createElement('span');
          el.className='pkg '+p.status;
          el.textContent=p.name;
          el.dataset.pkg=p.name;
          grid.appendChild(el);
        });
      } else {
        dp.packages.forEach(p=>{
          const el=grid.querySelector('[data-pkg="'+p.name+'"]');
          if(el) el.className='pkg '+p.status;
        });
      }
    } else if(depsStep.status==='done'||depsStep.status==='error'){
      sub.classList.remove('open');
    }
    // ── Overall progress bar ──
    {
      let done=0,activeIdx=-1;
      d.steps.forEach((st,i)=>{ if(st.status==='done') done++; if(st.status==='active') activeIdx=i; });
      let pct=Math.round(done/d.steps.length*100);
      const dp2=d.deps_progress;
      if(activeIdx>=0 && dp2 && dp2.total>0 && d.steps[2].status==='active'){
        pct=Math.round((done + dp2.installed/dp2.total) / d.steps.length * 100);
      } else if(activeIdx>=0){
        pct=Math.round((done+0.1)/d.steps.length*100);
      }
      document.getElementById('progressFill').style.width=Math.min(pct,100)+'%';
      document.getElementById('progressPct').textContent=Math.min(pct,100)+'%';
      const stLabel=activeIdx>=0?d.steps[activeIdx].label+'…':(done===d.steps.length?'Complete':'Starting …');
      document.getElementById('progressStep').textContent=stLabel;
    }
    // ── Live pip output ──
    if(d.pip_output && d.pip_output.length>0){
      const po=document.getElementById('pipOut');
      po.style.display='';
      po.textContent=d.pip_output.join('\\n');
      if(dbgOpen) po.scrollTop=po.scrollHeight;
    }
    // ── Debug log ──
    if(d.debug_log && d.debug_log.length>0){
      const dl=document.getElementById('debugLog');
      dl.innerHTML=d.debug_log.map(l=>{
        const cls=colorLine(l);
        return cls?'<span class="'+cls+'">'+l.replace(/</g,'&lt;')+'</span>':l.replace(/</g,'&lt;');
      }).join('\\n');
      if(dbgOpen) dl.scrollTop=dl.scrollHeight;
    }
    // ── Error ──
    if(d.error && !d.complete){
      document.getElementById('errMsg').textContent=d.error;
      document.getElementById('err').style.display='';
      document.getElementById('sub').textContent='Something went wrong';
      // Auto-open debug panel on error
      if(!dbgOpen){dbgOpen=true;document.getElementById('debugPanel').classList.add('open');document.getElementById('dbgArrow').classList.add('open');}
    }
    if(d.complete&&d.redirect){
      busy=true;
      document.getElementById('err').style.display='none';
      document.getElementById('sub').textContent='Setup complete!';
      document.getElementById('steps').style.display='none';
      bars.forEach(b=>{b.className='seg done'});
      document.getElementById('fin').style.display='';
      document.getElementById('progressFill').style.width='100%';
      document.getElementById('progressPct').textContent='100%';
      document.getElementById('progressStep').textContent='Complete';
      let sec=3;
      const cd=document.getElementById('cd');
      const iv=setInterval(()=>{
        sec--;cd.textContent=sec;
        if(sec<=0){clearInterval(iv);window.location.href=d.redirect;}
      },1000);
    }
  }catch(e){}
}
setInterval(tick,600);
tick();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("  🔑  Magic Key Assistant — Launcher")
    print("  ───────────────────────────────────")
    print()

    # ── Clean up stale lock from a previous crashed launcher ──────────────
    if LOCK_FILE.exists():
        try:
            stale_pid_str = LOCK_FILE.read_text(encoding="utf-8").strip()
            stale_pid = int(stale_pid_str) if stale_pid_str.isdigit() else None
        except Exception:
            stale_pid = None
        pid_alive = False
        if stale_pid is not None and IS_WIN:
            try:
                import ctypes, ctypes.wintypes
                k32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, stale_pid)
                if h:
                    exit_code = ctypes.wintypes.DWORD()
                    if k32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                        pid_alive = (exit_code.value == 259)  # STILL_ACTIVE
                    k32.CloseHandle(h)
            except Exception:
                pass
        if not pid_alive:
            print(f"  (removing stale lock from PID {stale_pid or '?'})")
            try:
                LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Singleton guard — only one launcher at a time ─────────────────────
    if not _acquire_singleton_lock():
        # Another launcher is running — try to read its PID
        try:
            other_pid = LOCK_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            other_pid = "unknown"
        print(f"  ✗  Another launcher is already running (PID {other_pid}).")
        print("     Only one instance can run at a time.")
        print("     If the other instance is stuck, delete:")
        print(f"     {LOCK_FILE}")
        print()
        _logf(f"ABORT: another launcher holds the lock (PID {other_pid})")
        sys.exit(0)
    atexit.register(_release_singleton_lock)
    _logf(f"Singleton lock acquired (PID {os.getpid()})")

    # Kill any orphaned pip processes from a previous crashed launcher
    _kill_stale_pips()

    # Announce start in the log file
    _logf("="*60)
    _logf("LAUNCHER STARTED")
    _logf(f"PID={os.getpid()} CWD={os.getcwd()}")
    _logf(f"sys.executable={sys.executable}")
    _logf(f"sys.version={sys.version}")
    _logf(f"platform={platform.platform()}")
    _logf(f"ROOT={ROOT}")
    _logf(f"VENV={VENV}")
    _logf(f"WHEELHOUSE={WHEELHOUSE} (exists={WHEELHOUSE.exists()})")
    _logf(f"LOG_FILE={LOG_FILE}")
    _logf("="*60)

    _stage_portable_payload()

    # ── Returning user: skip bootstrap, go straight to running the bot ──
    already_setup = _is_setup_complete()

    if already_setup and _venv_healthy():
        print("  Setup already complete — starting the bot …")
        print()
        _start_bot_and_serve()
        return

    # ── First-run: show progress page and bootstrap everything ──────────
    # Start temporary progress-page server
    port = _find_free_port()
    class _ThreadedHTTPServer(http.server.ThreadingHTTPServer):
        """Threaded HTTP server so multiple browser connections don't deadlock."""
        daemon_threads = True
    httpd = _ThreadedHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    url = f"http://localhost:{port}"
    print(f"  Progress page : {url}")
    print("  Opening browser …")
    _open_app_window(url)
    print()

    # Run bootstrap in background thread
    setup_thread = threading.Thread(target=_run_setup, daemon=True)
    setup_thread.start()
    setup_thread.join()

    # If setup failed, keep the progress page alive so the user can see the
    # error and click Retry.  Loop until a retry succeeds or Ctrl+C.
    while STATUS.get("error") and not STATUS.get("complete"):
        print()
        print(f"  ✗  Setup failed: {STATUS['error']}")
        print(f"  Progress page : {url}")
        print("  Click 'Retry' in the browser, or press Ctrl+C to exit.")
        try:
            # Wait for either: retry started (error cleared) or completion
            while STATUS.get("error") and not STATUS.get("complete"):
                time.sleep(1)
            # A retry was triggered — wait for it to finish (up to 15 min)
            for _ in range(1800):
                if not _setup_running.is_set():
                    break
                time.sleep(0.5)
            # Loop back to re-check success/error
        except KeyboardInterrupt:
            print("\n  Exiting …")
            httpd.shutdown()
            _exit_error(1)

    if not STATUS.get("complete"):
        # Should not get here, but just in case
        httpd.shutdown()
        _exit_error(1)

    print()
    print("  ════════════════════════════════════")
    print("  ✅  Magic Key Assistant is running!")
    print("  ════════════════════════════════════")
    print()
    print("  Admin console : http://localhost:8000")
    print("  Auth is OFF for this first-run session.")
    print("  Complete the setup wizard, then press")
    restart_hint = "run MagicKeyAssistant-Portable.exe" if IS_FROZEN else "run `python launcher.py`"
    print(f"  Ctrl+C and {restart_hint}")
    print("  again to start the full bot.")
    print()
    print("  Press Ctrl+C to stop the server.")
    print()

    # The progress page already redirected the browser to localhost:8000,
    # so we don't need to open a second window here.

    # Keep alive until Ctrl+C
    try:
        if _server_proc:
            _server_proc.wait()
    except KeyboardInterrupt:
        print("\n  Shutting down …")
        if _server_proc and _server_proc.poll() is None:
            _server_proc.terminate()
        httpd.shutdown()
        print("  Server stopped. Goodbye!")


def _start_bot_and_serve() -> None:
    """Daily-use mode: start the full bot and open the admin console.

    Handles both solo mode (admin GUI only) and team mode (Discord +
    admin GUI).  Automatically opens the browser so the user doesn't
    have to remember ``localhost:8000``.
    """
    global _server_proc

    # Load .env so we can detect operation mode
    env = os.environ.copy()
    env_path = LEISURELLM / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()

    # Determine mode
    mode = env.get("OPERATION_MODE", "").lower()
    has_discord_token = bool(env.get("DISCORD_TOKEN", "").strip())
    if not mode:
        mode = "team" if has_discord_token else "solo"

    print(f"  Mode          : {mode}")
    print("  Admin console : http://localhost:8000")
    print()

    # Suppress the in-process browser-open in leisureLLM.py;
    # the launcher handles opening the app-mode window itself.
    env["NO_BROWSER"] = "1"

    _server_proc = subprocess.Popen(
        [str(VENV_PYTHON), str(LEISURELLM / "leisureLLM.py")],
        cwd=str(ROOT),
        env=env,
    )

    # Wait for the admin server to become ready (up to 30s)
    import urllib.request
    admin_url = "http://localhost:8000/api/v1/auth/status"
    ready = False
    for _ in range(30):
        time.sleep(1)
        if _server_proc.poll() is not None:
            print(f"  ✗  Bot exited with code {_server_proc.returncode}.")
            print("     Check leisurellm.log for details.")
            _exit_error(1)
        try:
            urllib.request.urlopen(admin_url, timeout=2)
            ready = True
            break
        except Exception:
            continue

    if ready:
        _open_app_window("http://localhost:8000")
        print("  ✅  Magic Key Assistant is running!")
        print("     Browser opened to http://localhost:8000")
    else:
        print("  ⚠  Admin console not responding yet.")
        print("     The bot may still be starting — try http://localhost:8000 manually.")

    print()
    print("  Press Ctrl+C to stop.")
    print()

    # Keep alive until Ctrl+C or process exit
    try:
        _server_proc.wait()
    except KeyboardInterrupt:
        print("\n  Shutting down …")
        if _server_proc and _server_proc.poll() is None:
            _server_proc.terminate()
            try:
                _server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _server_proc.kill()
        print("  Stopped. Goodbye!")


if __name__ == "__main__":
    main()
