"""
Device Capability Scanner — Auto-detect hardware and recommend LLM configs.

Scans the local machine for:
  • GPU  — NVIDIA (nvidia-smi), AMD (rocm-smi), Apple Silicon (Metal)
  • RAM  — Total system memory
  • Disk — Available space in the models directory
  • CPU  — Core count and architecture

From these, derives a capability tier and recommends Ollama models
that the hardware can actually run well.

Usage
-----
    from services.device_capability import DeviceCapabilityScanner

    scanner = DeviceCapabilityScanner()
    profile = await scanner.scan()
    # profile.tier          -> "standard"
    # profile.recommended   -> [ModelRecommendation(...), ...]
    # profile.warnings      -> ["Low disk space — 12 GB free, models need 4–50 GB"]
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═════════════════════════════════════════════════════════════════════════════

class CapabilityTier(str, Enum):
    """Hardware capability tiers that map to model recommendations."""
    CLOUD_ONLY = "cloud_only"   # <8 GB RAM, no GPU — can't run local models well
    LITE = "lite"               # 8 GB RAM or ~4 GB VRAM — small models only
    STANDARD = "standard"       # 16 GB RAM or 8 GB VRAM — 7–8B models comfortably
    POWER = "power"             # 32 GB+ RAM or 12 GB+ VRAM — 13–70B quantised
    WORKSTATION = "workstation" # 64 GB+ RAM or 24 GB+ VRAM — large models, multi-model


class GPUVendor(str, Enum):
    NVIDIA = "nvidia"
    AMD = "amd"
    APPLE = "apple"
    INTEL = "intel"
    NONE = "none"


@dataclass
class GPUInfo:
    """Detected GPU details."""
    vendor: GPUVendor = GPUVendor.NONE
    name: str = ""
    vram_mb: int = 0
    driver_version: str = ""
    cuda_version: str = ""
    compute_capability: str = ""

    @property
    def vram_gb(self) -> float:
        return round(self.vram_mb / 1024, 1)


@dataclass
class SystemProfile:
    """Full hardware profile of the current machine."""
    # Identifiers
    os_name: str = ""
    os_version: str = ""
    arch: str = ""
    hostname: str = ""

    # CPU
    cpu_name: str = ""
    cpu_cores_physical: int = 0
    cpu_cores_logical: int = 0

    # Memory
    ram_total_mb: int = 0
    ram_available_mb: int = 0

    # GPU
    gpus: List[GPUInfo] = field(default_factory=list)

    # Disk (for model storage)
    disk_free_mb: int = 0
    disk_total_mb: int = 0
    models_dir: str = ""

    # Derived
    tier: CapabilityTier = CapabilityTier.CLOUD_ONLY

    @property
    def ram_total_gb(self) -> float:
        return round(self.ram_total_mb / 1024, 1)

    @property
    def ram_available_gb(self) -> float:
        return round(self.ram_available_mb / 1024, 1)

    @property
    def disk_free_gb(self) -> float:
        return round(self.disk_free_mb / 1024, 1)

    @property
    def best_gpu(self) -> Optional[GPUInfo]:
        if not self.gpus:
            return None
        return max(self.gpus, key=lambda g: g.vram_mb)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for API responses."""
        best = self.best_gpu
        return {
            "os": f"{self.os_name} {self.os_version}",
            "arch": self.arch,
            "cpu": self.cpu_name,
            "cpu_cores": self.cpu_cores_logical,
            "ram_total_gb": self.ram_total_gb,
            "ram_available_gb": self.ram_available_gb,
            "gpu": {
                "vendor": best.vendor.value if best else "none",
                "name": best.name if best else "",
                "vram_gb": best.vram_gb if best else 0,
                "driver": best.driver_version if best else "",
                "cuda": best.cuda_version if best else "",
            } if best else None,
            "gpu_count": len(self.gpus),
            "disk_free_gb": self.disk_free_gb,
            "tier": self.tier.value,
        }


@dataclass
class ModelRecommendation:
    """A recommended Ollama model with context."""
    model_name: str           # e.g. "llama3.1:8b"
    display_name: str         # e.g. "Llama 3.1 8B"
    size_gb: float            # Approximate download size
    purpose: str              # e.g. "General chat and reasoning"
    why: str                  # e.g. "Good fit for 16 GB RAM"
    priority: int = 0         # Higher = more recommended
    required: bool = False    # True for embedding model
    category: str = "general" # general | embedding | specialist | code
    preset: str = "custom"    # embedding | fast | better | cloud
    latency_ms: int = 0       # Estimated first-token latency (ms)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "display_name": self.display_name,
            "size_gb": self.size_gb,
            "purpose": self.purpose,
            "why": self.why,
            "priority": self.priority,
            "required": self.required,
            "category": self.category,
            "preset": self.preset,
            "latency_ms": self.latency_ms,
        }


