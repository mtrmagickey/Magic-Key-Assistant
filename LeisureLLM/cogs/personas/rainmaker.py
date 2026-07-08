"""
Rainmaker Persona - Lead management and pipeline.

The Rainmaker handles business development by:
- Morning pipeline review
- Opportunity hunting (web search)
- Follow-up nudges for stale leads
- Cold lead reviews
- Past client check-ins

Key capabilities:
- Lead creation and tracking
- Pipeline visualization
- Activity logging
- Opportunity assessment with LLM
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from bs4 import BeautifulSoup

from .base import EASTERN

logger = logging.getLogger(__name__)


class RainmakerMixin:
    """
    Rainmaker persona mixin providing lead management capabilities.
    
    This is designed to be mixed into the AutonomousOps cog.
    Requires:
        - self.bot (with db attribute)
        - self.llm_service
        - self.tavily_service
        - self.post_to_bots_channel()
        - self._job_already_ran()
        - self._record_job_run()
    """
    
    # ========================================
    # RAINMAKER: Lead Creation & Management
    # ========================================
    
    async def _rainmaker_create_lead(
        self,
        title: str,
        source: str,
        description: str = None,
        source_id: str = None,
        owner_user_id: int = None,
        owner_username: str = None,
        priority: str = "medium"
    ) -> Optional[int]:
        """Create a new lead and return its ID.

        Writes are audited to the tool_executions table so that
        autonomous lead creation is visible in the same audit trail
        as interactive (chat) tool calls.
        """
        db = getattr(self.bot, "db", None)
        if not db:
            return None

        arguments = {
            "name": title, "source": source, "description": description,
            "source_id": source_id, "priority": priority,
        }
        logger.info(
            "Autonomous mutation: tool=create_lead source=rainmaker args=%s",
            json.dumps(arguments, default=str),
        )

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO leads (name, description, source, source_id, owner_user_id, owner_username, priority)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (title, description, source, source_id, owner_user_id, owner_username, priority)
                )
                await conn.commit()
                async with conn.execute("SELECT last_insert_rowid()") as cursor:
                    row = await cursor.fetchone()
                    lead_id = row[0] if row else None

                if lead_id:
                    await self._rainmaker_log_activity(lead_id, 'creation', f"Lead created from {source}")

                # Audit trail — mirrors tool_registry._persist_execution
                await self._audit_autonomous_tool(
                    conn, "create_lead", arguments,
                    success=lead_id is not None,
                    message=f"Lead #{lead_id}" if lead_id else "insert returned no id",
                    artifact_refs=[f"[lead#{lead_id}]"] if lead_id else [],
                )

                return lead_id
        except Exception as e:
            logger.warning(f"Failed to create lead: {e}")
            return None

    async def _rainmaker_log_activity(
        self,
        lead_id: int,
        activity_type: str,
        description: str,
        old_status: str = None,
        new_status: str = None,
        user_id: int = None,
        username: str = None
    ):
        """Log activity on a lead."""
        db = getattr(self.bot, "db", None)
        if not db:
            return

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO lead_activity (lead_id, activity_type, summary, old_status, new_status, 
                                                created_by_user_id, created_by_username)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (lead_id, activity_type, description, old_status, new_status, user_id, username)
                )
                # Update last_activity
                await conn.execute(
                    "UPDATE leads SET last_activity = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                    (lead_id,)
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to log lead activity: {e}")
    
    # ========================================
    # RAINMAKER: Pipeline Stats & Queries
    # ========================================
    
    async def _rainmaker_get_pipeline_stats(self) -> Dict[str, int]:
        """Get counts by lead status."""
        db = getattr(self.bot, "db", None)
        if not db:
            return {}

        stats = {}
        try:
            async with db.acquire() as conn:
                async with conn.execute(
                    """
                    SELECT status, COUNT(*) as cnt
                    FROM leads
                    WHERE status NOT IN ('won', 'lost', 'dormant')
                    GROUP BY status
                    """
                ) as cursor:
                    for row in await cursor.fetchall():
                        stats[row[0]] = row[1]

                # Won/lost this month
                month_start = datetime.now(EASTERN).replace(day=1).strftime("%Y-%m-%d")
                async with conn.execute(
                    "SELECT COUNT(*) FROM leads WHERE status = 'won' AND closed_at >= ?",
                    (month_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    stats['won_this_month'] = row[0] if row else 0

                async with conn.execute(
                    "SELECT COUNT(*) FROM leads WHERE status = 'lost' AND closed_at >= ?",
                    (month_start,)
                ) as cursor:
                    row = await cursor.fetchone()
                    stats['lost_this_month'] = row[0] if row else 0

        except Exception as e:
            logger.warning(f"Failed to get pipeline stats: {e}")
        return stats

    async def _rainmaker_get_leads_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get leads by status."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, name, description, status, priority, owner_user_id, 
                           next_action, next_action_date, proposal_due_date, last_activity, created_at
                    FROM leads
                    WHERE status = ?
                    ORDER BY priority DESC, last_activity ASC
                    LIMIT 20
                    """,
                (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to get leads by status: {e}")
            return []

    async def _rainmaker_get_stale_leads(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get leads not touched in X days."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, name, status, owner_user_id, last_activity
                    FROM leads
                    WHERE status NOT IN ('won', 'lost', 'dormant')
                      AND (last_activity IS NULL OR last_activity < ?)
                    ORDER BY last_activity ASC
                    LIMIT 20
                    """,
                (cutoff,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to get stale leads: {e}")
            return []

    async def _rainmaker_get_leads_with_action_today(self) -> List[Dict[str, Any]]:
        """Get leads with next_action_date = today."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        today = datetime.now(EASTERN).strftime("%Y-%m-%d")
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, name, status, owner_user_id, next_action, next_action_date
                    FROM leads
                    WHERE status NOT IN ('won', 'lost', 'dormant')
                      AND next_action_date <= ?
                    ORDER BY priority DESC
                    LIMIT 10
                    """,
                (today,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to get today's action leads: {e}")
            return []

    async def _rainmaker_get_overdue_actions(self) -> List[Dict[str, Any]]:
        """Get leads with overdue next_action_date."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        today = datetime.now(EASTERN).strftime("%Y-%m-%d")
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, name, status, owner_user_id, owner_username, next_action, next_action_date
                    FROM leads
                    WHERE status NOT IN ('won', 'lost', 'dormant')
                      AND next_action_date < ?
                    ORDER BY next_action_date ASC
                    LIMIT 15
                    """,
                (today,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to get overdue leads: {e}")
            return []

    async def _rainmaker_get_past_clients_due(self) -> List[Dict[str, Any]]:
        """Get past clients due for re-engagement."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        # Due within next 7 days
        cutoff = (datetime.now(EASTERN) + timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, org_name, contact_name, contact_email, last_project_date, reengagement_date
                    FROM past_clients
                    WHERE reengagement_date IS NOT NULL AND reengagement_date <= ?
                    ORDER BY reengagement_date ASC
                    LIMIT 10
                    """,
                (cutoff,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in (rows or [])]
        except Exception as e:
            logger.warning(f"Failed to get past clients due: {e}")
            return []

    async def _rainmaker_get_recent_discoveries(self, limit: int = 3) -> List[Dict[str, Any]]:
        """Get recently found opportunities from search."""
        db = getattr(self.bot, "db", None)
        if not db:
            return []

        try:
            async with db.acquire() as conn:
                # Get opportunities found in last 3 days that weren't immediately rejected
                async with conn.execute(
                    """
                    SELECT title, url, assessment_reason
                    FROM rainmaker_seen_opportunities
                    WHERE datetime(first_seen_date) >= datetime('now', '-3 days')
                    AND assessment != 'rejected'
                    ORDER BY first_seen_date DESC
                    LIMIT ?
                    """,
                    (limit,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        {'title': r[0], 'url': r[1], 'reason': r[2]}
                        for r in rows
                    ]
        except Exception:
            return []
    
    # ========================================
    # RAINMAKER: Opportunity Assessment
    # ========================================
    
    async def _rainmaker_assess_opportunities(self, findings: List[Dict]) -> List[Dict]:
        """Use LLM to assess which opportunities are worth pursuing."""
        if not findings:
            return []

        # Load operational context for assessment
        operational_ctx = ""
        try:
            ops_path = Path(__file__).parent.parent / "prompts" / "operational_context.txt"
            if ops_path.exists():
                operational_ctx = ops_path.read_text(encoding='utf-8')[:2000]
        except Exception as e:
            logger.warning("_rainmaker_assess_opportunities: suppressed %s", e)

        # Build assessment prompt
        findings_text = ""
        for i, f in enumerate(findings[:10], 1):  # Assess up to 10
            findings_text += f"\n{i}. **{f.get('title', 'No title')}**\n"
            findings_text += f"   Category: {f.get('category', 'Unknown')}\n"
            findings_text += f"   Snippet: {f.get('snippet', '')[:200]}...\n"
            findings_text += f"   URL: {f.get('url', '')}\n"

        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        location_hint = org["location"] or "our area"
        region_hint = org["region"] or "our region"

        prompt = f"""Assess these potential business opportunities for {org["org_name"]}.

=== ORGANISATION PROFILE ===
{org["org_description"]}

=== OPERATIONAL FACTS ===
{operational_ctx}

=== OPPORTUNITIES TO ASSESS ===
{findings_text}

For EACH opportunity, determine:
1. verdict: "elevate" (worth pursuing) or "pass" (not a fit)
2. reason: 1-2 sentence explanation
3. confidence: 0.0-1.0 score

Criteria for ELEVATE:
- Actual RFP, bid request, vendor call, or grant opportunity
- STRONG PREFERENCE for {location_hint}, {region_hint}, or nearby areas
- New venue or project in our region needing our capabilities
- Government/institutional procurement in our industry
- Aligns with our core capabilities (per operational facts)
- Clear opportunity we could realistically win
- Budget range we can handle

Criteria for PASS:
- Just news/announcements with no actionable RFP or bid
- Already completed projects or post-hoc coverage
- Too far from our base to pursue practically
- Outside our industry or capabilities
- Requires capabilities we don't have (per operational facts)
- Vague or no clear next step

Return JSON array matching input order:
[{{"title": "...", "verdict": "elevate|pass", "reason": "...", "confidence": 0.X}}, ...]
"""
        try:
            # Use llm_service.generate with a simple template
            assess_prompt = """You are a business development analyst. Be selective - only elevate real opportunities. Return only valid JSON array.

{user_prompt}"""
            response = await self.llm_service.generate(
                assess_prompt,
                {"user_prompt": prompt}
            )
            
            if response:
                json_match = re.search(r'\[.*\]', response, re.DOTALL)
                if json_match:
                    assessed = json.loads(json_match.group())
                    
                    # Merge assessment back with original findings
                    results = []
                    for i, finding in enumerate(findings[:10]):
                        assessment = assessed[i] if i < len(assessed) else {"verdict": "pass", "reason": "Could not assess"}
                        results.append({
                            **finding,
                            "verdict": assessment.get("verdict", "pass"),
                            "reason": assessment.get("reason", ""),
                            "confidence": assessment.get("confidence", 0.5),
                        })
                    return results
        except Exception as e:
            logger.error(f"Failed to assess opportunities: {e}")
        
        # Fallback: pass on everything if assessment fails
        return [{"verdict": "pass", "reason": "Assessment unavailable", **f} for f in findings]
    
    # ========================================
    # RAINMAKER: Hunt Query Generation
    # ========================================
    
    async def _generate_rainmaker_hunt_queries(self, today: str) -> List[Dict[str, str]]:
        """Generate targeted search queries for RFPs, vendor calls, and procurement opportunities.

        Uses org_profile.yaml location/region/capabilities to build geographically
        focused queries instead of hard-coded values.
        """
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        location = org["location"] or "our area"
        region = org["region"] or "our region"
        industry = org["industry"] or "our industry"

        day_of_week = datetime.now(EASTERN).weekday()  # 0=Mon, 6=Sun

        # Daily Search Strategy Rotation — themes are generic, queries
        # are populated dynamically from org profile.
        strategies = {
            0: {  # Monday
                "theme": f"Core verticals — {region}",
                "focus": f"{region} institutions, venues, public facilities",
                "base": [
                    {"category": "RFPs", "query": f"{location} {industry} RFP", "rationale": f"Mon: core {industry} market"},
                    {"category": "RFPs", "query": f"{region} {industry} bid opportunity", "rationale": f"Mon: {region} opportunities"},
                ]
            },
            1: {  # Tuesday
                "theme": f"Adjacent verticals — {region}",
                "focus": f"{region} adjacent market segments and emerging venues",
                "base": [
                    {"category": "RFPs", "query": f"{location} vendor call {industry}", "rationale": "Tue: local vendor calls"},
                    {"category": "RFPs", "query": f"{region} procurement {industry}", "rationale": "Tue: regional procurement"},
                ]
            },
            2: {  # Wednesday
                "theme": f"Government & Public — {location}",
                "focus": f"Government and municipal procurement near {location}",
                "base": [
                    {"category": "Procurement", "query": f"{location} state procurement RFP", "rationale": "Wed: gov procurement"},
                    {"category": "Bids", "query": f"{region} public sector bid {industry}", "rationale": "Wed: public sector"},
                ]
            },
            3: {  # Thursday
                "theme": f"Corporate & Education — {region}",
                "focus": f"Corporate and university projects near {location}",
                "base": [
                    {"category": "Corporate", "query": f"{location} corporate {industry} project", "rationale": "Thu: corporate market"},
                    {"category": "University", "query": f"{region} university {industry} upgrade", "rationale": "Thu: education market"},
                ]
            },
            4: {  # Friday
                "theme": f"Construction & New Builds — {region}",
                "focus": f"New construction and capital projects near {location}",
                "base": [
                    {"category": "Construction", "query": f"{location} new construction {industry} 2026", "rationale": "Fri: new builds"},
                    {"category": "Capital Projects", "query": f"{region} renovation bid {industry}", "rationale": "Fri: capital projects"},
                ]
            }
        }

        # Fallback to general (Monday) if weekend
        strategy = strategies.get(day_of_week, strategies[0])
        queries = list(strategy['base'])

        # Load operational context for better query generation
        operational_ctx = ""
        try:
            ops_path = Path(__file__).parent.parent / "prompts" / "operational_context.txt"
            if ops_path.exists():
                operational_ctx = ops_path.read_text(encoding='utf-8')[:1500]
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # Try to get dynamic queries from LLM based on recent context AND daily theme
        try:
            prompt = f"""Generate 3 *specific* search queries to find business opportunities for {org["org_name"]}.

Today is {today}.
Daily Theme: {strategy['theme']}
Target Verticals: {strategy['focus']}

=== ORGANISATION CAPABILITIES (use for query targeting) ===
{operational_ctx}

GEOGRAPHIC CONSTRAINT:
- We are based in {location}
- ALL queries MUST focus on {region} or nearby areas
- Avoid queries for distant or international opportunities

INSTRUCTIONS:
1. Create queries that find *active* RFPs, bids, or lead announcements near {location}
2. Include geographic terms relevant to {region}
3. Focus on our actual capabilities per the context above
4. Avoid generic terms outside our industry

Return JSON array with objects containing: category, query, rationale.
"""
            query_gen_prompt = """You are a business development assistant. Geographic focus is CRITICAL. Return only valid JSON.

{user_prompt}"""
            response = await self.llm_service.generate(
                query_gen_prompt,
                {"user_prompt": prompt}
            )

            # Try to parse additional queries
            if response:
                try:
                    json_match = re.search(r'\[.*\]', response, re.DOTALL)
                    if json_match:
                        additional = json.loads(json_match.group())
                        if isinstance(additional, list):
                            queries.extend(additional[:3])
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        return queries

    # ========================================
    # RAINMAKER: Government Procurement Portal (stub)
    # ========================================
    # Deployment-specific: implement a scraper for your state/region's
    # procurement portal (e.g. SAM.gov, state e-vendor portals).
    # See docs/architecture/system.md for guidance.


