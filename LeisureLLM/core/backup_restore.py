"""
backup_restore — Database backup, restore, and support bundle utilities.

Provides:
- Full SQLite backup (via VACUUM INTO or file copy)
- Config snapshot (org_profile.yaml, workflows.yaml, bot_settings.json)
- Support bundle (config + schema + stats + redacted env)
- Restore from a backup archive
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────
_BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"
_REDACT_KEYS = {"token", "api_key", "secret", "password", "webhook_url"}


def _ensure_backup_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


def _timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


# ── Database backup ──────────────────────────────────────────

def backup_database(db_path: str | Path, *, label: str = "") -> Path:
    """
    Create a consistent point-in-time copy of the SQLite database.

    Uses the SQLite `VACUUM INTO` command for a safe hot backup.
    Falls back to file copy if VACUUM INTO is unavailable.

    Returns the path to the backup file.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    dest_dir = _ensure_backup_dir()
    suffix = f"_{label}" if label else ""
    dest = dest_dir / f"assistant{suffix}_{_timestamp()}.db"

    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("VACUUM INTO ?", (str(dest),))
        conn.close()
        logger.info("Database backed up via VACUUM INTO → %s", dest)
    except sqlite3.OperationalError:
        # Fallback: plain file copy (less safe during concurrent writes)
        shutil.copy2(str(db_path), str(dest))
        logger.info(f"Database backed up via file copy → {dest}")

    return dest


def list_backups() -> list[Dict[str, Any]]:
    """List available backup files with metadata."""
    dest_dir = _ensure_backup_dir()
    backups = []
    for f in sorted(dest_dir.glob("assistant*.db"), reverse=True):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "path": str(f),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return backups


def restore_database(backup_path: str | Path, db_path: str | Path) -> Path:
    """
    Restore a database from a backup.

    - Creates a safety backup of the current DB first
    - Copies the backup file over the active database
    - Returns the path of the safety backup
    """
    backup_path = Path(backup_path)
    db_path = Path(db_path)

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    # Safety backup of current DB
    safety = None
    if db_path.exists():
        safety = backup_database(db_path, label="pre_restore")

    shutil.copy2(str(backup_path), str(db_path))
    logger.info(f"Database restored from {backup_path}")
    return safety


# ── Config snapshot ──────────────────────────────────────────

def _redact(data: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """Recursively redact sensitive values."""
    if depth > 10:
        return data
    result = {}
    for k, v in data.items():
        if any(secret in k.lower() for secret in _REDACT_KEYS):
            result[k] = "***REDACTED***" if v else None
        elif isinstance(v, dict):
            result[k] = _redact(v, depth + 1)
        else:
            result[k] = v
    return result


def snapshot_config(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Capture a snapshot of all configuration files.

    Returns a dict with the config filename as key and contents as value.
    Sensitive values are redacted.
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    snapshot: Dict[str, Any] = {}

    # YAML configs
    for name in ("org_profile.yaml", "workflows.yaml"):
        p = base_dir / name
        if p.exists():
            try:
                import yaml
                with open(p) as f:
                    data = yaml.safe_load(f) or {}
                snapshot[name] = _redact(data) if isinstance(data, dict) else data
            except ImportError:
                snapshot[name] = p.read_text(encoding="utf-8")

    # JSON configs
    config_dir = base_dir / "LeisureLLM" / "config"
    if config_dir.exists():
        for f in config_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                snapshot[f"config/{f.name}"] = _redact(data) if isinstance(data, dict) else data
            except json.JSONDecodeError:
                snapshot[f"config/{f.name}"] = "<invalid JSON>"

    # .env presence (not contents)
    env_file = base_dir / ".env"
    snapshot[".env_exists"] = env_file.exists()

    return snapshot


# ── Support bundle ───────────────────────────────────────────

def create_support_bundle(
    db_path: str | Path,
    *,
    base_dir: Optional[Path] = None,
) -> Path:
    """
    Create a support bundle ZIP containing:
    - Redacted config snapshot
    - Database schema (no data)
    - Table row counts
    - Environment fingerprint
    - Recent logs (if available)

    Returns the path to the ZIP file.
    """
    db_path = Path(db_path)
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    bundle_dir = _ensure_backup_dir()
    bundle_path = bundle_dir / f"support_bundle_{_timestamp()}.zip"

    bundle_data: Dict[str, str] = {}

    # 1. Config snapshot
    try:
        cfg = snapshot_config(base_dir)
        bundle_data["config_snapshot.json"] = json.dumps(cfg, indent=2, default=str)
    except Exception as e:
        bundle_data["config_snapshot.json"] = json.dumps({"error": str(e)})

    # 2. Database schema + row counts
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = cursor.fetchall()

            schema_lines = []
            counts: Dict[str, int] = {}
            for name, sql in tables:
                if name.startswith("sqlite_"):
                    continue
                schema_lines.append(f"-- {name}")
                schema_lines.append(sql or "")
                schema_lines.append("")
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()
                    counts[name] = row[0] if row else 0
                except Exception as e:
                    logger.debug("Could not count rows for table %s: %s", name, e)
                    counts[name] = -1

            conn.close()

            bundle_data["schema.sql"] = "\n".join(schema_lines)
            bundle_data["row_counts.json"] = json.dumps(counts, indent=2)
        except Exception as e:
            bundle_data["database_error.txt"] = str(e)
    else:
        bundle_data["database_error.txt"] = f"Database not found: {db_path}"

    # 3. Environment fingerprint
    env_info = {
        "os": platform.platform(),
        "python": sys.version,
        "arch": platform.machine(),
        "cwd": str(Path.cwd()),
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "db_size_mb": round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.exists() else 0,
        "timestamp": datetime.utcnow().isoformat(),
    }
    bundle_data["environment.json"] = json.dumps(env_info, indent=2)

    # 4. Recent log file (last 500 lines)
    log_candidates = [
        base_dir / "bot.log",
        base_dir / "LeisureLLM" / "bot.log",
        base_dir / "logs" / "bot.log",
    ]
    for log_path in log_candidates:
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                bundle_data["recent_log.txt"] = "\n".join(lines[-500:])
            except Exception as e:
                bundle_data["recent_log_error.txt"] = f"Could not read log: {e}"
            break

    # 5. Write ZIP
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in bundle_data.items():
            zf.writestr(name, content)

    logger.info(f"Support bundle created: {bundle_path}")
    return bundle_path
