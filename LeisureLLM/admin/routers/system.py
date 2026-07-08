"""System router — Ollama, llama.cpp, Bot Control, Backup/Restore, Sweeps, Seed."""

import asyncio
import io
import logging
import os
import zipfile

from core.system_explainer import (
    explain_health_summary,
    explain_jobs,
    explain_mode,
    explain_pipeline,
    explain_storage,
    explain_workflows,
)
from core.system_snapshot import build_system_doctor, build_system_snapshot
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from admin.dependencies import (
    LEISURELLM_DIR,
    get_bot,
    get_db,
    require_admin,
)
from admin.performance import get_cached_ollama_status, invalidate_cache

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["system"], dependencies=[Depends(require_admin)])


def _system_fail_soft(error: str, message: str, **extra):
    payload = {"success": False, "error": error, "message": message}
    payload.update(extra)
    return payload


def _sections_for_topic(topic: str, snapshot):
    mapping = {
        "mode": [explain_mode(snapshot)],
        "jobs": [explain_jobs(snapshot)],
        "pipeline": [explain_pipeline(snapshot)],
        "workflows": [explain_workflows(snapshot)],
        "storage": [explain_storage(snapshot)],
        "health": [explain_health_summary(snapshot)],
        "all": [
            explain_mode(snapshot),
            explain_workflows(snapshot),
            explain_jobs(snapshot),
            explain_pipeline(snapshot),
            explain_storage(snapshot),
            explain_health_summary(snapshot),
        ],
    }
    return mapping[topic]


@router.get("/api/v1/system/manifest")
async def api_system_manifest():
    snapshot = build_system_snapshot()
    return {"success": True, "manifest": snapshot.to_dict()}


@router.get("/api/v1/system/explain")
async def api_system_explain(topic: str = "all"):
    normalized_topic = (topic or "all").strip().lower()
    if normalized_topic not in {"mode", "jobs", "pipeline", "workflows", "storage", "health", "all"}:
        return _system_fail_soft(
            "invalid_topic",
            "Unsupported explanation topic.",
            topic=normalized_topic,
        )
    snapshot = build_system_snapshot()
    sections = _sections_for_topic(normalized_topic, snapshot)
    return {
        "success": True,
        "topic": normalized_topic,
        "sections": [{"title": section.title, "lines": section.lines} for section in sections],
    }


@router.get("/api/v1/system/doctor")
async def api_system_doctor():
    snapshot = build_system_snapshot()
    doctor = build_system_doctor(snapshot)
    return {
        "success": True,
        "doctor": {
            "summary": doctor.summary,
            "healthy": doctor.healthy,
            "status_counts": doctor.status_counts,
            "database": {
                "path": doctor.database.path,
                "exists": doctor.database.exists,
                "integrity": doctor.database.integrity,
                "table_count": doctor.database.table_count,
                "schema_versions_present": doctor.database.schema_versions_present,
                "pending_versions": doctor.database.pending_versions,
                "latest_applied_version": doctor.database.latest_applied_version,
                "latest_discovered_version": doctor.database.latest_discovered_version,
                "error": doctor.database.error,
            },
            "checks": [
                {
                    "status": check.status,
                    "code": check.code,
                    "title": check.title,
                    "detail": check.detail,
                    "source": check.source,
                }
                for check in doctor.checks
            ],
        },
    }


# =============================================================================
# Ollama
# =============================================================================

@router.get("/api/v1/ollama/status")
async def api_ollama_status(request: Request):
    try:
        refresh = request.query_params.get("refresh", "").lower() in {"1", "true", "yes"}
        if refresh:
            invalidate_cache("ollama_status")
        status = get_cached_ollama_status()
        return {"success": True, **status}
    except Exception as exc:
        logger.error("Ollama status failed: %s", exc, exc_info=True)
        return _system_fail_soft(
            "ollama_status_unavailable",
            "Could not check Ollama status right now.",
            installed=False,
            running=False,
            models=[],
        )


@router.post("/api/v1/ollama/start")
async def api_start_ollama():
    """Start the Ollama server if it's installed but not running."""
    from services.system_tools import SystemTools

    try:
        exe = SystemTools._ollama_executable()
        if not exe:
            return _system_fail_soft("ollama_not_installed", "Ollama is not installed")
        ok = SystemTools.ensure_ollama_running(exe)
        if ok:
            invalidate_cache("ollama_status")
            status = get_cached_ollama_status(ttl_seconds=2.0)
            return {"success": True, "message": "Ollama server is running", "status": status}
        return _system_fail_soft("ollama_start_failed", "Could not start Ollama server. Try launching it manually.")
    except Exception as exc:
        logger.error("Ollama start failed: %s", exc, exc_info=True)
        return _system_fail_soft("ollama_start_failed", "Could not start Ollama right now. Try again.")


