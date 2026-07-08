from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import aiosqlite

logger = logging.getLogger(__name__)

_TRACE_DB_ENV_VAR = "REQUEST_TRACE_DATABASE_PATH"

_REQUEST_TRACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS request_traces (
    request_id TEXT PRIMARY KEY,
    trace_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    entrypoint TEXT NOT NULL,
    route_name TEXT NOT NULL,
    user_visible_flow TEXT NOT NULL,
    conversation_id TEXT,
    thread_id INTEGER,
    packet_id INTEGER,
    lane TEXT NOT NULL,
    query_text_hash TEXT,
    query_word_count INTEGER NOT NULL DEFAULT 0,
    used_cache INTEGER NOT NULL DEFAULT 0,
    cache_key_hash TEXT,
    retrieval_used INTEGER NOT NULL DEFAULT 0,
    retrieval_doc_count INTEGER NOT NULL DEFAULT 0,
    context_word_count INTEGER NOT NULL DEFAULT 0,
    web_augmented INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    retrieval_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    models_used_json TEXT,
    pipeline_stages_json TEXT,
    first_token_ms INTEGER,
    retrieval_ms INTEGER,
    generation_ms INTEGER,
    total_ms INTEGER,
    input_tokens_est INTEGER NOT NULL DEFAULT 0,
    output_tokens_est INTEGER NOT NULL DEFAULT 0,
    total_tokens_est INTEGER NOT NULL DEFAULT 0,
    actual_cost_usd REAL,
    cloud_equiv_cost_usd REAL,
    policy_reason TEXT,
    routing_flags_json TEXT,
    failure_mode TEXT,
    completed_successfully INTEGER NOT NULL DEFAULT 1,
    produced_artifact_type TEXT,
    produced_artifact_id TEXT,
    had_sources INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    background_jobs_spawned_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_request_traces_flow_time
    ON request_traces(user_visible_flow, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_lane_time
    ON request_traces(lane, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_total_ms
    ON request_traces(total_ms DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_failure
    ON request_traces(failure_mode, created_at DESC);

CREATE TABLE IF NOT EXISTS request_stage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES request_traces(request_id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_request_stage_events_request
    ON request_stage_events(request_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_request_stage_events_name_time
    ON request_stage_events(stage_name, created_at DESC);
"""

_trace_init_locks: dict[str, asyncio.Lock] = {}
_trace_write_locks: dict[str, asyncio.Lock] = {}
_trace_schema_ready: set[str] = set()

_ARTIFACT_REF_RE = re.compile(
    r"^\[(?P<artifact_type>[a-z_]+)(?:#|:)(?P<artifact_id>[^\]]+)\]$"
)


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex}"


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def hash_cache_key(query: str, context: str) -> str:
    return hash_text(f"{(query or '').strip().lower()}|{context or ''}")


def parse_primary_artifact_ref(artifact_refs: Sequence[str]) -> tuple[Optional[str], Optional[str]]:
    for ref in artifact_refs or []:
        match = _ARTIFACT_REF_RE.match(ref or "")
        if match:
            return match.group("artifact_type"), match.group("artifact_id")
    return None, None


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _iso_from_epoch(ts: float | None) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _request_trace_insert_sql(placeholders: str) -> str:
    return f"""INSERT OR REPLACE INTO request_traces
               (request_id, trace_id, started_at, finished_at, entrypoint,
                route_name, user_visible_flow, conversation_id, thread_id,
                packet_id, lane, query_text_hash, query_word_count,
                used_cache, cache_key_hash, retrieval_used,
                retrieval_doc_count, context_word_count, web_augmented,
                llm_calls, retrieval_calls, tool_calls, models_used_json,
                pipeline_stages_json, first_token_ms, retrieval_ms,
                generation_ms, total_ms, input_tokens_est,
                output_tokens_est, total_tokens_est, actual_cost_usd,
                cloud_equiv_cost_usd, policy_reason, routing_flags_json,
                failure_mode, completed_successfully,
                produced_artifact_type, produced_artifact_id, had_sources,
                source_count, background_jobs_spawned_json)
               VALUES ({placeholders})"""


def resolve_request_trace_db_path(db_or_path: Any) -> Optional[Path]:
    configured = os.getenv(_TRACE_DB_ENV_VAR)
    if configured:
        return Path(configured)

    raw_path: Optional[Path] = None
    if isinstance(db_or_path, (str, Path)):
        raw_path = Path(db_or_path)
    elif db_or_path is not None:
        database_path = getattr(db_or_path, "database_path", None)
        if database_path:
            raw_path = Path(database_path)

    if raw_path is None:
        return None

    if raw_path.suffix:
        return raw_path.with_name(f"{raw_path.stem}.request_traces{raw_path.suffix}")
    return raw_path.with_name(f"{raw_path.name}.request_traces.db")


def _get_trace_init_lock(db_path: Path) -> asyncio.Lock:
    key = str(db_path)
    lock = _trace_init_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _trace_init_locks[key] = lock
    return lock


def _get_trace_write_lock(db_path: Path) -> asyncio.Lock:
    key = str(db_path)
    lock = _trace_write_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _trace_write_locks[key] = lock
    return lock


async def _open_trace_connection(db_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(str(db_path), timeout=30.0)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA busy_timeout = 30000")
    await conn.execute("PRAGMA synchronous = NORMAL")
    return conn


async def ensure_request_trace_schema(db_or_path: Any) -> Optional[Path]:
    trace_db_path = resolve_request_trace_db_path(db_or_path)
    if trace_db_path is None:
        return None

    key = str(trace_db_path)
    if key in _trace_schema_ready:
        return trace_db_path

    trace_db_path.parent.mkdir(parents=True, exist_ok=True)
    async with _get_trace_init_lock(trace_db_path):
        if key in _trace_schema_ready:
            return trace_db_path
        conn = await _open_trace_connection(trace_db_path)
        try:
            await conn.executescript(_REQUEST_TRACE_SCHEMA_SQL)
            await conn.commit()
        finally:
            await conn.close()
        _trace_schema_ready.add(key)
    return trace_db_path


async def _write_request_trace_rows(
    conn: Any,
    *,
    request_id: str,
    request_row: tuple[Any, ...],
    request_placeholders: str,
    stage_rows: list[tuple[Any, ...]],
) -> None:
    await conn.execute(
        "DELETE FROM request_stage_events WHERE request_id = ?",
        (request_id,),
    )
    await conn.execute(_request_trace_insert_sql(request_placeholders), request_row)
    if stage_rows:
        await conn.executemany(
            """INSERT INTO request_stage_events
               (request_id, stage_name, started_at, finished_at, duration_ms, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            stage_rows,
        )
    await conn.commit()


async def persist_request_trace(
    db: Any,
    *,
    request_id: str,
    trace_id: Optional[str],
    started_at: float,
    finished_at: float,
    entrypoint: str,
    route_name: str,
    user_visible_flow: str,
    lane: str,
    query_text: str,
    conversation_id: Optional[str] = None,
    thread_id: Optional[int] = None,
    packet_id: Optional[int] = None,
    used_cache: bool = False,
    retrieval_used: bool = False,
    retrieval_doc_count: int = 0,
    context_word_count: int = 0,
    web_augmented: bool = False,
    llm_calls: int = 0,
    retrieval_calls: int = 0,
    tool_calls: int = 0,
    models_used: Optional[dict[str, Any]] = None,
    pipeline_stages: Optional[dict[str, Any]] = None,
    first_token_ms: Optional[int] = None,
    retrieval_ms: Optional[int] = None,
    generation_ms: Optional[int] = None,
    total_ms: Optional[int] = None,
    input_tokens_est: int = 0,
    output_tokens_est: int = 0,
    total_tokens_est: int = 0,
    actual_cost_usd: Optional[float] = None,
    cloud_equiv_cost_usd: Optional[float] = None,
    policy_reason: Optional[str] = None,
    routing_flags: Optional[dict[str, Any]] = None,
    failure_mode: Optional[str] = None,
    completed_successfully: bool = True,
    artifact_refs: Optional[Sequence[str]] = None,
    source_count: int = 0,
    background_jobs_spawned: Optional[Sequence[str]] = None,
    stage_events: Optional[Iterable[Any]] = None,
    cache_key_context: Optional[str] = None,
) -> bool:
    if not db:
        return False

    trace_db_path = await ensure_request_trace_schema(db)
    if trace_db_path is None:
        logger.error("Request trace persistence failed for %s: no trace database path is available", request_id)
        return False

    artifact_type, artifact_id = parse_primary_artifact_ref(artifact_refs or [])
    cache_key_hash = hash_cache_key(query_text, cache_key_context or "") if cache_key_context is not None else None
    query_hash = hash_text(query_text)

    request_row = (
        request_id,
        trace_id,
        _iso_from_epoch(started_at),
        _iso_from_epoch(finished_at),
        entrypoint,
        route_name,
        user_visible_flow,
        conversation_id,
        thread_id,
        packet_id,
        lane,
        query_hash,
        len((query_text or "").split()),
        1 if used_cache else 0,
        cache_key_hash,
        1 if retrieval_used else 0,
        retrieval_doc_count,
        context_word_count,
        1 if web_augmented else 0,
        llm_calls,
        retrieval_calls,
        tool_calls,
        json.dumps(models_used or {}, ensure_ascii=True),
        json.dumps(list((pipeline_stages or {}).keys()), ensure_ascii=True),
        first_token_ms,
        retrieval_ms,
        generation_ms,
        total_ms,
        input_tokens_est,
        output_tokens_est,
        total_tokens_est,
        actual_cost_usd,
        cloud_equiv_cost_usd,
        policy_reason,
        json.dumps(routing_flags or {}, ensure_ascii=True),
        failure_mode,
        1 if completed_successfully else 0,
        artifact_type,
        artifact_id,
        1 if source_count > 0 else 0,
        source_count,
        json.dumps(list(background_jobs_spawned or []), ensure_ascii=True),
    )

    stage_rows = []
    for stage in stage_events or []:
        try:
            duration_ms = float(getattr(stage, "elapsed_ms", 0.0) or 0.0)
            finished_iso = _iso_from_epoch(getattr(stage, "timestamp", finished_at))
            started_iso = _iso_from_epoch((getattr(stage, "timestamp", finished_at) or finished_at) - (duration_ms / 1000.0))
            metadata = {
                "doc_count": getattr(stage, "doc_count", 0),
                "detail": getattr(stage, "detail", {}) or {},
                "docs_preview": getattr(stage, "docs_preview", []) or [],
            }
            stage_rows.append(
                (
                    request_id,
                    getattr(stage, "name", "unknown"),
                    started_iso,
                    finished_iso,
                    duration_ms,
                    json.dumps(metadata, ensure_ascii=True),
                )
            )
        except Exception as exc:
            logger.debug("Skipping malformed request stage event: %s", exc)

    request_placeholders = ", ".join(["?"] * len(request_row))
    try:
        async with _get_trace_write_lock(trace_db_path):
            conn = await _open_trace_connection(trace_db_path)
            try:
                await _write_request_trace_rows(
                    conn,
                    request_id=request_id,
                    request_row=request_row,
                    request_placeholders=request_placeholders,
                    stage_rows=stage_rows,
                )
            finally:
                await conn.close()
        return True
    except Exception as exc:
        logger.error(
            "Request trace persistence failed for %s on dedicated trace DB %s: %s",
            request_id,
            trace_db_path,
            exc,
        )
        return False


async def list_request_traces(
    db_or_path: Any,
    *,
    limit: int = 50,
    offset: int = 0,
    lane: Optional[str] = None,
    failures_only: bool = False,
) -> dict[str, Any]:
    trace_db_path = await ensure_request_trace_schema(db_or_path)
    if trace_db_path is None:
        return {"success": True, "traces": [], "count": 0, "total": 0, "limit": limit, "offset": offset}

    where = []
    params: list[Any] = []
    if lane:
        where.append("lane = ?")
        params.append(lane)
    if failures_only:
        where.append("(failure_mode IS NOT NULL OR completed_successfully = 0)")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    conn = await _open_trace_connection(trace_db_path)
    try:
        async with conn.execute(
            f"""SELECT rt.*,
                       (SELECT COUNT(*) FROM request_stage_events rse WHERE rse.request_id = rt.request_id) AS stage_event_count
                FROM request_traces rt
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?""",
            tuple(params + [limit, offset]),
        ) as cur:
            rows = await cur.fetchall()
        async with conn.execute(
            f"SELECT COUNT(*) FROM request_traces {where_sql}",
            tuple(params),
        ) as cur:
            total_row = await cur.fetchone()
    finally:
        await conn.close()

    traces = []
    for row in rows:
        item = dict(row)
        item["models_used"] = json.loads(item.pop("models_used_json", "{}") or "{}")
        item["pipeline_stages"] = json.loads(item.pop("pipeline_stages_json", "[]") or "[]")
        item["routing_flags"] = json.loads(item.pop("routing_flags_json", "{}") or "{}")
        item["background_jobs_spawned"] = json.loads(item.pop("background_jobs_spawned_json", "[]") or "[]")
        traces.append(item)

    return {
        "success": True,
        "traces": traces,
        "count": len(traces),
        "total": total_row[0] if total_row else len(traces),
        "limit": limit,
        "offset": offset,
    }


async def get_request_trace_detail(db_or_path: Any, request_id: str) -> dict[str, Any]:
    trace_db_path = await ensure_request_trace_schema(db_or_path)
    if trace_db_path is None:
        return {"success": False, "error": "Request trace store unavailable"}

    conn = await _open_trace_connection(trace_db_path)
    try:
        async with conn.execute(
            "SELECT * FROM request_traces WHERE request_id = ?",
            (request_id,),
        ) as cur:
            trace_row = await cur.fetchone()
        if not trace_row:
            return {"success": False, "error": "Request trace not found"}
        async with conn.execute(
            "SELECT * FROM request_stage_events WHERE request_id = ? ORDER BY id ASC",
            (request_id,),
        ) as cur:
            stage_rows = await cur.fetchall()
    finally:
        await conn.close()

    trace = dict(trace_row)
    trace["models_used"] = json.loads(trace.pop("models_used_json", "{}") or "{}")
    trace["pipeline_stages"] = json.loads(trace.pop("pipeline_stages_json", "[]") or "[]")
    trace["routing_flags"] = json.loads(trace.pop("routing_flags_json", "{}") or "{}")
    trace["background_jobs_spawned"] = json.loads(trace.pop("background_jobs_spawned_json", "[]") or "[]")

    stages = []
    for row in stage_rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
        stages.append(item)

    return {"success": True, "trace": trace, "stage_events": stages, "count": len(stages)}


async def update_request_trace_after_confirmation(
    db: Any,
    *,
    request_id: str,
    artifact_refs: Optional[Sequence[str]] = None,
    failure_mode: Optional[str] = None,
    completed_successfully: Optional[bool] = None,
    artifact_funnel: Optional[dict[str, Any]] = None,
    stage_name: str = "artifact_confirmation",
) -> None:
    if not db or not request_id:
        return

    trace_db_path = await ensure_request_trace_schema(db)
    if trace_db_path is None:
        logger.warning("Failed to update request trace %s after confirmation: trace database is unavailable", request_id)
        return

    artifact_type, artifact_id = parse_primary_artifact_ref(artifact_refs or [])
    if (
        artifact_type is None
        and artifact_id is None
        and failure_mode is None
        and completed_successfully is None
        and not artifact_funnel
    ):
        return

    try:
        async with _get_trace_write_lock(trace_db_path):
            conn = await _open_trace_connection(trace_db_path)
            try:
                async with conn.execute(
                    """SELECT produced_artifact_type, produced_artifact_id, routing_flags_json,
                              failure_mode, completed_successfully
                       FROM request_traces WHERE request_id = ?""",
                    (request_id,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    return

                existing_flags = json.loads(row[2] or "{}") if row[2] else {}
                merged_flags = existing_flags
                if artifact_funnel:
                    merged_flags = _deep_merge_dict(existing_flags, {"artifact_funnel": artifact_funnel})

                await conn.execute(
                    """UPDATE request_traces
                       SET produced_artifact_type = ?,
                           produced_artifact_id = ?,
                           routing_flags_json = ?,
                           failure_mode = ?,
                           completed_successfully = ?
                       WHERE request_id = ?""",
                    (
                        artifact_type or row[0],
                        artifact_id or row[1],
                        json.dumps(merged_flags, ensure_ascii=True),
                        failure_mode,
                        row[4] if completed_successfully is None else (1 if completed_successfully else 0),
                        request_id,
                    ),
                )
                if artifact_funnel:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    await conn.execute(
                        """INSERT INTO request_stage_events
                           (request_id, stage_name, started_at, finished_at, duration_ms, metadata_json)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            request_id,
                            stage_name,
                            now_iso,
                            now_iso,
                            0.0,
                            json.dumps({"detail": artifact_funnel}, ensure_ascii=True),
                        ),
                    )
                await conn.commit()
            finally:
                await conn.close()
    except Exception as exc:
        logger.warning("Failed to update request trace %s after confirmation: %s", request_id, exc)