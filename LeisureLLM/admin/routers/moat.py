"""
Moat router — API endpoints for moat infrastructure services.

Exposes:
- User preferences (personalisation moat)
- Folder watcher (workflow integration moat)
- Inference cost tracking (cost counter-position moat)
- Encrypted backups and data retention (privacy/trust moat)
- Feedback learning loop analytics (network effect moat)
"""

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from admin.dependencies import get_db, require_admin

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["moat"], dependencies=[Depends(require_admin)])


# ═══════════════════════════════════════════════════════════════
# 1. USER PREFERENCES — Personalisation Moat
# ═══════════════════════════════════════════════════════════════

class SetPreferenceRequest(BaseModel):
    key: str
    value: str | int | float | bool | list | None


class SetPreferencesBulkRequest(BaseModel):
    preferences: dict


@router.get("/api/v1/preferences/{user_id}")
async def api_get_preferences(user_id: str, db=Depends(get_db)):
    """Get all preferences for a user (merged with defaults)."""
    from services.user_preferences import UserPreferenceService
    svc = UserPreferenceService(db)
    prefs = await svc.get_preferences(user_id)
    return {"success": True, "user_id": user_id, "preferences": prefs}


@router.put("/api/v1/preferences/{user_id}")
async def api_set_preference(user_id: str, body: SetPreferenceRequest, db=Depends(get_db)):
    """Set a single preference."""
    from services.user_preferences import UserPreferenceService
    svc = UserPreferenceService(db)
    await svc.set_preference(user_id, body.key, body.value)
    return {"success": True, "user_id": user_id, "key": body.key, "value": body.value}


@router.put("/api/v1/preferences/{user_id}/bulk")
async def api_set_preferences_bulk(user_id: str, body: SetPreferencesBulkRequest, db=Depends(get_db)):
    """Set multiple preferences at once."""
    from services.user_preferences import UserPreferenceService
    svc = UserPreferenceService(db)
    await svc.set_preferences_bulk(user_id, body.preferences)
    return {"success": True, "user_id": user_id, "count": len(body.preferences)}


@router.get("/api/v1/preferences/{user_id}/prompt")
async def api_get_preference_prompt(user_id: str, db=Depends(get_db)):
    """Get the adaptive prompt fragment for a user."""
    from services.user_preferences import UserPreferenceService
    svc = UserPreferenceService(db)
    prompt = await svc.build_preference_prompt(user_id)
    return {"success": True, "user_id": user_id, "prompt_fragment": prompt}


@router.get("/api/v1/preferences/{user_id}/export")
async def api_export_preferences(user_id: str, db=Depends(get_db)):
    """Export a user's full preference profile."""
    from services.user_preferences import UserPreferenceService
    svc = UserPreferenceService(db)
    export = await svc.export_preferences(user_id)
    return {"success": True, "export": export}


@router.post("/api/v1/preferences/{user_id}/import")
async def api_import_preferences(user_id: str, request: Request, db=Depends(get_db)):
    """Import preferences from an export bundle."""
    from services.user_preferences import UserPreferenceService
    svc = UserPreferenceService(db)
    data = await request.json()
    ok = await svc.import_preferences(user_id, data)
    return {"success": ok}


# ═══════════════════════════════════════════════════════════════
# 2. FOLDER WATCHER — Workflow Integration Moat
# ═══════════════════════════════════════════════════════════════

class AddFolderRequest(BaseModel):
    folder_path: str
    recursive: bool = True
    extensions: list[str] | None = None


@router.get("/api/v1/watcher/folders")
async def api_list_watched_folders(db=Depends(get_db)):
    """List all watched folders."""
    from services.folder_watcher import FolderWatcherService
    svc = FolderWatcherService(db)
    folders = await svc.list_folders()
    return {"success": True, "folders": folders}


@router.post("/api/v1/watcher/folders")
async def api_add_watched_folder(body: AddFolderRequest, db=Depends(get_db)):
    """Add a new folder to watch."""
    from services.folder_watcher import FolderWatcherService
    svc = FolderWatcherService(db)
    ext = set(body.extensions) if body.extensions else None
    folder_id = await svc.add_folder(body.folder_path, recursive=body.recursive, extensions=ext)
    if folder_id:
        return {"success": True, "folder_id": folder_id, "path": body.folder_path}
    return {"success": False, "error": "Folder does not exist or could not be added"}


@router.delete("/api/v1/watcher/folders")
async def api_remove_watched_folder(request: Request, db=Depends(get_db)):
    """Remove a folder from watching."""
    from services.folder_watcher import FolderWatcherService
    body = await request.json()
    svc = FolderWatcherService(db)
    ok = await svc.remove_folder(body.get("folder_path", ""))
    return {"success": ok}


