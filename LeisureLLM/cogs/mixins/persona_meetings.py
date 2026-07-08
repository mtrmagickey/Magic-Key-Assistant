"""Persona meetings system — hourly cross-persona conversations,
exercises, context gathering, and takeaway processing.

Extracted from AutonomousOps.py to reduce god-object size."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


class PersonaMeetingsMixin:
    """Mixin: Persona meetings system — hourly cross-persona conversations,"""

    # PERSONA MEETINGS: Hourly cross-persona conversations
    # ========================================

    @tasks.loop(time=dt_time(hour=16, minute=30, tzinfo=EASTERN))
    async def weekly_persona_meeting_digest(self):
        """
        Fridays at 4:30pm: Compile weekly digest of persona meeting takeaways.
        Posts a summary of what the personas discussed and discovered this week.
        """
        await self.bot.wait_until_ready()
        
        now = datetime.now(EASTERN)
        if now.weekday() != 4:  # Friday only
            return
            
        run_date = now.strftime("%Y-%m-%d")
        if await self._job_already_ran("weekly_persona_digest", run_date):
            return
            
        db = getattr(self.bot, "db", None)
        if not db:
            return
            
        try:
            # Get all takeaways from the past 7 days that haven't been digested
            week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
            
            async with db.acquire() as conn:
                async with conn.execute(
                    """
                    SELECT id, meeting_date, meeting_topic, attendees, insight, owner, urgency, 
                           why_now, actioned, actioned_as, opening_provocation, tangent_explored, 
                           unresolved_tension
                    FROM persona_meeting_takeaways
                    WHERE meeting_date >= ? AND included_in_digest_date IS NULL
                    ORDER BY meeting_date DESC, urgency DESC
                    """,
                    (week_ago,)
                ) as cursor:
                    rows = await cursor.fetchall()
                
                if not rows:
                    logger.info("No persona meeting takeaways to digest this week")
                    await self._record_job_run("weekly_persona_digest", run_date)
                    return
                
                # Group by topic/theme
                high_urgency = []
                medium_urgency = []
                low_urgency = []
                knowledge_gaps_created = 0
                escalations_sent = 0
                meetings_count = set()
                topics_discussed = set()
                
                for row in rows:
                    takeaway = {
                        'id': row[0],
                        'date': row[1],
                        'topic': row[2],
                        'attendees': row[3],
                        'insight': row[4],
                        'owner': row[5],
                        'urgency': row[6],
                        'why_now': row[7],
                        'actioned': row[8],
                        'actioned_as': row[9],
                        'opening': row[10],
                        'tangent': row[11],
                        'tension': row[12]
                    }
                    
                    meetings_count.add(f"{row[1]}_{row[2]}")
                    if row[2]:
                        topics_discussed.add(row[2])
                    
                    if row[9] == 'knowledge_gap':
                        knowledge_gaps_created += 1
                    elif row[9] == 'escalation':
                        escalations_sent += 1
                    
                    if row[6] == 'high':
                        high_urgency.append(takeaway)
                    elif row[6] == 'medium':
                        medium_urgency.append(takeaway)
                    else:
                        low_urgency.append(takeaway)
                
                # Build the digest
                lines = [
                    "# 🗓️ Weekly Persona Meeting Digest",
                    f"*Week ending {now.strftime('%B %d, %Y')}*",
                    "",
                    f"**{len(meetings_count)} meetings** held this week",
                    f"**{len(rows)} takeaways** captured",
                    f"**{knowledge_gaps_created}** knowledge gaps identified",
                    f"**{escalations_sent}** escalations triggered",
                    ""
                ]
                
                # Topics discussed
                if topics_discussed:
                    lines.append("## 💬 Topics Explored")
                    for topic in list(topics_discussed)[:10]:
                        lines.append(f"- {topic}")
                    lines.append("")
                
                # High urgency first
                if high_urgency:
                    lines.append("## 🔴 High Priority Insights")
                    for t in high_urgency[:5]:
                        action_note = f" *(→ {t['actioned_as']})*" if t['actioned'] else ""
                        lines.append(f"**{t['insight']}**{action_note}")
                        if t['why_now']:
                            lines.append(f"↳ {t['why_now']}")
                        lines.append(f"*Owner: {t['owner']} | From: {t['topic']}*")
                        lines.append("")
                
                # Medium urgency (condensed)
                if medium_urgency:
                    lines.append("## 🟡 Notable Insights")
                    for t in medium_urgency[:8]:
                        action_note = f" *(→ {t['actioned_as']})*" if t['actioned'] else ""
                        lines.append(f"- **{t['insight']}**{action_note} → *{t['owner']}*")
                    lines.append("")
                
                # Interesting tangents and tensions
                tangents = [t['tangent'] for t in rows if t[11]]
                tensions = [t['tension'] for t in rows if t[12]]
                
                if tangents:
                    lines.append("## 🔀 Interesting Tangents")
                    for tangent in list(set(tangents))[:5]:
                        lines.append(f"- {tangent}")
                    lines.append("")
                
                if tensions:
                    lines.append("## ⚡ Unresolved Tensions")
                    lines.append("*These disagreements or open questions may need human input:*")
                    for tension in list(set(tensions))[:5]:
                        lines.append(f"- {tension}")
                    lines.append("")
                
                # Post to bots office
                digest_text = "\n".join(lines)
                
                target_channel_id = int(self.bots_office_channel_id) if self.bots_office_channel_id else None
                if not target_channel_id:
                    target_channel_id = int(self.bots_channel_id) if self.bots_channel_id else None
                
                if target_channel_id:
                    channel = self.bot.get_channel(target_channel_id)
                    if channel:
                        # Split if needed
                        if len(digest_text) <= 4000:
                            embed = discord.Embed(
                                description=digest_text,
                                color=discord.Color.gold(),
                                timestamp=now
                            )
                            embed.set_footer(text="Weekly Persona Meeting Digest")
                            await channel.send(embed=embed)
                        else:
                            # Send in chunks
                            chunks = [digest_text[i:i+4000] for i in range(0, len(digest_text), 4000)]
                            for i, chunk in enumerate(chunks[:3]):
                                embed = discord.Embed(
                                    description=chunk,
                                    color=discord.Color.gold(),
                                    timestamp=now if i == 0 else None
                                )
                                if i == 0:
                                    embed.set_footer(text="Weekly Persona Meeting Digest")
                                await channel.send(embed=embed)
                
                # Mark all takeaways as included in this digest
                takeaway_ids = [row[0] for row in rows]
                for tid in takeaway_ids:
                    await conn.execute(
                        """
                        UPDATE persona_meeting_takeaways 
                        SET included_in_digest_date = ?
                        WHERE id = ?
                        """,
                        (run_date, tid)
                    )
                await conn.commit()
                
            await self._record_job_run("weekly_persona_digest", run_date)
            logger.info(f"Posted weekly persona meeting digest: {len(rows)} takeaways")
            
        except Exception as e:
            logger.error(f"Weekly persona meeting digest failed: {e}")

    @weekly_persona_meeting_digest.before_loop
    async def before_weekly_digest(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=2)
    async def hourly_persona_meeting(self):
        """Event-triggered persona exercises. Only runs when there's a reason."""
        await self.bot.wait_until_ready()

        now = datetime.now(EASTERN)
        if not (8 <= now.hour <= 20):
            logger.debug(f"Persona exercise check skipped - outside hours ({now.hour}:00)")
            return

        # Check for triggers - only hold a meeting if something warrants it
        trigger = await self._check_exercise_triggers(now)
        if not trigger:
            logger.debug("No exercise triggers found - skipping meeting")
            return

        # Unique key to prevent duplicate runs
        trigger_key = f"{now.strftime('%Y-%m-%d')}-{trigger['type']}-{trigger.get('id', 'none')}"
        if await self._job_already_ran("persona_exercise", trigger_key):
            logger.debug(f"Exercise already ran for {trigger_key}, skipping")
            return

        if not self.llm_service:
            logger.warning("Persona exercise skipped - LLM service not configured")
            return

        try:
            await self._hold_persona_exercise(now, trigger)
            await self._record_job_run("persona_exercise", trigger_key)
        except Exception as e:
            logger.error(f"Persona exercise failed: {e}")

    async def _check_exercise_triggers(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Check for reasons to hold an exercise. Prioritizes knowledge-base tensions over DB events."""
        
        # FIRST: Check if a partner submitted an agenda item (always highest priority)
        partner_agenda = await self._check_partner_agenda()
        if partner_agenda:
            return partner_agenda
        
        # SECOND: Have the Librarian/Steward scour the knowledge base for tensions
        knowledge_tension = await self._find_knowledge_tension(now)
        if knowledge_tension:
            return knowledge_tension
        
        # THIRD: Fall back to database event triggers (lower priority)
        db_trigger = await self._check_db_triggers(now)
        if db_trigger:
            return db_trigger
        
        # If nothing warrants an exercise, return None (no meeting today)
        return None

    async def _check_partner_agenda(self) -> Optional[Dict[str, Any]]:
        """Check for partner-submitted agenda items."""
        db = getattr(self.bot, "db", None)
        if not db:
            return None
        
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT id, topic, context, priority, submitted_by_username 
                    FROM meeting_agenda_items
                    WHERE status = 'pending'
                    ORDER BY 
                        CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                        created_at ASC
                    LIMIT 1
                    """
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        'type': 'partner_agenda',
                        'id': row[0],
                        'subject': row[1],
                        'context': row[2],
                        'priority': 100,
                        'prompt': row[1],
                        'submitted_by': row[4],
                        'source': 'partner_request'
                    }
        except Exception as e:
            logger.warning(f"Failed to check partner agenda: {e}")
        
        return None

    async def _find_knowledge_tension(self, now: datetime) -> Optional[Dict[str, Any]]:
        """
        Have the Librarian/Steward scour the knowledge base for interesting tensions.
        
        This is the organic alternative to database triggers. We actually READ the docs
        and find things worth discussing:
        - Unresolved questions from past conversations
        - Contradictions between documents
        - Commitments/deadlines that may have passed
        - Technical claims without evidence
        - Recurring themes that haven't been addressed
        """
        if not self.llm_service:
            return None
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        
        # Get the LLM cog's retriever
        llm_cog = self.bot.get_cog("LLM")
        if not llm_cog or not hasattr(llm_cog, '_ensure_retriever'):
            return None
        
        retriever = llm_cog._ensure_retriever()
        if not retriever:
            return None
        
        # Sample diverse docs from the knowledge base
        sample_queries = [
            "deadline commitment timeline due",
            "problem issue concern risk blocker",
            "decision decided agreed conclusion",
            "question unclear unknown don't know",
            "next steps action items follow up",
            "client feedback response reaction",
        ]
        
        all_snippets = []
        try:
            from LeisureLLM.cogs.LLM import run_retriever_query
            
            # Pull docs from several angles
            for query in random.sample(sample_queries, 3):
                docs = await asyncio.to_thread(run_retriever_query, retriever, query)
                for doc in docs[:4]:
                    if doc.page_content:
                        meta = doc.metadata or {}
                        source = meta.get("source_relpath") or meta.get("source") or "unknown"
                        all_snippets.append({
                            'content': doc.page_content[:800],
                            'source': source
                        })
        except Exception as e:
            logger.warning(f"Failed to sample knowledge base: {e}")
            return None
        
        if not all_snippets:
            return None
        
        # Dedupe by content hash
        seen = set()
        unique_snippets = []
        for s in all_snippets:
            h = hash(s['content'][:200])
            if h not in seen:
                seen.add(h)
                unique_snippets.append(s)
        
        # Format for the LLM
        snippet_text = "\n\n---\n\n".join([
            f"[{s['source']}]:\n{s['content']}" for s in unique_snippets[:10]
        ])
        
        # Ask the LLM (as Librarian/Steward) to find tensions worth discussing
        tension_prompt = f"""You are the Librarian and Steward for {org['org_name']}, reviewing documents from the knowledge base.

Your job: Find ONE thing in these documents that warrants a focused discussion. 

GOOD tensions (worth discussing):
- A commitment or deadline mentioned that may have passed without follow-up
- A question someone asked that was never answered
- Two documents that seem to contradict each other
- A technical claim without evidence or testing documented
- A client concern mentioned but not addressed
- A decision that was deferred and may need revisiting
- Something that was "supposed to happen" but we can't confirm it did

BAD tensions (not worth discussing):
- Generic observations ("we should document more")
- Process improvements ("we need better tracking")
- Anything that's clearly already resolved
- Vague concerns without specific grounding

If you find a worthy tension, respond with JSON:
{{
    "found": true,
    "tension": "One sentence describing the specific tension",
    "question": "The question this raises that we should discuss",
    "source": "Which document(s) this came from",
    "exercise_type": "case_study" or "technical_spike" or "pre_mortem" or "devils_advocate",
    "why_now": "Why this matters right now (1 sentence)"
}}

If nothing in these documents warrants a discussion, respond:
{{"found": false, "reason": "Brief explanation"}}

Today's date: {now.strftime('%B %d, %Y')}

=== KNOWLEDGE BASE SAMPLE ===
{snippet_text}

JSON:"""

        try:
            raw = await self.llm_service.complete(tension_prompt, temperature=0.4)
            result = self._parse_json_response(raw)
            
            if not result or not result.get('found'):
                reason = result.get('reason', 'unknown') if result else 'parse failed'
                logger.debug(f"No knowledge tension found: {reason}")
                return None
            
            # Quality check: reject vague/meta tensions
            tension = result.get('tension', '').lower()
            reject_phrases = [
                'documentation', 'tracking', 'process', 'framework', 'metrics',
                'we should', 'we need to', 'establishing', 'defining'
            ]
            if any(phrase in tension for phrase in reject_phrases):
                logger.debug(f"Rejected meta tension: {result.get('tension')}")
                return None
            
            return {
                'type': result.get('exercise_type', 'case_study'),
                'id': None,
                'subject': result.get('tension', 'Knowledge base finding'),
                'prompt': result.get('question', result.get('tension')),
                'priority': 75,
                'source': 'knowledge_tension',
                'source_docs': result.get('source', ''),
                'why_now': result.get('why_now', '')
            }
            
        except Exception as e:
            logger.warning(f"Failed to find knowledge tension: {e}")
            return None

    async def _check_db_triggers(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Check database for event-based triggers. Lower priority than knowledge tensions."""
        db = getattr(self.bot, "db", None)
        if not db:
            return None

        triggers = []

        try:
            async with db.acquire() as conn:
                # TRIGGER: Project completed recently (Case Study)
                async with conn.execute(
                    """
                    SELECT id, name FROM projects 
                    WHERE status = 'completed'
                      AND datetime(updated_at) >= datetime('now', '-7 days')
                    ORDER BY updated_at DESC LIMIT 1
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        triggers.append({
                            'type': 'case_study',
                            'id': row[0],
                            'subject': row[1],
                            'priority': 85,
                            'prompt': f"What did we learn from the {row[1]} project?"
                        })

                # TRIGGER: Task overdue by 7+ days (Pre-Mortem)
                async with conn.execute(
                    """
                    SELECT id, title, due_date FROM tasks 
                    WHERE status NOT IN ('done', 'canceled')
                      AND due_date < date('now', '-7 days')
                    ORDER BY due_date ASC LIMIT 1
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        triggers.append({
                            'type': 'pre_mortem',
                            'id': row[0],
                            'subject': row[1],
                            'priority': 80,
                            'prompt': f"This task is 7+ days overdue: {row[1]}. What's actually blocking it?"
                        })

                # TRIGGER: Stale knowledge gap (Technical Spike)
                async with conn.execute(
                    """
                    SELECT id, topic, question FROM knowledge_gaps 
                    WHERE status = 'open'
                      AND datetime(first_asked) <= datetime('now', '-14 days')
                    ORDER BY priority_score DESC LIMIT 1
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        triggers.append({
                            'type': 'technical_spike',
                            'id': row[0],
                            'subject': row[1],
                            'priority': 70,
                            'prompt': row[2] or f"Research: {row[1]}"
                        })

                # TRIGGER: Lead going stale (Client Roleplay prep)
                async with conn.execute(
                    """
                    SELECT id, name, next_action FROM leads 
                    WHERE status IN ('contacted', 'proposal_sent')
                      AND datetime(updated_at) <= datetime('now', '-14 days')
                    ORDER BY value_estimate DESC LIMIT 1
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        triggers.append({
                            'type': 'client_roleplay',
                            'id': row[0],
                            'subject': row[1],
                            'priority': 75,
                            'prompt': f"This lead is going cold: {row[1]}. How do we re-engage?"
                        })

                # TRIGGER: Too many open tasks (Prioritization Poker)
                async with conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('todo', 'in_progress')"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0] > 15:
                        triggers.append({
                            'type': 'prioritization',
                            'id': None,
                            'subject': f"{row[0]} open tasks",
                            'priority': 65,
                            'prompt': f"We have {row[0]} open tasks. What's actually most important?"
                        })

                # TRIGGER: User-submitted agenda item (highest priority)
                async with conn.execute(
                    """
                    SELECT id, topic, context, priority, submitted_by_username 
                    FROM meeting_agenda_items
                    WHERE status = 'pending'
                    ORDER BY 
                        CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                        created_at ASC
                    LIMIT 1
                    """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        triggers.append({
                            'type': 'partner_agenda',
                            'id': row[0],
                            'subject': row[1],
                            'context': row[2],
                            'priority': 100,  # Partner requests always win
                            'prompt': row[1],
                            'submitted_by': row[4]
                        })

        except Exception as e:
            logger.warning(f"Failed to check exercise triggers: {e}")
            return None

        if not triggers:
            # Fallback: Check if it's been 8+ hours since last exercise
            try:
                async with db.acquire() as conn, conn.execute(
                    """
                        SELECT MAX(run_date) FROM job_runs 
                        WHERE job_name = 'persona_exercise'
                        """
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        # If last run was today, skip
                        if row[0] == now.strftime("%Y-%m-%d"):
                            return None
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
            
            # Run a low-priority exercise if we haven't done anything today
            return {
                'type': 'proof_point',
                'id': None,
                'subject': 'Build credibility materials',
                'priority': 30,
                'prompt': 'What proof points can we extract from our recent work?'
            }

        # Return highest priority trigger
        triggers.sort(key=lambda t: -t['priority'])
        return triggers[0]

    async def _hold_persona_exercise(self, now: datetime, trigger: Dict[str, Any]):
        """Execute a structured persona exercise based on the trigger."""
        
        # Exercise configurations with assigned roles
        exercise_configs = {
            'devils_advocate': {
                'name': 'Devil\'s Advocate',
                'emoji': '👿',
                'roles': {
                    'advocate': ['dreamer', 'rainmaker'],  # Must defend the idea
                    'critic': ['accountant', 'steward'],   # Must attack the idea
                    'jury': ['scout', 'librarian', 'shepherd']  # Ask questions, decide
                },
                'artifact_type': 'decision',
                'structure': """
EXERCISE: DEVIL'S ADVOCATE
You are conducting a structured debate.

ADVOCATE ({advocate_name}): You MUST defend the proposal. Find every reason it could work.
CRITIC ({critic_name}): You MUST attack the proposal. Find every flaw, risk, and cost.
JURY ({jury_names}): Ask clarifying questions. Then vote: GO / NO-GO / NEEDS MORE INFO.

RULES:
- Advocate speaks first (2-3 sentences defending)
- Critic responds (2-3 sentences attacking)
- Jury asks ONE pointed question each
- Continue until jury is ready to vote
- End with a clear DECISION and RATIONALE

The proposal: {prompt}
"""
            },
            'pre_mortem': {
                'name': 'Pre-Mortem',
                'emoji': '💀',
                'roles': {
                    'facilitator': ['coordinator'],
                    'pessimists': ['accountant', 'steward', 'shepherd', 'scout']
                },
                'artifact_type': 'risk_register',
                'structure': """
EXERCISE: PRE-MORTEM
Imagine it's 6 months from now. This failed catastrophically.

FACILITATOR ({facilitator_name}): Announce the failure scenario. Keep the exercise on track.
PESSIMISTS ({pessimists_names}): Each must name ONE specific, concrete failure mode from your domain.

RULES:
- Facilitator sets the scene: "It's [date]. [Subject] has failed. The client is furious. What went wrong?"
- Each pessimist names ONE failure mode (not abstract "risk" — specific events)
- Group votes on the 3 most likely failures
- Assign ONE mitigation action per top failure

The subject: {prompt}
"""
            },
            'case_study': {
                'name': 'Case Study Dissection',
                'emoji': '🔬',
                'roles': {
                    'historian': ['librarian'],
                    'questioners': ['dreamer', 'accountant', 'rainmaker', 'scout']
                },
                'artifact_type': 'lessons_learned',
                'structure': """
EXERCISE: CASE STUDY DISSECTION
Learn from a real past project.

HISTORIAN ({historian_name}): Present the facts from the knowledge base. What actually happened?
QUESTIONERS ({questioners_names}): Each asks ONE probing question from your perspective.

RULES:
- Historian presents: timeline, budget, outcome, surprises
- Each questioner asks ONE question (not generic — specific to your concerns)
- Group identifies: 1 thing to REPEAT, 1 thing to NEVER DO AGAIN
- Output is a 3-bullet lessons learned brief

The project: {prompt}
"""
            },
            'technical_spike': {
                'name': 'Technical Spike',
                'emoji': '🔧',
                'roles': {
                    'researcher': ['scout'],
                    'proposer': ['dreamer'],
                    'skeptic': ['steward', 'accountant']
                },
                'artifact_type': 'recommendation',
                'structure': """
EXERCISE: TECHNICAL SPIKE
Investigate a specific technical question.

RESEARCHER ({researcher_name}): Search the web. Find specs, tutorials, examples. Report findings.
PROPOSER ({proposer_name}): Based on research, propose 1-2 concrete approaches.
SKEPTIC ({skeptic_names}): Identify risks, unknowns, and what we'd need to test.

RULES:
- Researcher MUST cite external sources (URLs, product names, version numbers)
- Proposer gives concrete implementation approach (not vague "we could try")
- Skeptic identifies what could go wrong and what we don't know
- End with: YES (here's how) / NO (here's why) / MAYBE (here's what to test)

The question: {prompt}
"""
            },
            'client_roleplay': {
                'name': 'Client Roleplay',
                'emoji': '🎭',
                'roles': {
                    'client': ['accountant', 'shepherd'],  # Skeptical, budget-conscious
                    'team': ['rainmaker', 'dreamer', 'scout']
                },
                'artifact_type': 'talking_points',
                'structure': """
EXERCISE: CLIENT ROLEPLAY
Prepare for a real client interaction.

CLIENT ({client_name}): You are a skeptical museum director. You have a hidden concern (budget? timeline? reliability?). Be polite but push back.
TEAM ({team_names}): Pitch, answer questions, handle objections. Try to uncover the hidden concern.

RULES:
- Team opens with a 2-sentence pitch
- Client asks 2-3 tough questions (real objections clients have)
- Team responds, tries to address concerns
- At the end, CLIENT reveals: "My real concern was ___. Did you address it?"
- Output is talking points for the real conversation

The context: {prompt}
"""
            },
            'prioritization': {
                'name': 'Prioritization Poker',
                'emoji': '🃏',
                'roles': {
                    'facilitator': ['coordinator'],
                    'advocates': ['rainmaker', 'dreamer', 'steward', 'accountant']
                },
                'artifact_type': 'priority_list',
                'structure': """
EXERCISE: PRIORITIZATION POKER
Force a decision when everything feels urgent.

FACILITATOR ({facilitator_name}): Present 3-5 items competing for attention. Force a single choice.
ADVOCATES ({advocates_names}): Each argues for ONE item in 2 sentences. Then everyone votes.

RULES:
- Facilitator lists the competing items (from tasks, leads, gaps)
- Each advocate picks ONE to champion and explains why in 2 sentences
- Forced question: "If you could ONLY do ONE, which?"
- Document the winner AND the reasoning (so we remember why)

The context: {prompt}
"""
            },
            'proof_point': {
                'name': 'Proof Point Sprint',
                'emoji': '📊',
                'roles': {
                    'facilitator': ['rainmaker'],
                    'miners': ['librarian', 'scout', 'steward']
                },
                'artifact_type': 'proof_point',
                'structure': """
EXERCISE: PROOF POINT SPRINT
Extract compelling evidence from our work.

FACILITATOR ({facilitator_name}): Name a claim we want to make to clients (e.g., "We deliver reliable systems").
MINERS ({miners_names}): Each finds ONE piece of evidence from docs/projects to support it.

RULES:
- Facilitator states the claim clearly
- Each miner cites SPECIFIC evidence: numbers, quotes, project outcomes
- Group assembles into a proof point: "[Claim] — for example, [Evidence]"
- Output is ONE proof point ready for proposals

The focus: {prompt}
"""
            },
            'partner_agenda': {
                'name': 'Partner Request',
                'emoji': '📋',
                'roles': {
                    'all': ['scout', 'dreamer', 'accountant', 'steward', 'rainmaker', 'librarian']
                },
                'artifact_type': 'response',
                'structure': """
EXERCISE: PARTNER-REQUESTED DISCUSSION
A partner specifically asked for this topic to be discussed.

ALL PERSONAS: Engage substantively with the topic. This is a real request from a human partner.

RULES:
- Take this seriously — a partner is waiting for output
- Produce something USEFUL: a recommendation, a decision, a draft
- If you can't resolve it, clearly state what information is needed
- End with a concrete next step

The topic: {prompt}
"""
            }
        }

        exercise_type = trigger.get('type', 'proof_point')
        config = exercise_configs.get(exercise_type, exercise_configs['proof_point'])
        
        # Assign personas to roles
        role_assignments = await self._assign_exercise_roles(config['roles'])
        if not role_assignments:
            logger.warning(f"Could not assign roles for exercise: {exercise_type}")
            return

        # Build the exercise context
        context = await self._gather_meeting_context(now)
        context['exercise_type'] = exercise_type
        context['exercise_name'] = config['name']
        context['trigger'] = trigger
        context['role_assignments'] = role_assignments

        # Generate the exercise conversation
        conversation = await self._generate_exercise_conversation(
            config, role_assignments, trigger, context, now
        )

        if not conversation:
            logger.warning(f"Exercise {exercise_type} generated no output")
            return

        # Post to bots office
        await self._post_exercise_results(config, role_assignments, trigger, conversation, now)

    async def _assign_exercise_roles(self, role_config: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """Assign actual personas to exercise roles."""
        assignments = {}
        used_personas = set()

        for role_name, preferred_personas in role_config.items():
            if role_name == 'all':
                # Special case: use 3-4 random personas
                available = [p for p in preferred_personas if p not in used_personas]
                count = min(4, len(available))
                selected = random.sample(available, count) if len(available) >= count else available
                assignments[role_name] = selected
                used_personas.update(selected)
            else:
                # Pick one from preferred list that hasn't been used
                available = [p for p in preferred_personas if p not in used_personas]
                if available:
                    selected = random.choice(available)
                    assignments[role_name] = [selected]
                    used_personas.add(selected)
                elif preferred_personas:
                    # Fallback: allow reuse if necessary
                    assignments[role_name] = [random.choice(preferred_personas)]

        return assignments

    async def _generate_exercise_conversation(
        self,
        config: Dict[str, Any],
        role_assignments: Dict[str, List[str]],
        trigger: Dict[str, Any],
        context: Dict[str, Any],
        now: datetime
    ) -> Optional[Dict[str, Any]]:
        """Generate a structured exercise conversation."""
        
        if not self.llm_service:
            return None
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()

        # Load persona prompts
        persona_prompts = await self._load_persona_prompts()
        shared_context = self._build_shared_context(context)

        # Build role name strings for the structure prompt
        role_names = {}
        all_attendees = []
        for role, personas in role_assignments.items():
            names = [p.title() for p in personas]
            role_names[f"{role}_name"] = names[0] if len(names) == 1 else ", ".join(names)
            role_names[f"{role}_names"] = ", ".join(names)
            all_attendees.extend(personas)

        # Format the structure prompt
        structure = config['structure'].format(
            prompt=trigger.get('prompt', trigger.get('subject', 'the topic')),
            **role_names
        )

        # Build the exercise prompt
        exercise_prompt = f"""You are generating a structured EXERCISE for {org['org_name']}.

=== EXERCISE TYPE: {config['name']} ===
{structure}

=== KNOWLEDGE BASE (cite this!) ===
{shared_context['knowledge_text'][:6000]}

{shared_context.get('web_research_text', '')}

=== LIVE CONTEXT ===
Projects: {shared_context['projects_text']}
Leads: {shared_context['leads_text']}
Open Tasks: {shared_context['actions_text']}

=== RULES ===
1. Each persona speaks in their distinct voice (load from their prompt files)
2. Follow the exercise structure EXACTLY — this is not a free-form discussion
3. End with a concrete ARTIFACT: decision, recommendation, lessons learned, etc.
4. Max 10-12 exchanges. Quality over quantity.
5. Cite specific facts from the knowledge base, not generalities.

Generate the exercise as a JSON object:
{{
    "exercise_type": "{config['name']}",
    "subject": "{trigger.get('subject', '')}",
    "exchanges": [
        {{"speaker": "persona_key", "role": "their_role", "message": "what they said"}},
        ...
    ],
    "artifact": {{
        "type": "{config['artifact_type']}",
        "title": "Brief title",
        "content": "The actual artifact content"
    }},
    "next_step": "Concrete next action if any"
}}
JSON:"""

        try:
            raw = await self.llm_service.complete(exercise_prompt, temperature=0.7)
            result = self._parse_json_response(raw)
            if result:
                result['trigger'] = trigger
            return result
        except Exception as e:
            logger.error(f"Exercise generation failed: {e}")
            return None

    async def _post_exercise_results(
        self,
        config: Dict[str, Any],
        role_assignments: Dict[str, List[str]],
        trigger: Dict[str, Any],
        conversation: Dict[str, Any],
        now: datetime
    ):
        """Post exercise results to the bots channel."""
        
        # Build header
        all_attendees = []
        for personas in role_assignments.values():
            all_attendees.extend(personas)
        
        meeting_personas = {
            "librarian": {"name": "Librarian", "emoji": "📚"},
            "coordinator": {"name": "Coordinator", "emoji": "📋"},
            "scout": {"name": "Scout", "emoji": "🔍"},
            "dreamer": {"name": "Dreamer", "emoji": "💭"},
            "rainmaker": {"name": "Rainmaker", "emoji": "🎯💰"},
            "steward": {"name": "Steward", "emoji": "🪴"},
            "shepherd": {"name": "Shepherd", "emoji": "🐑"},
            "accountant": {"name": "Accountant", "emoji": "💵"},
        }

        attendee_emojis = " ".join([meeting_personas.get(p, {}).get('emoji', '🤖') for p in all_attendees])
        attendee_names = ", ".join([meeting_personas.get(p, {}).get('name', p) for p in all_attendees])

        subject = conversation.get('subject', trigger.get('subject', 'Exercise'))
        
        lines = [
            f"## {config['emoji']} {config['name']}: {subject}",
            f"*{attendee_emojis} {attendee_names} • {now.strftime('%I:%M %p')}*",
            "",
            "---",
            ""
        ]

        # Add exchanges
        for ex in conversation.get('exchanges', []):
            speaker_key = ex.get('speaker', 'unknown')
            speaker_info = meeting_personas.get(speaker_key, {'emoji': '🤖', 'name': speaker_key})
            role = ex.get('role', '')
            role_tag = f" [{role}]" if role else ""
            message = ex.get('message', '')
            lines.append(f"**{speaker_info['emoji']} {speaker_info['name']}{role_tag}:** {message}")
            lines.append("")

        # Add artifact (the key output!)
        artifact = conversation.get('artifact', {})
        if artifact and artifact.get('content'):
            artifact_type = artifact.get('type', 'output')
            artifact_title = artifact.get('title', 'Output')
            artifact_content = artifact.get('content', '')
            
            type_emojis = {
                'decision': '⚖️',
                'risk_register': '⚠️',
                'lessons_learned': '📝',
                'recommendation': '💡',
                'talking_points': '🎤',
                'priority_list': '📋',
                'proof_point': '📊',
                'response': '💬'
            }
            emoji = type_emojis.get(artifact_type, '📄')
            
            lines.append("---")
            lines.append(f"### {emoji} {artifact_title}")
            lines.append("")
            lines.append(artifact_content)
            lines.append("")

        # Add next step if present
        next_step = conversation.get('next_step')
        if next_step:
            lines.append("---")
            lines.append(f"**➡️ Next Step:** {next_step}")

        # Post to channel
        full_text = "\n".join(lines)
        
        target_channel_id = int(self.bots_office_channel_id) if self.bots_office_channel_id else None
        if not target_channel_id:
            target_channel_id = int(self.bots_channel_id) if self.bots_channel_id else None
            
        if not target_channel_id:
            logger.warning("No bots channel configured for exercise")
            return
            
        channel = self.bot.get_channel(target_channel_id)
        if not channel:
            logger.error(f"Could not find channel: {target_channel_id}")
            return

        try:
            embed = discord.Embed(
                description=full_text[:4000],
                color=discord.Color.from_rgb(66, 135, 181),
                timestamp=now
            )
            await channel.send(embed=embed)
            logger.info(f"Posted exercise: {config['name']} - {subject}")
        except Exception as e:
            logger.error(f"Failed to post exercise: {e}")

        # Mark partner agenda item as discussed if applicable
        if trigger.get('type') == 'partner_agenda' and trigger.get('id'):
            db = getattr(self.bot, "db", None)
            if db:
                try:
                    async with db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE meeting_agenda_items 
                            SET status = 'discussed', used_at = datetime('now')
                            WHERE id = ?
                            """,
                            (trigger['id'],)
                        )
                        await conn.commit()
                except Exception as e:
                    logger.warning(f"Failed to mark agenda item as discussed: {e}")

    @hourly_persona_meeting.before_loop
    async def before_hourly_persona_meeting(self):
        await self.bot.wait_until_ready()
        # Stagger start to avoid running immediately on bot start
        await asyncio.sleep(random.randint(60, 300))

    async def _hold_persona_meeting(self, now: datetime, meeting_type: str = "general", topic: Optional[str] = None):
        """Execute a persona meeting with 2-3 randomly selected personas."""
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()

        # Meeting type configurations
        meeting_configs = {
            "general": {
                "name": "General Discussion",
                "focus": "open-ended exploration of a topic from the knowledge base",
                "preferred_personas": None,  # Random selection
                "prompt_addon": """
This is a conversation, not a meeting. No agenda. No facilitator.

These are coworkers who:
- Have history together (some good, some awkward)
- Have strong opinions they don't always filter
- Go on tangents
- Interrupt each other
- Reference old jokes and old failures
- Sometimes agree loudly, sometimes disagree loudly
- Get distracted, get excited, get frustrated

Let the conversation go wherever it goes. If it stays on topic, great. If it veers into something unexpected, even better. The interesting stuff is in the tangents.

There is no "correct" outcome for this conversation.
"""
            },
            "risk_review": {
                "name": "Risk Review",
                "focus": "identifying and assessing current risks to projects, deadlines, or relationships",
                "preferred_personas": ["coordinator", "accountant", "shepherd"],
                "prompt_addon": """
Talk about what could go wrong. But don't be clinical about it.
Argue about whose fault it would be. Get defensive. Blame vendors.
Someone should be paranoid. Someone should be dismissive.
Let the tension play out."""
            },
            "pipeline_review": {
                "name": "Pipeline Review",
                "focus": "analyzing active leads, proposals, and revenue opportunities",
                "preferred_personas": ["rainmaker", "accountant", "coordinator"],
                "prompt_addon": """
MEETING TYPE: PIPELINE REVIEW
Focus this conversation on business development:
- Which leads are stale and need follow-up?
- Are our proposals competitive? Priced right?
- What's the likelihood of closing current opportunities?
- Where should we be prospecting that we're not?
The conversation should produce specific actions for pipeline health."""
            },
            "gaps_review": {
                "name": "Knowledge Gaps Review",
                "focus": "reviewing open knowledge gaps and deciding how to address them",
                "preferred_personas": ["librarian", "steward", "scout"],
                "prompt_addon": """
MEETING TYPE: KNOWLEDGE GAPS REVIEW
Focus this conversation on what we don't know:
- Which open gaps are most urgent to fill?
- Are there recurring questions we keep failing to answer?
- What documentation is missing or outdated?
- How do we capture knowledge that's only in people's heads?
The conversation should prioritize gaps and assign research tasks."""
            },
            "standup": {
                "name": "Daily Standup",
                "focus": "quick status update on current work and blockers",
                "preferred_personas": ["coordinator", "shepherd"],
                "prompt_addon": """
MEETING TYPE: QUICK STANDUP
Keep this conversation BRIEF and action-focused:
- What got done since yesterday?
- What's blocked or at risk?
- Who needs help with what?
Generate only 6-10 exchanges. Be concise. No rambling."""
            },
            "technical_deep_dive": {
                "name": "Technical Deep Dive",
                "focus": "solving a specific technical challenge or debugging a system",
                "preferred_personas": ["scout", "dreamer", "librarian"],
                "prompt_addon": """
MEETING TYPE: TECHNICAL DEEP DIVE
This is an R&D problem-solving session. Focus on:
- A specific technical challenge from an active project
- How would we actually BUILD or FIX this?
- What libraries, APIs, hardware would solve this?
- Search the web for solutions, tutorials, datasheets
- Propose concrete code approaches or hardware configurations
DO NOT talk about process, documentation, or knowledge gaps as abstract concepts.
Talk about SOLUTIONS: "We could use OpenCV's BackgroundSubtractorMOG2" or "The Sony API supports VISCA over IP"."""
            },
            "prototyping": {
                "name": "Prototyping Session",
                "focus": "brainstorming and designing a new feature or interactive experience",
                "preferred_personas": ["dreamer", "scout", "steward"],
                "prompt_addon": """
MEETING TYPE: PROTOTYPING SESSION  
This is a creative build session. Focus on:
- What would be cool to build?
- How would visitors/users interact with it?
- What's the simplest prototype that proves the concept?
- What tech stack makes sense? Unity? Python? Arduino?
- Sketch out the user flow or interaction model
Be inventive. Propose wild ideas, then figure out how to make them real."""
            },
            "research": {
                "name": "Research Sprint",
                "focus": "investigating a technology, vendor, or approach we don't know enough about",
                "preferred_personas": ["scout", "librarian"],
                "prompt_addon": """
MEETING TYPE: RESEARCH SPRINT
This is a web search and investigation session. Focus on:
- What technology/vendor/approach should we research?
- SEARCH THE WEB for specs, tutorials, case studies, datasheets
- Compare alternatives: "Matrox vs Magewell vs NDI"
- Find real-world examples of similar projects
- Summarize findings with links and concrete recommendations
You MUST use web search in this meeting. Don't just discuss - investigate."""
            },
            "strategic": {
                "name": "Strategic Discussion",
                "focus": "longer-term thinking about direction, opportunities, and positioning",
                "preferred_personas": ["scout", "dreamer", "rainmaker"],
                "prompt_addon": """
MEETING TYPE: STRATEGIC DISCUSSION
Focus on bigger-picture questions:
- Where should we be in 6-12 months?
- What market shifts should we prepare for?
- Are we building the right capabilities?
- What would make us more competitive?
This can be more speculative, but ground it in real market context."""
            }
        }
        
        config = meeting_configs.get(meeting_type, meeting_configs["general"])
        # Override config name if topic provided
        if topic:
             config = config.copy()
             config['name'] += f": {topic}"
             # Inject topic into prompt addon
             original_addon = config.get("prompt_addon", "")
             config["prompt_addon"] = f"{original_addon}\n\nSPECIAL FOCUS TOPIC: {topic}\nThe team MUST discuss this specific topic primarily."

        # Define available personas for meetings (with personality traits)
        meeting_personas = {
            "librarian": {
                "name": "Librarian",
                "emoji": "📚",
                "personality": "methodical, detail-oriented, knowledge-focused",
                "concerns": "documentation gaps, knowledge organization, retrieval quality"
            },
            "coordinator": {
                "name": "Coordinator", 
                "emoji": "📋",
                "personality": "practical, deadline-aware, process-focused",
                "concerns": "project timelines, task coordination, meeting efficiency"
            },
            "scout": {
                "name": "Scout",
                "emoji": "🔍", 
                "personality": "curious, outward-looking, opportunity-driven",
                "concerns": "market trends, competitor moves, new technologies, industry news"
            },
            "dreamer": {
                "name": "Dreamer",
                "emoji": "💭",
                "personality": "imaginative, unconventional, future-focused",
                "concerns": "innovation opportunities, creative pivots, blue-sky possibilities"
            },
            "rainmaker": {
                "name": "Rainmaker",
                "emoji": "🎯💰",
                "personality": "results-oriented, relationship-focused, revenue-conscious",
                "concerns": "pipeline health, client relationships, revenue targets, closing deals"
            },
            "steward": {
                "name": "Steward",
                "emoji": "🪴",
                "personality": "reflective, improvement-focused, systems-thinking",
                "concerns": "bot health, learning loop effectiveness, partner engagement, continuous improvement"
            },
            "shepherd": {
                "name": "Shepherd",
                "emoji": "🐑",
                "personality": "nurturing, community-focused, morale-aware",
                "concerns": "team wellbeing, partner satisfaction, workload balance, culture"
            },
            "accountant": {
                "name": "Accountant",
                "emoji": "💵",
                "personality": "pragmatic, cynical about 'exposure', focused on hard numbers",
                "concerns": "profitability (not just revenue), creeping costs, unpaid invoices"
            }
        }

        # Add custom personas from database
        db = getattr(self.bot, "db", None)
        if db:
            try:
                async with db.acquire() as conn, conn.execute(
                    """
                        SELECT key, name, emoji, personality, concerns, project_context
                        FROM custom_personas
                        WHERE active = 1
                        """
                ) as cursor:
                    custom_rows = await cursor.fetchall()
                
                for row in custom_rows:
                    key, name, emoji, personality, concerns, project_context = row
                    # Add project context to concerns if specified
                    full_concerns = concerns
                    if project_context:
                        full_concerns = f"{concerns} (especially related to {project_context})"
                    
                    meeting_personas[key] = {
                        "name": name,
                        "emoji": emoji,
                        "personality": personality,
                        "concerns": full_concerns,
                        "project_context": project_context,
                        "is_custom": True,  # Flag for potential future use
                    }
                    
            except Exception as e:
                logger.warning(f"Failed to load custom personas for meeting: {e}")

        def _norm_ctx(ctx: Optional[str]) -> Optional[str]:
            if not ctx:
                return None
            return str(ctx).strip().lower()

        def _contexts_compatible(ctxs: List[str]) -> bool:
            # Allow:
            # - none
            # - a single shared context
            # - containment (e.g., "daily-derby" vs "daily derby")
            uniq = [c for c in {c for c in (ctxs or []) if c}]
            if len(uniq) <= 1:
                return True

            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    a = uniq[i]
                    b = uniq[j]
                    if a in b or b in a:
                        continue
                    return False

            return True

        def _pick_attendees(candidate_keys: List[str], desired_count: int) -> List[str]:
            # Try random groups first.
            for _ in range(30):
                if desired_count >= len(candidate_keys):
                    group = list(candidate_keys)
                else:
                    group = random.sample(candidate_keys, desired_count)

                ctxs = [
                    _norm_ctx(meeting_personas.get(k, {}).get("project_context"))
                    for k in group
                ]
                ctxs = [c for c in ctxs if c]
                if _contexts_compatible(ctxs):
                    return group

            # Fallback: if any custom persona has a context, pick ONE context owner then fill with generalists.
            ctx_owners = [
                k for k in candidate_keys
                if _norm_ctx(meeting_personas.get(k, {}).get("project_context"))
            ]
            if ctx_owners:
                anchor = random.choice(ctx_owners)
                anchor_ctx = _norm_ctx(meeting_personas.get(anchor, {}).get("project_context"))
                compatible_pool = [
                    k for k in candidate_keys
                    if k == anchor
                    or _norm_ctx(meeting_personas.get(k, {}).get("project_context")) in (None, anchor_ctx)
                ]
                if len(compatible_pool) >= desired_count:
                    # Ensure anchor included.
                    rest = [k for k in compatible_pool if k != anchor]
                    return [anchor] + random.sample(rest, desired_count - 1)

            # Last resort: whatever.
            if desired_count >= len(candidate_keys):
                return list(candidate_keys)
            return random.sample(candidate_keys, desired_count)

        # Select personas based on meeting type - "Popcorn" Style (Large Groups)
        preferred = config.get("preferred_personas")
        
        # Determine target size - User requested "All the personas"
        # We aim for a large group (4 to All), filtered for context compatibility
        total_available = len(meeting_personas)
        if total_available > 3:
            min_size = 4
            max_size = total_available
            desired_count = random.randint(min_size, max_size)
        else:
            desired_count = total_available

        # Prioritize preferred if they exist, but fill the room
        candidate_pool = list(meeting_personas.keys())
        if preferred:
            # Move preferred to front to ensure they are picked by _pick_attendees logic if possible?
            # actually _pick_attendees takes a list and samples.
            pass

        attendees = _pick_attendees(candidate_pool, desired_count)
        
        # Fallback: if we got too few (due to strict context matching or something), force at least 3 if possible
        if len(attendees) < 3 and total_available >= 3:
             attendees = _pick_attendees(candidate_pool, 3)

        logger.info(f"Persona meeting starting ({config['name']}): {', '.join(attendees)}")

        # Gather context to ground the conversation
        context = await self._gather_meeting_context(now)
        
        # Add meeting type info to context
        context['meeting_type'] = meeting_type
        context['meeting_type_name'] = config['name']
        context['meeting_type_focus'] = config['focus']
        context['meeting_type_addon'] = config.get('prompt_addon', '')

        # Generate the conversation
        conversation = await self._generate_persona_conversation(
            attendees, 
            meeting_personas, 
            context, 
            now
        )

        if not conversation:
            logger.warning("Persona meeting generated no output")
            return

        # Post to bots office
        await self._post_persona_meeting(attendees, meeting_personas, conversation, now, meeting_type)

    async def _gather_meeting_context(self, now: datetime) -> Dict[str, Any]:
        """Gather current business context to ground persona conversations."""
        context: Dict[str, Any] = {}
        db = getattr(self.bot, "db", None)

        # Time context
        context['day_of_week'] = now.strftime('%A')
        context['time_of_day'] = 'morning' if now.hour < 12 else ('afternoon' if now.hour < 17 else 'evening')
        context['date'] = now.strftime('%B %d, %Y')

        if db:
            try:
                async with db.acquire() as conn:
                    # Recent questions (what partners are asking about)
                    async with conn.execute(
                        """
                        SELECT question_text, username FROM bot_questions 
                        WHERE date(created_at) >= date('now', '-2 days')
                        ORDER BY created_at DESC LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['recent_questions'] = [
                            {'question': r[0][:100], 'user': r[1]} for r in (rows or [])
                        ]

                    # Open knowledge gaps (what we don't know)
                    async with conn.execute(
                        """
                        SELECT topic, question FROM knowledge_gaps 
                        WHERE status = 'open'
                        ORDER BY priority_score DESC LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['open_gaps'] = [
                            {'topic': r[0], 'question': r[1][:80]} for r in (rows or [])
                        ]

                    # Active projects with budget/dates
                    async with conn.execute(
                        """
                        SELECT name, status, end_date, budget_usd, description FROM projects 
                        WHERE status IN ('active', 'in_progress')
                        LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['active_projects'] = [
                            {
                                'title': r[0], 
                                'status': r[1], 
                                'milestone': f"Due {r[2]}" if r[2] else "No due date",
                                'budget': f"${r[3]:,.0f}" if r[3] else "No budget set",
                                'description': (r[4] or "")[:100]
                            } for r in (rows or [])
                        ]

                    # Pipeline leads with values
                    async with conn.execute(
                        """
                        SELECT name, status, next_action, value_estimate, updated_at FROM leads 
                        WHERE status NOT IN ('won', 'lost', 'dormant')
                        ORDER BY updated_at DESC LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['active_leads'] = [
                            {
                                'company': r[0], 
                                'stage': r[1], 
                                'next': r[2],
                                'value': f"${r[3]:,.0f}" if r[3] else "TBD",
                                'last_touch': r[4][:10] if r[4] else "unknown"
                            } for r in (rows or [])
                        ]

                    # Recent wins/updates (from /did entries)
                    async with conn.execute(
                        """
                        SELECT details, category, created_at, partner_username FROM partner_updates 
                        WHERE date(created_at) >= date('now', '-7 days')
                        ORDER BY created_at DESC LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['recent_wins'] = [
                            {
                                'update': r[0][:100], 
                                'category': r[1],
                                'when': r[2][:10] if r[2] else "",
                                'who': r[3] or ""
                            } for r in (rows or [])
                        ]

                    # Open action items with owners
                    async with conn.execute(
                        """
                        SELECT title, status, priority, assigned_to_username, due_date, created_at FROM tasks 
                        WHERE status NOT IN ('done', 'canceled')
                        ORDER BY 
                            CASE priority WHEN 'p0' THEN 1 WHEN 'p1' THEN 2 WHEN 'p2' THEN 3 ELSE 4 END,
                            due_date ASC NULLS LAST
                        LIMIT 8
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['open_actions'] = [
                            {
                                'title': r[0], 
                                'status': r[1], 
                                'priority': r[2] or 'p3',
                                'owner': r[3] or 'unassigned',
                                'due': r[4] or 'no due date',
                                'age_days': (now - datetime.fromisoformat(r[5].replace('Z', '+00:00'))).days if r[5] else 0
                            } for r in (rows or [])
                        ]
                    
                    # === LIVE STATE: Aggregate metrics ===
                    
                    # Count tasks by status
                    async with conn.execute(
                        """
                        SELECT status, COUNT(*) FROM tasks GROUP BY status
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['task_counts'] = {r[0]: r[1] for r in (rows or [])}
                    
                    # Count gaps by status
                    async with conn.execute(
                        """
                        SELECT status, COUNT(*) FROM knowledge_gaps GROUP BY status
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['gap_counts'] = {r[0]: r[1] for r in (rows or [])}
                    
                    # Pipeline value
                    async with conn.execute(
                        """
                        SELECT SUM(value_estimate) FROM leads WHERE status NOT IN ('won', 'lost', 'dormant')
                        """
                    ) as cursor:
                        row = await cursor.fetchone()
                        context['pipeline_value'] = row[0] if row and row[0] else 0
                    
                    # Won deals this quarter
                    async with conn.execute(
                        """
                        SELECT COUNT(*), SUM(value_estimate) FROM leads 
                        WHERE status = 'won' AND date(updated_at) >= date('now', '-90 days')
                        """
                    ) as cursor:
                        row = await cursor.fetchone()
                        context['won_this_quarter'] = {
                            'count': row[0] if row else 0,
                            'value': row[1] if row and row[1] else 0
                        }
                    
                    # Overdue tasks
                    async with conn.execute(
                        """
                        SELECT COUNT(*) FROM tasks 
                        WHERE due_date < date('now') AND status NOT IN ('done', 'canceled')
                        """
                    ) as cursor:
                        row = await cursor.fetchone()
                        context['overdue_tasks'] = row[0] if row else 0
                    
                    # Last meeting takeaways (what did personas conclude recently?)
                    async with conn.execute(
                        """
                        SELECT meeting_topic, insight, owner, urgency 
                        FROM persona_meeting_takeaways 
                        WHERE date(created_at) >= date('now', '-3 days')
                        ORDER BY created_at DESC LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['recent_meeting_takeaways'] = [
                            {
                                'topic': r[0],
                                'insight': r[1][:80],
                                'owner': r[2],
                                'urgency': r[3]
                            } for r in (rows or [])
                        ]
                    
                    # Distinct topics from last 24 hours to AVOID repetition
                    async with conn.execute(
                        """
                        SELECT DISTINCT meeting_topic 
                        FROM persona_meeting_takeaways 
                        WHERE datetime(created_at) >= datetime('now', '-24 hours')
                        AND meeting_topic IS NOT NULL AND meeting_topic != ''
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['recent_topics_to_avoid'] = [r[0] for r in (rows or [])]
                    
                    # ==== USER-SUBMITTED AGENDA ITEMS (PRIORITY) ====
                    # These are topics users explicitly want discussed. 
                    # We allow re-discussion of active items, but prevent immediate looping (wait 24h).
                    async with conn.execute(
                        """
                        SELECT id, topic, context, submitted_by_username, priority
                        FROM meeting_agenda_items 
                        WHERE status IN ('pending', 'discussed') 
                        AND datetime(expires_at) > datetime('now')
                        AND (used_at IS NULL OR datetime(used_at) < datetime('now', '-24 hours'))
                        ORDER BY 
                            CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                            COALESCE(datetime(used_at), '1970-01-01') ASC,
                            datetime(created_at) ASC
                        LIMIT 5
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                        context['agenda_items'] = [
                            {
                                'id': r[0],
                                'topic': r[1],
                                'context': r[2] or '',
                                'submitted_by': r[3],
                                'priority': r[4]
                            } for r in (rows or [])
                        ]
                    
                    # Expire old agenda items
                    await conn.execute(
                        """
                        UPDATE meeting_agenda_items 
                        SET status = 'expired' 
                        WHERE status IN ('pending', 'discussed') AND datetime(expires_at) <= datetime('now')
                        """
                    )
                    await conn.commit()

            except Exception as e:
                logger.warning(f"Failed to gather some meeting context: {e}")

        # ==== DEEP KNOWLEDGE RETRIEVAL ====
        # Build comprehensive search topics from all available sources
        grounding_topics = []
        if context.get('active_projects'):
            grounding_topics.extend([p['title'] for p in context['active_projects']])
        if context.get('recent_questions'):
            grounding_topics.extend([q['question'][:50] for q in context['recent_questions']])
        if context.get('open_gaps'):
            grounding_topics.extend([g['topic'] for g in context['open_gaps']])
        if context.get('active_leads'):
            grounding_topics.extend([l['company'] for l in context['active_leads']])
        if context.get('recent_wins'):
            grounding_topics.extend([w['update'][:40] for w in context['recent_wins']])

        # Filter out meta/process-heavy topics that tend to collapse meetings into the same narrow groove.
        # We want meetings grounded in specific projects/clients/tools from the knowledge base.
        _meta_filters = [
            "tracking", "contribution", "contributions", "suggestion", "suggestions",
            "documentation", "process", "workflow", "framework", "kpi", "metrics",
        ]
        grounding_topics = [
            t for t in grounding_topics
            if t and not any(f in t.lower() for f in _meta_filters)
        ]
        
        # Core org topics to always include for rich grounding
        # Loaded from org_profile.yaml → org.knowledge_topics
        from LeisureLLM.core.config_loader import OrgProfile
        _org = OrgProfile.load()
        core_topics = list(_org.knowledge_topics) if _org.knowledge_topics else []
        if not core_topics:
            # Fallback: use capabilities as search topics if no knowledge_topics configured
            core_topics = list(_org.capabilities) if _org.capabilities else [
                "project planning and deliverables",
                "team expertise and skills",
                "client communications and updates",
                "business development and pipeline",
                "community engagement and announcements",
            ]
        
        # ANTI-REPETITION: Filter out core topics that overlap with recently discussed topics
        recent_topics_to_avoid = context.get('recent_topics_to_avoid', [])
        if recent_topics_to_avoid:
            def topic_overlaps_recent(topic: str) -> bool:
                topic_lower = topic.lower()
                for recent in recent_topics_to_avoid:
                    recent_lower = (recent or '').lower()
                    # Check if key terms overlap
                    topic_words = set(w for w in topic_lower.split() if len(w) > 3)
                    recent_words = set(w for w in recent_lower.split() if len(w) > 3)
                    if topic_words & recent_words:  # If any significant word overlaps
                        return True
                return False
            
            filtered_core_topics = [t for t in core_topics if not topic_overlaps_recent(t)]
            # Use filtered list if we have enough topics, otherwise use originals
            if len(filtered_core_topics) >= 5:
                core_topics = filtered_core_topics
                logger.info(f"Filtered core topics to avoid recent: {len(core_topics)} remaining")
        
        # Combine context topics with random core topics
        num_core = min(5, len(core_topics))
        all_search_topics = grounding_topics + random.sample(core_topics, num_core)
        
        # Query knowledge base extensively - speed doesn't matter
        try:
            llm_cog = self.bot.get_cog("LLM")
            if llm_cog and hasattr(llm_cog, '_ensure_retriever'):
                retriever = llm_cog._ensure_retriever()
                if retriever:
                    # Search 5-6 different topics for comprehensive coverage
                    search_topics = random.sample(all_search_topics, min(6, len(all_search_topics)))
                    
                    from LeisureLLM.cogs.LLM import run_retriever_query
                    all_docs = []
                    for topic in search_topics:
                        docs = await asyncio.to_thread(run_retriever_query, retriever, topic)
                        all_docs.extend(docs[:5])  # Top 5 per topic = up to 30 docs
                    
                    # Deduplicate by content hash
                    seen_content = set()
                    unique_docs = []
                    for doc in all_docs:
                        content_hash = hash(doc.page_content[:200] if doc.page_content else "")
                        if content_hash not in seen_content:
                            seen_content.add(content_hash)
                            unique_docs.append(doc)
                    
                    if unique_docs:
                        # Format as knowledge snippets - generous content allowance
                        snippets = []
                        for doc in unique_docs[:15]:  # Up to 15 unique snippets
                            meta = doc.metadata or {}
                            src = meta.get("source_relpath") or meta.get("source") or "doc"
                            content = (doc.page_content or "")[:1200]  # 1200 chars per snippet
                            snippets.append(f"[{src}]:\n{content}")
                        
                        context['knowledge_snippets'] = snippets
                        logger.info(f"[RETRIEVAL-FIRST] Retrieved {len(snippets)} knowledge snippets from ChromaDB for meeting topics: {search_topics}")
                else:
                    logger.warning("[RETRIEVAL-FIRST] No retriever available - meeting will lack knowledge base context!")
            else:
                    logger.warning("[RETRIEVAL-FIRST] LLM cog not found - meeting will lack knowledge base context!")
        except Exception as e:
            logger.warning(f"[RETRIEVAL-FIRST] Failed to retrieve knowledge for meeting: {e}")

        # ==== WEB SEARCH: PROACTIVE FOR R&D ====
        # An R&D studio should actively research technical topics.
        # Web search runs for:
        #   1. Technical/research meeting types (always)
        #   2. When knowledge snippets mention specific tech/products
        #   3. When knowledge base is sparse on the topic
        meeting_type = context.get('meeting_type', 'general')
        is_research_meeting = meeting_type in ('research', 'technical_deep_dive', 'prototyping')
        knowledge_snippet_count = len(context.get('knowledge_snippets', []))
        
        # Extract technical terms from knowledge snippets for targeted web search
        tech_terms_to_research = []
        for snippet in context.get('knowledge_snippets', [])[:5]:
            # Look for product names, libraries, hardware mentioned
            snippet_lower = snippet.lower()
            tech_patterns = [
                'matrox', 'magewell', 'sony', 'opencv', 'python', 'unity', 'arduino',
                'raspberry pi', 'ndi', 'srt', 'rtsp', 'visca', 'pelco', 'ptz',
                'touchscreen', 'kiosk', 'encoder', 'decoder', 'hdmi', 'sdi',
                'api', 'sdk', 'library', 'framework', 'sensor', 'camera'
            ]
            for term in tech_patterns:
                if term in snippet_lower:
                    tech_terms_to_research.append(term)
        tech_terms_to_research = list(set(tech_terms_to_research))[:3]
        
        should_web_search = (
            is_research_meeting or  # Always search in research-focused meetings
            len(tech_terms_to_research) > 0 or  # Technical terms found in docs
            knowledge_snippet_count < 5  # Sparse knowledge base
        )
        
        if should_web_search and self.tavily_service and self.tavily_service.is_configured:
            try:
                web_queries = []
                
                # Technical terms get SPECIFIC, useful searches
                tech_query_templates = {
                    'matrox': 'Matrox Maevex encoder latency bandwidth specs datasheet',
                    'magewell': 'Magewell video encoder NDI specs',
                    'sony': 'Sony PTZ camera VISCA control API',
                    'opencv': 'OpenCV background subtraction outdoor lighting',
                    'unity': 'Unity kiosk mode memory management',
                    'arduino': 'Arduino sensor integration serial communication',
                    'raspberry pi': 'Raspberry Pi video streaming performance',
                    'ndi': 'NDI vs SRT latency comparison',
                    'srt': 'SRT streaming protocol configuration',
                    'rtsp': 'RTSP stream capture Python',
                    'visca': 'VISCA over IP PTZ camera control',
                    'ptz': 'PTZ camera preset programming',
                    'encoder': 'hardware video encoder 4K latency comparison',
                    'sensor': 'environmental sensor integration museum',
                    'camera': 'IP camera streaming low latency',
                }
                
                for term in tech_terms_to_research[:2]:
                    if term in tech_query_templates:
                        web_queries.append(tech_query_templates[term])
                    else:
                        web_queries.append(f"{term} technical specifications best practices")
                
                # Project-based searches - more specific
                if context.get('active_projects'):
                    project = context['active_projects'][0]['title']
                    web_queries.append(f'"{project}" museum exhibit technology')
                
                # If this is a research meeting, add comparison searches
                if is_research_meeting:
                    web_queries.append("interactive exhibit touchscreen kiosk software 2025")
                
                web_results = []
                for query in web_queries[:4]:  # Up to 4 web searches for R&D depth
                    try:
                        results = await self.tavily_service.search(query, max_results=3)
                        if results:
                            for r in results[:2]:
                                title = r.get('title', '')
                                content = r.get('content', '')[:500]
                                url = r.get('url', '')
                                if content:
                                    web_results.append(f"[WEB: {title}] ({url})\n{content}")
                    except Exception as e:
                        logger.debug(f"Web search failed for '{query}': {e}")
                
                if web_results:
                    context['web_research'] = web_results
                    logger.info(f"[R&D WEB SEARCH] Added {len(web_results)} web results for meeting. Queries: {web_queries[:4]}")
                else:
                    logger.info(f"[R&D WEB SEARCH] No results for queries: {web_queries[:4]}")
            except Exception as e:
                logger.warning(f"Web research for meeting failed: {e}")

        return context

    async def _generate_persona_conversation(
        self, 
        attendees: List[str], 
        personas: Dict[str, Dict], 
        context: Dict[str, Any],
        now: datetime
    ) -> Optional[Dict[str, Any]]:
        """Generate a conversation between personas using turn-by-turn generation.
        
        Each persona has their own system prompt, and exchanges are generated
        one at a time to produce genuinely distinct voices.
        """
        if not self.llm_service:
            return None

        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()

        # Load persona system prompts
        persona_prompts = await self._load_persona_prompts()
        
        # Build shared context that all personas can see
        shared_context = self._build_shared_context(context)

        # If we failed to retrieve docs, do not run a meeting (otherwise it devolves into generic filler topics).
        if shared_context.get('knowledge_text', '').startswith('[WARNING: No ChromaDB documents retrieved'):
            logger.warning("[RETRIEVAL-FIRST] Aborting persona meeting: no ChromaDB context available.")
            return None
        
        # Meeting type info
        meeting_type_name = context.get('meeting_type_name', 'General Discussion')
        meeting_type_addon = context.get('meeting_type_addon', '')

        # If any attendees are scoped (custom persona project_context), enforce that scope in both
        # topic selection and turn-by-turn generation.
        required_scope_terms = []
        scope_lines = []
        seen_scopes = set()
        for p in attendees:
            pinfo = personas.get(p, {})
            pctx = (pinfo.get('project_context') or '').strip()
            if not pctx:
                continue
            norm = pctx.lower()
            if norm in seen_scopes:
                continue
            seen_scopes.add(norm)
            required_scope_terms.append(pctx)
            scope_lines.append(f"- {pinfo.get('name', p)}: {pctx}")
        scope_block = "\n".join(scope_lines) if scope_lines else "- (none)"

        topic_anchors = self._extract_topic_anchors(context)
        anchor_lines = "\n".join(f"- {a}" for a in topic_anchors[:25]) if topic_anchors else "- (none)"
        
        # Build ban list from recently discussed topics
        recent_topics_to_avoid = context.get('recent_topics_to_avoid', [])
        recent_ban_lines = "\n".join(f"- {t}" for t in recent_topics_to_avoid[:10]) if recent_topics_to_avoid else "- (none recently)"
        
        # Include web research in topic selection if available
        web_research_block = shared_context.get('web_research_text', '')
        
        # ==== CHECK FOR USER-SUBMITTED AGENDA ITEMS FIRST ====
        agenda_items = context.get('agenda_items', [])
        agenda_topic_override = None
        agenda_item_id = None
        
        if agenda_items:
            # Use the highest priority agenda item
            top_agenda = agenda_items[0]
            agenda_topic_override = top_agenda['topic']
            agenda_item_id = top_agenda['id']
            agenda_context = top_agenda.get('context', '')
            agenda_priority = top_agenda.get('priority', 'normal')
            agenda_submitter = top_agenda.get('submitted_by', 'a partner')
            
            logger.info(f"Using agenda item #{agenda_item_id} as meeting topic: {agenda_topic_override[:50]}...")
            
            # Build a more focused prompt for user-submitted topics
            topic_prompt = f"""You are a meeting facilitator for {org['org_name']}.

=== MANDATORY TOPIC (submitted by {agenda_submitter}) ===
A partner has requested this topic be discussed. You MUST use this topic:

TOPIC: {agenda_topic_override}
{f'CONTEXT: {agenda_context}' if agenda_context else ''}
PRIORITY: {agenda_priority}

Your job: Create an opening question that kicks off a productive R&D discussion on this exact topic.
The question should drive toward SOLUTIONS, not just identify problems.

=== MEETING TYPE: {meeting_type_name} ===
{meeting_type_addon}

=== KNOWLEDGE BASE (for context) ===
{shared_context['knowledge_text'][:4000]}

{web_research_block}

=== ATTENDEES ===
{', '.join([personas[p]['name'] for p in attendees])}

Respond in JSON:
{{
    "meeting_topic": "{agenda_topic_override[:100]}",
    "opening_provocation": "A pointed HOW or WHAT question that drives toward a solution for this specific topic"
}}
JSON:"""
        
        else:
            # === PHASE 1: Generate meeting topic and opening (no agenda override) ===
            topic_prompt = f"""You are a meeting facilitator for {org['org_name']}.

Your job: Pick ONE specific TECHNICAL or CREATIVE topic for these personas to discuss.
This is an R&D shop - they want to BUILD things, SOLVE problems, and EXPERIMENT.

=== {org['org_name'].upper()} NEAR-TERM PRIORITIES ===
Meetings should support the team's immediate business goals.
Use the operational context and knowledge base below to identify specific priorities.

Topics should be:
- Directly actionable by the team
- Focused on actual capabilities listed in the operational context
- Relevant to the team's market and region

=== CRITICAL: R&D FOCUS ===
Pick topics about:
- HOW to build something (code, hardware, integration)
- HOW to fix a technical problem
- WHAT technology/approach to use
- HOW a prototype should work
- WHAT we learned from testing/research

Do NOT pick topics about:
- Process, documentation, or tracking
- "Defining" or "establishing" anything abstract
- Knowledge gaps as a meta-concept (instead, pick the actual technical question)

=== RECENTLY DISCUSSED (DO NOT REPEAT) ===
{recent_ban_lines}
Pick a DIFFERENT project, tool, or problem than these.

=== GROUNDING: Topic MUST reference one of these anchors ===
{anchor_lines}

=== MEETING TYPE: {meeting_type_name} ===
{meeting_type_addon}

=== SCOPE CONSTRAINTS ===
{scope_block}

=== OPERATIONAL FACTS (rates, portfolio, contracts) ===
{shared_context.get('operational_context', '')[:2500]}

=== CHROMADB KNOWLEDGE BASE (PRIMARY SOURCE) ===
{shared_context['knowledge_text']}

{web_research_block}

=== ATTENDEES ===
{', '.join([personas[p]['name'] for p in attendees])}

Examples of GOOD R&D topics (directly tied to team priorities):
- "How do we package our best project as a case study for procurement reviewers?"
- "What alternatives exist when our image processing pipeline fails under variable lighting?"
- "What's the minimum viable credibility packet for an RFP response?"
- "How do we reduce scene transition latency in our camera preset system?"
- "Prototype: self-service viewer for one of our deployed projects"
- "How do we quote a system retrofit for a small client?"
- "Admin interface - what monitoring features do clients actually need?"
- "Long-running kiosk memory management after 4+ hours of continuous use"

Examples of BAD topics (NEVER generate):
- "Establishing documentation practices"
- "Defining metrics for success"
- "Reviewing organizational knowledge gaps"
- "What are the latency figures?" (just stating a gap - instead, propose how to MEASURE them)
- "Differentiation that survives procurement" (too abstract - be SPECIFIC about what we're differentiating)
- "Packaging and pricing discipline" (too meta - discuss a SPECIFIC package or price)

Respond in JSON:
{{
    "meeting_topic": "Specific TECHNICAL problem or BUILD challenge (8-15 words)",
    "opening_provocation": "A pointed HOW or WHAT question that drives toward a solution"
}}
JSON:"""

        meeting_topic = None
        opening_provocation = None
        try:
            for attempt in range(3):
                topic_raw = await self.llm_service.complete(topic_prompt, temperature=0.6)
                topic_data = self._parse_json_response(topic_raw)
                if not topic_data:
                    logger.warning("Failed to parse meeting topic JSON")
                    continue

                candidate_topic = (topic_data.get('meeting_topic') or '').strip() or 'Quick discussion'
                candidate_open = (topic_data.get('opening_provocation') or '').strip() or 'What should we discuss?'

                # REJECT garbage/meta topics - if the topic smells like process-talk, abort/retry
                garbage_indicators = [
                    'establishing', 'formal', 'norms', 'framework', 'accountability',
                    'metrics', 'kpi', 'organizational', 'review process', 'evaluation',
                    'defining', 'practices', 'dynamics', 'culture', 'timeline for review',
                    'tracking contributions', 'tracking suggestions', 'tracking', 'contributions',
                    'suggestions', 'documentation', 'documentation practices', 'knowledge management',
                    'ai tools', 'tooling for tracking',
                ]
                topic_lower = candidate_topic.lower()
                if any(indicator in topic_lower for indicator in garbage_indicators):
                    logger.warning(f"Rejected garbage meeting topic (attempt {attempt+1}/3): {candidate_topic}")
                    continue

                # REJECT topics too similar to recently discussed topics (anti-repetition)
                recent_topics = context.get('recent_topics_to_avoid', [])
                if recent_topics:
                    topic_words = set(w.lower() for w in re.split(r'\W+', candidate_topic) if len(w) > 3)
                    is_repeat = False
                    for recent in recent_topics:
                        recent_words = set(w.lower() for w in re.split(r'\W+', recent or '') if len(w) > 3)
                        # If more than 60% of significant words overlap, it's too similar
                        if topic_words and recent_words:
                            overlap = len(topic_words & recent_words)
                            similarity = overlap / min(len(topic_words), len(recent_words))
                            if similarity > 0.6:
                                logger.warning(
                                    f"Rejected repeat topic (attempt {attempt+1}/3): '{candidate_topic}' "
                                    f"too similar to recent: '{recent}' (similarity: {similarity:.0%})"
                                )
                                is_repeat = True
                                break
                    if is_repeat:
                        continue

                # Prefer topics that mention real anchors, but don't reject otherwise
                # (This allows creative R&D topics while preferring grounded ones)
                if topic_anchors and not self._topic_contains_anchor(candidate_topic, topic_anchors):
                    logger.info(
                        f"Topic doesn't contain anchor (attempt {attempt+1}/3): {candidate_topic} - allowing anyway"
                    )

                # If any attendees are scoped, check topic relevance (but don't be overly strict).
                # Extract key terms from scope and see if topic mentions any of them.
                if required_scope_terms:
                    topic_lower = candidate_topic.lower()
                    
                    # Extract key terms (3+ char words) from each scope
                    def extract_key_terms(scope_text: str) -> set:
                        words = re.split(r'\W+', scope_text.lower())
                        # Filter out common words and keep significant terms
                        stopwords = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'have', 'will', 'can', 'are', 'was', 'new', 'ship', 'grow', 'build'}
                        return {w for w in words if len(w) > 3 and w not in stopwords}
                    
                    all_scope_terms = set()
                    for scope in required_scope_terms:
                        all_scope_terms.update(extract_key_terms(scope))
                    
                    # Check if topic contains ANY of the scope terms
                    topic_terms = extract_key_terms(candidate_topic)
                    matching_terms = topic_terms & all_scope_terms
                    
                    if not matching_terms and all_scope_terms:
                        if agenda_topic_override:
                            logger.info(
                                f"Agenda topic has no scope overlap (allowed): {candidate_topic}"
                            )
                        else:
                            logger.info(
                                f"Topic has no scope term overlap (attempt {attempt+1}/3): {candidate_topic} - allowing anyway"
                            )
                            # Don't reject - just log. The personas will steer conversation.

                meeting_topic = candidate_topic
                opening_provocation = candidate_open
                break

            if not meeting_topic or not opening_provocation:
                logger.warning("Failed to generate an acceptable meeting topic after retries")
                return None

        except Exception as e:
            logger.warning(f"Meeting topic generation failed: {e}")
            return None

        # === PHASE 2: Generate exchanges turn-by-turn ===
        exchanges = []
        conversation_history = f"Topic: {meeting_topic}\n\nOpening question: {opening_provocation}\n\n"
        
        # Determine number of exchanges based on meeting type - slightly longer for larger groups
        base_exchanges = 8 if context.get('meeting_type') == 'standup' else random.randint(12, 16)
        # Scale up slightly if many attendees so everyone gets a chance
        num_exchanges = max(base_exchanges, len(attendees) * 2)
        
        # Track last speakers to ensure "popcorn" alternation across the room
        last_speaker = None
        recent_speakers = [] # Keep track of last N speakers
        
        for i in range(num_exchanges):
            # Pick speaker - Try to pick someone who hasn't spoken recently
            # Exclude recent speakers to force "popcorn" around the room
            candidates = [p for p in attendees if p not in recent_speakers]
            
            if not candidates:
                 # If everyone has spoken recently (or small group), just avoid immediate repetition
                candidates = [p for p in attendees if p != last_speaker]
            
            if not candidates:
                candidates = attendees # Fallback (solo)
            
            speaker = random.choice(candidates)
            
            last_speaker = speaker
            recent_speakers.append(speaker)
            # Maintain memory of ~50% of the room size to ensure rotation
            memory_size = max(1, len(attendees) // 2)
            if len(recent_speakers) > memory_size:
                recent_speakers.pop(0)
            
            # Get persona prompt
            persona_key = speaker
            persona_info = personas.get(persona_key, {})
            persona_prompt = persona_prompts.get(persona_key, '')
            
            if not persona_prompt:
                # Dynamic prompt for custom personas (hired via /hire)
                name = persona_info.get('name', speaker)
                personality = persona_info.get('personality', 'thoughtful and direct')
                concerns = persona_info.get('concerns', 'general improvements')
                project_context = (persona_info.get('project_context') or '').strip()

                persona_prompt = self._build_custom_persona_prompt(
                    org['org_name'],
                    name,
                    personality,
                    concerns,
                )

                if project_context:
                    persona_prompt += f"""

=== HARD SCOPE CONSTRAINT ===
You are scoped to: {project_context}.
- You MUST stay within this project/domain.
- If the conversation drifts outside this scope, say so plainly and convert it into a KNOWLEDGE GAP with a concrete source_hint rather than speculating.
"""
            
            # Determine conversation stage - loose guidance, not rigid structure
            progress = i / num_exchanges
            if progress < 0.2:
                stage_instruction = ""  # Let them find their own way in
            elif progress > 0.8:
                stage_instruction = "(The conversation is winding down. You can wrap up, make a final point, or throw in a curveball.)"
            else:
                stage_instruction = ""  # No stage management - let it flow

            # Meeting type specific goal - just a nudge, not a mandate
            meeting_type = context.get('meeting_type', 'general')
            if meeting_type == 'risk_review':
                goal_instruction = "(This is supposed to be about risk, but you can go wherever the conversation takes you.)"
            elif meeting_type == 'pipeline_review':
                goal_instruction = "(This started as a pipeline review. Feel free to derail it if something more interesting comes up.)"
            elif meeting_type == 'standup':
                goal_instruction = "(Quick check-in. But if someone says something provocative, run with it.)"
            elif meeting_type == 'strategic':
                goal_instruction = "(Big picture thinking. Or small picture griping. Whatever feels right.)"
            elif meeting_type == 'technical_deep_dive':
                goal_instruction = "(Nerding out encouraged. Get into the weeds.)"
            elif meeting_type == 'prototyping':
                goal_instruction = "(What would be fun to build? What would be weird to build?)"
            elif meeting_type == 'research':
                goal_instruction = "(What did you find? What surprised you? What's bullshit?)"
            else:
                goal_instruction = ""  # No goal, just vibes

            # Build turn prompt
            speaker_scope = (persona_info.get('project_context') or '').strip()
            speaker_scope_block = ""
            if speaker_scope:
                speaker_scope_block = f"""

=== YOUR SCOPE (HARD CONSTRAINT) ===
Your domain is: {speaker_scope}.
- Only speak within this domain.
- If asked about other projects/domains, refuse briefly and state a knowledge gap with a concrete source_hint.
"""

            # Attendees context
            other_attendees_names = [personas.get(p, {}).get('name', p) for p in attendees if p != speaker]
            attendees_block = f"You are in this meeting with: {', '.join(other_attendees_names)}."

            # Include web research if available
            web_research_section = ""
            if shared_context.get('web_research_text'):
                web_research_section = f"""

=== WEB RESEARCH (use this!) ===
{shared_context['web_research_text'][:3000]}
"""

            challenge_instruction = ""
            if i > 0 and last_speaker and last_speaker != speaker:
                last_persona_info = personas.get(last_speaker, {})
                last_name = last_persona_info.get('name', last_speaker)
                challenge_instruction = f"""
=== PRESSURE TEST ===
If {last_name}'s last point is weak, unsourced, risky, expensive, or off-target, challenge that point directly.
If it is sound, move the conversation forward instead of agreeing generically.
"""

            turn_prompt = f"""{persona_prompt}

=== MEETING CONTEXT ===
You are {persona_info.get('name', speaker)}.
{attendees_block}
Topic: {meeting_topic}
{goal_instruction}
{speaker_scope_block}

=== AVAILABLE CONTEXT ===
Operational facts:
{shared_context.get('operational_context', '')[:2000]}

Knowledge base:
{shared_context['knowledge_text'][:3500]}
{web_research_section}

=== CONVERSATION SO FAR ===
{conversation_history}

{challenge_instruction}

=== YOUR TURN CONTRACT ===
- Make one primary move: add evidence, challenge an assumption, name a tradeoff, propose an experiment, surface a risk, or connect to prior work.
- Use a specific detail when one is available.
- If you go beyond the evidence, label it plainly as a hypothesis.
- Do not repeat the previous point in different words.
- Stay concise, 2-4 sentences max.
- Do not use speaker labels.

Write only what {persona_info.get('name', speaker)} says next.
"""

            try:
                response = await self.llm_service.complete(
                    turn_prompt, temperature=0.7
                )
                
                # Clean up response
                message = response.strip()
                # Remove any accidental speaker labels
                for p in attendees:
                    pname = personas.get(p, {}).get('name', p)
                    if message.startswith(f"{pname}:"):
                        message = message[len(pname)+1:].strip()
                    if message.startswith(f"**{pname}**:"):
                        message = message[len(pname)+4:].strip()
                
                # ANTI-SLOP: Reject responses that sound like manager-speak
                slop_patterns = [
                    "who can compile", "who can gather", "who's gathering", "who can take that on",
                    "let's focus on", "we need to ensure", "we need solid data",
                    "let's not lose sight", "the core issue", "what are the specific metrics",
                    "building on what", "to add to that", "i agree with",
                    "who will be responsible", "by friday", "let's assign",
                ]
                message_lower = message.lower()
                is_slop = any(pattern in message_lower for pattern in slop_patterns)
                
                # Also reject if too similar to the previous exchange
                if exchanges and not is_slop:
                    last_msg = exchanges[-1].get('message', '').lower()
                    # Simple word overlap check
                    msg_words = set(message_lower.split())
                    last_words = set(last_msg.split())
                    if len(msg_words) > 5 and len(last_words) > 5:
                        overlap = len(msg_words & last_words) / min(len(msg_words), len(last_words))
                        if overlap > 0.5:
                            is_slop = True
                            logger.debug(f"Rejected repetitive message from {speaker}: {message[:50]}...")
                
                if message and not is_slop:
                    exchanges.append({
                        'speaker': speaker,
                        'message': message
                    })
                    conversation_history += f"{persona_info.get('name', speaker)}: {message}\n\n"
                elif is_slop:
                    logger.debug(f"Rejected slop from {speaker}: {message[:50]}...")
                    # Don't add to history - try to generate a better response next turn
                    
            except Exception as e:
                logger.warning(f"Turn generation failed for {speaker}: {e}")
                continue
        
        if len(exchanges) < 4:
            logger.warning(f"Too few exchanges generated: {len(exchanges)}")
            return None

        # === PHASE 3: Generate takeaways and wrap-up ===
        attendee_names = [personas[p]['name'] for p in attendees]
        
        wrapup_prompt = f"""You are summarizing a meeting at {org['org_name']}.

=== MEETING ===
Topic: {meeting_topic}
Attendees: {', '.join(attendee_names)}

=== CONVERSATION ===
{conversation_history}

=== OPERATIONAL FACTS ===
{shared_context.get('operational_context', '')[:2000]}

=== KNOWLEDGE CONTEXT ===
{shared_context['knowledge_text'][:4000]}

=== CRITICAL RULES ===
1. OWNERS must be one of the persona names from this meeting: {', '.join(attendee_names)}
   - NEVER invent human names like "Jamie", "Sarah", etc.
   - NEVER assign to "partners", "team leads", or real people
   - These are AI personas — they can only assign work to THEMSELVES or each other

2. KNOWLEDGE GAPS are the most valuable output. If the conversation revealed something we don't know, capture it in detail:
   - What specific question was raised?
   - Why does it matter?
   - What would answering it unlock?

3. Takeaways should be OBSERVATIONS or QUESTIONS, not action items for humans.

Based on this conversation, generate:
1. A tangent that was explored (unexpected connection discovered)
2. An unresolved tension (real disagreement that wasn't settled)
3. 2-4 takeaways — these should be INSIGHTS or KNOWLEDGE GAPS, not assignments to humans
4. Optionally, a draft artifact if the discussion warrants a written document

Respond in JSON:
{{
    "tangent_explored": "Unexpected connection discovered",
    "unresolved_tension": "Disagreement that remains",
    "takeaways": [
        {{
            "insight": "A specific question we need answered, or a gap in our knowledge",
            "owner": "ONE OF: {', '.join(attendee_names)}",
            "urgency": "low|medium|high",
            "why_now": "what makes this timely"
        }}
    ],
    "knowledge_gaps": [
        {{
            "question": "Specific question we realized we can't answer",
            "context": "Why this came up and why it matters",
            "source_hint": "Where we might find the answer"
        }}
    ],
    "draft_artifact": {{
        "type": "email|proposal|checklist|timeline|budget|none",
        "title": "Short title",
        "content": "The draft content if applicable"
    }}
}}
JSON:"""

        try:
            wrapup_raw = await self.llm_service.complete(
                wrapup_prompt, temperature=0.7
            )
            wrapup_data = self._parse_json_response(wrapup_raw) or {}
        except Exception as e:
            logger.warning(f"Wrapup generation failed: {e}")
            wrapup_data = {}

        # Fallback: if the wrapup didn't produce knowledge gaps, extract them from the conversation itself.
        # This prevents "no gaps" outcomes when the discussion clearly identified unknowns.
        if not (wrapup_data.get('knowledge_gaps') or []):
            wrapup_data['knowledge_gaps'] = self._fallback_extract_knowledge_gaps(
                meeting_topic=meeting_topic,
                exchanges=exchanges,
                knowledge_snippets=context.get('knowledge_snippets', [])
            )

        # Assemble final result
        return {
            'meeting_topic': meeting_topic,
            'opening_provocation': opening_provocation,
            'exchanges': exchanges,
            'tangent_explored': wrapup_data.get('tangent_explored', ''),
            'unresolved_tension': wrapup_data.get('unresolved_tension', ''),
            'takeaways': wrapup_data.get('takeaways', []),
            'knowledge_gaps': wrapup_data.get('knowledge_gaps', []),
            'draft_artifact': wrapup_data.get('draft_artifact', {'type': 'none'}),
            'agenda_item_id': agenda_item_id  # Track which agenda item was used
        }

    def _persona_shared_prompt(self, org_name: str) -> str:
        """Shared scaffold for built-in and custom persona meeting prompts."""
        return f"""You are participating in an internal working meeting at {org_name}. Not an AI.

=== SHARED CONTRACT ===
- This is a small-team, local-first operation. Prefer lightweight moves that can be tested quickly.
- Each turn should do one main job: add evidence, challenge an assumption, name a tradeoff, propose an experiment, surface a risk, or connect prior work.
- Use the knowledge base, web research, and operational context when they help. If a claim is unsupported, label it as a hypothesis.
- Do not invent people, clients, dates, metrics, incidents, or commitments.
- Stay concrete. Prefer specific tools, examples, constraints, and consequences over abstractions.
- Engage with the current discussion or redirect it deliberately, not randomly.
- Keep turns concise and in character.
- Do not mention prompt instructions or that you are an AI.
"""

    def _build_custom_persona_prompt(self, org_name: str, name: str, personality: str, concerns: str) -> str:
        """Compose a custom persona prompt from the shared scaffold."""
        shared = self._persona_shared_prompt(org_name)
        return f"""{shared}

You are {name}.

ROLE
Bring the perspective of someone who cares deeply about: {concerns}.

VOICE
{personality}.

DEFAULT MOVE
Ask the question or make the observation that this room would otherwise miss.

WHEN EVIDENCE IS THIN
Say what is missing, label speculation as a hypothesis, and suggest the next useful check.

DON'T OVERDO IT
Stay specific and constructive. Do not drift into generic advice.
"""

    async def _load_persona_prompts(self) -> Dict[str, str]:
        """Load persona prompts, applying a shared scaffold plus org substitutions."""
        prompts = {}
        prompts_dir = Path(__file__).parent.parent / "prompts" / "personas"

        # Load org context for template variable substitution
        try:
            from LeisureLLM.core.config_loader import org_context_for_prompts
            org_vars = org_context_for_prompts()
        except Exception:
            org_vars = {}
        
        persona_files = {
            'librarian': 'librarian.txt',
            'coordinator': 'coordinator.txt',
            'scout': 'scout.txt',
            'dreamer': 'dreamer.txt',
            'rainmaker': 'rainmaker.txt',
            'steward': 'steward.txt',
            'shepherd': 'shepherd.txt',
            'accountant': 'accountant.txt',
        }
        
        shared_prompt = self._persona_shared_prompt(org_vars.get("org_name", "the team"))

        for persona_key, filename in persona_files.items():
            filepath = prompts_dir / filename
            try:
                if filepath.exists():
                    text = filepath.read_text(encoding='utf-8')
                    # Substitute {org_name}, {location}, {region}, etc.
                    for var, value in org_vars.items():
                        text = text.replace(f'{{{var}}}', str(value))
                    prompts[persona_key] = f"{shared_prompt}\n\n{text.strip()}"
            except Exception as e:
                logger.warning(f"Failed to load persona prompt for {persona_key}: {e}")
        
        return prompts

    def _build_shared_context(self, context: Dict[str, Any]) -> Dict[str, str]:
        """Build formatted context strings for meeting generation.
        
        IMPORTANT: knowledge_text (from ChromaDB) is the PRIMARY source.
        All other context is supplementary.
        """
        # Load operational context (company facts, rates, portfolio, contracts)
        operational_context_text = ""
        try:
            operational_ctx_path = Path(__file__).parent.parent / "prompts" / "operational_context.txt"
            if operational_ctx_path.exists():
                operational_context_text = operational_ctx_path.read_text(encoding='utf-8')
        except Exception as e:
            logger.warning(f"Failed to load operational context: {e}")
        
        # Knowledge snippets from ChromaDB - THIS IS THE CORE
        snippets = context.get('knowledge_snippets', [])
        if snippets:
            knowledge_text = "=== PRIMARY SOURCE: ChromaDB Knowledge Base ===\n\n"
            knowledge_text += "\n\n---\n\n".join(snippets)
        else:
            knowledge_text = "[WARNING: No ChromaDB documents retrieved - knowledge base gap!]"
        
        # Live state
        live_state_lines = []
        task_counts = context.get('task_counts', {})
        if task_counts:
            todo = task_counts.get('todo', 0)
            in_progress = task_counts.get('in_progress', 0)
            live_state_lines.append(f"Tasks: {todo} todo, {in_progress} in-progress")
        
        overdue = context.get('overdue_tasks', 0)
        if overdue > 0:
            live_state_lines.append(f"OVERDUE TASKS: {overdue}")
        
        pipeline_value = context.get('pipeline_value', 0)
        if pipeline_value:
            live_state_lines.append(f"Pipeline value: ${pipeline_value:,.0f}")
        
        live_state_text = "\n".join(live_state_lines) or "No live state"
        
        # Projects
        projects_text = "\n".join([
            f"- {p['title']} ({p['status']}): {p.get('budget', '?')}"
            for p in context.get('active_projects', [])
        ]) or "None"
        
        # Leads
        leads_text = "\n".join([
            f"- {l['company']} ({l['stage']}): {l.get('value', 'TBD')}"
            for l in context.get('active_leads', [])
        ]) or "None"
        
        # Actions
        actions_text = "\n".join([
            f"- [{a['priority']}] {a['title']} ({a['status']})"
            for a in context.get('open_actions', [])
        ]) or "None"
        
        # Gaps
        gaps_text = "\n".join([
            f"- {g['topic']}: {g['question']}"
            for g in context.get('open_gaps', [])
        ]) or "None"
        
        # Web research (from Tavily) - important for R&D discussions
        web_research = context.get('web_research', [])
        if web_research:
            web_text = "=== WEB RESEARCH (fresh from the internet) ===\n\n"
            web_text += "\n\n---\n\n".join(web_research)
        else:
            web_text = ""
        
        return {
            'knowledge_text': knowledge_text,
            'live_state_text': live_state_text,
            'projects_text': projects_text,
            'leads_text': leads_text,
            'actions_text': actions_text,
            'gaps_text': gaps_text,
            'web_research_text': web_text,
            'operational_context': operational_context_text,
        }

    def _extract_topic_anchors(self, context: Dict[str, Any]) -> List[str]:
        """Extract anchor terms from live context + retrieved snippet sources.

        Used to force meeting topics to reference real entities (projects/clients/tools/people)
        instead of generic process themes.
        """
        anchors: set[str] = set()

        def add_phrase(phrase: str):
            if not phrase:
                return
            p = phrase.strip()
            if len(p) < 3:
                return
            anchors.add(p)
            # Also add longer individual words to improve matching
            for w in re.split(r"[^A-Za-z0-9]+", p):
                wl = w.strip()
                if len(wl) >= 4:
                    anchors.add(wl)

        for p in context.get('active_projects', []) or []:
            add_phrase(p.get('title') or '')
        for l in context.get('active_leads', []) or []:
            add_phrase(l.get('company') or '')
        for g in context.get('open_gaps', []) or []:
            add_phrase(g.get('topic') or '')

        # Also pull anchors from snippet source filenames if present: "[path/file.txt]:\n..."
        for snip in context.get('knowledge_snippets', []) or []:
            m = re.match(r"^\[(.+?)\]:", snip.strip())
            if not m:
                continue
            src = m.group(1)
            # basename, strip extension
            base = src.replace('\\', '/').split('/')[-1]
            if base.lower().endswith('.txt'):
                base = base[:-4]
            add_phrase(base)

        # De-emphasize meta/process anchors if they leaked in
        meta = {
            'tracking', 'contribution', 'contributions', 'suggestion', 'suggestions',
            'documentation', 'process', 'workflow', 'framework', 'kpi', 'metrics',
            'meeting', 'strategy', 'timeline'
        }
        filtered = [a for a in anchors if a and a.lower() not in meta]
        # Prefer longer/more specific anchors first
        filtered.sort(key=lambda s: (-len(s), s.lower()))
        return filtered

    def _topic_contains_anchor(self, topic: str, anchors: List[str]) -> bool:
        if not topic or not anchors:
            return False
        t = topic.lower()
        return any(a.lower() in t for a in anchors if a and len(a) >= 4)

    def _fallback_extract_knowledge_gaps(
        self,
        meeting_topic: str,
        exchanges: List[Dict[str, Any]],
        knowledge_snippets: List[str],
        max_gaps: int = 5
    ) -> List[Dict[str, str]]:
        """Extract knowledge gaps from conversation text when LLM wrapup is empty/invalid."""
        if not exchanges:
            return []

        text = "\n".join((ex.get('message') or '').strip() for ex in exchanges if ex.get('message'))
        if not text:
            return []

        # Identify candidate sentences that imply unknowns.
        gap_cues = [
            "documents do not", "docs do not", "do not provide", "no data", "lack",
            "missing", "unknown", "we don't have", "we do not have", "we need",
            "unclear", "not sure", "can't quantify", "flying blind"
        ]

        # Split into rough sentences.
        candidates: List[str] = []
        for raw in re.split(r"(?<=[\.!\?])\s+", text):
            s = (raw or "").strip()
            if not s:
                continue
            sl = s.lower()
            if "?" in s or any(cue in sl for cue in gap_cues):
                # Ignore obvious meta/process questions
                if any(bad in sl for bad in ["tracking contributions", "tracking suggestions", "kpi", "metrics", "framework"]):
                    continue
                candidates.append(s)

        if not candidates:
            return []

        # Prefer question sentences first.
        candidates.sort(key=lambda s: (0 if "?" in s else 1, -len(s)))

        # Build a few decent source hints from snippet sources.
        source_names: List[str] = []
        for snip in (knowledge_snippets or [])[:10]:
            m = re.match(r"^\[(.+?)\]:", (snip or "").strip())
            if m:
                source_names.append(m.group(1))
        source_names = list(dict.fromkeys(source_names))

        gaps: List[Dict[str, str]] = []
        seen: set[str] = set()

        for s in candidates:
            if len(gaps) >= max_gaps:
                break
            norm = re.sub(r"\s+", " ", s.lower()).strip()
            if norm in seen:
                continue
            seen.add(norm)

            # Question: if there's a '?', keep the shortest clause containing it.
            if "?" in s:
                question = s
            else:
                question = f"What specifically are we missing to resolve: {meeting_topic}?"

            question = question.strip()
            if len(question) > 180:
                question = question[:177] + "..."

            # Context: keep it grounded without inventing.
            context = "The discussion explicitly identified missing information needed for decisions/risk assessment."
            if any(k in norm for k in ["failure", "downtime", "reliability", "component"]):
                context = "Needed to quantify reliability/downtime risk and its margin/cash-flow impact."

            # Source hint: point back into the KB and the likely missing artifact.
            hint_bits = []
            if source_names:
                hint_bits.append(f"Start with: {', '.join(source_names[:3])}")
            hint_bits.append("Search Chroma for: component list/BOM, vendor datasheets, install logs")
            source_hint = "; ".join(hint_bits)

            gaps.append({
                "question": question,
                "context": context,
                "source_hint": source_hint,
            })

        return gaps

    def _parse_json_response(self, raw: str) -> Optional[Dict]:
        """Parse JSON from LLM response, handling common issues."""
        if not raw:
            return None
            
        cleaned = raw.strip()
        
        # Strip markdown fences
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        
        # Fix control characters
        cleaned = cleaned.replace('\r\n', '\\n').replace('\r', '\\n')
        
        # Escape unescaped control chars in strings
        result = []
        in_string = False
        escape_next = False
        for char in cleaned:
            if escape_next:
                result.append(char)
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                result.append(char)
                continue
            if char == '"':
                in_string = not in_string
                result.append(char)
                continue
            if in_string:
                if char == '\n':
                    result.append('\\n')
                elif char == '\t':
                    result.append('\\t')
                elif ord(char) < 32:
                    result.append(f'\\u{ord(char):04x}')
                else:
                    result.append(char)
            else:
                result.append(char)
        
        cleaned = ''.join(result)
        
        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    async def _post_persona_meeting(
        self, 
        attendees: List[str], 
        personas: Dict[str, Dict], 
        conversation: Dict[str, Any],
        now: datetime,
        meeting_type: str = "general"
    ):
        """Post the persona meeting results to #bots-office."""
        # Meeting type labels
        type_labels = {
            "general": "💬 General Discussion",
            "risk_review": "⚠️ Risk Review",
            "pipeline_review": "💰 Pipeline Review",
            "gaps_review": "❓ Knowledge Gaps Review",
            "standup": "📊 Daily Standup",
            "strategic": "🎯 Strategic Discussion"
        }
        type_label = type_labels.get(meeting_type, "💬 Discussion")
        
        # Build the meeting summary
        attendee_emojis = " ".join([personas[p]['emoji'] for p in attendees])
        attendee_names = ", ".join([personas[p]['name'] for p in attendees])
        topic = conversation.get('meeting_topic', 'Quick Sync')

        # Build opening section
        lines = [
            f"## {attendee_emojis} {topic}",
            f"*{type_label} • {attendee_names} • {now.strftime('%I:%M %p')}*",
        ]
        
        # Add opening provocation if present
        opening = conversation.get('opening_provocation')
        if opening:
            lines.append(f"\n> 💡 *\"{opening}\"*")
        
        lines.append("")
        lines.append("---")
        lines.append("")

        # Add ALL conversation exchanges (no cap - let it breathe)
        exchanges = conversation.get('exchanges', [])
        for ex in exchanges:
            speaker_key = ex.get('speaker', 'unknown')
            speaker_info = personas.get(speaker_key, {'emoji': '🤖', 'name': speaker_key})
            message = ex.get('message', '')
            lines.append(f"**{speaker_info['emoji']} {speaker_info['name']}:** {message}")
            lines.append("")  # Add spacing between exchanges
        
        # Add tangent explored if present
        tangent = conversation.get('tangent_explored')
        if tangent:
            lines.append(f"*🔀 Tangent explored: {tangent}*")
            lines.append("")

        # Add unresolved tension if present
        tension = conversation.get('unresolved_tension')
        if tension:
            lines.append(f"*⚡ Unresolved: {tension}*")
            lines.append("")

        # Add takeaways
        takeaways = conversation.get('takeaways', [])
        if takeaways:
            lines.append("---")
            lines.append("### 📌 Observations")
            for t in takeaways:
                urgency = t.get('urgency', 'medium')
                urgency_emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(urgency, '⚪')
                insight = t.get('insight', '')
                owner = t.get('owner', 'team')
                why_now = t.get('why_now', '')
                lines.append(f"{urgency_emoji} **{insight}** → *{owner}*")
                if why_now:
                    lines.append(f"   ↳ {why_now}")
        
        # Add knowledge gaps - these are the most valuable output!
        knowledge_gaps = conversation.get('knowledge_gaps', [])
        if knowledge_gaps:
            lines.append("")
            lines.append("---")
            lines.append("### ❓ Knowledge Gaps Identified")
            for gap in knowledge_gaps:
                question = gap.get('question', '')
                context = gap.get('context', '')
                source_hint = gap.get('source_hint', '')
                lines.append(f"**Q: {question}**")
                if context:
                    lines.append(f"*Why it matters:* {context}")
                if source_hint:
                    lines.append(f"*Where to look:* {source_hint}")
                lines.append("")
        
        # Add draft artifact if present
        draft_artifact = conversation.get('draft_artifact', {})
        artifact_type = draft_artifact.get('type', 'none')
        if artifact_type and artifact_type != 'none':
            artifact_title = draft_artifact.get('title', 'Draft')
            artifact_content = draft_artifact.get('content', '')
            
            type_emojis = {
                'email': '📧',
                'proposal': '📄',
                'checklist': '✅',
                'timeline': '📅',
                'budget': '💰'
            }
            emoji = type_emojis.get(artifact_type, '📝')
            
            lines.append("")
            lines.append("---")
            lines.append(f"### {emoji} Draft: {artifact_title}")
            lines.append("")
            lines.append(artifact_content)
        
        # Post to channel - split into multiple messages if needed
        full_text = "\n".join(lines)
        
        target_channel_id = int(self.bots_office_channel_id) if self.bots_office_channel_id else None
        if not target_channel_id:
            target_channel_id = int(self.bots_channel_id) if self.bots_channel_id else None
            
        if not target_channel_id:
            logger.warning("No bots channel configured for persona meeting")
            return
            
        channel = self.bot.get_channel(target_channel_id)
        if not channel:
            logger.error(f"Could not find channel: {target_channel_id}")
            return
            
        try:
            # Split into chunks if too long for single embed (Discord embed description ~4096 chars).
            # IMPORTANT: never drop the tail sections (Observations / Knowledge Gaps) due to truncation.
            def _split_for_embeds(text: str, limit: int = 3900) -> List[str]:
                parts: List[str] = []
                buf: List[str] = []
                size = 0
                for line in (text or "").split("\n"):
                    # +1 for newline
                    add = len(line) + 1
                    if buf and (size + add) > limit:
                        parts.append("\n".join(buf).strip())
                        buf = []
                        size = 0
                    buf.append(line)
                    size += add
                if buf:
                    parts.append("\n".join(buf).strip())
                # Remove empties
                return [p for p in parts if p]

            chunks = _split_for_embeds(full_text)
            for idx, chunk in enumerate(chunks):
                embed = discord.Embed(
                    description=chunk[:4000],
                    color=discord.Color.from_rgb(181, 166, 66),
                    timestamp=now if idx == 0 else None
                )
                await channel.send(embed=embed)
                    
            logger.info(f"Posted persona meeting: {topic} ({attendee_names})")
        except Exception as e:
            logger.error(f"Failed to post persona meeting: {e}")

        # === MARK AGENDA ITEM AS DISCUSSED ===
        agenda_item_id = conversation.get('agenda_item_id')
        if agenda_item_id:
            db = getattr(self.bot, "db", None)
            if db:
                try:
                    async with db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE meeting_agenda_items 
                            SET status = 'discussed', 
                                used_at = datetime('now'),
                                used_in_meeting_date = ?
                            WHERE id = ?
                            """,
                            (now.strftime("%Y-%m-%d"), agenda_item_id)
                        )
                        await conn.commit()
                    logger.info(f"Marked agenda item #{agenda_item_id} as discussed")
                except Exception as e:
                    logger.warning(f"Failed to mark agenda item as discussed: {e}")

        # === PROCESS TAKEAWAYS: Store for digest, create gaps, escalate if needed ===
        await self._process_persona_meeting_takeaways(
            attendees=attendees,
            conversation=conversation,
            now=now,
            channel=channel
        )

    async def _process_persona_meeting_takeaways(
        self,
        attendees: List[str],
        conversation: Dict[str, Any],
        now: datetime,
        channel: discord.TextChannel
    ):
        """
        Process takeaways from a persona meeting into real workflows:
        1. Store all takeaways for weekly digest
        2. Create TASKS from action-oriented takeaways
        3. Create KNOWLEDGE GAPS from questions/unknowns
        4. Create DRAFT /did entries for accomplishments discovered
        5. Escalate high-urgency items to partners
        """
        db = getattr(self.bot, "db", None)
        if not db:
            return

        takeaways = conversation.get('takeaways', []) or []
        explicit_gaps = conversation.get('knowledge_gaps', []) or []
        if not takeaways and not explicit_gaps:
            return

        topic = conversation.get('meeting_topic', 'Persona Meeting')
        opening = conversation.get('opening_provocation', '')
        tangent = conversation.get('tangent_explored', '')
        tension = conversation.get('unresolved_tension', '')
        meeting_date = now.strftime('%Y-%m-%d')
        attendees_json = json.dumps(attendees)
        
        # Track what we created for summary
        created_items = {
            'tasks': [],
            'gaps': [],
            'escalations': []
        }

        try:
            async with db.acquire() as conn:
                # First, persist explicit knowledge gaps (these come from wrapup or fallback extraction)
                for g in explicit_gaps:
                    gap_question = (g.get('question') or '').strip()
                    if not gap_question:
                        continue
                    gap_context = (g.get('context') or '').strip()
                    gap_topic = f"Persona Meeting: {topic}"[:100]

                    async with conn.execute(
                        """
                        SELECT id FROM knowledge_gaps
                        WHERE question = ? AND status = 'open'
                        LIMIT 1
                        """,
                        (gap_question,)
                    ) as cursor:
                        existing = await cursor.fetchone()
                    if existing:
                        continue

                    try:
                        await conn.execute(
                            """
                            INSERT INTO knowledge_gaps (topic, question, context, status, priority_score, notes, curation_status)
                            VALUES (?, ?, ?, 'open', ?, ?, 'defer')
                            """,
                            (
                                gap_topic,
                                gap_question[:500],
                                (gap_context or f"Emerged from {', '.join(attendees)} meeting.")[:500],
                                20,
                                f"Auto-created from persona meeting on {meeting_date}"
                            )
                        )
                    except Exception:
                        # Back-compat if older DB is missing curation columns
                        await conn.execute(
                            """
                            INSERT INTO knowledge_gaps (topic, question, context, status, priority_score, notes)
                            VALUES (?, ?, ?, 'open', ?, ?)
                            """,
                            (
                                gap_topic,
                                gap_question[:500],
                                (gap_context or f"Emerged from {', '.join(attendees)} meeting.")[:500],
                                20,
                                f"Auto-created from persona meeting on {meeting_date}"
                            )
                        )
                    async with conn.execute("SELECT last_insert_rowid()") as cursor:
                        row = await cursor.fetchone()
                        gap_id = row[0] if row else None
                    if gap_id:
                        created_items['gaps'].append({'id': gap_id, 'question': gap_question[:50]})

                for t in takeaways:
                    insight = t.get('insight', '')[:500]
                    owner = t.get('owner', 'team')[:100]
                    urgency = t.get('urgency', 'medium')
                    why_now = t.get('why_now', '')[:300]

                    if not insight:
                        continue
                    
                    insight_lower = insight.lower()

                    # 1. Store takeaway for weekly digest
                    await conn.execute(
                        """
                        INSERT INTO persona_meeting_takeaways 
                        (meeting_date, meeting_topic, attendees, insight, owner, urgency, why_now,
                         opening_provocation, tangent_explored, unresolved_tension)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (meeting_date, topic[:200], attendees_json, insight, owner, urgency, why_now,
                         opening[:300], tangent[:300], tension[:300])
                    )
                    
                    # Get the inserted takeaway ID
                    async with conn.execute("SELECT last_insert_rowid()") as cursor:
                        row = await cursor.fetchone()
                        takeaway_id = row[0] if row else None

                    # === CLASSIFY THE TAKEAWAY ===
                    
                    # ACTION indicators (things to DO)
                    action_indicators = [
                        'need to', 'should', 'must', 'will', 'going to',
                        'create', 'update', 'fix', 'check', 'verify', 'confirm',
                        'schedule', 'contact', 'email', 'call', 'meet with',
                        'order', 'purchase', 'budget', 'allocate',
                        'document', 'write', 'draft', 'prepare',
                        'review', 'audit', 'test', 'validate',
                        'assign', 'delegate', 'follow up'
                    ]
                    
                    # GAP indicators (things we DON'T KNOW)
                    gap_indicators = [
                        'we don\'t know', 'we need to find out', 'unclear', 'question',
                        'investigate', 'research', 'unknown', 'missing', 'gap',
                        'what if', 'how do we', 'why don\'t we', 'who knows',
                        'no documentation', 'undocumented', 'need to understand',
                        'not sure', 'uncertain', 'depends on'
                    ]
                    
                    is_gap = any(ind in insight_lower for ind in gap_indicators)
                    
                    # 2. Create KNOWLEDGE GAP if gap-oriented
                    # Knowledge gaps are the most valuable output - we no longer create tasks
                    # because the personas shouldn't assign work to humans
                    if is_gap:
                        gap_topic = f"Persona Meeting: {topic}"[:100]
                        gap_question = insight
                        gap_context = f"Emerged from {', '.join(attendees)} meeting. {why_now}"[:500]
                        
                        # Check for duplicate gaps
                        async with conn.execute(
                            """
                            SELECT id FROM knowledge_gaps 
                            WHERE question = ? AND status = 'open'
                            LIMIT 1
                            """,
                            (gap_question,)
                        ) as cursor:
                            existing = await cursor.fetchone()
                        
                        if not existing:
                            try:
                                await conn.execute(
                                    """
                                    INSERT INTO knowledge_gaps (topic, question, context, status, priority_score, notes, curation_status)
                                    VALUES (?, ?, ?, 'open', ?, ?, 'defer')
                                    """,
                                    (gap_topic, gap_question, gap_context, 
                                     30 if urgency == 'high' else 20 if urgency == 'medium' else 10,
                                     f"Auto-created from persona meeting on {meeting_date}")
                                )
                            except Exception:
                                # Back-compat if older DB is missing curation columns
                                await conn.execute(
                                    """
                                    INSERT INTO knowledge_gaps (topic, question, context, status, priority_score, notes)
                                    VALUES (?, ?, ?, 'open', ?, ?)
                                    """,
                                    (gap_topic, gap_question, gap_context, 
                                     30 if urgency == 'high' else 20 if urgency == 'medium' else 10,
                                     f"Auto-created from persona meeting on {meeting_date}")
                                )
                            
                            async with conn.execute("SELECT last_insert_rowid()") as cursor:
                                row = await cursor.fetchone()
                                gap_id = row[0] if row else None

                            if takeaway_id and gap_id:
                                await conn.execute(
                                    """
                                    UPDATE persona_meeting_takeaways 
                                    SET actioned = 1, actioned_as = 'knowledge_gap', actioned_entity_id = ?
                                    WHERE id = ?
                                    """,
                                    (gap_id, takeaway_id)
                                )
                                created_items['gaps'].append({'id': gap_id, 'question': insight[:50]})
                            
                            logger.info(f"Created knowledge gap #{gap_id} from persona meeting: {gap_question[:50]}...")

                    # NOTE: We no longer create TASKS from persona meetings.
                    # The personas can identify problems and gaps, but they shouldn't 
                    # assign work to real humans. That's for humans to decide.

                    # 3. Escalate high-urgency OBSERVATIONS (not assignments)
                    if urgency == 'high' and channel:
                        escalation_indicators = [
                            'urgent', 'critical', 'deadline', 'risk', 'blocked', 'failing',
                            'partner', 'client', 'immediately', 'asap', 'today', 'now',
                            'breaking', 'broken', 'emergency', 'lost', 'losing', 'overdue'
                        ]
                        should_escalate = any(ind in insight_lower for ind in escalation_indicators)
                        
                        if should_escalate:
                            escalation_embed = discord.Embed(
                                title="⚠️ Observation from Persona Meeting",
                                description=f"**{insight}**\n\n*Context: {why_now}*",
                                color=discord.Color.orange(),
                                timestamp=now
                            )
                            escalation_embed.set_footer(text=f"From: {topic}")
                            
                            try:
                                await channel.send(
                                    content="@here — High-urgency insight from personas:",
                                    embed=escalation_embed,
                                    allowed_mentions=discord.AllowedMentions(everyone=True)
                                )
                                
                                if takeaway_id:
                                    await conn.execute(
                                        """
                                        UPDATE persona_meeting_takeaways 
                                        SET actioned = 1, actioned_as = 'escalation'
                                        WHERE id = ? AND actioned = 0
                                        """,
                                        (takeaway_id,)
                                    )
                                
                                created_items['escalations'].append(insight[:50])
                                logger.info(f"Escalated high-urgency takeaway: {insight[:50]}...")
                            except Exception as e:
                                logger.warning(f"Failed to post escalation: {e}")

                await conn.commit()
                
            # 5. Post summary of created items (if any)
            if channel and (created_items['tasks'] or created_items['gaps']):
                summary_lines = ["### 🔄 Auto-Created Workflows"]
                
                if created_items['tasks']:
                    summary_lines.append(f"\n**📋 Tasks Created ({len(created_items['tasks'])})**")
                    for task in created_items['tasks'][:3]:
                        summary_lines.append(f"• `#{task['id']}` {task['title']}...")
                    if len(created_items['tasks']) > 3:
                        summary_lines.append(f"  *...and {len(created_items['tasks']) - 3} more*")
                
                if created_items['gaps']:
                    summary_lines.append(f"\n**❓ Knowledge Gaps Created ({len(created_items['gaps'])})**")
                    for gap in created_items['gaps'][:3]:
                        summary_lines.append(f"• `#{gap['id']}` {gap['question']}...")
                    if len(created_items['gaps']) > 3:
                        summary_lines.append(f"  *...and {len(created_items['gaps']) - 3} more*")
                
                summary_lines.append("\n*Use `/action list` and `/gap list` to review.*")
                
                try:
                    summary_embed = discord.Embed(
                        description="\n".join(summary_lines),
                        color=discord.Color.blue()
                    )
                    await channel.send(embed=summary_embed)
                except Exception as e:
                    logger.warning(f"Failed to post workflow summary: {e}")
                
        except Exception as e:
            logger.error(f"Failed to process persona meeting takeaways: {e}")

    # ========================================
