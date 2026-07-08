"""Conversation operational extractor.

Background autonomous service that scans recent multi-turn conversations
for proposed operational records that need human review before becoming
canonical state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List

from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_record_service import OperationalRecordService

logger = logging.getLogger(__name__)


# ── LLM Prompt ───────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """\
You are an operational continuity extractor reviewing a conversation between a user and an AI assistant.

## Conversation Transcript
{transcript}

## Task
Identify any proposed operational records in this conversation that a human
should review before storing as canonical continuity data. Extract only items
explicitly grounded in the conversation itself.

Focus on these record types only:
- actions: commitments, assigned follow-ups, or concrete next steps
- decisions: choices, approvals, or explicit resolutions
- blockers: constraints preventing work or decisions from progressing
- source_link: URLs, file references, or cited evidence that should be tracked
- conditional: contingency plans, fallback agreements, or "if X then Y" commitments
- correction: explicit reversals or amendments to earlier statements, plans, or decisions
- risk_caveat: stated assumptions, risks, or caveats qualifying a commitment

Do NOT extract:
- Pure assistant suggestions not adopted by the human
- Casual conversation, greetings, or chit-chat
- Vague aspirations without a concrete operational meaning
- Information that is clearly uncertain but not actually stated

Respond in STRICT JSON only:
{{
    "has_proposals": <true if extractable operational records were found>,
    "proposals": [
    {{
            "record_type": "<action|decision|blocker|source_link|conditional|correction|risk_caveat>",
            "title": "<short clear title>",
            "summary": "<concise summary of what should be reviewed>",
            "fields": {{
                "title": "<title again for canonical payload>",
                "summary": "<summary again for canonical payload>",
                "decision": "<decision text if record_type=decision>",
                "rationale": "<why this exists, if explicit>",
                "due_at": "<ISO date if explicit, else empty>",
                "url": "<URL or file reference if record_type=source_link>",
                "notes": "<other explicit structured detail worth preserving>"
            }},
            "confidence": <0.0-1.0 estimate, clearly uncertain when the conversation is ambiguous>,
            "field_confidence": {{
                "title": <0.0-1.0>,
                "summary": <0.0-1.0>,
                "decision": <0.0-1.0 if present>,
                "due_at": <0.0-1.0 if present>,
                "url": <0.0-1.0 if present>
            }},
            "rationale": "<brief explanation of why this proposal was extracted>",
            "supporting_snippet": "<short verbatim supporting quote from the transcript>"
    }}
  ]
}}

Rules:
- Confidence is not truth. Use lower scores when wording is ambiguous or partial.
- Keep supporting_snippet verbatim from the transcript.
- Do not invent owners, due dates, or URLs.
- If a field is not explicit, leave it empty or omit it.