@router.get("/api/v1/watcher/stats")
async def api_watcher_stats(db=Depends(get_db)):
    """Get folder watcher statistics."""
    from services.folder_watcher import FolderWatcherService
    svc = FolderWatcherService(db)
    stats = await svc.get_stats()
    return {"success": True, **stats}


@router.get("/api/v1/watcher/activity")
async def api_watcher_activity(limit: int = 20, db=Depends(get_db)):
    """Get recent auto-ingest activity."""
    from services.folder_watcher import FolderWatcherService
    svc = FolderWatcherService(db)
    activity = await svc.get_recent_activity(limit)
    return {"success": True, "activity": activity}


# ═══════════════════════════════════════════════════════════════
# 3. INFERENCE COST TRACKING — Cost Counter-Position Moat
# ═══════════════════════════════════════════════════════════════

@router.get("/api/v1/costs/savings")
async def api_cost_savings(days: int = 30, db=Depends(get_db)):
    """Get savings summary — the headline retention metric."""
    from services.inference_cost_tracker import InferenceCostTracker
    tracker = InferenceCostTracker(db)
    summary = await tracker.get_savings_summary(days)
    return {"success": True, **summary}


@router.get("/api/v1/costs/by-model")
async def api_cost_by_model(days: int = 30, db=Depends(get_db)):
    """Get per-model usage breakdown."""
    from services.inference_cost_tracker import InferenceCostTracker
    tracker = InferenceCostTracker(db)
    models = await tracker.get_usage_by_model(days)
    return {"success": True, "models": models}


@router.get("/api/v1/costs/by-role")
async def api_cost_by_role(days: int = 30, db=Depends(get_db)):
    """Get per-pipeline-role usage breakdown."""
    from services.inference_cost_tracker import InferenceCostTracker
    tracker = InferenceCostTracker(db)
    roles = await tracker.get_usage_by_role(days)
    return {"success": True, "roles": roles}


@router.get("/api/v1/costs/trend")
async def api_cost_trend(days: int = 30, db=Depends(get_db)):
    """Get daily token/cost trend for charting."""
    from services.inference_cost_tracker import InferenceCostTracker
    tracker = InferenceCostTracker(db)
    trend = await tracker.get_daily_trend(days)
    return {"success": True, "trend": trend}


@router.get("/api/v1/costs/pricing")
async def api_get_pricing(db=Depends(get_db)):
    """Get the current pricing table."""
    from services.inference_cost_tracker import InferenceCostTracker
    tracker = InferenceCostTracker(db)
    return {"success": True, "pricing": tracker.get_pricing_table()}


class SetBudgetRequest(BaseModel):
    backend_name: str
    period: str  # daily | weekly | monthly
    budget_usd: float
    alert_threshold: float = 0.8


@router.post("/api/v1/costs/budget")
async def api_set_budget(body: SetBudgetRequest, db=Depends(get_db)):
    """Set a spending budget for a backend."""
    from services.inference_cost_tracker import InferenceCostTracker
    tracker = InferenceCostTracker(db)
    await tracker.set_budget(body.backend_name, body.period, body.budget_usd, body.alert_threshold)
    return {"success": True}


# ═══════════════════════════════════════════════════════════════
# 4. ENCRYPTED BACKUP & DATA RETENTION — Privacy/Trust Moat
# ═══════════════════════════════════════════════════════════════

class EncryptedBackupRequest(BaseModel):
    passphrase: str
    label: str = ""
    include_config: bool = True


@router.post("/api/v1/backup/encrypted/create")
async def api_create_encrypted_backup(body: EncryptedBackupRequest, db=Depends(get_db)):
    """Create an AES-256 encrypted backup."""
    from services.encrypted_backup import create_encrypted_backup
    try:
        dest = create_encrypted_backup(
            db.database_path,
            body.passphrase,
            label=body.label,
            include_config=body.include_config,
        )
        return {
            "success": True,
            "path": str(dest),
            "filename": dest.name,
            "size_mb": round(dest.stat().st_size / 1024 / 1024, 2),
        }
    except Exception as e:
        logger.error("Encrypted backup failed: %s", e)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


class EncryptedRestoreRequest(BaseModel):
    filename: str
    passphrase: str
    restore_config: bool = False


