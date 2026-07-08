import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

class SystemTools:
    """Methods for managing local environment and tools."""

    # Track whether we already started Ollama this session
    _ollama_started = False

    @staticmethod
    def _parse_ollama_version(raw: str) -> str:
        """Extract a clean version string from `ollama --version` output.

        The CLI may emit warnings (e.g. "Warning: could not connect to a
        running Ollama instance") before the actual version line.  We look
        for the first semver-like pattern and return it.
        """
        m = re.search(r"(\d+\.\d+\.\d+)", raw)
        return m.group(1) if m else raw.strip().split("\n")[-1].strip()

    @staticmethod
    def _ollama_executable() -> Optional[str]:
        """Return the path to the Ollama executable, or None."""
        _cflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        if platform.system() == "Windows":
            default_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if default_path.exists():
                return str(default_path)
        cmd = "where" if platform.system() == "Windows" else "which"
        try:
            result = subprocess.run(
                [cmd, "ollama"], capture_output=True, text=True, timeout=3,
                creationflags=_cflags,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
        except Exception as e:
            logger.warning("_ollama_executable: suppressed %s", e)
        return None

    @staticmethod
    def _ollama_server_reachable() -> bool:
        """Quick connectivity check — True if Ollama API answers."""
        try:
            import urllib.request
            urllib.request.urlopen("http://localhost:11434/", timeout=2)
            return True
        except Exception:
            return False

    @classmethod
    def ensure_ollama_running(cls, exe_path: Optional[str] = None) -> bool:
        """Start Ollama serve if it isn't already running. Returns True if
        the server is reachable after the attempt."""
        if cls._ollama_server_reachable():
            return True  # already running

        exe = exe_path or cls._ollama_executable()
        if not exe:
            return False

        try:
            # Launch `ollama serve` detached so it survives our process
            if platform.system() == "Windows":
                subprocess.Popen(
                    [exe, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
            else:
                subprocess.Popen(
                    [exe, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            cls._ollama_started = True
            logger.info("Started Ollama server (%s serve)", exe)
        except Exception as exc:
            logger.warning("Failed to start Ollama server: %s", exc)
            return False

        # Give it a moment to bind the port
        import time
        for _ in range(8):
            time.sleep(0.5)
            if cls._ollama_server_reachable():
                return True
        logger.warning("Ollama server started but not reachable within 4 s")
        return False

    @staticmethod
    def get_ollama_status() -> dict:
        """Check if Ollama is installed and running."""
        _cflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        exe = SystemTools._ollama_executable()
        is_installed = exe is not None

        # Version
        version = "unknown"
        if is_installed:
            try:
                ver_proc = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True, timeout=3,
                    creationflags=_cflags,
                )
                if ver_proc.returncode == 0:
                    version = SystemTools._parse_ollama_version(ver_proc.stdout)
            except Exception as e:
                logger.warning("get_ollama_status: suppressed %s", e)

        # Auto-start if installed but not running
        is_running = False
        if is_installed:
            is_running = SystemTools.ensure_ollama_running(exe)

        # Count models
        model_count = 0
        models: list[str] = []
        if is_running:
            try:
                import json as _json
                import urllib.request
                data = _json.loads(
                    urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3).read()
                )
                raw_models = data.get("models", [])
                model_count = len(raw_models)
                models = [m.get("name", m.get("model", "")) for m in raw_models]
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        return {
            "installed": is_installed,
            "path": exe,
            "version": version,
            "running": is_running,
            "model_count": model_count,
            "models": models,
        }

    @staticmethod
    async def download_file(url: str, dest_path: Path):
        async with aiohttp.ClientSession() as session, session.get(url) as resp:
            if resp.status == 200:
                with open(dest_path, 'wb') as f:
                    while True:
                        chunk = await resp.content.read(1024*1024) # 1MB chunks
                        if not chunk:
                            break
                        f.write(chunk)
            else:
                raise RuntimeError(f"Download failed with status {resp.status}")

    @staticmethod
    async def install_ollama_windows() -> Tuple[bool, str]:
        """Download and run Ollama installer for Windows."""
        installer_url = "https://ollama.com/download/OllamaSetup.exe"
        temp_dir = Path(os.environ.get("TEMP", ".")) / "LeisureLLM_Installers"
        temp_dir.mkdir(exist_ok=True)
        installer_path = temp_dir / "OllamaSetup.exe"
        
        try:
            logger.info(f"Downloading Ollama installer from {installer_url}...")
            await SystemTools.download_file(installer_url, installer_path)
            
            logger.info("Running installer...")
            # Launch installer. /silent? OllamaSetup might not support silent flags officially yet,
            # but usually standard InnoSetup/etc flags might work. 
            # For now, let's just launch it so the user sees it (Freemium experience)
            
            # Use Popen to launch and detach, or wait?
            # User experience: "Click Install" -> Installer Pops up.
            if platform.system() == "Windows":
                os.startfile(installer_path)
                return True, "Installer launched. Please complete the setup wizard."
            else:
                return False, "Automated install only supported on Windows for this beta."
                
        except Exception as e:
            logger.error(f"Install failed: {e}")
            return False, str(e)
