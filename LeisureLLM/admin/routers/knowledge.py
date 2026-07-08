"""Knowledge router — Knowledge Base storage + Knowledge Gaps browser."""

import asyncio
import io
import json as _json
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

from core.services.audit_service import AuditService
from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi import File as FastAPIFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from admin.dependencies import get_current_actor, get_db, require_admin, templates
from admin.performance import get_or_set_cache, invalidate_cache, timed

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["knowledge"], dependencies=[Depends(require_admin)])

_KNOWLEDGE_STATS_CACHE_KEY = "knowledge_stats"
_KNOWLEDGE_STORAGE_CACHE_KEY = "knowledge_storage"
_KNOWLEDGE_CACHE_TTL_SECONDS = 15.0


def invalidate_knowledge_caches() -> None:
    invalidate_cache(_KNOWLEDGE_STATS_CACHE_KEY, _KNOWLEDGE_STORAGE_CACHE_KEY)


def _resolve_actor(actor):
    if hasattr(actor, "actor_kind") and hasattr(actor, "stable_id"):
        return actor
    return SimpleNamespace(
        actor_id=0,
        actor_kind="system",
        stable_id="actor_knowledge_fallback",
        external_ref="knowledge-fallback",
        display_name="Knowledge Service",
        username="knowledge-service",
    )


def _actor_display_name(actor) -> str:
    return str(actor.display_name or actor.username or actor.external_ref)


def _append_actor_note(existing_notes: Optional[str], actor, action: str, detail: Optional[str] = None) -> str:
    prefix = f"[{action} by {_actor_display_name(actor)} at {datetime.utcnow().isoformat()}Z]"
    block = prefix if not detail else f"{prefix}\n{detail.strip()}"
    current = (existing_notes or "").strip()
    return block if not current else f"{current}\n\n{block}"