@dataclass
class DeviceReport:
    """Complete device scan report with recommendations."""
    profile: SystemProfile
    recommendations: List[ModelRecommendation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ollama_installed: bool = False
    ollama_running: bool = False
    ollama_models: List[str] = field(default_factory=list)
    scan_duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hardware": self.profile.to_dict(),
            "recommendations": [r.to_dict() for r in self.recommendations],
            "warnings": self.warnings,
            "ollama": {
                "installed": self.ollama_installed,
                "running": self.ollama_running,
                "models": self.ollama_models,
            },
            "scan_duration_ms": self.scan_duration_ms,
        }


# ═════════════════════════════════════════════════════════════════════════════
# MODEL CATALOG — Dynamic: loads from config/recommended_models.json, falls
# back to a built-in static list if the JSON is missing or unreadable.
# ═════════════════════════════════════════════════════════════════════════════

# fmt: off
_STATIC_MODEL_CATALOG = [
    # ── Embedding (required for RAG) ──────────────────────────────────────
    {
        "model": "nomic-embed-text",
        "display": "Nomic Embed Text",
        "size_gb": 0.3,
        "purpose": "Document embedding for knowledge base search",
        "category": "embedding",
        "min_ram_gb": 4,
        "min_vram_gb": 0,
        "required": True,
        "tiers": ["lite", "standard", "power", "workstation"],
    },
    # ── Lite tier models ──────────────────────────────────────────────────
    {
        "model": "gemma3:4b",
        "display": "Gemma 3 (4B)",
        "size_gb": 3.3,
        "purpose": "Compact next-gen model — strong reasoning for its size",
        "category": "specialist",
        "min_ram_gb": 6,
        "min_vram_gb": 3,
        "tiers": ["lite", "standard", "power", "workstation"],
    },
    {
        "model": "phi4-mini",
        "display": "Phi-4 Mini (3.8B)",
        "size_gb": 2.5,
        "purpose": "Fast, lightweight reasoning — chunk enrichment, classification",
        "category": "specialist",
        "min_ram_gb": 6,
        "min_vram_gb": 3,
        "tiers": ["lite", "standard", "power", "workstation"],
    },
    # ── Standard tier models ──────────────────────────────────────────────
    {
        "model": "qwen3.5:9b",
        "display": "Qwen 3.5 (9B, Dense)",
        "size_gb": 6.6,
        "purpose": "Latest Qwen dense model — strong reasoning, tool-calling, multilingual",
        "category": "general",
        "min_ram_gb": 12,
        "min_vram_gb": 8,
        "tiers": ["standard", "power", "workstation"],
    },
    {
        "model": "qwen3.5:35b-a3b",
        "display": "Qwen 3.5 35B-A3B (MoE, 3B active)",
        "size_gb": 24.0,
        "purpose": "MoE breakthrough — 35B quality at 3B speed",
        "category": "general",
        "min_ram_gb": 32,
        "min_vram_gb": 16,
        "tiers": ["power", "workstation"],
    },
    {
        "model": "llama4:scout",
        "display": "Llama 4 Scout (17B active, MoE)",
        "size_gb": 67.0,
        "purpose": "Frontier MoE — excellent reasoning with efficient inference",
        "category": "general",
        "min_ram_gb": 72,
        "min_vram_gb": 48,
        "tiers": ["workstation"],
    },
    # ── Power tier models ─────────────────────────────────────────────────
    {
        "model": "qwen3.5:27b",
        "display": "Qwen 3.5 (27B, Dense)",
        "size_gb": 17.0,
        "purpose": "Large dense model — excellent critique and synthesis quality",
        "category": "general",
        "min_ram_gb": 28,
        "min_vram_gb": 16,
        "tiers": ["power", "workstation"],
    },
    {
        "model": "deepseek-r1:14b",
        "display": "DeepSeek-R1 (14B)",
        "size_gb": 9.0,
        "purpose": "Strong reasoning and chain-of-thought — good for critique pipeline stage",
        "category": "specialist",
        "min_ram_gb": 20,
        "min_vram_gb": 10,
        "tiers": ["power", "workstation"],
    },
    # ── Workstation tier models ───────────────────────────────────────────
    {
        "model": "qwen3.5:122b-a10b",
        "display": "Qwen 3.5 122B-A10B (MoE, 10B active)",
        "size_gb": 81.0,
        "purpose": "Frontier MoE — near-cloud quality with 10B active params",
        "category": "general",
        "min_ram_gb": 96,
        "min_vram_gb": 48,
        "tiers": ["workstation"],
    },
]
# fmt: on


