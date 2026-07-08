"""
Built-in tool definitions for MKA's tool registry.

Each tool wraps an existing service capability (artifact CRUD, knowledge
search) as an LLM-callable function.  Tools are registered at startup
by ``build_default_registry()``.

These are the *bounded* operations the LLM can perform — the constrained
action space that makes MKA a domain-specific agent rather than a
general-purpose one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.tool_registry import (
    Tool,
    ToolCategory,
    ToolParameter,
    ToolRegistry,
    ToolResult,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ARTIFACT TOOLS — Actions, Decisions, Leads, Meetings
# =============================================================================


async def _create_action(
    *,
    db: Any,
    title: str,
    description: str = "",
    priority: str = "medium",
    assigned_to: str = "",
    due_date: str = "",
) -> ToolResult:
    """Create a new action item."""
    now = datetime.utcnow().isoformat()
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO tasks (title, description, status, priority,
                   assigned_to_username, due_date, created_at, updated_at)
                   VALUES (?, ?, 'todo', ?, ?, ?, ?, ?)""",
                (
                    title.strip()[:180],
                    description[:500] if description else None,
                    priority if priority in ("low", "medium", "high", "urgent") else "medium",
                    assigned_to or None,
                    due_date or None,
                    now,
                    now,
                ),
            ) as cur:
                action_id = cur.lastrowid
            await conn.commit()
        ref = f"[action#{action_id}]"
        return ToolResult(
            success=True,
            message=f"Created action: '{title}' (#{action_id})",
            data={"id": action_id, "title": title, "status": "todo", "priority": priority},
            artifact_refs=[ref],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to create action: {exc}")


async def _update_action(
    *,
    db: Any,
    action_id: int,
    status: str = "",
    priority: str = "",
    assigned_to: str = "",
    due_date: str = "",
    title: str = "",
) -> ToolResult:
    """Update an existing action item."""
    now = datetime.utcnow().isoformat()
    updates, params = [], []

    if title:
        updates.append("title = ?")
        params.append(title.strip()[:180])
    if status and status in ("todo", "in_progress", "blocked", "done", "cancelled"):
        updates.append("status = ?")
        params.append(status)
        if status == "done":
            updates.append("completed_at = ?")
            params.append(now)
    if priority and priority in ("low", "medium", "high", "urgent"):
        updates.append("priority = ?")
        params.append(priority)
    if assigned_to:
        updates.append("assigned_to_username = ?")
        params.append(assigned_to)
    if due_date:
        updates.append("due_date = ?")
        params.append(due_date)

    if not updates:
        return ToolResult(success=False, message="No valid fields to update")

    updates.append("updated_at = ?")
    params.append(now)
    params.append(action_id)

    try:
        async with db.acquire() as conn:
            # Verify it exists
            async with conn.execute("SELECT id, title FROM tasks WHERE id = ?", (action_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return ToolResult(success=False, message=f"Action #{action_id} not found")
            await conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
            await conn.commit()
        ref = f"[action#{action_id}]"
        return ToolResult(
            success=True,
            message=f"Updated action #{action_id}: '{row['title']}'",
            data={"id": action_id},
            artifact_refs=[ref],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to update action: {exc}")


async def _list_actions(
    *,
    db: Any,
    status: str = "",
    assigned_to: str = "",
    limit: int = 10,
) -> ToolResult:
    """List action items, optionally filtered."""
    try:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if assigned_to:
            where.append("assigned_to_username LIKE ?")
            params.append(f"%{assigned_to}%")
        w = f"WHERE {' AND '.join(where)}" if where else ""
        limit = min(limit, 25)
        params.append(limit)
        async with db.acquire() as conn:
            async with conn.execute(
                f"SELECT id, title, status, priority, assigned_to_username, due_date FROM tasks {w} ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        return ToolResult(
            success=True,
            message=f"Found {len(rows)} action(s)",
            data={"actions": rows, "count": len(rows)},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to list actions: {exc}")


async def _list_operational_records(
    *,
    db: Any,
    record_type: str = "",
    state: str = "",
    limit: int = 10,
) -> ToolResult:
    """List canonical operational records (actions, decisions, blockers, source links) extracted from conversations."""
    try:
        from core.services.operational_record_service import OperationalRecordService

        svc = OperationalRecordService(db)
        rows = await svc.list_records(
            record_type=record_type or None,
            state=state or None,
            limit=min(limit, 50),
        )
        # Flatten for LLM readability
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "type": r["record_type"],
                "title": r["title"],
                "summary": r.get("summary"),
                "state": r["state"],
                "due": r.get("due_at"),
                "deliverables": r.get("deliverables"),
                "source_conversation": r.get("source_context_id"),
                "created": r.get("created_at"),
                "updated": r.get("updated_at"),
            })
        return ToolResult(
            success=True,
            message=f"Found {len(items)} operational record(s)",
            data={"records": items, "count": len(items)},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to list operational records: {exc}")


async def _create_decision(
    *,
    db: Any,
    title: str,
    decision: str,
    rationale: str = "",
    decided_by: str = "",
    category: str = "",
) -> ToolResult:
    """Record a decision."""
    now = datetime.utcnow().isoformat()
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO decisions (title, decision, rationale, decided_by,
                   category, decided_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (title.strip()[:200], decision[:1000], rationale[:500] or None,
                 decided_by or None, category or None, now),
            ) as cur:
                dec_id = cur.lastrowid
            await conn.commit()
        ref = f"[decision#{dec_id}]"
        return ToolResult(
            success=True,
            message=f"Recorded decision: '{title}' (#{dec_id})",
            data={"id": dec_id, "title": title},
            artifact_refs=[ref],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to record decision: {exc}")


async def _create_lead(
    *,
    db: Any,
    name: str,
    source: str = "",
    contact_name: str = "",
    notes: str = "",
    next_action: str = "",
    value_estimate: str = "",
) -> ToolResult:
    """Create a new lead."""
    now = datetime.utcnow().isoformat()
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO leads (name, source, status, contact_name, value_estimate,
                   notes, next_action, created_at, updated_at, last_activity)
                   VALUES (?, ?, 'cold', ?, ?, ?, ?, ?, ?, ?)""",
                (name.strip()[:200], source or None, contact_name or None,
                 value_estimate or None, notes[:500] or None, next_action or None,
                 now, now, now),
            ) as cur:
                lead_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO lead_activity (lead_id, activity_type, summary) VALUES (?, ?, ?)",
                (lead_id, "creation", f"Lead created via chat from {source or 'manual'}"),
            )
            await conn.commit()
        ref = f"[lead#{lead_id}]"
        return ToolResult(
            success=True,
            message=f"Created lead: '{name}' (#{lead_id})",
            data={"id": lead_id, "name": name, "stage": "cold"},
            artifact_refs=[ref],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to create lead: {exc}")


async def _advance_lead(
    *,
    db: Any,
    lead_id: int,
    new_stage: str,
    note: str = "",
) -> ToolResult:
    """Advance a lead to a new pipeline stage."""
    valid_stages = {"cold", "warm", "hot", "proposal", "won", "lost"}
    if new_stage not in valid_stages:
        return ToolResult(success=False, message=f"Invalid stage '{new_stage}'. Valid: {valid_stages}")

    now = datetime.utcnow().isoformat()
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT id, name, status FROM leads WHERE id = ?", (lead_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return ToolResult(success=False, message=f"Lead #{lead_id} not found")
            old_stage = row["status"]
            await conn.execute(
                "UPDATE leads SET status = ?, updated_at = ?, last_activity = ? WHERE id = ?",
                (new_stage, now, now, lead_id),
            )
            summary = f"Stage: {old_stage} → {new_stage}" + (f" — {note}" if note else "")
            await conn.execute(
                "INSERT INTO lead_activity (lead_id, activity_type, summary) VALUES (?, ?, ?)",
                (lead_id, "status_change", summary),
            )
            await conn.commit()
        ref = f"[lead#{lead_id}]"
        return ToolResult(
            success=True,
            message=f"Advanced lead #{lead_id} '{row['name']}': {old_stage} → {new_stage}",
            data={"id": lead_id, "old_stage": old_stage, "new_stage": new_stage},
            artifact_refs=[ref],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to advance lead: {exc}")


# =============================================================================
# KNOWLEDGE TOOLS — Search, gaps
# =============================================================================


async def _search_knowledge(
    *,
    query: str,
    max_results: int = 5,
) -> ToolResult:
    """Search the knowledge base using RAG retrieval."""
    try:
        from services.hyde_retrieval import hyde_retrieve

        from core.chroma_factory import get_vectorstore

        vectorstore = get_vectorstore()
        docs = await hyde_retrieve(vectorstore, query, k=max_results * 2)

        results = []
        for doc in docs[:max_results]:
            source = doc.metadata.get("source_relpath") or doc.metadata.get("source", "unknown")
            results.append({
                "source": source,
                "content": doc.page_content[:300],
                "content_type": doc.metadata.get("content_type", ""),
            })

        return ToolResult(
            success=True,
            message=f"Found {len(results)} relevant document(s)",
            data={"results": results, "count": len(results)},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Knowledge search failed: {exc}")


async def _search_web(
    *,
    query: str,
    max_results: int = 4,
) -> ToolResult:
    """Search the web for current information using Tavily.

    Use this when the knowledge base doesn't have the answer and the
    question is about current events, external standards, industry
    practices, or anything that changes over time.
    """
    try:
        import os

        from services.tavily_service import TavilyService
        from services.web_research import chat_web_augment

        tavily_key = os.getenv("TAVILY_API_KEY")
        if not tavily_key:
            return ToolResult(
                success=False,
                message="Web search is not configured (TAVILY_API_KEY not set)",
            )

        tavily = TavilyService(tavily_key)
        if not tavily.is_configured:
            return ToolResult(
                success=False,
                message="Web search service is not available",
            )

        context_block = await chat_web_augment(tavily, query, max_results=max_results)
        if not context_block:
            return ToolResult(
                success=True,
                message="Web search returned no results",
                data={"results": [], "count": 0},
            )

        # Parse the context block into structured results
        lines = context_block.strip().split("\n")
        results = []
        for line in lines:
            if line.startswith("•"):
                results.append({"snippet": line.lstrip("• ").strip()})

        return ToolResult(
            success=True,
            message=f"Found {len(results)} web result(s)",
            data={"results": results, "count": len(results), "raw": context_block},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Web search failed: {exc}")


async def _create_knowledge_gap(
    *,
    db: Any,
    question: str,
    context: str = "",
    priority: str = "medium",
) -> ToolResult:
    """Create a knowledge gap for questions that need human answers."""
    now = datetime.utcnow().isoformat()
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO knowledge_gaps (question, context, status, priority,
                   times_asked, created_at, updated_at)
                   VALUES (?, ?, 'open', ?, 1, ?, ?)""",
                (question.strip()[:500], context[:500] or None,
                 priority if priority in ("low", "medium", "high") else "medium",
                 now, now),
            ) as cur:
                gap_id = cur.lastrowid
            await conn.commit()
        ref = f"[gap#{gap_id}]"
        return ToolResult(
            success=True,
            message=f"Created knowledge gap: '{question[:80]}…' (#{gap_id})",
            data={"id": gap_id, "question": question},
            artifact_refs=[ref],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to create gap: {exc}")


async def _save_knowledge(
    *,
    db: Any,
    title: str,
    content: str,
    category: str = "general",
    trigger_ingest: bool = True,
) -> ToolResult:
    """Save a piece of knowledge to the document corpus.

    This creates a markdown file in ``docs/memos/`` with proper YAML
    frontmatter and triggers a background ChromaDB ingest so the
    information is immediately available for future RAG retrieval.
    """
    import re as _re
    from pathlib import Path

    content = (content or "").strip()
    if not content:
        return ToolResult(success=False, message="Content cannot be empty")
    if len(content) < 20:
        return ToolResult(success=False, message="Content too short to be useful — add more detail")

    try:
        import yaml

        import config as app_config

        now = datetime.utcnow()
        title_clean = (title or "").strip() or f"Knowledge note {now.strftime('%Y-%m-%d %H:%M')}"

        slug = _re.sub(r"[^a-z0-9_]+", "_", title_clean.lower())[:50].strip("_") or "note"

        docs_root = Path(app_config.directory_path)
        memo_dir = docs_root / "memos" / str(now.year) / f"{now.month:02d}"
        memo_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{now.day:02d}_{slug}.md"
        filepath = memo_dir / filename

        # Avoid overwriting
        counter = 1
        while filepath.exists():
            filename = f"{now.day:02d}_{slug}_{counter}.md"
            filepath = memo_dir / filename
            counter += 1

        meta = {
            "title": title_clean,
            "doc_type": "human_knowledge",
            "category": category,
            "source": "chat_tool",
            "created_at": now.isoformat() + "Z",
        }
        frontmatter = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n"
        filepath.write_text(frontmatter + content, encoding="utf-8")

        # Trigger background ingest so it's immediately RAG-searchable when requested.
        if trigger_ingest:
            try:

                from admin.routers.knowledge import _schedule_background_ingest
                _schedule_background_ingest()
            except Exception:
                pass  # ingest will pick it up on next sync

        rel_path = str(filepath.relative_to(docs_root))
        return ToolResult(
            success=True,
            message=f"Saved to knowledge base: '{title_clean}' → {rel_path}",
            data={"file": rel_path, "title": title_clean},
            artifact_refs=[f"[doc:{rel_path}]"],
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to save knowledge: {exc}")


async def _create_document(
    *,
    db: Any,
    title: str,
    content: str,
    category: str = "general",
) -> ToolResult:
    """Create a durable memo/brief/report/outline/proposal document artifact."""
    return await _save_knowledge(
        db=db,
        title=title,
        content=content,
        category=category,
        trigger_ingest=False,
    )


async def _list_leads(
    *,
    db: Any,
    stage: str = "",
    limit: int = 10,
) -> ToolResult:
    """List leads, optionally filtered by pipeline stage."""
    try:
        where, params = [], []
        if stage:
            where.append("status = ?")
            params.append(stage)
        w = f"WHERE {' AND '.join(where)}" if where else ""
        limit = min(limit, 25)
        params.append(limit)
        async with db.acquire() as conn:
            async with conn.execute(
                f"SELECT id, name, status, contact_name, value_estimate, next_action, last_activity FROM leads {w} ORDER BY updated_at DESC LIMIT ?",
                params,
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        return ToolResult(
            success=True,
            message=f"Found {len(rows)} lead(s)",
            data={"leads": rows, "count": len(rows)},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to list leads: {exc}")


async def _get_overdue_actions(*, db: Any) -> ToolResult:
    """Get all overdue action items (past due date, not done/cancelled)."""
    try:
        async with db.acquire() as conn, conn.execute(
            """SELECT id, title, status, priority, assigned_to_username, due_date
                   FROM tasks
                   WHERE due_date < date('now')
                     AND status NOT IN ('done', 'cancelled')
                   ORDER BY due_date ASC
                   LIMIT 20""",
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        refs = [f"[action#{r['id']}]" for r in rows]
        return ToolResult(
            success=True,
            message=f"{len(rows)} overdue action(s)",
            data={"actions": rows, "count": len(rows)},
            artifact_refs=refs,
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to get overdue actions: {exc}")


async def _get_pipeline_summary(*, db: Any) -> ToolResult:
    """Get a summary of the leads pipeline."""
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                "SELECT status, COUNT(*) FROM leads GROUP BY status"
            ) as cur:
                pipeline = {r[0]: r[1] for r in await cur.fetchall()}
            async with conn.execute(
                "SELECT COUNT(*) FROM leads WHERE last_activity < datetime('now', '-7 days') AND status NOT IN ('won', 'lost')"
            ) as cur:
                stale = (await cur.fetchone())[0]
        return ToolResult(
            success=True,
            message=f"Pipeline: {dict(pipeline)}. {stale} stale lead(s).",
            data={"pipeline": pipeline, "stale_count": stale},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to get pipeline summary: {exc}")


# =============================================================================
# OPERATIONAL INTELLIGENCE TOOLS — read-only views into background work
# =============================================================================


async def _get_recent_discoveries(
    *,
    db: Any,
    days: int = 14,
    limit: int = 10,
) -> ToolResult:
    """Get recent web research discoveries and their assessment status."""
    days = min(max(days, 1), 90)
    limit = min(max(limit, 1), 25)
    try:
        async with db.acquire() as conn, conn.execute(
            """SELECT rso.id, rso.title, rso.url, rso.source_query,
                          rso.assessment, rso.assessment_reason,
                          rso.first_seen_date, rso.seen_count,
                          l.name AS lead_name, l.status AS lead_status
                   FROM rainmaker_seen_opportunities rso
                   LEFT JOIN leads l ON rso.lead_id = l.id
                   WHERE rso.first_seen_date >= date('now', ?)
                   ORDER BY
                       CASE rso.assessment WHEN 'elevated' THEN 0 ELSE 1 END,
                       rso.first_seen_date DESC
                   LIMIT ?""",
            (f"-{days} days", limit),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        elevated = sum(1 for r in rows if r.get("assessment") == "elevated")
        return ToolResult(
            success=True,
            message=f"Found {len(rows)} discovery(ies) in the last {days} days ({elevated} elevated to leads)",
            data={"discoveries": rows, "count": len(rows), "elevated_count": elevated},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to get discoveries: {exc}")


async def _get_knowledge_gaps(
    *,
    db: Any,
    status: str = "open",
    limit: int = 10,
) -> ToolResult:
    """List knowledge gaps — questions the assistant can't fully answer yet."""
    limit = min(max(limit, 1), 25)
    try:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        # Exclude curated-away gaps
        where.append("(curation_status IS NULL OR curation_status = 'keep')")
        w = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        async with db.acquire() as conn, conn.execute(
            f"""SELECT id, topic, question, times_asked, priority_score,
                           first_asked, last_asked, assigned_to_user, status
                    FROM knowledge_gaps
                    {w}
                    ORDER BY priority_score DESC, times_asked DESC
                    LIMIT ?""",
            params,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return ToolResult(
            success=True,
            message=f"Found {len(rows)} knowledge gap(s)",
            data={"gaps": rows, "count": len(rows)},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to get knowledge gaps: {exc}")


async def _get_health_trend(
    *,
    db: Any,
    days: int = 7,
) -> ToolResult:
    """Get engagement and health metrics over recent days."""
    days = min(max(days, 1), 90)
    try:
        async with db.acquire() as conn, conn.execute(
            """SELECT snapshot_date, questions_asked, questions_helpful,
                          questions_unhelpful, commands_used, unique_users,
                          docs_ingested, gaps_opened, gaps_closed,
                          memos_written, leads_created, leads_won
                   FROM bot_health_snapshots
                   ORDER BY snapshot_date DESC
                   LIMIT ?""",
            (days,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return ToolResult(
                success=True,
                message="No health snapshots available yet",
                data={"snapshots": [], "summary": {}},
            )
        total_q = sum(r.get("questions_asked") or 0 for r in rows)
        total_helpful = sum(r.get("questions_helpful") or 0 for r in rows)
        total_gaps_opened = sum(r.get("gaps_opened") or 0 for r in rows)
        total_gaps_closed = sum(r.get("gaps_closed") or 0 for r in rows)
        helpfulness = round(total_helpful / total_q * 100) if total_q > 0 else 0
        summary = {
            "period_days": len(rows),
            "total_questions": total_q,
            "helpfulness_pct": helpfulness,
            "gaps_opened": total_gaps_opened,
            "gaps_closed": total_gaps_closed,
            "total_leads_created": sum(r.get("leads_created") or 0 for r in rows),
            "total_leads_won": sum(r.get("leads_won") or 0 for r in rows),
        }
        return ToolResult(
            success=True,
            message=f"Health trend ({len(rows)} days): {total_q} questions, {helpfulness}% helpful",
            data={"snapshots": rows, "summary": summary},
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to get health trend: {exc}")


async def _get_learning_progress(*, db: Any) -> ToolResult:
    """Get the learning loop progress — how the assistant is improving."""
    try:
        async with db.acquire() as conn:
            # Event type counts
            async with conn.execute(
                """SELECT event_type, COUNT(*) AS cnt, MAX(created_at) AS latest
                   FROM learning_loop_events
                   GROUP BY event_type
                   ORDER BY cnt DESC"""
            ) as cur:
                events = [dict(r) for r in await cur.fetchall()]

            # Recent events
            async with conn.execute(
                """SELECT event_type, description, created_at
                   FROM learning_loop_events
                   ORDER BY created_at DESC LIMIT 10"""
            ) as cur:
                recent = [dict(r) for r in await cur.fetchall()]

            # Blind spots
            async with conn.execute(
                """SELECT question_pattern, occurrence_count, last_asked_at
                   FROM recurring_blind_spots
                   WHERE status = 'open'
                   ORDER BY occurrence_count DESC LIMIT 5"""
            ) as cur:
                blind_spots = [dict(r) for r in await cur.fetchall()]

        return ToolResult(
            success=True,
            message=f"Learning loop: {len(events)} event types tracked, {len(blind_spots)} open blind spot(s)",
            data={
                "event_summary": events,
                "recent_events": recent,
                "blind_spots": blind_spots,
            },
        )
    except Exception as exc:
        return ToolResult(success=False, message=f"Failed to get learning progress: {exc}")


# =============================================================================
# REGISTRY BUILDER
# =============================================================================


def build_default_registry() -> ToolRegistry:
    """
    Build the default tool registry with all built-in tools.

    Call this at application startup.  The resulting registry can be
    filtered at query-time by passing a workflows config dict.
    """
    registry = ToolRegistry()

    # ── Artifact tools (mutating) ──────────────────────────────────────

    registry.register(Tool(
        name="create_document",
        description="Create and save a memo, brief, report, outline, proposal, or plan as a durable document artifact.",
        category=ToolCategory.ARTIFACTS,
        config_gate="memory.enabled",
        mutates=True,
        parameters=[
            ToolParameter("title", "string", "Document title", required=True),
            ToolParameter("content", "string", "Full document content to save", required=True),
            ToolParameter("category", "string", "Document category", required=False, default="general"),
        ],
        executor=_create_document,
    ))

    registry.register(Tool(
        name="create_action",
        description="Create a new action item / task with a title, optional description, priority, owner, and due date.",
        category=ToolCategory.ARTIFACTS,
        config_gate="work.action_items.enabled",
        mutates=True,
        parameters=[
            ToolParameter("title", "string", "Short title for the action item", required=True),
            ToolParameter("description", "string", "Detailed description", required=False, default=""),
            ToolParameter("priority", "string", "Priority level", required=False, default="medium",
                          enum=["low", "medium", "high", "urgent"]),
            ToolParameter("assigned_to", "string", "Username of the person responsible", required=False, default=""),
            ToolParameter("due_date", "string", "Due date in YYYY-MM-DD format", required=False, default=""),
        ],
        executor=_create_action,
    ))

    registry.register(Tool(
        name="update_action",
        description="Update an existing action item — change its status, priority, owner, due date, or title.",
        category=ToolCategory.ARTIFACTS,
        config_gate="work.action_items.enabled",
        mutates=True,
        parameters=[
            ToolParameter("action_id", "integer", "The ID of the action item to update", required=True),
            ToolParameter("status", "string", "New status", required=False, default="",
                          enum=["todo", "in_progress", "blocked", "done", "cancelled"]),
            ToolParameter("priority", "string", "New priority", required=False, default="",
                          enum=["low", "medium", "high", "urgent"]),
            ToolParameter("assigned_to", "string", "New owner username", required=False, default=""),
            ToolParameter("due_date", "string", "New due date (YYYY-MM-DD)", required=False, default=""),
            ToolParameter("title", "string", "New title", required=False, default=""),
        ],
        executor=_update_action,
    ))

    registry.register(Tool(
        name="list_actions",
        description="List action items, optionally filtered by status (todo, in_progress, blocked, done, cancelled) or assigned person.",
        category=ToolCategory.ARTIFACTS,
        config_gate="work.action_items.enabled",
        mutates=False,
        parameters=[
            ToolParameter("status", "string", "Filter by status", required=False, default=""),
            ToolParameter("assigned_to", "string", "Filter by owner username", required=False, default=""),
            ToolParameter("limit", "integer", "Max results (default 10, max 25)", required=False, default=10),
        ],
        executor=_list_actions,
    ))

    registry.register(Tool(
        name="list_operational_records",
        description=(
            "List tracked work extracted from conversations — actions, decisions, "
            "blockers, and source links. Each record is traced to its source "
            "conversation with timestamps. Use when users ask about existing work, "
            "past decisions, deadlines, or deliverables."
        ),
        category=ToolCategory.ARTIFACTS,
        config_gate="work.action_items.enabled",
        mutates=False,
        parameters=[
            ToolParameter("record_type", "string", "Filter by type", required=False, default="",
                          enum=["action", "decision", "blocker", "source_link"]),
            ToolParameter("state", "string", "Filter by state", required=False, default="",
                          enum=["open", "in_progress", "blocked", "done", "canceled", "overdue", "stale"]),
            ToolParameter("limit", "integer", "Max results (default 10, max 50)", required=False, default=10),
        ],
        executor=_list_operational_records,
    ))

    registry.register(Tool(
        name="create_decision",
        description="Record a decision — what was decided, why (rationale), who decided, and optional category.",
        category=ToolCategory.ARTIFACTS,
        config_gate="memory.decisions.enabled",
        mutates=True,
        parameters=[
            ToolParameter("title", "string", "Short title for the decision", required=True),
            ToolParameter("decision", "string", "What was decided", required=True),
            ToolParameter("rationale", "string", "Why this was decided", required=False, default=""),
            ToolParameter("decided_by", "string", "Who made the decision", required=False, default=""),
            ToolParameter("category", "string", "Category (e.g. strategy, hiring, product)", required=False, default=""),
        ],
        executor=_create_decision,
    ))

    registry.register(Tool(
        name="create_lead",
        description="Create a new lead / opportunity in the pipeline with name, source, contact, and notes.",
        category=ToolCategory.PIPELINE,
        config_gate="pipeline.leads.enabled",
        mutates=True,
        parameters=[
            ToolParameter("name", "string", "Name of the lead / opportunity", required=True),
            ToolParameter("source", "string", "Where the lead came from", required=False, default=""),
            ToolParameter("contact_name", "string", "Contact person", required=False, default=""),
            ToolParameter("notes", "string", "Additional context", required=False, default=""),
            ToolParameter("next_action", "string", "What should happen next", required=False, default=""),
            ToolParameter("value_estimate", "string", "Estimated value (e.g. '$5,000')", required=False, default=""),
        ],
        executor=_create_lead,
    ))

    registry.register(Tool(
        name="advance_lead",
        description="Move a lead to a new pipeline stage: cold → warm → hot → proposal → won/lost.",
        category=ToolCategory.PIPELINE,
        config_gate="pipeline.leads.enabled",
        mutates=True,
        parameters=[
            ToolParameter("lead_id", "integer", "The ID of the lead to advance", required=True),
            ToolParameter("new_stage", "string", "Target stage", required=True,
                          enum=["cold", "warm", "hot", "proposal", "won", "lost"]),
            ToolParameter("note", "string", "Optional note about why", required=False, default=""),
        ],
        executor=_advance_lead,
    ))

    registry.register(Tool(
        name="list_leads",
        description="List leads in the pipeline, optionally filtered by stage.",
        category=ToolCategory.PIPELINE,
        config_gate="pipeline.leads.enabled",
        mutates=False,
        parameters=[
            ToolParameter("stage", "string", "Filter by pipeline stage", required=False, default="",
                          enum=["cold", "warm", "hot", "proposal", "won", "lost"]),
            ToolParameter("limit", "integer", "Max results (default 10)", required=False, default=10),
        ],
        executor=_list_leads,
    ))

    # ── Knowledge tools ────────────────────────────────────────────────

    registry.register(Tool(
        name="search_knowledge",
        description="Search the knowledge base for information. Returns relevant document excerpts with sources.",
        category=ToolCategory.KNOWLEDGE,
        config_gate="memory.enabled",
        mutates=False,
        parameters=[
            ToolParameter("query", "string", "What to search for", required=True),
            ToolParameter("max_results", "integer", "Max results (default 5)", required=False, default=5),
        ],
        executor=_search_knowledge,
    ))

    registry.register(Tool(
        name="search_web",
        description=(
            "Search the web for current information. Use when the knowledge base "
            "doesn't have the answer, or for real-time data, industry standards, "
            "news, regulations, or anything that changes over time."
        ),
        category=ToolCategory.KNOWLEDGE,
        config_gate="memory.enabled",
        mutates=False,
        parameters=[
            ToolParameter("query", "string", "What to search the web for", required=True),
            ToolParameter("max_results", "integer", "Max results (default 4)", required=False, default=4),
        ],
        executor=_search_web,
    ))

    registry.register(Tool(
        name="create_knowledge_gap",
        description="Flag a question that needs a human answer — the system doesn't have enough information to answer it reliably.",
        category=ToolCategory.KNOWLEDGE,
        config_gate="memory.gaps.enabled",
        mutates=True,
        parameters=[
            ToolParameter("question", "string", "The question that needs answering", required=True),
            ToolParameter("context", "string", "Context about why this matters", required=False, default=""),
            ToolParameter("priority", "string", "How urgent", required=False, default="medium",
                          enum=["low", "medium", "high"]),
        ],
        executor=_create_knowledge_gap,
    ))

    registry.register(Tool(
        name="save_knowledge",
        description=(
            "Save a piece of knowledge, fact, procedure, or document draft to the knowledge base "
            "so it can be retrieved in future conversations. Use when the user "
            "teaches you something new, corrects a wrong answer, or shares information "
            "worth preserving. The content is immediately searchable."
        ),
        category=ToolCategory.KNOWLEDGE,
        config_gate="memory.enabled",
        mutates=True,
        parameters=[
            ToolParameter("title", "string", "Short descriptive title for this knowledge", required=True),
            ToolParameter("content", "string", "The knowledge to save — be detailed and specific", required=True),
            ToolParameter("category", "string", "Category (e.g. operations, policy, partner, procedure)", required=False, default="general"),
        ],
        executor=_save_knowledge,
    ))

    # ── Read-only status tools ─────────────────────────────────────────

    registry.register(Tool(
        name="get_overdue_actions",
        description="Get all action items that are past their due date and not yet completed.",
        category=ToolCategory.SYSTEM,
        config_gate="work.action_items.enabled",
        mutates=False,
        parameters=[],
        executor=_get_overdue_actions,
    ))

    registry.register(Tool(
        name="get_pipeline_summary",
        description="Get a summary of the leads pipeline — how many leads at each stage and how many are stale.",
        category=ToolCategory.PIPELINE,
        config_gate="pipeline.enabled",
        mutates=False,
        parameters=[],
        executor=_get_pipeline_summary,
    ))

    # ── Operational intelligence tools (read-only) ─────────────────────

    registry.register(Tool(
        name="get_recent_discoveries",
        description=(
            "Get recent web research discoveries — opportunities, partnerships, "
            "and leads the assistant found through background research. "
            "Use when users ask about new opportunities, market intel, or what's been found."
        ),
        category=ToolCategory.SYSTEM,
        config_gate="pipeline.enabled",
        mutates=False,
        parameters=[
            ToolParameter("days", "integer", "How many days back to look (default 14)", required=False, default=14),
            ToolParameter("limit", "integer", "Max results (default 10)", required=False, default=10),
        ],
        executor=_get_recent_discoveries,
    ))

    registry.register(Tool(
        name="get_knowledge_gaps",
        description=(
            "List questions the assistant can't fully answer yet — "
            "knowledge gaps that need human input. Use when users ask "
            "about what the assistant doesn't know or what needs attention."
        ),
        category=ToolCategory.KNOWLEDGE,
        config_gate="memory.gaps.enabled",
        mutates=False,
        parameters=[
            ToolParameter("status", "string", "Filter by status (default: open)", required=False, default="open",
                          enum=["open", "in_progress", "resolved"]),
            ToolParameter("limit", "integer", "Max results (default 10)", required=False, default=10),
        ],
        executor=_get_knowledge_gaps,
    ))

    registry.register(Tool(
        name="get_health_trend",
        description=(
            "Get engagement and performance metrics — question volume, helpfulness rate, "
            "gaps opened/closed, leads created. Use when users ask about how the "
            "assistant is performing or want operational metrics."
        ),
        category=ToolCategory.SYSTEM,
        config_gate="memory.enabled",
        mutates=False,
        parameters=[
            ToolParameter("days", "integer", "How many days of history (default 7)", required=False, default=7),
        ],
        executor=_get_health_trend,
    ))

    registry.register(Tool(
        name="get_learning_progress",
        description=(
            "See how the assistant is improving — learning loop events, "
            "blind spots being addressed, knowledge being built. "
            "Use when users ask about the assistant's learning or improvement."
        ),
        category=ToolCategory.SYSTEM,
        config_gate="memory.enabled",
        mutates=False,
        parameters=[],
        executor=_get_learning_progress,
    ))

    logger.info("Default tool registry built: %d tools", registry.tool_count)
    return registry