@router.post("/api/v1/ollama/install")
async def api_install_ollama():
    from services.system_tools import SystemTools
    logger.info("Installation requested via GUI")
    try:
        success, message = await SystemTools.install_ollama_windows()
        return {"success": success, "message": message}
    except Exception as exc:
        logger.error("Ollama install failed: %s", exc, exc_info=True)
        return _system_fail_soft(
            "ollama_install_failed",
            "Could not launch the Ollama installer. Try again or download it directly from ollama.com.",
        )


@router.post("/api/v1/ollama/remove")
async def api_remove_model(request: Request):
    """Remove an installed Ollama model."""
    import subprocess as _sp

    from services.system_tools import SystemTools

    body = await request.json()
    model_name = (body.get("model") or "").strip()
    if not model_name:
        return {"success": False, "message": "No model name provided"}

    exe = SystemTools._ollama_executable()
    if not exe:
        return {"success": False, "message": "Ollama is not installed"}

    try:
        proc = _sp.run(
            [exe, "rm", model_name],
            capture_output=True, text=True, timeout=60,
            creationflags=_sp.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode == 0:
            invalidate_cache("ollama_status")
            return {"success": True, "message": f"Removed {model_name}"}
        else:
            return {"success": False, "message": proc.stderr or f"Failed to remove {model_name}"}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/ollama/pull")
async def api_pull_model(request: Request):
    """Pull an Ollama model by name.  Runs ``ollama pull <model>`` and
    streams progress back as newline-delimited JSON (Server-Sent Events).

    Uses a threaded ``subprocess.Popen`` bridged to an ``asyncio.Queue``
    so that streaming works regardless of the Windows event-loop type.
    """
    import json as _json
    import subprocess as _sp
    import threading

    from services.system_tools import SystemTools
    from starlette.responses import StreamingResponse

    body = await request.json()
    model_name = (body.get("model") or "").strip()
    if not model_name:
        return {"success": False, "message": "No model name provided"}

    exe = SystemTools._ollama_executable()
    if not exe:
        return {"success": False, "message": "Ollama is not installed"}

    # Make sure server is running first
    if not SystemTools.ensure_ollama_running(exe):
        return {"success": False, "message": "Ollama is installed but could not be started. Start Ollama and try again."}

    async def _stream():
        import re as _re

        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _read_cr_lines(stdout):
            """Read ollama output splitting on both \\r and \\n."""
            buf = b""
            while True:
                chunk = stdout.read(256)
                if not chunk:
                    if buf.strip():
                        yield buf.decode("utf-8", errors="replace")
                    break
                buf += chunk
                while b"\r" in buf or b"\n" in buf:
                    r_pos = buf.find(b"\r")
                    n_pos = buf.find(b"\n")
                    if r_pos == -1:
                        pos = n_pos
                    elif n_pos == -1:
                        pos = r_pos
                    else:
                        pos = min(r_pos, n_pos)
                    line = buf[:pos].decode("utf-8", errors="replace")
                    if pos + 1 < len(buf) and buf[pos:pos+2] == b"\r\n":
                        buf = buf[pos+2:]
                    else:
                        buf = buf[pos+1:]
                    if line.strip():
                        yield line

        def _reader():
            try:
                proc = _sp.Popen(
                    [exe, "pull", model_name],
                    stdout=_sp.PIPE, stderr=_sp.STDOUT,
                    creationflags=_sp.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                for line in _read_cr_lines(proc.stdout):
                    text = line.strip()
                    evt: dict = {"progress": text}
                    pct_m = _re.search(r'(\d+)%', text)
                    if pct_m:
                        evt["percent"] = int(pct_m.group(1))
                    size_m = _re.search(r'([\d.]+)\s*(GB|MB|KB)\s*/\s*([\d.]+)\s*(GB|MB|KB)', text)
                    if size_m:
                        evt["downloaded"] = f"{size_m.group(1)} {size_m.group(2)}"
                        evt["total_size"] = f"{size_m.group(3)} {size_m.group(4)}"
                    speed_m = _re.search(r'([\d.]+)\s*(GB|MB|KB)/s', text)
                    if speed_m:
                        evt["speed"] = f"{speed_m.group(1)} {speed_m.group(2)}/s"
                    loop.call_soon_threadsafe(q.put_nowait, ("progress", evt))
                rc = proc.wait()
                if rc == 0:
                    status = SystemTools.get_ollama_status()
                    loop.call_soon_threadsafe(q.put_nowait, ("done", {"done": True, "model": model_name, "status": status}))
                else:
                    loop.call_soon_threadsafe(q.put_nowait, ("done", {"done": True, "error": f"Pull failed (exit code {rc})"}))
            except Exception as exc:
                loop.call_soon_threadsafe(q.put_nowait, ("done", {"done": True, "error": str(exc)}))

        threading.Thread(target=_reader, daemon=True).start()

        while True:
            kind, payload = await q.get()
            if kind == "progress":
                yield f"data: {_json.dumps(payload)}\n\n"
            else:
                yield f"data: {_json.dumps(payload)}\n\n"
                break

    return StreamingResponse(_stream(), media_type="text/event-stream")


# =============================================================================
# llama.cpp Backend
# =============================================================================

@router.get("/api/v1/llamacpp/status")
async def api_llamacpp_status():
    """Get llama-server status: installed, running, models, config."""
    from services.llamacpp_manager import get_llamacpp_manager
    manager = get_llamacpp_manager()
    status = manager.get_status()
    return status.to_dict()


@router.post("/api/v1/llamacpp/install")
async def api_llamacpp_install():
    """Download and install the llama-server binary.

    Auto-detects platform and GPU (CUDA/Metal/CPU) and downloads
    the appropriate release from GitHub.
    """
    import asyncio
    import json as _json

    from services.llamacpp_manager import get_llamacpp_manager
    from starlette.responses import StreamingResponse

    manager = get_llamacpp_manager()

    async def _stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def _on_progress(pct, msg):
            await queue.put({"percent": pct, "message": msg})

        async def _run_install():
            try:
                success, message = await manager.download_server(progress_callback=_on_progress)
                await queue.put({"done": True, "success": success, "message": message})
            except Exception as exc:
                await queue.put({"done": True, "success": False, "message": str(exc)})

        task = asyncio.create_task(_run_install())

        while True:
            evt = await queue.get()
            yield f"data: {_json.dumps(evt)}\n\n"
            if evt.get("done"):
                break

        await task

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/api/v1/llamacpp/start")
async def api_llamacpp_start(request: Request):
    """Start llama-server with a specified model.

    Expects ``{"model": "model-name"}`` where model-name is the stem
    of a ``.gguf`` file in the models directory.  If omitted, uses
    the first available model.

    Automatically builds optimal flags from hardware profile.
    """
    from services.llamacpp_manager import get_llamacpp_manager
    manager = get_llamacpp_manager()

    try:
        body = await request.json()
    except Exception:
        body = {}
    model_name = (body.get("model") or "").strip()

    model_path = None
    if model_name:
        models = manager.list_models()
        match = next((m for m in models if m["name"] == model_name), None)
        if match:
            model_path = match["path"]
        else:
            return {"success": False, "message": f"Model '{model_name}' not found in models directory"}

    import asyncio
    ok, msg = await asyncio.to_thread(manager.launch, model_path=model_path)

    # Auto-register as backend if launch succeeded
    if ok:
        try:
            from admin.dependencies import get_model_router
            mr = get_model_router()
            if mr:
                await manager.register_as_backend(mr)
        except Exception as e:
            logger.warning("llama.cpp launched but backend registration failed: %s", e)

    return {"success": ok, "message": msg, "status": manager.get_status().to_dict()}


@router.post("/api/v1/llamacpp/stop")
async def api_llamacpp_stop():
    """Stop the running llama-server."""
    from services.llamacpp_manager import get_llamacpp_manager
    manager = get_llamacpp_manager()
    ok, msg = manager.stop()
    return {"success": ok, "message": msg}


@router.get("/api/v1/llamacpp/models")
async def api_llamacpp_models():
    """List available GGUF models."""
    from services.llamacpp_manager import get_llamacpp_manager
    manager = get_llamacpp_manager()
    return {"success": True, "models": manager.list_models()}


@router.post("/api/v1/llamacpp/models/download")
async def api_llamacpp_download_model(request: Request):
    """Download a GGUF model from a URL (typically HuggingFace).

    Expects ``{"url": "https://...", "filename": "model.gguf"}``.
    Streams progress as SSE.
    """
    import asyncio
    import json as _json

    from services.llamacpp_manager import get_llamacpp_manager
    from starlette.responses import StreamingResponse

    body = await request.json()
    url = (body.get("url") or "").strip()
    filename = (body.get("filename") or "").strip() or None

    if not url:
        return {"success": False, "message": "No URL provided"}

    manager = get_llamacpp_manager()

    async def _stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def _callback(pct, msg):
            await queue.put({"percent": pct, "message": msg})

        async def _run_download():
            try:
                success, message = await manager.download_model(url, filename, _callback)
                await queue.put({"done": True, "success": success, "message": message})
            except Exception as exc:
                await queue.put({"done": True, "success": False, "message": str(exc)})

        task = asyncio.create_task(_run_download())

        while True:
            evt = await queue.get()
            yield f"data: {_json.dumps(evt)}\n\n"
            if evt.get("done"):
                break

        await task  # ensure clean-up

    return StreamingResponse(_stream(), media_type="text/event-stream")


# =============================================================================
# Device Capability Scanning
# =============================================================================

@router.get("/api/v1/device/scan")
async def api_device_scan():
    """Scan local hardware and return capability profile with model recommendations.

    Returns GPU, RAM, disk info, the computed capability tier, and a
    prioritised list of Ollama model recommendations sized for this machine.
    """
    try:
        from services.device_capability import DeviceCapabilityScanner
        scanner = DeviceCapabilityScanner()
        report = await scanner.scan()
        return {"success": True, **report.to_dict()}
    except Exception as e:
        logger.error("Device scan failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/device/pull-preflight")
async def api_model_pull_preflight(request: Request):
    """Run resource checks before starting model pulls.

    Expects ``{"models": ["nomic-embed-text", "llama3.1:8b"]}``.
    Returns per-check results with remediation text.
    """
    try:
        from services.device_capability import DeviceCapabilityScanner
        body = await request.json()
        models = body.get("models", [])
        if not models:
            return {"success": False, "error": "No models specified"}

        scanner = DeviceCapabilityScanner()
        report = await scanner.scan()
        preflight = scanner.model_pull_preflight(
            models=models,
            profile=report.profile,
            ollama_installed=report.ollama_installed,
            ollama_running=report.ollama_running,
        )
        return {"success": True, **preflight}
    except Exception as e:
        logger.error("Model pull preflight failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Alpha log export
# =============================================================================

@router.get("/api/v1/alpha/logs/export")
async def api_export_alpha_logs():
    """Export alpha logs as a zip for testers."""
    # Logs live in the app root (same folder as leisurellm.log)
    app_root = LEISURELLM_DIR.parent
    candidates = [
        app_root / "alpha_session.log",
        app_root / "leisurellm.log",
        app_root / "tray.log",
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in candidates:
            if path.exists():
                zf.write(path, arcname=path.name)

    buf.seek(0)
    headers = {"Content-Disposition": "attachment; filename=alpha_logs.zip"}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


# =============================================================================
# Model Discovery — Upgrade Checking
# =============================================================================

@router.get("/api/v1/models/upgrade-check")
async def api_model_upgrade_check():
    """Check installed Ollama models against the curated catalog for upgrades.

    Returns a list of available upgrades, unknown installed models, and
    the catalog freshness date.  The admin UI can use this to nudge users
    toward newer frontier models.
    """
    try:
        from services.model_discovery import check_upgrades
        report = await check_upgrades()
        return {"success": True, **report.to_dict()}
    except Exception as e:
        logger.error("Model upgrade check failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/models/catalog")
async def api_model_catalog():
    """Return the full recommended model catalog (static + dynamic).

    Useful for the admin UI to show all available models with their
    hardware requirements and upgrade paths.
    """
    try:
        from services.device_capability import get_effective_catalog
        from services.model_discovery import (
            get_catalog_date,
            get_installed_model_names,
            get_upgrade_paths,
        )

        catalog = get_effective_catalog()
        installed = await get_installed_model_names()
        installed_set = set(installed)

        # Annotate each catalog entry with installed status
        annotated = []
        for entry in catalog:
            item = dict(entry)
            item["installed"] = entry["model"] in installed_set
            annotated.append(item)

        return {
            "success": True,
            "models": annotated,
            "installed": sorted(installed_set),
            "upgrade_paths": get_upgrade_paths(),
            "catalog_updated": get_catalog_date(),
        }
    except Exception as e:
        logger.error("Model catalog failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/models/catalog/refresh")
async def api_refresh_model_catalog():
    """Force re-read of recommended_models.json and clear caches."""
    try:
        from services.model_discovery import invalidate_catalog_cache
        invalidate_catalog_cache()
        return {"success": True, "message": "Catalog cache invalidated — next request will re-read from disk."}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/device/apply-recommendations")
async def api_apply_model_recommendations(request: Request):
    """Accept a list of model names and pull them all sequentially via Ollama.

    Expects ``{"models": ["nomic-embed-text", "llama3.1:8b"]}``.
    Returns SSE stream with per-model progress.
    """
    import json as _json

    from services.system_tools import SystemTools
    from starlette.responses import StreamingResponse

    body = await request.json()
    models = body.get("models", [])
    if not models:
        return {"success": False, "message": "No models specified"}

    # Whitelist model names (avoid arbitrary shell-like input)
    try:
        from services.device_capability import get_allowed_model_names
        allowed = get_allowed_model_names()
        models = [m for m in models if m in allowed]
    except Exception:
        try:
            from services.device_capability import ALLOWED_MODELS
            models = [m for m in models if m in ALLOWED_MODELS]
        except Exception as e:
            logger.warning("api_apply_model_recommendations: suppressed %s", e)

    if not models:
        return {"success": False, "message": "No allowed models specified"}

    exe = SystemTools._ollama_executable()
    if not exe:
        return {"success": False, "message": "Ollama is not installed"}

    SystemTools.ensure_ollama_running(exe)

    async def _stream():
        import re as _re
        import subprocess as _sp
        import threading

        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _parse_ollama_line(raw: str, model: str, index: int) -> dict:
            """Parse an ollama pull progress line into a structured event.

            Ollama outputs lines like:
              pulling abc123...  45% \u2595\u2588\u2588\u2588\u2591\u2591\u2595 1.9 GB/4.1 GB  52 MB/s  42s
              verifying sha256 digest
              writing manifest
              success
            """
            text = raw.strip()
            evt: dict = {"model": model, "index": index, "progress": text}

            # Try to extract percentage
            pct_m = _re.search(r'(\d+)%', text)
            if pct_m:
                evt["percent"] = int(pct_m.group(1))

            # Try to extract downloaded / total (e.g. "1.9 GB/4.1 GB")
            size_m = _re.search(
                r'([\d.]+)\s*(GB|MB|KB)\s*/\s*([\d.]+)\s*(GB|MB|KB)', text,
            )
            if size_m:
                evt["downloaded"] = f"{size_m.group(1)} {size_m.group(2)}"
                evt["total_size"] = f"{size_m.group(3)} {size_m.group(4)}"

            # Try to extract speed (e.g. "52 MB/s")
            speed_m = _re.search(r'([\d.]+)\s*(GB|MB|KB)/s', text)
            if speed_m:
                evt["speed"] = f"{speed_m.group(1)} {speed_m.group(2)}/s"

            # Try to extract ETA (e.g. "42s" or "1m20s")
            eta_m = _re.search(r'(?:^|\s)((?:\d+h)?(?:\d+m)?\d+s)\s*$', text)
            if eta_m:
                evt["eta"] = eta_m.group(1)

            # Phase messages
            lower = text.lower()
            if lower.startswith('pulling manifest'):
                evt["phase"] = "manifest"
            elif lower.startswith('pulling'):
                evt["phase"] = "downloading"
            elif 'verifying' in lower:
                evt["phase"] = "verifying"
                evt["percent"] = 100
            elif lower == 'success':
                evt["phase"] = "success"
                evt["percent"] = 100

            return evt

        def _read_ollama_lines(stdout):
            """Yield individual progress lines from ollama's stdout.

            Ollama uses \\r (carriage return) to overwrite the current
            line for download bars.  The default Python line iterator
            only splits on \\n, so progress events would be silently
            buffered until the layer finishes.  We read raw bytes in
            small chunks and split on both \\r and \\n.
            """
            buf = b""
            while True:
                chunk = stdout.read(256)
                if not chunk:
                    # Process remaining buffer
                    if buf.strip():
                        yield buf.decode("utf-8", errors="replace")
                    break
                buf += chunk
                # Split on \r or \n
                while b"\r" in buf or b"\n" in buf:
                    # Find earliest delimiter
                    r_pos = buf.find(b"\r")
                    n_pos = buf.find(b"\n")
                    if r_pos == -1:
                        pos = n_pos
                    elif n_pos == -1:
                        pos = r_pos
                    else:
                        pos = min(r_pos, n_pos)
                    line = buf[:pos].decode("utf-8", errors="replace")
                    # Skip \r\n as single delimiter
                    if pos + 1 < len(buf) and buf[pos:pos+2] == b"\r\n":
                        buf = buf[pos+2:]
                    else:
                        buf = buf[pos+1:]
                    if line.strip():
                        yield line

        def _pull_all():
            """Run all pulls sequentially in a background thread."""
            try:
                for i, model_name in enumerate(models):
                    loop.call_soon_threadsafe(
                        q.put_nowait,
                        ("event", {"model": model_name, "index": i, "total": len(models), "status": "pulling"}),
                    )
                    try:
                        proc = _sp.Popen(
                            [exe, "pull", model_name],
                            stdout=_sp.PIPE, stderr=_sp.STDOUT,
                            creationflags=_sp.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                        for line in _read_ollama_lines(proc.stdout):
                            evt = _parse_ollama_line(line, model_name, i)
                            if evt:
                                loop.call_soon_threadsafe(
                                    q.put_nowait,
                                    ("event", evt),
                                )
                        rc = proc.wait(timeout=2700)
                    except _sp.TimeoutExpired:
                        proc.kill()
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            ("event", {"model": model_name, "index": i, "status": "error", "error": "Timeout"}),
                        )
                        # Best-effort cleanup of partial download
                        try:
                            _sp.run([exe, "rm", model_name], timeout=30)
                        except Exception as e:
                            logger.warning("operation: suppressed %s", e)
                        continue
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            ("event", {"model": model_name, "index": i, "status": "error", "error": str(exc)}),
                        )
                        continue

                    if rc == 0:
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            ("event", {"model": model_name, "index": i, "status": "done"}),
                        )
                    else:
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            ("event", {"model": model_name, "index": i, "status": "error", "error": f"Exit code {rc}"}),
                        )
            finally:
                status = SystemTools.get_ollama_status()
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    ("done", {"done": True, "status": status}),
                )

        threading.Thread(target=_pull_all, daemon=True).start()

        while True:
            kind, payload = await q.get()
            yield f"data: {_json.dumps(payload)}\n\n"
            if kind == "done":
                break

    return StreamingResponse(_stream(), media_type="text/event-stream")