def _load_dynamic_catalog() -> List[Dict[str, Any]]:
    """Load the model catalog from recommended_models.json, merging with
    the static fallback so that new JSON entries are immediately available
    while old static entries still work if the JSON is absent.
    """
    try:
        from services.model_discovery import get_model_catalog
        dynamic = get_model_catalog()
        if dynamic:
            # Merge: dynamic entries take precedence by model name
            seen = {e["model"] for e in dynamic}
            merged = list(dynamic)
            for static_entry in _STATIC_MODEL_CATALOG:
                if static_entry["model"] not in seen:
                    merged.append(static_entry)
            return merged
    except Exception as exc:
        logger.debug("Dynamic catalog unavailable, using static: %s", exc)
    return list(_STATIC_MODEL_CATALOG)


# Public API — always use these instead of _STATIC_MODEL_CATALOG directly
MODEL_CATALOG = _STATIC_MODEL_CATALOG  # initial value for import-time references

def get_effective_catalog() -> List[Dict[str, Any]]:
    """Return the merged model catalog (dynamic + static fallback).

    Call this at runtime rather than using MODEL_CATALOG directly so
    that newly added models in recommended_models.json are picked up.
    """
    return _load_dynamic_catalog()


def get_allowed_model_names() -> set[str]:
    """Return the set of all model names from the effective catalog.

    Replaces the old static ALLOWED_MODELS constant with a dynamic
    version that includes models from recommended_models.json.
    """
    return {entry["model"] for entry in get_effective_catalog()}


ALLOWED_MODELS = {entry["model"] for entry in MODEL_CATALOG}  # backward compat


# ═════════════════════════════════════════════════════════════════════════════
# SCANNER
# ═════════════════════════════════════════════════════════════════════════════