@router.post("/api/v1/backup/encrypted/restore")
async def api_restore_encrypted_backup(body: EncryptedRestoreRequest, db=Depends(get_db)):
    """Restore from an encrypted backup."""
    from services.encrypted_backup import _ensure_backup_dir, restore_encrypted_backup
    try:
        backup_dir = _ensure_backup_dir()
        backup_path = (backup_dir / body.filename).resolve()
        if not str(backup_path).startswith(str(backup_dir.resolve())):
            return {"success": False, "error": "Invalid filename"}
        safety = restore_encrypted_backup(
            backup_path, body.passphrase, db.database_path,
            restore_config=body.restore_config,
        )
        return {
            "success": True,
            "safety_backup": str(safety) if safety else None,
            "message": "Encrypted backup restored. Restart for changes to take effect.",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Encrypted restore failed: %s", e)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


class VerifyBackupRequest(BaseModel):
    filename: str
    passphrase: str


@router.post("/api/v1/backup/encrypted/verify")
async def api_verify_encrypted_backup(body: VerifyBackupRequest):
    """Verify an encrypted backup without restoring."""
    from services.encrypted_backup import _ensure_backup_dir, verify_encrypted_backup
    backup_dir = _ensure_backup_dir()
    backup_path = (backup_dir / body.filename).resolve()
    if not str(backup_path).startswith(str(backup_dir.resolve())):
        return {"success": False, "error": "Invalid filename"}
    result = verify_encrypted_backup(backup_path, body.passphrase)
    return {"success": result.get("valid", False), **result}


@router.post("/api/v1/retention/enforce")
async def api_enforce_retention(db=Depends(get_db)):
    """Run data retention policies to purge old data."""
    from services.encrypted_backup import DataRetentionService
    svc = DataRetentionService(db)
    purged = await svc.enforce_policies()
    return {"success": True, "purged": purged}


@router.get("/api/v1/retention/report")
async def api_retention_report(db=Depends(get_db)):
    """Get data retention status report."""
    from services.encrypted_backup import DataRetentionService
    svc = DataRetentionService(db)
    report = await svc.get_retention_report()
    return {"success": True, **report}


@router.post("/api/v1/audit/export")
async def api_export_audit_log(days: int = 90, db=Depends(get_db)):
    """Export a compliance-ready audit log."""
    from services.encrypted_backup import export_audit_log
    try:
        path = await export_audit_log(db, days=days)
        return {"success": True, "path": str(path), "filename": path.name}
    except Exception as e:
        logger.error("Audit export failed: %s", e)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# ═══════════════════════════════════════════════════════════════
# 5. FEEDBACK LEARNING LOOP — Network Effect Moat
# ═══════════════════════════════════════════════════════════════

@router.get("/api/v1/feedback/chunks/low-quality")
async def api_low_quality_chunks(threshold: float = 0.3, min_retrievals: int = 3, db=Depends(get_db)):
    """Get chunks with consistently poor feedback scores."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    chunks = await loop.get_low_quality_chunks(threshold, min_retrievals)
    return {"success": True, "chunks": chunks, "count": len(chunks)}


@router.get("/api/v1/feedback/chunks/high-quality")
async def api_high_quality_chunks(threshold: float = 0.8, min_retrievals: int = 3, db=Depends(get_db)):
    """Get chunks with consistently positive feedback scores."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    chunks = await loop.get_high_quality_chunks(threshold, min_retrievals)
    return {"success": True, "chunks": chunks, "count": len(chunks)}


@router.post("/api/v1/feedback/learning-cycle")
async def api_run_learning_cycle(db=Depends(get_db)):
    """Run the feedback learning cycle (retire bad variants, surface outliers)."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    results = await loop.run_learning_cycle()
    return {"success": True, **results}


@router.get("/api/v1/feedback/signals")
async def api_feedback_signals(days: int = 30, db=Depends(get_db)):
    """Get aggregated improvement signals by failure mode."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    signals = await loop._aggregate_signals(days)
    return {"success": True, "signals": signals}


@router.get("/api/v1/feedback/export-anonymised")
async def api_export_anonymised_signals(days: int = 30, db=Depends(get_db)):
    """Export anonymised improvement signals (opt-in network effect)."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    export = await loop.export_anonymised_signals(days)
    return {"success": True, **export}


class RegisterVariantRequest(BaseModel):
    variant_name: str
    prompt_text: str
    category: str = "system_prompt"


@router.post("/api/v1/feedback/variants")
async def api_register_variant(body: RegisterVariantRequest, db=Depends(get_db)):
    """Register a new prompt variant for A/B testing."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    await loop.register_prompt_variant(body.variant_name, body.prompt_text, body.category)
    return {"success": True, "variant": body.variant_name}


@router.get("/api/v1/feedback/prompt-suffix")
async def api_active_prompt_suffix(db=Depends(get_db)):
    """Return the currently active feedback-driven prompt suffix."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    suffix = await loop.get_active_prompt_suffix()
    return {"success": True, "has_suffix": suffix is not None, "suffix": suffix}


@router.post("/api/v1/feedback/refine-prompts")
async def api_refine_prompts(days: int = 30, db=Depends(get_db)):
    """Trigger prompt refinement from accumulated feedback patterns."""
    from services.feedback_learning_loop import FeedbackLearningLoop
    loop = FeedbackLearningLoop(db)
    results = await loop._refine_prompts_from_feedback(days)
    return {"success": True, "refinements": results}