# =============================================================================
# Bot Control
# =============================================================================

@router.post("/api/v1/bot/restart")
async def api_restart_bot():
    """Request a bot restart."""
    bot = get_bot()
    try:
        restart_flag = LEISURELLM_DIR / ".restart_requested"
        restart_flag.write_text("restart")
        logger.info("Restart requested via admin UI")

        if bot:
            async def _do_shutdown():
                await asyncio.sleep(1)
                logger.info("Initiating bot shutdown for restart...")
                await bot.close()
            asyncio.create_task(_do_shutdown())
            return {"success": True, "message": "Bot restart initiated."}
        else:
            async def _exit_process():
                await asyncio.sleep(1)
                os._exit(0)
            asyncio.create_task(_exit_process())
            return {"success": True, "message": "Restart signal sent."}
    except Exception as e:
        logger.error(f"Failed to initiate restart: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/bot/status")
async def api_bot_status():
    # Determine accelerator mode from workflows config
    try:
        from core.config_loader import WorkflowConfig
        wf = WorkflowConfig.load()
        mode = "accelerators" if wf.personas_enabled else "continuity"
    except Exception:
        mode = "continuity"

    bot = get_bot()
    if bot:
        try:
            return {
                "online": not bot.is_closed(),
                "user": str(bot.user) if bot.user else None,
                "guilds": len(bot.guilds) if hasattr(bot, "guilds") else 0,
                "latency_ms": round(bot.latency * 1000, 2) if bot.latency else None,
                "mode": mode,
            }
        except Exception:
            return {"online": False, "error": "request_failed", "message": "Something went wrong.", "mode": mode}
    return {"online": False, "user": None, "guilds": 0, "latency_ms": None, "mode": mode}


