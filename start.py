#!/usr/bin/env python3
"""
Quick-start script — runs the bot using the existing virtual environment.

Usage:
    python start.py            # Normal start (opens browser)
    python start.py --no-browser   # Start without opening the browser
    pythonw start.py           # Windows: run without a visible terminal

If the virtual environment doesn't exist yet, run `python launcher.py` first.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
LEISURELLM = ROOT / "LeisureLLM"
IS_WIN = sys.platform == "win32"
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def _venv_healthy() -> bool:
    """Return True only if the venv python AND pyvenv.cfg both exist."""
    return VENV_PYTHON.exists() and (VENV / "pyvenv.cfg").exists()


def main() -> None:
    if not _venv_healthy():
        print("Virtual environment not found or corrupted. Run `python launcher.py` first.")
        sys.exit(1)

    env = os.environ.copy()

    # Load .env
    env_path = LEISURELLM / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()

    if "--no-browser" in sys.argv:
        env["NO_BROWSER"] = "1"

    try:
        proc = subprocess.Popen(
            [str(VENV_PYTHON), str(LEISURELLM / "leisureLLM.py")],
            cwd=str(ROOT),
            env=env,
        )
        proc.wait()
        if proc.returncode not in (0, None):
            print(
                f"\n⚠️  leisureLLM.py exited with code {proc.returncode}.\n"
                "    Check logs/leisurellm.log for details, or re-run with:\n"
                f"    {VENV_PYTHON} {LEISURELLM / 'leisureLLM.py'}\n"
            )
            sys.exit(proc.returncode)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