class DeviceCapabilityScanner:
    """Scans the local machine and produces a DeviceReport."""

    # ── Public API ────────────────────────────────────────────────────────

    _SCAN_TIMEOUT = 15  # seconds — abort and return partial results

    async def scan(self) -> DeviceReport:
        """Run all hardware detection and return a complete report.

        Applies a global timeout so the endpoint never hangs if a
        subprocess stalls.  Returns partial results on timeout.
        """
        try:
            return await asyncio.wait_for(
                self._scan_inner(), timeout=self._SCAN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Device scan timed out after %ss — returning partial results", self._SCAN_TIMEOUT)
            # Return whatever we have so far
            return DeviceReport(
                profile=self._partial_profile or SystemProfile(),
                warnings=["Hardware scan timed out — some results may be missing."],
            )

    _partial_profile: Optional[SystemProfile] = None

    async def _scan_inner(self) -> DeviceReport:
        """Actual scan logic, called inside a timeout wrapper."""
        import time
        t0 = time.monotonic()

        profile = SystemProfile()
        self._partial_profile = profile

        # OS / CPU basics (fast, sync)
        self._detect_os(profile)
        self._detect_cpu(profile)
        self._detect_ram(profile)
        self._detect_disk(profile)

        # GPU + Ollama in parallel (both may do slow subprocess calls)
        await asyncio.gather(
            self._detect_gpus(profile),
            self._check_ollama_async(),
            return_exceptions=True,
        )

        # Derive capability tier
        profile.tier = self._compute_tier(profile)

        # Pull Ollama results from the async helper
        ollama_installed = getattr(self, '_ollama_installed', False)
        ollama_running = getattr(self, '_ollama_running', False)
        ollama_models = getattr(self, '_ollama_models', [])

        # Generate recommendations
        recommendations = self._generate_recommendations(
            profile, ollama_models, ollama_installed
        )
        warnings = self._generate_warnings(profile, ollama_installed)

        elapsed = int((time.monotonic() - t0) * 1000)

        return DeviceReport(
            profile=profile,
            recommendations=recommendations,
            warnings=warnings,
            ollama_installed=ollama_installed,
            ollama_running=ollama_running,
            ollama_models=ollama_models,
            scan_duration_ms=elapsed,
        )

    async def _check_ollama_async(self) -> None:
        """Run the synchronous Ollama check in a thread to avoid blocking the event loop."""
        try:
            installed, running, models = await asyncio.to_thread(self._check_ollama)
            self._ollama_installed = installed
            self._ollama_running = running
            self._ollama_models = models
        except Exception as e:
            logger.debug("Ollama check failed: %s", e)
            self._ollama_installed = False
            self._ollama_running = False
            self._ollama_models = []

    # ── OS detection ──────────────────────────────────────────────────────

    @staticmethod
    def _detect_os(profile: SystemProfile) -> None:
        profile.os_name = platform.system()
        profile.os_version = platform.version()
        profile.arch = platform.machine()
        profile.hostname = platform.node()

    # ── CPU detection ─────────────────────────────────────────────────────

    @staticmethod
    def _detect_cpu(profile: SystemProfile) -> None:
        profile.cpu_cores_logical = os.cpu_count() or 1

        # Physical cores (platform-dependent)
        try:
            import psutil
            profile.cpu_cores_physical = psutil.cpu_count(logical=False) or profile.cpu_cores_logical
        except ImportError:
            profile.cpu_cores_physical = profile.cpu_cores_logical

        # CPU name
        system = platform.system()
        if system == "Windows":
            profile.cpu_name = platform.processor() or "Unknown CPU"
            # Windows platform.processor() is often a generic arch string;
            # try the registry for something better
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                )
                profile.cpu_name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
                winreg.CloseKey(key)
            except Exception as e:
                logger.warning("_detect_cpu: suppressed %s", e)
        elif system == "Darwin":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    profile.cpu_name = result.stdout.strip()
            except Exception:
                profile.cpu_name = f"{platform.machine()} (Apple Silicon)" if platform.machine() == "arm64" else "Unknown CPU"
        else:
            try:
                with open("/proc/cpuinfo", "r") as f:
                    for line in f:
                        if line.startswith("model name"):
                            profile.cpu_name = line.split(":")[1].strip()
                            break
            except Exception:
                profile.cpu_name = "Unknown CPU"

    # ── RAM detection ─────────────────────────────────────────────────────

    @staticmethod
    def _detect_ram(profile: SystemProfile) -> None:
        try:
            import psutil
            mem = psutil.virtual_memory()
            profile.ram_total_mb = int(mem.total / (1024 * 1024))
            profile.ram_available_mb = int(mem.available / (1024 * 1024))
            return
        except ImportError:
            pass

        # Fallback: platform-specific
        system = platform.system()
        if system == "Windows":
            try:
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                profile.ram_total_mb = int(stat.ullTotalPhys / (1024 * 1024))
                profile.ram_available_mb = int(stat.ullAvailPhys / (1024 * 1024))
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        elif system == "Darwin":
            try:
                result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    profile.ram_total_mb = int(result.stdout.strip()) // (1024 * 1024)
                # Available memory on macOS: vm_stat
                result2 = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
                if result2.returncode == 0:
                    pages_free = 0
                    for line in result2.stdout.splitlines():
                        if "Pages free" in line or "Pages inactive" in line:
                            m = re.search(r"(\d+)", line.split(":")[1])
                            if m:
                                pages_free += int(m.group(1))
                    profile.ram_available_mb = (pages_free * 4096) // (1024 * 1024)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        else:
            try:
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal"):
                            profile.ram_total_mb = int(line.split()[1]) // 1024
                        elif line.startswith("MemAvailable"):
                            profile.ram_available_mb = int(line.split()[1]) // 1024
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    # ── Disk detection ────────────────────────────────────────────────────

    @staticmethod
    def _detect_disk(profile: SystemProfile) -> None:
        # Check the Ollama models directory, or fall back to home drive
        system = platform.system()
        if system == "Windows":
            models_dir = Path(os.environ.get("USERPROFILE", "")) / ".ollama" / "models"
        elif system == "Darwin":
            models_dir = Path.home() / ".ollama" / "models"
        else:
            models_dir = Path.home() / ".ollama" / "models"

        # Use the actual models dir if it exists, otherwise check the parent drive
        check_path = models_dir if models_dir.exists() else Path.home()

        try:
            usage = shutil.disk_usage(str(check_path))
            profile.disk_free_mb = int(usage.free / (1024 * 1024))
            profile.disk_total_mb = int(usage.total / (1024 * 1024))
        except Exception as e:
            logger.warning("_detect_disk: suppressed %s", e)

        profile.models_dir = str(models_dir)

    # ── GPU detection ─────────────────────────────────────────────────────

    async def _detect_gpus(self, profile: SystemProfile) -> None:
        system = platform.system()

        # Run all GPU detections in parallel to cut wall-clock time
        coros = [self._detect_nvidia(), self._detect_amd()]
        if system == "Windows":
            coros.append(self._detect_intel())

        results = await asyncio.gather(*coros, return_exceptions=True)

        for r in results:
            if isinstance(r, list):
                profile.gpus.extend(r)
            elif isinstance(r, Exception):
                logger.debug("GPU detection error: %s", r)

        # Apple Silicon — unified memory arch (sync, fast)
        if system == "Darwin" and platform.machine() == "arm64":
            apple_gpu = self._detect_apple_silicon(profile)
            if apple_gpu:
                profile.gpus.append(apple_gpu)

    async def _detect_nvidia(self) -> List[GPUInfo]:
        """Detect NVIDIA GPUs via nvidia-smi.

        Uses ``subprocess.run`` in a thread instead of
        ``asyncio.create_subprocess_exec`` so that detection works
        regardless of the active event-loop type (ProactorEventLoop
        vs SelectorEventLoop on Windows).
        """
        def _run() -> List[GPUInfo]:
            gpus: List[GPUInfo] = []
            _cflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            try:
                result = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=name,memory.total,driver_version",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_cflags,
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 3:
                            gpu = GPUInfo(
                                vendor=GPUVendor.NVIDIA,
                                name=parts[0],
                                vram_mb=int(float(parts[1])),
                                driver_version=parts[2],
                            )
                            # Try to get compute capability
                            try:
                                cc = subprocess.run(
                                    ["nvidia-smi",
                                     "--query-gpu=compute_cap",
                                     "--format=csv,noheader"],
                                    capture_output=True, text=True, timeout=3,
                                    creationflags=_cflags,
                                )
                                if cc.returncode == 0 and cc.stdout:
                                    gpu.compute_capability = cc.stdout.strip().splitlines()[0].strip()
                            except Exception as e:
                                logger.warning("operation: suppressed %s", e)
                            gpus.append(gpu)
            except FileNotFoundError:
                pass  # nvidia-smi not installed
            except Exception as e:
                logger.debug("NVIDIA detection failed: %s", e)
            return gpus

        return await asyncio.to_thread(_run)

    async def _detect_amd(self) -> List[GPUInfo]:
        """Detect AMD GPUs via rocm-smi (Linux) or basic WMI (Windows)."""
        def _run() -> List[GPUInfo]:
            gpus: List[GPUInfo] = []
            system = platform.system()

            _cflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            if system == "Linux":
                try:
                    result = subprocess.run(
                        ["rocm-smi", "--showproductname", "--showmeminfo", "vram"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout:
                        output = result.stdout
                        name_match = re.search(r"Card.*?:\s*(.+)", output)
                        vram_match = re.search(r"Total.*?:\s*(\d+)", output)
                        if name_match:
                            gpus.append(GPUInfo(
                                vendor=GPUVendor.AMD,
                                name=name_match.group(1).strip(),
                                vram_mb=int(vram_match.group(1)) if vram_match else 0,
                            ))
                except FileNotFoundError:
                    pass
                except Exception as e:
                    logger.debug("AMD detection (rocm-smi) failed: %s", e)

            elif system == "Windows":
                try:
                    result = subprocess.run(
                        ["wmic", "path", "win32_VideoController", "get",
                         "Name,AdapterRAM", "/format:csv"],
                        capture_output=True, text=True, timeout=5,
                        creationflags=_cflags,
                    )
                    if result.returncode == 0 and result.stdout:
                        for line in result.stdout.splitlines():
                            if "AMD" in line.upper() or "RADEON" in line.upper():
                                parts = [p.strip() for p in line.split(",")]
                                name = ""
                                vram = 0
                                for p in parts:
                                    if "AMD" in p.upper() or "RADEON" in p.upper():
                                        name = p
                                    elif p.isdigit() and int(p) > 1000000:
                                        vram = int(p) // (1024 * 1024)
                                if name:
                                    gpus.append(GPUInfo(vendor=GPUVendor.AMD, name=name, vram_mb=vram))
                except Exception as e:
                    logger.debug("AMD detection (wmic) failed: %s", e)

            return gpus

        return await asyncio.to_thread(_run)

    @staticmethod
    def _detect_apple_silicon(profile: SystemProfile) -> Optional[GPUInfo]:
        """Apple Silicon uses unified memory — GPU VRAM = shared with RAM."""
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                output = result.stdout
                name_match = re.search(r"Chipset Model:\s*(.+)", output)
                # Apple Silicon shares system RAM — report the full amount
                return GPUInfo(
                    vendor=GPUVendor.APPLE,
                    name=name_match.group(1).strip() if name_match else "Apple GPU",
                    vram_mb=profile.ram_total_mb,  # unified memory
                )
        except Exception as e:
            logger.debug("Apple Silicon detection failed: %s", e)
        return None

    async def _detect_intel(self) -> List[GPUInfo]:
        """Basic Intel Arc detection via WMIC on Windows."""
        if platform.system() != "Windows":
            return []

        def _run() -> List[GPUInfo]:
            gpus: List[GPUInfo] = []
            _cflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            try:
                result = subprocess.run(
                    ["wmic", "path", "win32_VideoController", "get",
                     "Name,AdapterRAM", "/format:csv"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_cflags,
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.splitlines():
                        if "INTEL" in line.upper() and "ARC" in line.upper():
                            parts = [p.strip() for p in line.split(",")]
                            name = ""
                            vram = 0
                            for p in parts:
                                if "ARC" in p.upper():
                                    name = p
                                elif p.isdigit() and int(p) > 1000000:
                                    vram = int(p) // (1024 * 1024)
                            if name:
                                gpus.append(GPUInfo(vendor=GPUVendor.INTEL, name=name, vram_mb=vram))
            except Exception as e:
                logger.debug("Intel Arc detection failed: %s", e)
            return gpus

        return await asyncio.to_thread(_run)

    # ── Ollama check ──────────────────────────────────────────────────────

    @staticmethod
    def _check_ollama() -> tuple[bool, bool, List[str]]:
        """Check Ollama installation, running status, and installed models."""
        from services.system_tools import SystemTools

        status = SystemTools.get_ollama_status()
        return (
            status["installed"],
            status["running"],
            status.get("models", []),
        )

    # ── Tier computation ──────────────────────────────────────────────────

    @staticmethod
    def _compute_tier(profile: SystemProfile) -> CapabilityTier:
        """Determine capability tier from hardware profile.

        Uses the *best* of RAM-based or VRAM-based tier.
        """
        ram_gb = profile.ram_total_gb
        best_gpu = profile.best_gpu
        vram_gb = best_gpu.vram_gb if best_gpu else 0

        # Apple Silicon uses unified memory — GPU and CPU share RAM,
        # so the effective "VRAM" is ~75% of total RAM
        if best_gpu and best_gpu.vendor == GPUVendor.APPLE:
            vram_gb = ram_gb * 0.75

        # Compute tier from RAM
        if ram_gb >= 64:
            ram_tier = CapabilityTier.WORKSTATION
        elif ram_gb >= 32:
            ram_tier = CapabilityTier.POWER
        elif ram_gb >= 16:
            ram_tier = CapabilityTier.STANDARD
        elif ram_gb >= 8:
            ram_tier = CapabilityTier.LITE
        else:
            ram_tier = CapabilityTier.CLOUD_ONLY

        # Compute tier from VRAM
        if vram_gb >= 24:
            vram_tier = CapabilityTier.WORKSTATION
        elif vram_gb >= 12:
            vram_tier = CapabilityTier.POWER
        elif vram_gb >= 8:
            vram_tier = CapabilityTier.STANDARD
        elif vram_gb >= 4:
            vram_tier = CapabilityTier.LITE
        else:
            vram_tier = CapabilityTier.CLOUD_ONLY

        # Use the best of the two
        tier_order = [
            CapabilityTier.CLOUD_ONLY,
            CapabilityTier.LITE,
            CapabilityTier.STANDARD,
            CapabilityTier.POWER,
            CapabilityTier.WORKSTATION,
        ]
        return max(ram_tier, vram_tier, key=lambda t: tier_order.index(t))

    # ── Recommendation engine ─────────────────────────────────────────────

    @staticmethod
    def _generate_recommendations(
        profile: SystemProfile,
        installed_models: List[str],
        ollama_installed: bool,
    ) -> List[ModelRecommendation]:
        """Generate model recommendations based on hardware + what's already installed."""
        recs: List[ModelRecommendation] = []
        tier = profile.tier
        tier_value = tier.value

        if tier == CapabilityTier.CLOUD_ONLY:
            # Can't run local models effectively — recommend cloud only
            return [
                ModelRecommendation(
                    model_name="cloud_only",
                    display_name="Cloud API Recommended",
                    size_gb=0,
                    purpose="Your hardware is best suited for cloud LLM APIs (OpenAI, Anthropic)",
                    why=f"With {profile.ram_total_gb} GB RAM and no discrete GPU, local models would run too slowly.",
                    priority=100,
                    category="general",
                    preset="cloud",
                    latency_ms=0,
                ),
            ]

        def estimate_latency_ms(size_gb: float) -> int:
            base_by_tier = {
                CapabilityTier.LITE: 1400,
                CapabilityTier.STANDARD: 950,
                CapabilityTier.POWER: 750,
                CapabilityTier.WORKSTATION: 550,
            }
            base = base_by_tier.get(tier, 1200)
            latency = int(base + (size_gb * 60))
            return max(300, min(latency, 5000))

        def build_rec(entry: Dict[str, Any], preset: str) -> ModelRecommendation:
            already_installed = any(
                entry["model"].split(":")[0] in m for m in installed_models
            )

            best_gpu = profile.best_gpu
            if best_gpu and best_gpu.vram_gb > 0:
                hw_desc = f"{best_gpu.vram_gb} GB VRAM ({best_gpu.name})"
            else:
                hw_desc = f"{profile.ram_total_gb} GB RAM"

            if already_installed:
                why = f"Already installed — {hw_desc} handles this well"
            elif entry.get("required"):
                why = f"Required for knowledge base search — small ({entry['size_gb']} GB)"
            else:
                why = f"Good fit for {hw_desc} — {entry['size_gb']} GB download"

            priority = 80 if preset == "better" else 90
            if entry.get("required"):
                priority = 100
            if already_installed:
                priority -= 15

            return ModelRecommendation(
                model_name=entry["model"],
                display_name=entry["display"],
                size_gb=entry["size_gb"],
                purpose=entry["purpose"],
                why=why,
                priority=priority,
                required=entry.get("required", False),
                category=entry["category"],
                preset=preset,
                latency_ms=estimate_latency_ms(entry["size_gb"]),
            )

        # Required embedding model
        _catalog = get_effective_catalog()
        embedding_entry = next((e for e in _catalog if e.get("required")), None)
        if embedding_entry and tier_value in embedding_entry["tiers"]:
            recs.append(build_rec(embedding_entry, "embedding"))

        # Pick two general models: "fast" (smallest) and "better" (largest)
        general_models = [
            e for e in _catalog
            if e.get("category") == "general" and tier_value in e["tiers"]
        ]
        general_models.sort(key=lambda e: e["size_gb"])

        if general_models:
            fast_entry = general_models[0]
            recs.append(build_rec(fast_entry, "fast"))

            better_entry = general_models[-1]
            if better_entry["model"] != fast_entry["model"]:
                recs.append(build_rec(better_entry, "better"))

        # Sort: required first, then by priority descending
        recs.sort(key=lambda r: (-r.required, -r.priority))

        return recs

    # ── Warnings ──────────────────────────────────────────────────────────

    @staticmethod
    def _generate_warnings(profile: SystemProfile, ollama_installed: bool) -> List[str]:
        """Generate user-friendly warnings about potential issues."""
        warnings = []

        if profile.ram_total_gb < 8:
            warnings.append(
                f"Low system memory ({profile.ram_total_gb} GB). "
                f"Local models need at least 8 GB RAM. Consider using cloud APIs instead."
            )

        if profile.disk_free_gb < 10:
            warnings.append(
                f"Low disk space ({profile.disk_free_gb} GB free). "
                f"Ollama models typically need 2–50 GB each. Free up space or change the models directory."
            )

        if not ollama_installed:
            warnings.append(
                "Ollama is not installed. It's required for local AI models. "
                "We can install it for you during setup."
            )

        best_gpu = profile.best_gpu
        if not best_gpu or best_gpu.vendor == GPUVendor.NONE:
            if profile.ram_total_gb >= 16:
                warnings.append(
                    "No discrete GPU detected. Models will run on CPU, which works "
                    "but is 3–10× slower than GPU inference. Still usable for 7B models."
                )
            elif profile.ram_total_gb >= 8:
                warnings.append(
                    "No discrete GPU detected and only 8 GB RAM. "
                    "Expect slow inference with small models. Cloud APIs recommended for complex tasks."
                )

        return warnings

    # ── Resource preflight before model pull ──────────────────────────────

    @staticmethod
    def model_pull_preflight(
        models: List[str],
        profile: SystemProfile,
        ollama_installed: bool,
        ollama_running: bool,
    ) -> Dict[str, Any]:
        """Run resource checks before starting a model pull.

        Returns a dict with per-check results and an overall ``ok`` flag.
        This should be called *before* SSE streaming begins so the UI can
        show the user a clear remediation message instead of failing midway.
        """
        checks: List[Dict[str, Any]] = []

        # ── 1. Disk space ────────────────────────────────────────────────
        _catalog = get_effective_catalog()
        total_size_gb = 0.0
        for model_name in models:
            entry = next((e for e in _catalog if e["model"] == model_name), None)
            if entry:
                total_size_gb += entry["size_gb"]
            else:
                total_size_gb += 4.0  # conservative fallback

        headroom_gb = total_size_gb * 1.3  # 30% headroom for extraction
        disk_ok = profile.disk_free_gb >= headroom_gb
        checks.append({
            "name": "Disk space",
            "ok": disk_ok,
            "detail": (
                f"{profile.disk_free_gb:.1f} GB free, need ~{headroom_gb:.1f} GB"
                if not disk_ok
                else f"{profile.disk_free_gb:.1f} GB free — sufficient"
            ),
            "remediation": (
                f"Free at least {headroom_gb - profile.disk_free_gb:.1f} GB of disk space, "
                f"or move the Ollama models directory to a larger drive."
            ) if not disk_ok else "",
        })

        # ── 2. RAM / VRAM ────────────────────────────────────────────────
        # For the largest model requested, check RAM fits
        largest_model_gb = 0.0
        largest_model_name = ""
        for model_name in models:
            entry = next((e for e in _catalog if e["model"] == model_name), None)
            if entry and entry["size_gb"] > largest_model_gb:
                largest_model_gb = entry["size_gb"]
                largest_model_name = entry.get("display", model_name)

        # Model needs ~1.2x its size in RAM when loaded
        needed_ram_gb = largest_model_gb * 1.2 + 2  # +2 GB for system overhead
        best_gpu = profile.best_gpu
        vram_gb = best_gpu.vram_gb if best_gpu and best_gpu.vendor != GPUVendor.NONE else 0

        if vram_gb >= largest_model_gb:
            ram_ok = True
            ram_detail = f"{largest_model_name} fits in GPU ({vram_gb:.0f} GB VRAM)"
        elif profile.ram_available_gb >= needed_ram_gb:
            ram_ok = True
            ram_detail = f"{largest_model_name} fits in RAM ({profile.ram_available_gb:.0f} GB available)"
        else:
            ram_ok = False
            ram_detail = (
                f"{largest_model_name} needs ~{needed_ram_gb:.0f} GB, "
                f"available: {profile.ram_available_gb:.0f} GB RAM"
                + (f" + {vram_gb:.0f} GB VRAM" if vram_gb else "")
            )
        checks.append({
            "name": "Memory",
            "ok": ram_ok,
            "detail": ram_detail,
            "remediation": (
                "Close other applications to free memory, or choose a smaller model."
            ) if not ram_ok else "",
        })

        # ── 3. Ollama installed / running ────────────────────────────────
        ollama_ok = ollama_installed and ollama_running
        checks.append({
            "name": "Ollama",
            "ok": ollama_ok,
            "detail": (
                "Running" if ollama_ok
                else "Not installed" if not ollama_installed
                else "Installed but not running"
            ),
            "remediation": (
                "Install Ollama from the button above." if not ollama_installed
                else "Click 'Start Ollama' above to start the server."
            ) if not ollama_ok else "",
        })

        # ── 4. Network reachability (Ollama registry) ────────────────────
        network_ok = False
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://registry.ollama.ai/",
                method="HEAD",
            )
            req.add_header("User-Agent", "MKA-preflight/1.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                network_ok = resp.status < 400
        except Exception as e:
            logger.warning("operation: suppressed %s", e)
        checks.append({
            "name": "Network",
            "ok": network_ok,
            "detail": "Ollama registry reachable" if network_ok else "Cannot reach Ollama registry",
            "remediation": (
                "Check your internet connection and firewall settings. "
                "The pull needs access to registry.ollama.ai."
            ) if not network_ok else "",
        })

        # ── 5. Permissions (models dir writable) ──────────────────────────
        models_dir = Path(profile.models_dir) if profile.models_dir else None
        perm_ok = False
        if models_dir:
            try:
                models_dir.mkdir(parents=True, exist_ok=True)
                test_file = models_dir / ".mka_write_test"
                test_file.write_text("ok")
                test_file.unlink()
                perm_ok = True
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        checks.append({
            "name": "Permissions",
            "ok": perm_ok,
            "detail": (
                "Models directory writable" if perm_ok
                else f"Cannot write to {models_dir or 'unknown path'}"
            ),
            "remediation": (
                f"Check permissions on {models_dir}. On Windows, try running as Administrator."
            ) if not perm_ok else "",
        })

        all_ok = all(c["ok"] for c in checks)
        blocking = [c for c in checks if not c["ok"]]
        return {
            "ok": all_ok,
            "checks": checks,
            "blocking_count": len(blocking),
            "models_requested": models,
            "total_download_gb": round(total_size_gb, 1),
        }