# =============================================================================
# Backup / Restore
# =============================================================================

@router.get("/api/v1/backup/list")
async def api_list_backups():
    try:
        from core.backup_restore import list_backups
        backups = list_backups()
        return {"success": True, "backups": backups}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/backup/create")
async def api_create_backup(db=Depends(get_db)):
    try:
        from core.backup_restore import backup_database
        dest = backup_database(db.database_path, label="manual")
        return {"success": True, "path": str(dest), "filename": dest.name}
    except Exception as e:
        logger.error(f"backup create: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/backup/restore/{filename}")
async def api_restore_backup(filename: str, db=Depends(get_db)):
    try:
        from core.backup_restore import _ensure_backup_dir, restore_database

        backup_dir = _ensure_backup_dir()
        backup_path = (backup_dir / filename).resolve()
        # Prevent path traversal — resolved path must stay inside backup_dir
        if not str(backup_path).startswith(str(backup_dir.resolve())):
            return {"success": False, "error": "Invalid backup filename"}
        if not backup_path.exists():
            return {"success": False, "error": "Backup file not found"}

        # Restore + run integrity verification
        safety = restore_database(backup_path, db.database_path)

        # Post-restore verification
        verification = await _verify_restore(db.database_path)

        return {
            "success": True,
            "safety_backup": str(safety) if safety else None,
            "verification": verification,
            "message": "Database restored. Restart the bot for changes to take effect.",
        }
    except Exception as e:
        logger.error(f"backup restore: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


async def _verify_restore(db_path) -> dict:
    """Run post-restore integrity checks."""
    import aiosqlite

    result = {"integrity": "unknown", "tables": [], "migration_count": 0}
    try:
        async with aiosqlite.connect(str(db_path)) as conn:
            # PRAGMA integrity_check
            async with conn.execute("PRAGMA integrity_check") as cur:
                row = await cur.fetchone()
                result["integrity"] = row[0] if row else "error"

            # Expected tables present
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) as cur:
                result["tables"] = [r[0] for r in await cur.fetchall()]

            # Count applied migrations
            try:
                async with conn.execute("SELECT COUNT(*) FROM applied_migrations") as cur:
                    result["migration_count"] = (await cur.fetchone())[0]
            except Exception:
                result["migration_count"] = -1  # table may not exist

            # Foreign key check
            async with conn.execute("PRAGMA foreign_key_check") as cur:
                fk_issues = await cur.fetchall()
                result["fk_issues"] = len(fk_issues)
    except Exception as e:
        result["error"] = str(e)
    return result


