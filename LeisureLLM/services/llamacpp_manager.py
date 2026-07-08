"""
LlamaCpp Manager — First-class llama.cpp server backend.

Downloads, configures, and manages a llama-server process with
optimal flags derived from the device capability scanner.  Once
running, registers as a CUSTOM (OpenAI-compatible) backend in the
ModelRouter — zero new LLM client code needed.

Key capabilities:
  • Download llama-server binary from GitHub releases
  • Download GGUF models from HuggingFace
  • Launch with community-validated optimal flags
  • Hardware-aware configuration (VRAM → n_gpu_layers, ctx, KV cache)
  • Health monitoring and auto-restart
  • Admin API + UI integration

Architecture note:
  llama-server exposes an OpenAI-compatible ``/v1/chat/completions``
  endpoint, so MKA's existing ``OpenAICompatibleClient`` handles all
  inference.  This module only manages the *server process*.

Usage
-----
    from services.llamacpp_manager import LlamaCppManager

    manager = LlamaCppManager()
    await manager.download_server()           # Get the binary
    await manager.download_model(url, name)   # Get a GGUF
    manager.launch(model_path, port=8012)     # Start server
    # The admin API auto-registers it as a CUSTOM backend in ModelRouter
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

_DEFAULT_PORT = 8012
_HEALTH_TIMEOUT = 5
_STARTUP_WAIT = 60  # seconds to wait for server to become ready (large GGUFs need time to mmap)

# GitHub release asset patterns by platform
_RELEASE_PATTERNS = {
    "Windows": {
        "cuda": "llama-*-bin-win-cuda-*-x64.zip",
        "cpu": "llama-*-bin-win-cpu-x64.zip",
    },
    "Linux": {
        "cuda": "llama-*-bin-ubuntu-vulkan-x64.tar.gz",
        "cpu": "llama-*-bin-ubuntu-x64.tar.gz",
    },
    "Darwin": {
        "metal": "llama-*-bin-macos-arm64.tar.gz",
        "cpu": "llama-*-bin-macos-x64.tar.gz",
    },
}

# Community-validated optimal flags from r/LocalLLaMA
# These are the consensus "best" flags as of early 2026.
_OPTIMAL_FLAGS = {
    "flash_attention": True,       # -fa — always beneficial
    "kv_cache_type_k": "q8_0",    # -ctk q8_0 — <0.4% PPL diff, +12-38% throughput
    "kv_cache_type_v": "q8_0",    # -ctv q8_0 — same free lunch
    "fit": True,                   # --fit — auto layer splitting for GPU
    # Notably absent: -b/-ub — community consensus is to NOT set these
}


@dataclass
class LlamaCppConfig:
    """Runtime configuration for a llama-server instance."""
    model_path: str = ""
    port: int = _DEFAULT_PORT
    host: str = "127.0.0.1"
    ctx_size: int = 8192
    n_gpu_layers: int = -1  # -1 = auto (offload all)
    flash_attention: bool = True
    kv_cache_type_k: str = "q8_0"
    kv_cache_type_v: str = "q8_0"
    fit: bool = True
    threads: int = 0  # 0 = auto
    parallel: int = 1  # concurrent request slots
    extra_args: List[str] = field(default_factory=list)

    def to_args(self) -> List[str]:
        """Build command-line arguments for llama-server."""
        args = [
            "--model", self.model_path,
            "--port", str(self.port),
            "--host", self.host,
            "--ctx-size", str(self.ctx_size),
        ]
        if self.n_gpu_layers != 0:
            args.extend(["--n-gpu-layers", str(self.n_gpu_layers)])
        if self.flash_attention:
            args.extend(["-fa", "on"])
        if self.kv_cache_type_k:
            args.extend(["-ctk", self.kv_cache_type_k])
        if self.kv_cache_type_v:
            args.extend(["-ctv", self.kv_cache_type_v])
        if self.fit:
            args.extend(["--fit", "on"])
        if self.threads > 0:
            args.extend(["--threads", str(self.threads)])
        if self.parallel > 1:
            args.extend(["--parallel", str(self.parallel)])
        args.extend(self.extra_args)
        return args

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_path": self.model_path,
            "port": self.port,
            "host": self.host,
            "ctx_size": self.ctx_size,
            "n_gpu_layers": self.n_gpu_layers,
            "flash_attention": self.flash_attention,
            "kv_cache_type_k": self.kv_cache_type_k,
            "kv_cache_type_v": self.kv_cache_type_v,
            "fit": self.fit,
            "threads": self.threads,
            "parallel": self.parallel,
            "extra_args": self.extra_args,
        }


@dataclass
class LlamaCppStatus:
    """Status of the llama-server process."""
    installed: bool = False
    binary_path: str = ""
    running: bool = False
    pid: int = 0
    port: int = _DEFAULT_PORT
    model_loaded: str = ""
    models_dir: str = ""
    available_models: List[str] = field(default_factory=list)
    config: Optional[LlamaCppConfig] = None
    uptime_seconds: float = 0
    version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "binary_path": self.binary_path,
            "running": self.running,
            "pid": self.pid,
            "port": self.port,
            "model_loaded": self.model_loaded,
            "models_dir": self.models_dir,
            "available_models": self.available_models,
            "config": self.config.to_dict() if self.config else None,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "version": self.version,
        }


class LlamaCppManager:
    """Manages the llama-server binary, GGUF models, and server lifecycle."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._base_dir = base_dir or self._default_base_dir()
        self._bin_dir = self._base_dir / "bin"
        self._models_dir = self._base_dir / "models"
        self._process: Optional[subprocess.Popen] = None
        self._config: Optional[LlamaCppConfig] = None
        self._start_time: float = 0
        self._config_path = self._base_dir / "llamacpp_config.json"

    @staticmethod
    def _default_base_dir() -> Path:
        """Default install location alongside the app."""
        # Use LOCALAPPDATA on Windows, ~/.local/share on Linux/macOS
        if platform.system() == "Windows":
            base = Path(os.environ.get("LOCALAPPDATA", "")) / "MagicKeyAssistant" / "llamacpp"
        else:
            base = Path.home() / ".local" / "share" / "mka" / "llamacpp"
        return base

    # ─── Binary Management ────────────────────────────────────────────────

    def _find_binary(self) -> Optional[str]:
        """Find the llama-server binary."""
        if platform.system() == "Windows":
            candidates = [
                self._bin_dir / "llama-server.exe",
                self._bin_dir / "build" / "bin" / "llama-server.exe",
            ]
            binary_name = "llama-server.exe"
        else:
            candidates = [
                self._bin_dir / "llama-server",
                self._bin_dir / "build" / "bin" / "llama-server",
            ]
            binary_name = "llama-server"

        for path in candidates:
            if path.exists():
                return str(path)

        # Some release archives unpack into a versioned subfolder.
        # Search recursively under bin/ so "installed" survives refreshes.
        if self._bin_dir.exists():
            for path in self._bin_dir.rglob(binary_name):
                if path.is_file():
                    return str(path)

        # Check PATH
        found = shutil.which(binary_name)
        return found

    def _get_version(self) -> str:
        """Get llama-server version."""
        binary = self._find_binary()
        if not binary:
            return ""
        try:
            cflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True, text=True, timeout=5,
                creationflags=cflags,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
            return result.stderr.strip().split("\n")[0] if result.stderr else ""
        except Exception:
            return ""

    async def download_server(
        self,
        progress_callback: Optional[Any] = None,
    ) -> Tuple[bool, str]:
        """Download the latest llama.cpp release binary.

        Auto-detects platform and GPU availability.

        Args:
            progress_callback: Optional async callable(percent, message)

        Returns:
            (success, message)
        """
        system = platform.system()
        if system not in _RELEASE_PATTERNS:
            return False, f"Unsupported platform: {system}"

        # Detect GPU to choose CUDA vs CPU build
        has_cuda = self._detect_cuda()
        if system == "Windows":
            variant = "cuda" if has_cuda else "cpu"
        elif system == "Darwin":
            variant = "metal" if platform.machine() == "arm64" else "cpu"
        else:
            variant = "cuda" if has_cuda else "cpu"

        # Get latest release URL from GitHub API
        try:
            release_url, asset_name = await self._find_release_asset(system, variant)
        except Exception as e:
            return False, f"Could not find release: {e}"

        if not release_url:
            return False, f"No matching release asset for {system}/{variant}"

        # Download
        self._bin_dir.mkdir(parents=True, exist_ok=True)
        download_path = self._bin_dir / asset_name

        try:
            if progress_callback:
                await progress_callback(0, f"Downloading {asset_name}...")

            await self._download_file(release_url, download_path, progress_callback)

            if progress_callback:
                await progress_callback(90, "Extracting...")

            # Extract
            self._extract_archive(download_path, self._bin_dir)
            download_path.unlink(missing_ok=True)

            # Verify binary exists
            binary = self._find_binary()
            if not binary:
                return False, "Downloaded but could not locate llama-server binary"

            # Make executable on Unix
            if system != "Windows":
                os.chmod(binary, 0o755)

            if progress_callback:
                await progress_callback(100, "llama-server installed successfully")

            logger.info("llama-server installed at %s", binary)
            return True, f"Installed at {binary}"

        except Exception as e:
            logger.error("llama-server download failed: %s", e)
            return False, str(e)

    async def _find_release_asset(
        self, system: str, variant: str,
    ) -> Tuple[str, str]:
        """Find the download URL for the latest llama.cpp release."""
        api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

        async with aiohttp.ClientSession() as session, session.get(api_url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GitHub API returned {resp.status}")
            release = await resp.json()

        assets = release.get("assets", [])
        pattern_map = _RELEASE_PATTERNS.get(system, {})
        pattern = pattern_map.get(variant, "")

        if not pattern:
            raise RuntimeError(f"No release pattern for {system}/{variant}")

        # Find matching asset using glob-like matching
        import fnmatch
        for asset in assets:
            name = asset.get("name", "")
            if fnmatch.fnmatch(name, pattern):
                return asset["browser_download_url"], name

        # Fallback: try CPU variant
        if variant != "cpu":
            cpu_pattern = pattern_map.get("cpu", "")
            for asset in assets:
                name = asset.get("name", "")
                if fnmatch.fnmatch(name, cpu_pattern):
                    return asset["browser_download_url"], name

        raise RuntimeError(f"No matching asset in release (pattern: {pattern})")

    # ─── Model Management ─────────────────────────────────────────────────

    def list_models(self) -> List[Dict[str, Any]]:
        """List available GGUF models in the models directory."""
        self._models_dir.mkdir(parents=True, exist_ok=True)
        models = []
        for f in sorted(self._models_dir.glob("*.gguf")):
            size_gb = round(f.stat().st_size / (1024**3), 2)
            models.append({
                "name": f.stem,
                "filename": f.name,
                "path": str(f),
                "size_gb": size_gb,
            })
        return models

    async def download_model(
        self,
        url: str,
        filename: Optional[str] = None,
        progress_callback: Optional[Any] = None,
    ) -> Tuple[bool, str]:
        """Download a GGUF model file.

        Args:
            url: Direct download URL (typically HuggingFace)
            filename: Target filename. If None, derived from URL.
            progress_callback: Optional async callable(percent, message)
        """
        self._models_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            filename = url.split("/")[-1].split("?")[0]
            if not filename.endswith(".gguf"):
                filename += ".gguf"

        dest = self._models_dir / filename
        if dest.exists():
            return True, f"Model already exists: {filename}"

        # Pre-flight: check available disk space
        try:
            free_bytes = shutil.disk_usage(self._models_dir).free
            free_gb = free_bytes / (1024**3)
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True) as head_resp:
                    expected = head_resp.content_length or 0
            if expected > 0:
                expected_gb = expected / (1024**3)
                if expected > free_bytes:
                    return False, f"Not enough disk space: need {expected_gb:.1f} GB but only {free_gb:.1f} GB free"
                if progress_callback:
                    await progress_callback(0, f"Downloading {filename} ({expected_gb:.1f} GB)...")
            elif progress_callback:
                await progress_callback(0, f"Downloading {filename}...")
        except Exception:
            # Pre-flight failed (network issue, etc.) — continue with download anyway
            if progress_callback:
                await progress_callback(0, f"Downloading {filename}...")

        try:
            await self._download_file(url, dest, progress_callback)

            if progress_callback:
                await progress_callback(100, f"Downloaded {filename}")

            size_gb = round(dest.stat().st_size / (1024**3), 2)
            logger.info("Downloaded GGUF model %s (%.1f GB)", filename, size_gb)
            return True, f"Downloaded {filename} ({size_gb} GB)"

        except Exception as e:
            dest.unlink(missing_ok=True)
            return False, f"Download failed: {e}"

    # ─── Server Lifecycle ─────────────────────────────────────────────────

    def launch(
        self,
        model_path: Optional[str] = None,
        config: Optional[LlamaCppConfig] = None,
    ) -> Tuple[bool, str]:
        """Launch llama-server with the given model.

        If ``config`` is not provided, builds one from hardware scan
        with community-optimal flags.
        """
        binary = self._find_binary()
        if not binary:
            return False, "llama-server not installed. Download it first."

        # Check if already running
        if self.is_running():
            return True, f"Already running on port {self._config.port if self._config else _DEFAULT_PORT}"

        # Resolve model
        if not model_path and not config:
            models = self.list_models()
            if models:
                model_path = models[0]["path"]
            else:
                return False, "No GGUF models available. Download one first."

        # Build config
        if not config:
            config = self._build_optimal_config(model_path or "")

        if model_path:
            config.model_path = model_path

        if not config.model_path or not Path(config.model_path).exists():
            return False, f"Model file not found: {config.model_path}"

        # Launch
        try:
            args = [binary] + config.to_args()
            logger.info("Launching llama-server: %s", " ".join(args))

            cflags = 0
            if platform.system() == "Windows":
                cflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

            # Log stderr to file for debugging startup failures
            log_path = self._base_dir / "llama-server.log"
            self._log_file = open(log_path, "w")  # noqa: SIM115

            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=self._log_file,
                creationflags=cflags,
                start_new_session=(platform.system() != "Windows"),
            )
            self._config = config
            self._start_time = time.monotonic()

            # Save config for restart recovery
            self._save_config()

            # Wait for server to be ready
            for _ in range(int(_STARTUP_WAIT / 0.5)):
                time.sleep(0.5)
                # Detect early crash — no point waiting if the process already exited
                if self._process.poll() is not None:
                    rc = self._process.returncode
                    log_path = self._base_dir / "llama-server.log"
                    hint = ""
                    if log_path.exists():
                        tail = log_path.read_text(errors="replace").strip().splitlines()
                        hint = " | " + (tail[-1] if tail else "see llama-server.log")
                    logger.error("llama-server exited immediately (code %d)%s", rc, hint)
                    return False, f"llama-server crashed on startup (exit code {rc}){hint}"
                if self._health_check(config.port):
                    logger.info(
                        "llama-server ready on port %d (PID %d)",
                        config.port, self._process.pid,
                    )
                    return True, f"Running on port {config.port}"

            return False, "Server started but not responding within timeout"

        except Exception as e:
            logger.error("Failed to launch llama-server: %s", e)
            return False, str(e)

    def stop(self) -> Tuple[bool, str]:
        """Stop the running llama-server process."""
        if not self._process:
            return True, "Not running"

        try:
            if platform.system() == "Windows":
                self._process.terminate()
            else:
                self._process.send_signal(signal.SIGTERM)

            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)

            logger.info("llama-server stopped")
            self._process = None
            if hasattr(self, "_log_file") and self._log_file:
                self._log_file.close()
                self._log_file = None
            return True, "Stopped"
        except Exception as e:
            return False, f"Error stopping: {e}"

    def is_running(self) -> bool:
        """Check if llama-server is running and healthy."""
        # Check our managed process first
        if self._process and self._process.poll() is None:
            port = self._config.port if self._config else _DEFAULT_PORT
            return self._health_check(port)

        # Check if a server is running on the default port
        return self._health_check(_DEFAULT_PORT)

    def get_status(self) -> LlamaCppStatus:
        """Get comprehensive status."""
        binary = self._find_binary()
        running = self.is_running()

        port = self._config.port if self._config else _DEFAULT_PORT
        model_loaded = ""
        if self._config:
            model_loaded = Path(self._config.model_path).stem if self._config.model_path else ""

        uptime = 0.0
        if running and self._start_time:
            uptime = time.monotonic() - self._start_time

        available_models = [m["name"] for m in self.list_models()]

        return LlamaCppStatus(
            installed=binary is not None,
            binary_path=binary or "",
            running=running,
            pid=self._process.pid if self._process and self._process.poll() is None else 0,
            port=port,
            model_loaded=model_loaded,
            models_dir=str(self._models_dir),
            available_models=available_models,
            config=self._config,
            uptime_seconds=uptime,
            version=self._get_version() if binary else "",
        )

    async def register_as_backend(self, router: Any) -> bool:
        """Register the running llama-server as a CUSTOM backend in ModelRouter.

        This bridges llama.cpp into the existing pipeline system with zero
        new client code — it's just an OpenAI-compatible endpoint.
        """
        if not self.is_running():
            return False

        port = self._config.port if self._config else _DEFAULT_PORT
        model_name = ""
        if self._config and self._config.model_path:
            model_name = Path(self._config.model_path).stem

        from services.model_router import BackendConfig, BackendType

        config = BackendConfig(
            backend_type=BackendType.CUSTOM,
            name="llamacpp",
            endpoint_url=f"http://127.0.0.1:{port}/v1",
            available_models=[model_name] if model_name else [],
            default_model=model_name,
            options={"managed_by": "LlamaCppManager"},
        )

        ok = await router.register_backend(config)
        if ok:
            logger.info("Registered llama.cpp as CUSTOM backend (model: %s)", model_name)
        return ok

    # ─── Hardware-Aware Configuration ─────────────────────────────────────

    def _build_optimal_config(self, model_path: str) -> LlamaCppConfig:
        """Build optimal llama-server flags from hardware profile.

        Uses community-validated settings from r/LocalLLaMA:
        - Flash attention always on
        - KV cache q8_0 for both K and V (free 12-38% throughput gain)
        - --fit for automatic GPU layer splitting
        - NO -b/-ub flags (community consensus: hurts more than helps)
        """
        config = LlamaCppConfig(
            model_path=model_path,
            port=_DEFAULT_PORT,
            flash_attention=_OPTIMAL_FLAGS["flash_attention"],
            kv_cache_type_k=_OPTIMAL_FLAGS["kv_cache_type_k"],
            kv_cache_type_v=_OPTIMAL_FLAGS["kv_cache_type_v"],
            fit=_OPTIMAL_FLAGS["fit"],
        )

        # Hardware-aware adjustments
        try:
            # Use a quick sync check rather than full async scan
            import psutil

            ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)

            if ram_gb >= 64:
                config.ctx_size = 32768
                config.parallel = 2
            elif ram_gb >= 32:
                config.ctx_size = 16384
                config.parallel = 1
            elif ram_gb >= 16:
                config.ctx_size = 8192
                config.parallel = 1
            else:
                config.ctx_size = 4096
                config.parallel = 1

        except Exception:
            config.ctx_size = 8192

        return config

    # ─── Internals ────────────────────────────────────────────────────────

    @staticmethod
    def _detect_cuda() -> bool:
        """Check if NVIDIA GPU with CUDA is available."""
        try:
            cflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
                creationflags=cflags,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    @staticmethod
    def _health_check(port: int = _DEFAULT_PORT) -> bool:
        """Quick health check against llama-server's /health endpoint."""
        try:
            import urllib.request
            url = f"http://127.0.0.1:{port}/health"
            resp = urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT)
            return resp.status == 200
        except Exception:
            return False

    async def _download_file(
        self,
        url: str,
        dest: Path,
        progress_callback: Optional[Any] = None,
    ) -> None:
        """Download a file with optional progress reporting."""
        async with aiohttp.ClientSession() as session, session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download failed: HTTP {resp.status}")

            total = resp.content_length or 0
            downloaded = 0

            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        pct = int(downloaded / total * 85)  # Leave 15% for extraction
                        await progress_callback(pct, f"Downloading... {downloaded // (1024*1024)} MB")

    @staticmethod
    def _extract_archive(archive_path: Path, dest_dir: Path) -> None:
        """Extract a zip or tar.gz archive."""
        name = archive_path.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
        elif name.endswith(".tar.gz") or name.endswith(".tgz"):
            import tarfile
            with tarfile.open(archive_path) as tf:
                tf.extractall(dest_dir)
        else:
            raise RuntimeError(f"Unknown archive format: {archive_path.name}")

    def _save_config(self) -> None:
        """Save current config for restart recovery."""
        if not self._config:
            return
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(self._config.to_dict(), f, indent=2)

    def _load_config(self) -> Optional[LlamaCppConfig]:
        """Load saved config."""
        if not self._config_path.exists():
            return None
        try:
            with open(self._config_path) as f:
                data = json.load(f)
            return LlamaCppConfig(**data)
        except Exception:
            return None