If no extractable proposals are found, return:
{{"has_proposals": false, "proposals": []}}
"""


# ── Main Entry Point ─────────────────────────────────────────────────────────


async def mine_recent_conversations(
    db: Any,
    *,
    llm_service: Any = None,
    min_turns: int = 4,
    max_extracts: int = 3,
    lookback_hours: int = 24,
    bot: Any = None,
) -> Dict[str, Any]:
    """Scan recent conversations and extract durable knowledge.

    Parameters
    ----------
    db : database pool
        Async database connection pool.
    llm_service : optional
        LLM service for knowledge extraction. Auto-detected if not provided.
    min_turns : int
        Minimum number of turns for a conversation to be eligible.
    max_extracts : int
        Maximum knowledge extracts to save per run.
    lookback_hours : int
        How far back to look for conversations (default: 24h).
    bot : optional
        Discord bot instance for accessing DocumentAuthor cog.

    Returns
    -------
    dict
        Summary: sessions_scanned, proposals_created, proposals_skipped.
    """
    result = {
        "sessions_scanned": 0,
        "proposals_created": 0,
        "proposals_skipped": 0,
        "extracts_saved": 0,
        "extracts_skipped": 0,
        "errors": 0,
    }

    try:
        # Load config
        from core.config_loader import WorkflowConfig
        wf = WorkflowConfig.load()
        if not wf.cq_conversation_mining_enabled:
            logger.debug("Conversation mining disabled by config")
            return result

        min_turns = wf.cq_conversation_mining_min_turns
        max_extracts = wf.cq_conversation_mining_max_per_run

        # Get eligible sessions
        sessions = await _get_eligible_sessions(db, min_turns, lookback_hours)
        if not sessions:
            logger.debug("No eligible conversations to mine")
            return result

        # Get LLM service
        if not llm_service:
            llm_service = _get_llm_service(bot)
        if not llm_service:
            logger.debug("Conversation mining skipped: no LLM service available")
            return result

        proposal_service = ExtractionProposalService(db)
        operational_records = OperationalRecordService(db)
        system_actor = await operational_records.ensure_actor(
            actor_kind="system_job",
            external_ref="conversation_miner",
            display_name="Conversation Miner",
        )

        total_saved = 0
        for session in sessions:
            if total_saved >= max_extracts:
                break

            result["sessions_scanned"] += 1

            try:
                # Get the turns for this session
                turns = await _get_session_turns(db, session["id"])
                if not turns or len(turns) < min_turns:
                    continue

                # Build transcript
                transcript = _build_transcript(turns)
                if len(transcript) < 100:
                    continue

                # Extract knowledge via LLM
                proposals = await _extract_operational_proposals(llm_service, transcript)
                if not proposals:
                    continue

                source_details = {
                    "label": session.get("summary") or f"Conversation session {session['id']}",
                    "summary": transcript[:280],
                    "turn_count": len(turns),
                }

                for proposal in proposals:
                    if total_saved >= max_extracts:
                        break

                    # Skip if a similar proposal was recently rejected
                    # (same record_type + similar title from same session source)
                    if await _recently_rejected(
                        db, proposal["record_type"], proposal.get("title", ""),
                    ):
                        result["proposals_skipped"] += 1
                        continue

                    created = await proposal_service.create_proposal(
                        record_type=proposal["record_type"],
                        title=proposal.get("title"),
                        summary=proposal.get("summary"),
                        extracted_fields=proposal.get("fields") or {},
                        created_by_actor_id=int(system_actor["id"]),
                        record_confidence=float(proposal.get("confidence", 0.0)),
                        field_confidences=proposal.get("field_confidence") or {},
                        rationale=proposal.get("rationale"),
                        supporting_snippet=proposal.get("supporting_snippet"),
                        source_entity_type="conversation_session",
                        source_entity_id=session["id"],
                        source_context_id=f"conversation_session:{session['id']}",
                        source_details=source_details,
                        extraction_metadata={
                            "pipeline": "conversation_miner",
                            "prompt_version": "operational_review_v1",
                            "model": getattr(llm_service, "model", None) or getattr(llm_service, "default_model", None),
                        },
                    )
                    if created:
                        total_saved += 1
                        result["proposals_created"] += 1
                    else:
                        result["proposals_skipped"] += 1

                # Mark session as mined
                await _mark_session_mined(db, session["id"])

            except Exception as exc:
                logger.warning(
                    "Failed to mine session %s: %s", session["id"], exc
                )
                result["errors"] += 1

        result["extracts_saved"] = result["proposals_created"]
        result["extracts_skipped"] = result["proposals_skipped"]
        return result

    except Exception as exc:
        logger.error("Conversation mining failed: %s", exc)
        result["errors"] += 1
        return result


# ── Internal Helpers ──────────────────────────────────────────────────────────


async def _get_eligible_sessions(
    db: Any, min_turns: int, lookback_hours: int
) -> List[Dict[str, Any]]:
    """Find conversation sessions eligible for mining."""
    try:
        async with db.acquire() as conn, conn.execute(
            """SELECT id, turn_count, last_active, summary, topics
                   FROM conversation_sessions
                   WHERE status = 'active'
                     AND turn_count >= ?
                     AND last_active >= datetime('now', ? || ' hours')
                     AND (mined_at IS NULL OR mined_at < datetime('now', '-24 hours'))
                   ORDER BY turn_count DESC
                   LIMIT 10""",
            (min_turns, f"-{lookback_hours}"),
        ) as cur:
            rows = await cur.fetchall()

        return [dict(r) for r in rows] if rows else []

    except Exception as exc:
        # Table might not have mined_at column yet; handle gracefully
        if "mined_at" in str(exc):
            try:
                async with db.acquire() as conn:
                    await conn.execute(
                        "ALTER TABLE conversation_sessions ADD COLUMN mined_at TEXT"
                    )
                    await conn.commit()
                # Retry
                return await _get_eligible_sessions(db, min_turns, lookback_hours)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        logger.debug("Failed to get eligible sessions: %s", exc)
        return []


async def _recently_rejected(
    db: Any, record_type: str, title: str, lookback_hours: int = 72,
) -> bool:
    """Check whether a similar proposal was recently rejected.

    Prevents the miner from re-creating proposals that a human already
    dismissed — without waiting for the nightly feedback loop.
    """
    if not title:
        return False
    try:
        async with db.acquire() as conn, conn.execute(
            """SELECT 1 FROM operational_extraction_proposals
                   WHERE status = 'rejected'
                     AND record_type = ?
                     AND title = ?
                     AND reviewed_at >= datetime('now', ? || ' hours')
                   LIMIT 1""",
            (record_type, title, f"-{lookback_hours}"),
        ) as cur:
            return (await cur.fetchone()) is not None
    except Exception:
        return False  # table may not exist yet


async def _get_session_turns(
    db: Any, session_id: str
) -> List[Dict[str, Any]]:
    """Get turns for a conversation session."""
    try:
        async with db.acquire() as conn, conn.execute(
            """SELECT role, content, created_at
                   FROM conversation_turns
                   WHERE session_id = ?
                   ORDER BY created_at ASC
                   LIMIT 50""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()

        return [dict(r) for r in rows] if rows else []

    except Exception as exc:
        logger.debug("Failed to get session turns: %s", exc)
        return []