@router.post("/api/v1/support-bundle")
async def api_create_support_bundle(db=Depends(get_db)):
    try:
        from core.backup_restore import create_support_bundle
        bundle = create_support_bundle(db.database_path)
        return {"success": True, "path": str(bundle), "filename": bundle.name}
    except Exception as e:
        logger.error(f"support bundle: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Sweep Jobs
# =============================================================================

@router.post("/api/v1/sweeps/run")
async def api_run_sweeps(db=Depends(get_db)):
    try:
        from core.sweep_jobs import run_all_sweeps
        results = await run_all_sweeps(db)
        summaries = {
            k: {"summary": v.summary, "flagged": v.items_flagged, "details": v.details}
            for k, v in results.items()
        }
        return {"success": True, "results": summaries}
    except Exception as e:
        logger.error(f"sweeps: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Seed Workspace
# =============================================================================

@router.post("/api/v1/seed")
async def api_seed_workspace(db=Depends(get_db)):
    try:
        from core.seed_workspace import is_seeded, seed_workspace

        if is_seeded():
            return {"success": True, "created": {"skipped": True, "reason": "already_seeded"}}

        result = await seed_workspace(db)
        return {"success": True, "created": result}
    except Exception as e:
        logger.error(f"seed workspace: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Data Retention
# =============================================================================

@router.post("/api/v1/retention/prune")
async def api_prune_old_records(
    days: int = 90,
    db=Depends(get_db),
):
    """Remove autonomous_posts and job_runs older than *days*."""
    try:
        pruned = {}
        async with db.acquire() as conn:
            for table in ("autonomous_posts", "job_runs"):
                try:
                    async with conn.execute(
                        f"DELETE FROM {table} WHERE created_at < datetime('now', ?)",
                        (f"-{days} days",),
                    ) as cur:
                        pruned[table] = cur.rowcount
                except Exception:
                    pruned[table] = 0
            await conn.commit()
        return {"success": True, "pruned": pruned, "older_than_days": days}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}