async def _fetch_gap_row(conn, gap_id: int) -> Optional[dict]:
    async with conn.execute(
        """SELECT id, topic, question, context, status, resolved_at, resolved_via,
                  priority_score, assigned_to_user, memo_path, notes,
                  curation_status, curation_reason, curated_at, curated_by_username,
                  response_count, last_response_at
           FROM knowledge_gaps WHERE id = ?""",
        (gap_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "topic": row[1],
        "question": row[2],
        "context": row[3],
        "status": row[4],
        "resolved_at": row[5],
        "resolved_via": row[6],
        "priority_score": row[7],
        "assigned_to_user": row[8],
        "memo_path": row[9],
        "notes": row[10],
        "curation_status": row[11],
        "curation_reason": row[12],
        "curated_at": row[13],
        "curated_by_username": row[14],
        "response_count": row[15],
        "last_response_at": row[16],
    }


def _build_storage_info() -> dict:
    import config as app_config

    docs_path = Path(app_config.directory_path)
    chroma_path = Path(app_config.persist_directory)
    hash_path = Path(app_config.hash_csv)

    storage_info = {
        "documents": {"path": str(docs_path), "exists": docs_path.exists(), "file_count": 0, "total_size_mb": 0, "files": []},
        "chroma": {"path": str(chroma_path), "exists": chroma_path.exists(), "total_size_mb": 0, "collections": []},
        "hashes": {"path": str(hash_path), "exists": hash_path.exists(), "entry_count": 0},
    }
    if docs_path.exists():
        files = list(docs_path.glob("*"))
        storage_info["documents"]["file_count"] = len(files)
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        storage_info["documents"]["total_size_mb"] = round(total_size / (1024 * 1024), 2)
        storage_info["documents"]["files"] = [
            {"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)}
            for f in sorted(files, key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True)[:10]
            if f.is_file()
        ]
    if chroma_path.exists():
        total_size = sum(f.stat().st_size for f in chroma_path.rglob("*") if f.is_file())
        storage_info["chroma"]["total_size_mb"] = round(total_size / (1024 * 1024), 2)
        sqlite_file = chroma_path / "chroma.sqlite3"
        if sqlite_file.exists():
            storage_info["chroma"]["sqlite_size_mb"] = round(sqlite_file.stat().st_size / (1024 * 1024), 2)
    if hash_path.exists():
        try:
            with open(hash_path, "r", encoding="utf-8") as f:
                storage_info["hashes"]["entry_count"] = max(0, len(f.readlines()) - 1)
        except Exception as e:
            logger.warning("operation: suppressed %s", e)
    return storage_info


def get_cached_storage_info() -> dict:
    def _load() -> dict:
        with timed("knowledge.storage"):
            return _build_storage_info()

    value, _ = get_or_set_cache(_KNOWLEDGE_STORAGE_CACHE_KEY, _KNOWLEDGE_CACHE_TTL_SECONDS, _load)
    return value


def _build_knowledge_stats() -> dict:
    from config import directory_path, hash_csv, persist_directory

    docs_path = Path(directory_path)
    chroma_path = Path(persist_directory)
    hash_path = Path(hash_csv)
    allowed = {".md", ".txt", ".pdf", ".docx", ".json", ".jsonl"}

    doc_count = (
        len([f for f in docs_path.rglob("*") if f.is_file() and f.suffix.lower() in allowed])
        if docs_path.exists() else 0
    )

    def get_dir_size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0

    return {
        "success": True,
        "documents": {"count": doc_count, "size_bytes": get_dir_size(docs_path)},
        "chroma": {"size_bytes": get_dir_size(chroma_path)},
        "hashes": {"exists": hash_path.exists(), "size_bytes": hash_path.stat().st_size if hash_path.exists() else 0},
    }


def get_cached_knowledge_stats() -> dict:
    def _load() -> dict:
        with timed("knowledge.stats"):
            return _build_knowledge_stats()

    value, _ = get_or_set_cache(_KNOWLEDGE_STATS_CACHE_KEY, _KNOWLEDGE_CACHE_TTL_SECONDS, _load)
    return value


# ── Pydantic models ──────────────────────────────────────────────────────────

class GapUpdate(BaseModel):
    status: Optional[str] = None
    curation_status: Optional[str] = None
    curation_reason: Optional[str] = None
    priority_score: Optional[int] = None
    notes: Optional[str] = None


class BulkGapAction(BaseModel):
    gap_ids: List[int]
    action: str  # delete | defer | discard | keep | resolve


class GapAnswer(BaseModel):
    answer: str
    create_memo: bool = True
    memo_title: Optional[str] = None


class RememberPayload(BaseModel):
    content: str
    title: Optional[str] = None
    category: str = "general"  # general | decision | meeting | knowledge


# =============================================================================
# Page routes
# =============================================================================

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_base_page(request: Request):
    return templates.TemplateResponse(request, "knowledge_base.html", {
        "active_page": "knowledge", "storage": get_cached_storage_info(),
    })


@router.get("/gaps", response_class=HTMLResponse)
async def knowledge_gaps_page(request: Request):
    return templates.TemplateResponse(request, "knowledge_gaps.html", {"active_page": "gaps"})


@router.get("/teach", response_class=HTMLResponse)
async def teach_page(request: Request):
    return templates.TemplateResponse(request, "teach.html", {"active_page": "teach"})


# =============================================================================
# Knowledge Base API
# =============================================================================

@router.post("/api/v1/knowledge/open-folder/{folder_key}")
async def api_open_folder(folder_key: str):
    import platform
    import subprocess

    from config import directory_path, persist_directory
    try:
        folder_map = {"docs": directory_path, "chroma": persist_directory, "root": str(Path(directory_path).parent)}
        if folder_key not in folder_map:
            return {"success": False, "error": f"Unknown folder: {folder_key}"}
        path = folder_map[folder_key]
        # Auto-create the folder if it doesn't exist yet (matches upload behaviour)
        Path(path).mkdir(parents=True, exist_ok=True)
        system = platform.system()
        if system == "Windows":
            import os
            os.startfile(path)
        elif system == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"success": True, "path": path}
    except Exception as e:
        logger.error(f"Failed to open folder: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/knowledge/stats")
async def api_knowledge_stats():
    try:
        return get_cached_knowledge_stats()
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# ── Recently Added Documents ─────────────────────────────────────────────────

@router.get("/api/v1/knowledge/documents")
async def api_list_documents(per_page: int = 50, page: int = 1):
    """List recently added/modified documents from the docs folder."""
    import yaml as _yaml

    import config as app_config

    docs_root = Path(app_config.directory_path)
    if not docs_root.exists():
        return {"success": True, "documents": [], "total": 0, "page": 1, "per_page": per_page, "total_pages": 1}

    _ALLOWED = {".md", ".txt", ".pdf", ".docx", ".json", ".jsonl"}
    all_files = sorted(
        [f for f in docs_root.rglob("*") if f.is_file() and f.suffix.lower() in _ALLOWED and not f.name.startswith(".")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    total = len(all_files)
    offset = (page - 1) * per_page
    page_files = all_files[offset : offset + per_page]

    documents = []
    for f in page_files:
        title = f.stem.replace("_", " ").replace("-", " ").strip()
        category = ""
        stat = f.stat()
        created_at = datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z"

        # Try to extract title/category from YAML frontmatter in markdown files
        if f.suffix.lower() == ".md":
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")[:600]
                if text.startswith("---"):
                    end = text.find("---", 3)
                    if end > 0:
                        meta = _yaml.safe_load(text[3:end])
                        if isinstance(meta, dict):
                            title = meta.get("title", title)
                            category = meta.get("category", category)
                            if meta.get("created_at"):
                                created_at = str(meta["created_at"])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        documents.append({
            "filename": f.name,
            "title": title,
            "category": category,
            "created_at": created_at,
            "size_kb": round(stat.st_size / 1024, 1),
        })

    return {
        "success": True, "documents": documents, "total": total,
        "page": page, "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


# ── Remember (web-based document capture) ────────────────────────────────────

@router.post("/api/v1/knowledge/remember")
async def api_remember(payload: RememberPayload):
    """Save a note/decision/knowledge to the docs folder and trigger re-index."""
    import yaml

    import config as app_config

    content = (payload.content or "").strip()
    if not content:
        return {"success": False, "error": "Content cannot be empty."}

    now = datetime.utcnow()
    title = (payload.title or "").strip() or f"Web note {now.strftime('%Y-%m-%d %H:%M')}"

    # Sanitise slug
    slug = re.sub(r"[^a-z0-9_]+", "_", title.lower())[:50].strip("_") or "note"

    # Pick directory — mirrors DocumentAuthor pattern
    docs_root = Path(app_config.directory_path)
    memo_dir = docs_root / "memos" / str(now.year) / f"{now.month:02d}"
    memo_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{now.day:02d}_{slug}.md"
    filepath = memo_dir / filename

    # Build YAML frontmatter
    meta = {
        "title": title,
        "doc_type": "human_knowledge",
        "category": payload.category,
        "source": "admin_ui",
        "created_at": now.isoformat() + "Z",
    }
    frontmatter = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n"
    filepath.write_text(frontmatter + content, encoding="utf-8")
    invalidate_knowledge_caches()

    # Trigger background ingest so the new doc appears in RAG immediately
    _schedule_background_ingest()

    rel = str(filepath.relative_to(docs_root))
    return {"success": True, "file": rel, "title": title}


# ── File upload ───────────────────────────────────────────────────────────────

_UPLOAD_ALLOWED = {".pdf", ".docx", ".json", ".jsonl", ".txt", ".md"}
_UPLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/api/v1/knowledge/upload")
async def api_upload_files(files: list[UploadFile] = FastAPIFile(...)):
    """Accept uploaded files, save to docs folder, and trigger background ingest."""
    import config as app_config

    docs_root = Path(app_config.directory_path)
    docs_root.mkdir(parents=True, exist_ok=True)

    saved = []
    errors = []

    for f in files:
        name = (f.filename or "upload").strip()
        # Sanitise filename — keep only safe characters
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        if not safe_name or safe_name.startswith('.'):
            errors.append({"file": name, "error": "Invalid filename"})
            continue

        ext = Path(safe_name).suffix.lower()
        if ext not in _UPLOAD_ALLOWED:
            errors.append({
                "file": name,
                "error": f"Unsupported type '{ext}'. Allowed: {', '.join(sorted(_UPLOAD_ALLOWED))}",
            })
            continue

        # Read content (enforce size limit)
        content = await f.read()
        if len(content) > _UPLOAD_MAX_SIZE:
            errors.append({"file": name, "error": f"File too large ({len(content) // (1024*1024)} MB). Max: 50 MB."})
            continue

        # Avoid overwriting — add counter suffix if needed
        dest = docs_root / safe_name
        counter = 1
        while dest.exists():
            stem = Path(safe_name).stem
            dest = docs_root / f"{stem}_{counter}{ext}"
            counter += 1

        dest.write_bytes(content)
        saved.append({"file": dest.name, "size_kb": round(len(content) / 1024, 1)})
        logger.info("Uploaded file: %s (%d bytes)", dest.name, len(content))

    # Trigger background ingest if any files were saved
    if saved:
        invalidate_knowledge_caches()
        _schedule_background_ingest()

    return {
        "success": len(saved) > 0 or len(errors) == 0,
        "uploaded": saved,
        "errors": errors,
        "message": f"{len(saved)} file(s) uploaded" + (f", {len(errors)} failed" if errors else ""),
    }


# ── Knowledge Export / Import ─────────────────────────────────────────────────

_EXPORT_VERSION = 1


@router.get("/api/v1/knowledge/export")
async def api_export_knowledge(db=Depends(get_db)):
    """Export the full knowledge base as a downloadable ZIP.

    Includes:
    - All documents from the docs/ folder (recursively)
    - Hash index CSV
    - Knowledge gaps from the database (as JSON)
    - A manifest with metadata

    ChromaDB is excluded (can be rebuilt via Sync after import).
    """
    import config as app_config

    docs_root = Path(app_config.directory_path)
    hash_path = Path(app_config.hash_csv)

    buf = io.BytesIO()
    file_count = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Documents
        if docs_root.exists():
            for fp in sorted(docs_root.rglob("*")):
                if fp.is_file():
                    arcname = f"docs/{fp.relative_to(docs_root)}"
                    zf.write(fp, arcname)
                    file_count += 1

        # 2. Hash index
        if hash_path.exists():
            zf.write(hash_path, "hashes.csv")

        # 3. Knowledge gaps
        gaps_data = []
        try:
            async with db.acquire() as conn, conn.execute(
                """SELECT id, topic, question, context, first_asked, last_asked,
                              times_asked, asked_by_users, status, resolved_at,
                              resolved_via, priority_score, assigned_to_user,
                              memo_path, notes, curation_status, curation_reason,
                              curated_at, curated_by_username
                       FROM knowledge_gaps"""
            ) as cur:
                cols = [d[0] for d in cur.description]
                for row in await cur.fetchall():
                    gaps_data.append(dict(zip(cols, row)))
        except Exception as exc:
            logger.warning("Could not export knowledge gaps: %s", exc)

        zf.writestr("knowledge_gaps.json", _json.dumps(gaps_data, indent=2, default=str))

        # 4. Manifest
        manifest = {
            "version": _EXPORT_VERSION,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "document_count": file_count,
            "gap_count": len(gaps_data),
            "has_hashes": hash_path.exists(),
        }
        zf.writestr("manifest.json", _json.dumps(manifest, indent=2))

    buf.seek(0)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="knowledge_export_{ts}.zip"'},
    )


class _ImportMode(BaseModel):
    mode: str = "merge"  # merge | replace


@router.post("/api/v1/knowledge/import")
async def api_import_knowledge(
    file: UploadFile = FastAPIFile(...),
    db=Depends(get_db),
):
    """Import a knowledge ZIP previously created by the export endpoint.

    - Documents are restored to the docs/ folder
    - Hash CSV is restored
    - Knowledge gaps are merged into the database (duplicates by question skipped)
    - A background Sync is triggered to rebuild the vector store
    """
    import config as app_config

    docs_root = Path(app_config.directory_path)
    hash_path = Path(app_config.hash_csv)

    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return {"success": False, "error": "Invalid ZIP file."}

    # Validate manifest
    if "manifest.json" not in zf.namelist():
        return {"success": False, "error": "Not a valid knowledge export (missing manifest)."}

    try:
        _json.loads(zf.read("manifest.json"))
    except Exception:
        return {"success": False, "error": "Corrupt manifest in ZIP."}

    results = {"documents_restored": 0, "documents_skipped": 0, "gaps_imported": 0, "gaps_skipped": 0, "hashes_restored": False}

    # 1. Restore documents
    docs_root.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        if not name.startswith("docs/") or name.endswith("/"):
            continue
        rel = name[5:]  # strip "docs/" prefix
        dest = docs_root / rel
        # Safety: ensure path stays inside docs_root
        try:
            dest.resolve().relative_to(docs_root.resolve())
        except ValueError:
            logger.warning("Skipping path traversal attempt: %s", name)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            results["documents_skipped"] += 1
        else:
            dest.write_bytes(zf.read(name))
            results["documents_restored"] += 1

    # 2. Restore hash index (validate CSV structure before writing)
    if "hashes.csv" in zf.namelist():
        _raw_csv = zf.read("hashes.csv")
        try:
            import csv as _csv_mod
            import io as _io_mod
            _reader = _csv_mod.reader(_io_mod.StringIO(_raw_csv.decode("utf-8", errors="replace")))
            _header = next(_reader, None)
            if _header is None or [h.strip() for h in _header] != ["FileName", "Hash"]:
                logger.warning("Imported hashes.csv has invalid header %s — skipping", _header)
            else:
                hash_path.write_bytes(_raw_csv)
                results["hashes_restored"] = True
        except Exception as _csv_err:
            logger.warning("Imported hashes.csv could not be validated: %s", _csv_err)

    # 3. Import knowledge gaps (merge — skip duplicates by question text)
    if "knowledge_gaps.json" in zf.namelist():
        try:
            gaps = _json.loads(zf.read("knowledge_gaps.json"))
            async with db.acquire() as conn:
                for g in gaps:
                    # Check for duplicate by question text
                    async with conn.execute(
                        "SELECT id FROM knowledge_gaps WHERE question = ?",
                        (g.get("question", ""),),
                    ) as cur:
                        if await cur.fetchone():
                            results["gaps_skipped"] += 1
                            continue
                    await conn.execute(
                        """INSERT INTO knowledge_gaps
                           (topic, question, context, first_asked, last_asked,
                            times_asked, asked_by_users, status, resolved_at,
                            resolved_via, priority_score, assigned_to_user,
                            memo_path, notes, curation_status, curation_reason,
                            curated_at, curated_by_username)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            g.get("topic"), g.get("question"), g.get("context"),
                            g.get("first_asked"), g.get("last_asked"),
                            g.get("times_asked", 0), g.get("asked_by_users"),
                            g.get("status", "open"), g.get("resolved_at"),
                            g.get("resolved_via"), g.get("priority_score", 0),
                            g.get("assigned_to_user"), g.get("memo_path"),
                            g.get("notes"), g.get("curation_status"),
                            g.get("curation_reason"), g.get("curated_at"),
                            g.get("curated_by_username"),
                        ),
                    )
                    results["gaps_imported"] += 1
                await conn.commit()
        except Exception as exc:
            logger.error("Failed to import knowledge gaps: %s", exc)
            results["gap_error"] = str(exc)

    # 4. Trigger re-index so new docs appear in vector store
    invalidate_knowledge_caches()
    _schedule_background_ingest()

    total_docs = results["documents_restored"]
    total_gaps = results["gaps_imported"]
    msg_parts = []
    if total_docs:
        msg_parts.append(f"{total_docs} document(s) restored")
    if results["documents_skipped"]:
        msg_parts.append(f"{results['documents_skipped']} already existed")
    if total_gaps:
        msg_parts.append(f"{total_gaps} gap(s) imported")
    if results["gaps_skipped"]:
        msg_parts.append(f"{results['gaps_skipped']} duplicate gap(s) skipped")
    if results["hashes_restored"]:
        msg_parts.append("hash index restored")
    msg_parts.append("sync triggered")

    return {
        "success": True,
        "message": ", ".join(msg_parts).capitalize() + ".",
        **results,
    }


# ── Sync (incremental re-index) ──────────────────────────────────────────────

_ingest_lock = asyncio.Lock()


def _schedule_background_ingest():
    """Fire-and-forget background ingest (non-blocking)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_ingest_background())
    except RuntimeError:
        pass  # no event loop — skip


async def _run_ingest_background():
    """Run incremental ingest in a thread so it doesn't block the event loop."""
    if _ingest_lock.locked():
        logger.info("Ingest already running — skipping")
        return
    async with _ingest_lock:
        try:
            from cogs.ingest_metadata import run_ingest

            _, stats, _ = await asyncio.to_thread(run_ingest)
            logger.info("Background ingest done: %s", stats)
            invalidate_knowledge_caches()

            # Invalidate response cache — knowledge base has changed
            try:
                from services.response_cache import get_response_cache
                get_response_cache().invalidate_all()
                logger.debug("Response cache invalidated after ingest")
            except Exception as e:
                logger.warning("_run_ingest_background: suppressed %s", e)

            # Persist sync stats for growth tracking
            try:
                from database import Database
                db = Database()
                await db.connect()
                await db.record_sync_result(stats, triggered_by="background")
                await db.close()
            except Exception as exc:
                logger.debug("Could not persist background sync stats: %s", exc)
        except Exception as exc:
            logger.error("Background ingest failed: %s", exc)


@router.post("/api/v1/knowledge/sync")
async def api_sync_documents(db=Depends(get_db)):
    """Run incremental document sync (hash-compare + ChromaDB re-index)."""
    if _ingest_lock.locked():
        return {"success": False, "error": "A sync is already in progress. Try again shortly."}

    try:
        from cogs.ingest_metadata import run_ingest

        async with _ingest_lock:
            _, stats, _ = await asyncio.to_thread(run_ingest)
        invalidate_knowledge_caches()

        # Persist sync stats for growth tracking
        try:
            async with db.acquire() as conn:
                import json as _json
                await conn.execute(
                    """INSERT INTO job_runs
                       (job_name, run_date, started_at, completed_at, status,
                        triggered_by, result_json)
                       VALUES ('doc_sync', date('now'), datetime('now'),
                               datetime('now'), 'completed', 'api', ?)""",
                    (_json.dumps(stats),),
                )
                await conn.commit()
        except Exception as exc:
            logger.debug("Could not persist sync stats: %s", exc)

        return {
            "success": True,
            "added": stats.get("added_files", 0),
            "updated": stats.get("updated_files", 0),
            "deleted": stats.get("deleted_files", 0),
            "chunks": stats.get("chunks_written", 0),
        }
    except Exception as exc:
        logger.error("Sync failed: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Knowledge Gaps API
# =============================================================================

@router.get("/api/v1/gaps")
async def api_list_gaps(
    status: Optional[str] = None,
    curation_status: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "priority_score",
    sort_dir: str = "desc",
    page: int = 1,
    per_page: int = 50,
    db=Depends(get_db),
):
    try:
        async with db.acquire() as conn:
            where_clauses, params = [], []
            if status:
                where_clauses.append("status = ?")
                params.append(status)
            if curation_status:
                where_clauses.append("curation_status = ?")
                params.append(curation_status)
            if search:
                where_clauses.append("(topic LIKE ? OR question LIKE ? OR context LIKE ?)")
                params.extend([f"%{search}%"] * 3)
            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            valid_sort = ["id", "topic", "priority_score", "times_asked", "first_asked", "last_asked", "status"]
            col = sort_by if sort_by in valid_sort else "priority_score"
            d = "DESC" if sort_dir.lower() == "desc" else "ASC"
            async with conn.execute(f"SELECT COUNT(*) FROM knowledge_gaps {where_sql}", params) as cur:
                total = (await cur.fetchone())[0]
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            async with conn.execute(
                f"""SELECT id, topic, question, context, first_asked, last_asked, times_asked,
                    status, resolved_at, resolved_via, priority_score, assigned_to_user,
                    memo_path, notes, curation_status, curation_reason, curated_at, curated_by_username
                FROM knowledge_gaps {where_sql} ORDER BY {col} {d} LIMIT ? OFFSET ?""",
                params,
            ) as cur:
                rows = await cur.fetchall()
            gaps = [
                {"id": r[0], "topic": r[1], "question": r[2], "context": r[3],
                 "first_asked": r[4], "last_asked": r[5], "times_asked": r[6],
                 "status": r[7], "resolved_at": r[8], "resolved_via": r[9],
                 "priority_score": r[10], "assigned_to_user": r[11],
                 "memo_path": r[12], "notes": r[13], "curation_status": r[14],
                 "curation_reason": r[15], "curated_at": r[16], "curated_by": r[17]}
                for r in rows
            ]
            return {"success": True, "gaps": gaps, "total": total, "page": page,
                    "per_page": per_page, "total_pages": (total + per_page - 1) // per_page}
    except Exception as e:
        logger.error(f"Failed to list gaps: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/gaps/recent-activity")
async def api_gaps_recent_activity(db=Depends(get_db), days: int = 14, limit: int = 10):
    """Self-healing activity feed — recently resolved gaps with source of resolution."""
    try:
        days = min(max(days, 1), 90)
        limit = min(max(limit, 1), 50)
        async with db.acquire() as conn, conn.execute(
            """SELECT id, topic, question, status, resolved_at, resolved_via,
                          priority_score, source
                   FROM knowledge_gaps
                   WHERE status = 'resolved'
                     AND resolved_at >= datetime('now', ? || ' days')
                   ORDER BY resolved_at DESC
                   LIMIT ?""",
            (f"-{days}", limit),
        ) as cur:
            rows = await cur.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0], "topic": r[1], "question": r[2], "status": r[3],
                "resolved_at": r[4], "resolved_via": r[5],
                "priority_score": r[6], "source": r[7],
            })
        return {"success": True, "activity": items, "count": len(items)}
    except Exception as e:
        logger.error(f"Failed to get gap activity: {e}")
        return {"success": False, "error": "request_failed"}


@router.get("/api/v1/gaps/stats")
async def api_gaps_stats(db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            stats = {}
            async with conn.execute("SELECT status, COUNT(*) FROM knowledge_gaps GROUP BY status") as cur:
                stats["by_status"] = {r[0]: r[1] for r in await cur.fetchall()}
            async with conn.execute("SELECT COALESCE(curation_status,'uncurated'), COUNT(*) FROM knowledge_gaps GROUP BY curation_status") as cur:
                stats["by_curation"] = {r[0]: r[1] for r in await cur.fetchall()}
            async with conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE priority_score >= 20 AND status='open'") as cur:
                stats["high_priority_open"] = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE times_asked >= 5 AND status='open'") as cur:
                stats["frequently_asked"] = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE last_asked >= datetime('now','-7 days') AND status='open'") as cur:
                stats["recent_activity"] = (await cur.fetchone())[0]
            return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Failed to get gap stats: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/gaps/{gap_id}")
async def api_get_gap(gap_id: int, db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                """SELECT id, topic, question, context, first_asked, last_asked, times_asked,
                   asked_by_users, status, resolved_at, resolved_via, priority_score,
                   assigned_to_user, memo_path, notes, curation_status, curation_reason,
                   curated_at, curated_by_username FROM knowledge_gaps WHERE id = ?""",
                (gap_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return {"success": False, "error": "Gap not found"}
            return {"success": True, "gap": {
                "id": row[0], "topic": row[1], "question": row[2], "context": row[3],
                "first_asked": row[4], "last_asked": row[5], "times_asked": row[6],
                "asked_by_users": row[7], "status": row[8], "resolved_at": row[9],
                "resolved_via": row[10], "priority_score": row[11], "assigned_to_user": row[12],
                "memo_path": row[13], "notes": row[14], "curation_status": row[15],
                "curation_reason": row[16], "curated_at": row[17], "curated_by": row[18],
            }}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.patch("/api/v1/gaps/{gap_id}")
async def api_update_gap(gap_id: int, update: GapUpdate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        audit = AuditService(db)
        async with db.acquire() as conn:
            updates, params = [], []
            before_gap = await _fetch_gap_row(conn, gap_id)
            if not before_gap:
                return {"success": False, "error": "Gap not found"}
            existing_notes = before_gap.get("notes")
            if update.status is not None:
                updates.append("status = ?")
                params.append(update.status)
                if update.status == "resolved":
                    updates.append("resolved_at = datetime('now')")
            if update.curation_status is not None:
                updates.append("curation_status = ?")
                params.append(update.curation_status)
                updates.append("curated_at = datetime('now')")
                updates.append("curated_by_username = ?")
                params.append(_actor_display_name(current_actor))
            if update.curation_reason is not None:
                updates.append("curation_reason = ?")
                params.append(update.curation_reason)
            if update.priority_score is not None:
                updates.append("priority_score = ?")
                params.append(update.priority_score)
            has_structured_change = bool(updates)
            note_detail = update.notes if update.notes is not None else None
            if not has_structured_change and note_detail is None:
                return {"success": False, "error": "No fields to update"}
            updates.append("notes = ?")
            params.append(_append_actor_note(existing_notes, current_actor, "Updated knowledge gap", note_detail))
            params.append(gap_id)
            await conn.execute(f"UPDATE knowledge_gaps SET {', '.join(updates)} WHERE id = ?", params)
            await conn.commit()
            after_gap = await _fetch_gap_row(conn, gap_id)
        await audit.record_mutation(
            entity_type="knowledge_gap",
            entity_id=gap_id,
            action="human_correction",
            before=before_gap,
            after=after_gap,
            actor_id=current_actor.actor_id,
            metadata={"route": "api_update_gap"},
        )
        return {"success": True, "gap_id": gap_id}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/gaps/bulk")
async def api_bulk_gap_action(data: BulkGapAction, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    if not data.gap_ids:
        return {"success": False, "error": "No gaps selected"}
    try:
        current_actor = _resolve_actor(current_actor)
        audit = AuditService(db)
        async with db.acquire() as conn:
            before_rows = {}
            for gap_id in data.gap_ids:
                row = await _fetch_gap_row(conn, gap_id)
                if row:
                    before_rows[gap_id] = row
            ph = ",".join("?" * len(data.gap_ids))
            action_map = {
                "delete": (f"DELETE FROM knowledge_gaps WHERE id IN ({ph})", "Deleted"),
                "defer": (f"UPDATE knowledge_gaps SET curation_status='defer', curated_at=datetime('now'), curated_by_username=? WHERE id IN ({ph})", "Deferred"),
                "discard": (f"UPDATE knowledge_gaps SET curation_status='discard', status='resolved', resolved_via='dismissed', resolved_at=datetime('now'), curated_at=datetime('now'), curated_by_username=? WHERE id IN ({ph})", "Discarded"),
                "keep": (f"UPDATE knowledge_gaps SET curation_status='keep', curated_at=datetime('now'), curated_by_username=? WHERE id IN ({ph})", "Marked as keep"),
                "resolve": (f"UPDATE knowledge_gaps SET status='resolved', resolved_at=datetime('now'), resolved_via='manual', curated_by_username=? WHERE id IN ({ph})", "Resolved"),
            }
            if data.action not in action_map:
                return {"success": False, "error": f"Unknown action: {data.action}"}
            sql, verb = action_map[data.action]
            params = data.gap_ids if data.action == "delete" else [_actor_display_name(current_actor), *data.gap_ids]
            await conn.execute(sql, params)
            await conn.commit()
            after_rows = {}
            for gap_id in data.gap_ids:
                row = await _fetch_gap_row(conn, gap_id)
                if row:
                    after_rows[gap_id] = row
        action_name = "knowledge_gap_deleted" if data.action == "delete" else "human_correction"
        for gap_id in data.gap_ids:
            if gap_id not in before_rows and gap_id not in after_rows:
                continue
            await audit.record_mutation(
                entity_type="knowledge_gap",
                entity_id=gap_id,
                action=action_name,
                before=before_rows.get(gap_id),
                after=after_rows.get(gap_id),
                actor_id=current_actor.actor_id,
                metadata={"route": "api_bulk_gap_action", "bulk_action": data.action},
            )
        return {"success": True, "message": f"{verb} {len(data.gap_ids)} gaps", "affected": len(data.gap_ids)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/gaps/{gap_id}/answer")
async def api_answer_gap(gap_id: int, data: GapAnswer, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Answer a knowledge gap from the admin UI and optionally generate a memo file."""
    current_actor = _resolve_actor(current_actor)
    answer_text = (data.answer or "").strip()
    if not answer_text:
        return {"success": False, "error": "Answer cannot be empty."}

    import re
    from datetime import datetime

    import config as app_config
    audit = AuditService(db)

    async with db.acquire() as conn:
        before_gap = await _fetch_gap_row(conn, gap_id)
        async with conn.execute(
            "SELECT topic, question, notes FROM knowledge_gaps WHERE id = ?",
            (gap_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {"success": False, "error": "Knowledge gap not found."}

        topic, question, existing_notes = row
        memo_relpath = None
        if data.create_memo:
            docs_root = Path(app_config.directory_path)
            date_path = docs_root / "admin_answers" / datetime.utcnow().strftime("%Y/%m")
            date_path.mkdir(parents=True, exist_ok=True)

            title = (data.memo_title or f"Knowledge Gap {gap_id}").strip()
            slug_base = f"{topic or ''} {question or ''}".strip() or f"gap_{gap_id}"
            slug = re.sub(r"[^a-z0-9_]+", "_", slug_base.lower())[:50].strip("_")
            filename = f"{datetime.utcnow().strftime('%Y-%m-%d')}_{slug}.md"
            memo_path = date_path / filename

            frontmatter = "\n".join(
                [
                    "---",
                    f"title: {title}",
                    "source: admin_answer",
                    f"gap_id: {gap_id}",
                    f"created_at: {datetime.utcnow().isoformat()}Z",
                    "---",
                    "",
                ]
            )
            body = "\n".join(
                [
                    "## Question",
                    str(question or "").strip(),
                    "",
                    "## Answer",
                    answer_text,
                    "",
                ]
            )
            memo_path.write_text(frontmatter + body, encoding="utf-8")
            invalidate_knowledge_caches()
            memo_relpath = str(memo_path.relative_to(docs_root))

        now = datetime.utcnow().isoformat() + "Z"
        note_prefix = f"[Admin Answer by {_actor_display_name(current_actor)} {now}]"
        appended_notes = f"{note_prefix}\n{answer_text}"
        merged_notes = (existing_notes or "").strip()
        if merged_notes:
            merged_notes = merged_notes + "\n\n" + appended_notes
        else:
            merged_notes = appended_notes

        await conn.execute(
            """
            UPDATE knowledge_gaps
            SET status = 'resolved',
                resolved_at = ?,
                resolved_via = 'admin_answer',
                memo_path = COALESCE(?, memo_path),
                notes = ?,
                response_count = COALESCE(response_count, 0) + 1,
                last_response_at = ?
            WHERE id = ?
            """,
            (now, memo_relpath, merged_notes, now, gap_id),
        )
        await conn.commit()
        after_gap = await _fetch_gap_row(conn, gap_id)

    await audit.record_mutation(
        entity_type="knowledge_gap",
        entity_id=gap_id,
        action="human_correction",
        before=before_gap,
        after=after_gap,
        actor_id=current_actor.actor_id,
        metadata={"route": "api_answer_gap", "memo_path": memo_relpath},
    )

    return {"success": True, "gap_id": gap_id, "memo_path": memo_relpath}


@router.delete("/api/v1/gaps/{gap_id}")
async def api_delete_gap(gap_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        audit = AuditService(db)
        async with db.acquire() as conn:
            before_gap = await _fetch_gap_row(conn, gap_id)
            await conn.execute("DELETE FROM knowledge_gaps WHERE id = ?", (gap_id,))
            await conn.commit()
        await audit.record_mutation(
            entity_type="knowledge_gap",
            entity_id=gap_id,
            action="knowledge_gap_deleted",
            before=before_gap,
            after=None,
            actor_id=current_actor.actor_id,
            metadata={"route": "api_delete_gap"},
        )
        return {"success": True, "gap_id": gap_id}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Corpus Growth Dashboard API
# =============================================================================

@router.get("/api/v1/corpus/growth")
async def api_corpus_growth(db=Depends(get_db)):
    """Return corpus growth dashboard data — topic coverage, gaps, and suggested actions."""
    try:
        from core.chroma_factory import get_vectorstore
        from services.corpus_health import CorpusHealthService

        vectorstore = get_vectorstore()
        svc = CorpusHealthService(vectorstore=vectorstore, db=db)
        dashboard = await svc.build_growth_dashboard()
        return {"success": True, **dashboard}
    except Exception as exc:
        logger.error("Corpus growth dashboard failed: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/corpus/growth/discord-embed")
async def api_corpus_growth_embed(db=Depends(get_db)):
    """Return a Discord-friendly text summary of corpus growth for bot use."""
    try:
        from core.chroma_factory import get_vectorstore
        from services.corpus_health import CorpusHealthService

        vectorstore = get_vectorstore()
        svc = CorpusHealthService(vectorstore=vectorstore, db=db)
        dashboard = await svc.build_growth_dashboard()

        lines = [
            f"📊 **Your Knowledge Base**: {dashboard['total_facts']} facts across "
            f"{dashboard['total_topics']} topics",
        ]

        if dashboard["strongest"]:
            strongest_str = ", ".join(
                f"{t['topic']} ({t['chunks']})" for t in dashboard["strongest"][:3]
            )
            lines.append(f"💪 **Strongest**: {strongest_str}")

        if dashboard["thin_topics"]:
            weakest_str = ", ".join(
                f"{t['topic']} ({t['corpus_chunks']} docs)" for t in dashboard["thin_topics"][:3]
            )
            lines.append(f"⚠️ **Needs content**: {weakest_str}")

        if dashboard["suggested_actions"]:
            action = dashboard["suggested_actions"][0]
            lines.append(f"👉 **Next step**: {action['action']}")

        return {"success": True, "text": "\n".join(lines)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Chunk Enrichment API
# =============================================================================

@router.get("/api/v1/knowledge/enrichment/status")
async def api_enrichment_status():
    """Return the current enrichment status and stats for all chunks."""
    try:
        from core.chroma_factory import get_vectorstore
        from services.chunk_enrichment import get_active_job

        db = get_vectorstore()
        raw = db.get(include=["metadatas"])
        metas = raw["metadatas"]
        total = len(metas)
        enriched = sum(1 for m in metas if m.get("enriched"))
        pending = total - enriched

        # Content type distribution (for enriched chunks)
        type_dist = {}
        for m in metas:
            ct = m.get("llm_content_type", "")
            if ct:
                type_dist[ct] = type_dist.get(ct, 0) + 1

        # Average actionability
        actionability_vals = [m.get("llm_actionability", 0) for m in metas if m.get("enriched")]
        avg_actionability = (
            round(sum(actionability_vals) / len(actionability_vals), 2) if actionability_vals else 0
        )

        # Active job progress
        job = get_active_job()
        job_progress = job.progress if job else None

        return {
            "success": True,
            "total_chunks": total,
            "enriched": enriched,
            "pending": pending,
            "content_types": type_dist,
            "avg_actionability": avg_actionability,
            "job": job_progress,
        }
    except Exception as exc:
        logger.error("Enrichment status failed: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/knowledge/enrichment/start")
async def api_start_enrichment(request: Request):
    """Start a background re-enrichment job for all un-enriched chunks."""
    try:
        from services.chunk_enrichment import start_reenrichment

        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        force = body.get("force", False)
        model = body.get("model", None)

        job = await start_reenrichment(force=force, model=model)
        return {"success": True, "message": "Re-enrichment started", "progress": job.progress}
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.error("Failed to start enrichment: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/knowledge/enrichment/cancel")
async def api_cancel_enrichment():
    """Cancel the active re-enrichment job."""
    from services.chunk_enrichment import get_active_job

    job = get_active_job()
    if not job or job.progress["status"] != "running":
        return {"success": False, "error": "No active enrichment job to cancel"}
    job.cancel()
    return {"success": True, "message": "Cancellation requested"}

