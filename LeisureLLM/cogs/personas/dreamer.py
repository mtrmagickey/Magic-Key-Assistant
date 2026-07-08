"""
Dreamer Persona - Ideation and blue-sky exploration.

The Dreamer generates creative business ideas by:
- Conjuring unconventional ventures, products, partnerships
- Dispatching Scout (web) and Archivist (internal) for evidence
- Refining ideas based on findings
- Escalating promising concepts to the Manager

Runs: Tuesdays at 2:30pm Eastern
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from langchain_core.prompts import ChatPromptTemplate

from .base import EASTERN, load_persona_prompt

logger = logging.getLogger(__name__)


class DreamerMixin:
    """
    Dreamer persona mixin providing ideation capabilities.
    
    This is designed to be mixed into the AutonomousOps cog.
    Requires:
        - self.bot
        - self.llm_service
        - self.tavily_service
        - self.post_to_bots_channel()
        - self._job_already_ran()
        - self._record_job_run()
        - self._rainmaker_create_lead() (for lead creation from ideas)
    """
    
    DREAMER_TEMPERATURE = 0.85  # Slightly creative but grounded
    
    # ========================================
    # DREAMER: Business Context Gathering
    # ========================================
    
    async def _dreamer_gather_business_context(self) -> Dict[str, Any]:
        """Gather current business state to ground the Dreamer's visions."""
        context: Dict[str, Any] = {
            "active_projects": [],
            "recent_discussions": [],
            "open_gaps": [],
            "recent_wins": [],
        }

        db = getattr(self.bot, "db", None)
        if not db:
            return context

        try:
            async with db.acquire() as conn:
                # Active action items (proxy for current projects)
                async with conn.execute(
                    """
                    SELECT title, description
                    FROM tasks
                    WHERE tags LIKE '%action_item%'
                      AND status IN ('todo', 'in_progress')
                    ORDER BY updated_at DESC
                    LIMIT 8
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
                context["active_projects"] = [
                    {"title": r[0], "description": (r[1] or "")[:150]} for r in (rows or [])
                ]

                # Open knowledge gaps (what we're trying to learn)
                async with conn.execute(
                    """
                    SELECT topic, question
                    FROM knowledge_gaps
                    WHERE status = 'open'
                    ORDER BY times_asked DESC, created_at DESC
                    LIMIT 5
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
                context["open_gaps"] = [
                    {"topic": r[0], "question": (r[1] or "")[:150]} for r in (rows or [])
                ]

                # Recent partner updates (what's actually happening)
                async with conn.execute(
                    """
                    SELECT category, details
                    FROM partner_updates
                    ORDER BY created_at DESC
                    LIMIT 6
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
                context["recent_wins"] = [
                    {"category": r[0], "details": (r[1] or "")[:150]} for r in (rows or [])
                ]

        except Exception as e:
            logger.warning(f"Dreamer context gathering failed (non-fatal): {e}")

        # Recent channel discussions (if available)
        try:
            partners_channel = discord.utils.get(self.bot.get_all_channels(), name="weekly-meeting-threads")
            if partners_channel:
                week_ago = datetime.now(timezone.utc) - timedelta(days=7)
                async for msg in partners_channel.history(after=week_ago, limit=30):
                    if not msg.author.bot and msg.content:
                        context["recent_discussions"].append(msg.content[:200])
                context["recent_discussions"] = context["recent_discussions"][:8]
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        return context
    
    # ========================================
    # DREAMER: Idea Generation
    # ========================================

    async def _dreamer_generate_ideas(
        self, 
        date_seed: str, 
        business_context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Generate creative but grounded business ideas."""
        if not self.llm_service:
            return []

        # Build context summary for the prompt
        ctx = business_context or {}
        projects_text = "\n".join(
            f"- {p['title']}: {p['description']}" for p in ctx.get("active_projects", [])
        ) or "None available"
        gaps_text = "\n".join(
            f"- {g['topic']}: {g['question']}" for g in ctx.get("open_gaps", [])
        ) or "None available"
        wins_text = "\n".join(
            f"- [{w['category']}] {w['details']}" for w in ctx.get("recent_wins", [])
        ) or "None available"
        discussions_text = "\n".join(
            f"- {d[:100]}..." for d in ctx.get("recent_discussions", [])[:5]
        ) or "None available"

        seed_hex = hex(random.getrandbits(48))

        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        capabilities_block = org["capabilities"] or "- (no capabilities listed in org_profile.yaml)"
        persona_guidance = load_persona_prompt("dreamer", org["org_name"]) or "None."

        prompt = ChatPromptTemplate.from_template("""
You are the Dreamer — {org_name}'s creative visionary. Your job is to conjure
imaginative ideas that could become real business ventures, products, partnerships,
or programs.

{org_name} specializes in:
{capabilities}

Persona guidance (optional):
{persona_guidance}

=== CURRENT BUSINESS CONTEXT (ground your ideas here) ===

**Active Projects:**
{projects_text}

**Open Knowledge Gaps (what we're trying to learn):**
{gaps_text}

**Recent Wins & Updates:**
{wins_text}

**Recent Partner Discussions:**
{discussions_text}

=== END CONTEXT ===

RULES:
1. Ideas must CONNECT to at least one item from the context above (a project, gap, win, or discussion).
2. Think creatively — extend, combine, or pivot from what's already happening.
3. Each idea should have a plausible path to value within 6-12 months.
4. Avoid pure fantasy. Wild is good; disconnected is bad.
5. Include the "grounding_anchor" field explaining which context item inspired this.

Today's seed: {date_seed} / {seed_hex}

Return a STRICT JSON array (no markdown, no prose) of 2-3 idea objects:
[
  {{
    "title": "Catchy 4-7 word title",
    "premise": "1-2 sentence elevator pitch",
    "grounding_anchor": "Which context item this builds on and how",
    "wild_factor": "What makes this creative/exciting",
    "target_audience": "Who would pay or benefit",
    "investigation_queries": ["search query 1", "search query 2"]
  }}
]

JSON:
""")

        try:
            raw = await self.llm_service.generate(
                prompt,
                {
                    "date_seed": date_seed,
                    "seed_hex": seed_hex,
                    "org_name": org["org_name"],
                    "capabilities": capabilities_block,
                    "projects_text": projects_text,
                    "gaps_text": gaps_text,
                    "wins_text": wins_text,
                    "discussions_text": discussions_text,
                    "persona_guidance": persona_guidance,
                },
                temperature=self.DREAMER_TEMPERATURE,
            )
            ideas = json.loads(raw)
            if not isinstance(ideas, list):
                return []
            return ideas[:3]
        except Exception as e:
            logger.warning(f"Dreamer idea generation failed: {e}")
            return []
    
    # ========================================
    # DREAMER: Investigation Dispatch
    # ========================================

    async def _dreamer_dispatch_scout(self, idea: Dict[str, Any]) -> List[Dict[str, str]]:
        """Use Tavily (via Scout) to search for external evidence relevant to the idea."""
        if not (self.tavily_service and self.tavily_service.is_configured):
            return []

        queries = idea.get("investigation_queries", [])[:2]
        if not queries:
            queries = [idea.get("title", "")]

        findings: List[Dict[str, str]] = []
        for q in queries:
            if not q:
                continue
            try:
                results = await self.tavily_service.search(
                    query=q,
                    search_depth="basic",
                    max_results=3,
                    topic="general",
                    time_range="year",
                    auto_parameters=True,
                )
                for r in (results.get("results") or []):
                    findings.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": (r.get("content") or "")[:250],
                    })
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Dreamer Scout search failed for '{q}': {e}")
        return findings[:5]

    async def _dreamer_dispatch_archivist(self, idea: Dict[str, Any]) -> List[str]:
        """Query the Chroma RAG for internal documents relevant to the idea."""
        # Use the LLM cog's retriever if available
        llm_cog = self.bot.get_cog("LLM")
        if not llm_cog:
            return []

        retriever = getattr(llm_cog, "retriever", None)
        if not retriever:
            return []

        query = f"{idea.get('title', '')} {idea.get('premise', '')}"[:200]
        try:
            docs = retriever.invoke(query)
            snippets = []
            for doc in docs[:3]:
                content = (doc.page_content or "")[:300]
                source = doc.metadata.get("source_relpath") or doc.metadata.get("source") or ""
                snippets.append(f"[{source}] {content}")
            return snippets
        except Exception as e:
            logger.warning(f"Dreamer Archivist search failed: {e}")
            return []
    
    # ========================================
    # DREAMER: Idea Refinement & Escalation
    # ========================================

    async def _dreamer_refine_ideas(self, ideas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Score and refine ideas based on Scout/Archivist findings."""
        if not self.llm_service or not ideas:
            return ideas

        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        persona_guidance = load_persona_prompt("dreamer", org["org_name"]) or "None."

        prompt = ChatPromptTemplate.from_template("""
You are the Dreamer's inner critic — still imaginative, but now grounded by evidence.

For each idea below, review the external (Scout) and internal (Archivist) findings,
then assign:
- viability_score: 1-10 (10 = moonshot with real traction signals)
- grounding_score: 1-10 (10 = strongly connected to current business; 1 = pure fantasy)
- refined_pitch: Updated 1-2 sentence pitch incorporating evidence
- next_step: Concrete action if this were pursued

IMPORTANT: Ideas with grounding_score < 5 should NOT be escalated. We want creative
extensions of real work, not disconnected fantasies.

Persona guidance (optional):
{persona_guidance}

Ideas & Evidence:
{ideas_json}

Return a STRICT JSON array with the same structure plus the four new fields.
JSON:
""")

        ideas_text = json.dumps(ideas, indent=2, default=str)[:6000]
        try:
            raw = await self.llm_service.generate(
                prompt,
                {
                    "ideas_json": ideas_text,
                    "persona_guidance": persona_guidance,
                },
            )
            refined = json.loads(raw)
            if isinstance(refined, list):
                return refined
        except Exception as e:
            logger.warning(f"Dreamer refinement failed: {e}")
        return ideas

    async def _dreamer_escalate_to_manager(self, idea: Dict[str, Any]) -> None:
        """Pass a promising idea to the Manager persona for partner visibility."""
        title = idea.get("title", "Untitled Vision")
        pitch = idea.get("refined_pitch") or idea.get("premise", "")
        viability = idea.get("viability_score", "?")
        grounding = idea.get("grounding_score", "?")
        anchor = idea.get("grounding_anchor", "")
        next_step = idea.get("next_step", "Discuss with partners.")

        # Build embed
        embed = discord.Embed(
            title=f"💡 New Idea: {title}",
            description=pitch,
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Viability", value=f"**{viability}/10**", inline=True)
        embed.add_field(name="Grounding", value=f"**{grounding}/10**", inline=True)
        if anchor:
            embed.add_field(name="🔗 Anchored To", value=anchor[:300], inline=False)
        embed.add_field(name="Suggested Next Step", value=next_step[:500], inline=False)

        # Scout findings summary
        scout_findings = idea.get("scout_findings") or []
        if scout_findings:
            links = "\n".join(f"• [{f['title'][:40]}]({f['url']})" for f in scout_findings[:3])
            embed.add_field(name="🔍 Supporting Evidence", value=links[:1000] or "None", inline=False)

        # Archivist findings summary
        archivist_findings = idea.get("archivist_findings") or []
        if archivist_findings:
            internal = "\n".join(f"• {s[:80]}..." for s in archivist_findings[:2])
            embed.add_field(name="📚 Archivist Context", value=internal[:800] or "None", inline=False)

        embed.set_footer(text="Blue-Sky Exploration")

        # Post via Manager to bots-office
        await self.post_to_bots_channel(
            "manager",
            "📬 A promising vision has been delivered for review:",
            embed=embed,
        )

        # ===== RAINMAKER INTEGRATION: Auto-create lead from escalated Dreamer idea =====
        try:
            viability_num = int(viability) if isinstance(viability, (int, float)) or str(viability).isdigit() else 0
            grounding_num = int(grounding) if isinstance(grounding, (int, float)) or str(grounding).isdigit() else 0
            
            if viability_num >= 6 and grounding_num >= 5:
                priority = 'high' if viability_num >= 8 else 'medium'
                lead_id = await self._rainmaker_create_lead(
                    title=title[:100],
                    source='dreamer',
                    description=f"{pitch[:400]}\n\nAnchor: {anchor[:200]}\nNext step: {next_step[:200]}",
                    source_id=title,
                    priority=priority
                )
                if lead_id:
                    await self.post_to_bots_channel(
                        "rainmaker",
                        f"📥 **Lead #{lead_id}** created from Dreamer vision: *{title[:50]}*\n"
                        f"Priority: {priority} | Use `/lead update {lead_id}` to assign and track!"
                    )
        except Exception as e:
            logger.warning(f"Failed to create lead from Dreamer idea: {e}")

        # Also try to tag partners in a partner-facing channel if configured
        partners_channel = discord.utils.get(self.bot.get_all_channels(), name="partners-assistant")
        if partners_channel:
            try:
                await partners_channel.send(
                    content="💡 **New idea from the Dreamer** — see `#bots-office` for full details.",
                )
            except Exception:
                pass  # Non-critical
    
    # ========================================
    # DREAMER: Schemes Channel Response
    # ========================================
    
    async def _dreamer_respond_to_scheme(self, message: discord.Message) -> Optional[str]:
        """Generate a playful riff on a schemes-n-dreams message.
        
        Returns the response text or None if no response should be sent.
        """
        if not self.llm_service:
            return None
        
        content = message.content.strip()
        if len(content) < 30:
            return None
        
        try:
            from LeisureLLM.core.config_loader import org_context_for_prompts
            org = org_context_for_prompts()
            persona_guidance = load_persona_prompt("dreamer", org["org_name"]) or "None."
            prompt = ChatPromptTemplate.from_template("""
Someone posted a business idea. Riff on it — escalate it, find an unexpected angle, 
or ask a question that pushes it further. 

Keep it to 1-3 sentences. Playful, not corporate. End with a question if it fits naturally.

Persona guidance (optional):
{persona_guidance}

The idea:
"{idea}"

Your take:
""")
            
            response = await self.llm_service.generate(
                prompt,
                {"idea": content[:800], "persona_guidance": persona_guidance},
                temperature=0.9,
            )
            
            return response[:500] if response else None
            
        except Exception as e:
            logger.warning(f"Schemes response generation failed: {e}")
            return None