def _build_transcript(turns: List[Dict[str, Any]]) -> str:
    """Build a readable transcript from conversation turns."""
    parts = []
    for t in turns:
        role = "User" if t["role"] == "user" else "Assistant"
        content = t["content"][:500] if t["content"] else ""
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


async def _extract_operational_proposals(
    llm_service: Any, transcript: str
) -> List[Dict[str, Any]]:
    """Use LLM to extract proposed operational records from a transcript."""
    # Truncate transcript to avoid token limits
    transcript_limit = 4000
    transcript_truncated = transcript[:transcript_limit]
    if len(transcript) > transcript_limit:
        transcript_truncated += (
            f"\n[TRUNCATED: {len(transcript) - transcript_limit} chars omitted "
            f"— extract only from the visible portion]"
        )

    prompt = _EXTRACTION_PROMPT.format(transcript=transcript_truncated)

    try:
        from services.model_router import LLMTimeouts
        from services.rag_pipeline import get_pipeline_router
        router = await get_pipeline_router()
        _timeout = router.timeouts.conversation_mining if router else LLMTimeouts().conversation_mining
    except Exception:
        _timeout = 20.0

    try:
        raw = await asyncio.wait_for(
            llm_service.complete(prompt, max_tokens=500, temperature=0.1),
            timeout=_timeout,
        )

        # Parse JSON response
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\"proposals\"\s*:\s*\[.*?\]\s*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                logger.debug("Could not parse extraction response: %s", text[:200])
                return []

        if not data.get("has_proposals"):
            return []

        extracts = data.get("proposals", [])
        valid = []
        for ext in extracts:
            record_type = str(ext.get("record_type", "")).strip().lower()
            title = str(ext.get("title", "")).strip()
            fields = ext.get("fields") or {}
            if record_type not in {"action", "decision", "blocker", "source_link",
                                    "conditional", "correction", "risk_caveat"}:
                continue
            if not title:
                continue
            valid.append(
                {
                    "record_type": record_type,
                    "title": title,
                    "summary": str(ext.get("summary", "")).strip() or None,
                    "fields": fields if isinstance(fields, dict) else {},
                    "confidence": ext.get("confidence", 0.0),
                    "field_confidence": ext.get("field_confidence") if isinstance(ext.get("field_confidence"), dict) else {},
                    "rationale": str(ext.get("rationale", "")).strip() or None,
                    "supporting_snippet": str(ext.get("supporting_snippet", "")).strip() or None,
                }
            )

        return valid

    except asyncio.TimeoutError:
        logger.debug("Knowledge extraction timed out")
        return []
    except Exception as exc:
        logger.debug("Knowledge extraction failed: %s", exc)
        return []
async def _mark_session_mined(db: Any, session_id: str) -> None:
    """Mark a session as having been mined for knowledge."""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """UPDATE conversation_sessions
                   SET mined_at = datetime('now')
                   WHERE id = ?""",
                (session_id,),
            )
            await conn.commit()
    except Exception as exc:
        # Column might not exist yet; add it
        if "mined_at" in str(exc):
            try:
                async with db.acquire() as conn:
                    await conn.execute(
                        "ALTER TABLE conversation_sessions ADD COLUMN mined_at TEXT"
                    )
                    await conn.commit()
                await _mark_session_mined(db, session_id)
            except Exception as e:
                logger.warning("_mark_session_mined: suppressed %s", e)
        else:
            logger.debug("Failed to mark session mined: %s", exc)


def _get_llm_service(bot: Any = None) -> Any:
    """Get the LLM service from the bot's service container."""
    if bot:
        sc = getattr(bot, "service_container", None)
        if sc:
            return getattr(sc, "llm", None) or getattr(sc, "llm_service", None)
    return None