# ═════════════════════════════════════════════════════════════════════════════
# AUTO-PROVISION (called at end of setup wizard)
# ═════════════════════════════════════════════════════════════════════════════

# Tier → (url, filename, label) — smallest model that's usable for the tier.
_GGUF_BY_TIER: dict[str, tuple[str, str, str]] = {
    "lite": (
        "https://huggingface.co/bartowski/google_gemma-3-4b-it-GGUF/resolve/main/google_gemma-3-4b-it-Q4_K_M.gguf",
        "google_gemma-3-4b-it-Q4_K_M.gguf",
        "Gemma 3 4B",
    ),
    "standard": (
        "https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF/resolve/main/Qwen_Qwen3.5-9B-Q4_K_M.gguf",
        "Qwen_Qwen3.5-9B-Q4_K_M.gguf",
        "Qwen 3.5 9B",
    ),
    "power": (
        "https://huggingface.co/bartowski/Qwen_Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf",
        "Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf",
        "Qwen 3.5 35B-A3B MoE",
    ),
    "workstation": (
        "https://huggingface.co/bartowski/Qwen_Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf",
        "Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf",
        "Qwen 3.5 35B-A3B MoE",
    ),
}


async def auto_provision_llamacpp() -> dict[str, Any]:
    """Install llama.cpp binary + download a tier-appropriate GGUF +
    start the server + register as backend + configure Phase 1.

    Called as a background task after setup-wizard completion when no
    other LLM backend (Ollama / cloud API key) is available.  Also
    callable manually.

    Returns a summary dict with ``success``, ``model``, ``decisions``.
    """
    summary: dict[str, Any] = {"success": False, "decisions": []}

    manager = get_llamacpp_manager()

    # 1. Detect hardware tier
    tier = "standard"
    try:
        from services.device_capability import DeviceCapabilityScanner
        scanner = DeviceCapabilityScanner()
        report = await scanner.scan()
        tier = report.profile.tier.value
        summary["tier"] = tier
    except Exception as exc:
        summary["decisions"].append(f"Device scan failed ({exc}); assuming standard")

    if tier == "cloud_only":
        summary["decisions"].append("Hardware too limited for local models — skipping")
        return summary

    # 2. Pick the right GGUF for this tier
    url, filename, label = _GGUF_BY_TIER.get(tier, _GGUF_BY_TIER["standard"])
    summary["model"] = label

    # 3. Install binary if needed
    status = manager.get_status()
    if not status.installed:
        summary["decisions"].append("Installing llama.cpp binary…")
        try:
            ok, msg = await manager.download_server()
            if not ok:
                summary["decisions"].append(f"Binary install failed: {msg}")
                return summary
            summary["decisions"].append("Binary installed")
        except Exception as exc:
            summary["decisions"].append(f"Binary install error: {exc}")
            return summary

    # 4. Download GGUF if not already present
    summary["decisions"].append(f"Downloading {label} ({filename})…")
    try:
        ok, msg = await manager.download_model(url, filename)
        if not ok:
            summary["decisions"].append(f"Model download failed: {msg}")
            return summary
        summary["decisions"].append(f"Model ready: {filename}")
    except Exception as exc:
        summary["decisions"].append(f"Model download error: {exc}")
        return summary

    # 5. Start server
    model_path = str(manager._models_dir / filename)
    try:
        ok, msg = await asyncio.to_thread(manager.launch, model_path=model_path)
        if not ok:
            summary["decisions"].append(f"Server start failed: {msg}")
            return summary
        summary["decisions"].append("llama.cpp server started")
    except Exception as exc:
        summary["decisions"].append(f"Server start error: {exc}")
        return summary

    # 6. Register as backend + set Phase 1
    try:
        from admin.dependencies import get_model_router
        mr = get_model_router()
        if mr:
            await manager.register_as_backend(mr)
            model_stem = Path(filename).stem
            role_cfg = {
                "backend": "llamacpp",
                "model": model_stem,
                "temperature": 0.4,
                "max_tokens": 4000,
                "enabled": True,
            }
            await mr.set_role("initial", role_cfg)
            summary["decisions"].append(f"Phase 1 → llamacpp/{model_stem}")
    except Exception as exc:
        summary["decisions"].append(f"Pipeline config failed (server is running): {exc}")

    summary["success"] = True
    logger.info("Auto-provision complete: %s", summary)
    return summary


# ═════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ═════════════════════════════════════════════════════════════════════════════

_instance: Optional[LlamaCppManager] = None


def get_llamacpp_manager() -> LlamaCppManager:
    """Get the global LlamaCppManager singleton."""
    global _instance
    if _instance is None:
        _instance = LlamaCppManager()
    return _instance
