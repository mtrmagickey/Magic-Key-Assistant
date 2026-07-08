"""
Scout Persona - Web research and opportunity discovery.

The Scout searches the web for:
- RFPs and procurement opportunities
- Industry news and trends
- Potential client leads
- Competitor intelligence

Key capabilities:
- Daily opportunity search
- Background web crawling
- Novel finding detection
- Automatic lead creation from high-value finds
"""

import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import discord
from discord.ext import tasks
from langchain_core.prompts import ChatPromptTemplate

from .base import EASTERN, load_persona_prompt

logger = logging.getLogger(__name__)


class ScoutMixin:
    """
    Scout persona mixin providing web research capabilities.
    
    This is designed to be mixed into the AutonomousOps cog.
    Requires:
        - self.bot
        - self.tavily_service
        - self.llm_service
        - self.post_to_bots_channel()
        - self._job_already_ran()
        - self._record_job_run()
        - self._rainmaker_create_lead() (for lead creation from findings)
        - self._rainmaker_assess_opportunities() (for opportunity assessment)
    """
    
    # ========================================
    # SCOUT: State Management
    # ========================================
    
    @property
    def _scout_state_path(self) -> Path:
        """Path to Scout's persistent state file."""
        return Path(__file__).parent.parent / "scout_state.json"
    
    def _load_scout_state(self) -> Dict:
        """Load Scout state from disk."""
        path = self._scout_state_path
        if not path.exists():
            return {
                "seen_urls": {},
                "seen_domains": {},
                "seed_queue": [],
                "last_cleanup": None,
            }
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("scout_state must be a dict")
            data.setdefault("seen_urls", {})
            data.setdefault("seen_domains", {})
            data.setdefault("seed_queue", [])
            data.setdefault("last_cleanup", None)
            return data
        except Exception as exc:
            logger.warning(f"Failed to read scout_state.json; starting fresh: {exc}")
            return {
                "seen_urls": {},
                "seen_domains": {},
                "seed_queue": [],
                "last_cleanup": None,
            }
    
    def _save_scout_state(self, state: Dict) -> None:
        """Save Scout state to disk atomically."""
        path = self._scout_state_path
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except Exception as exc:
            logger.warning(f"Failed to write scout_state.json: {exc}")
    
    def _scout_cleanup_state(self, state: Dict, today: str) -> Dict:
        """Keep state bounded so it doesn't grow forever."""
        try:
            seen_urls = state.get("seen_urls", {})
            if isinstance(seen_urls, dict) and len(seen_urls) > 3000:
                items = sorted(seen_urls.items(), key=lambda kv: kv[1], reverse=True)[:2500]
                state["seen_urls"] = dict(items)
        except Exception as e:
            logger.warning("_scout_cleanup_state: suppressed %s", e)
        
        try:
            q = state.get("seed_queue", [])
            if isinstance(q, list) and len(q) > 50:
                state["seed_queue"] = q[-50:]
        except Exception as e:
            logger.warning("_scout_cleanup_state: suppressed %s", e)
        
        state["last_cleanup"] = today
        return state
    
    # ========================================
    # SCOUT: URL/Domain Utilities
    # ========================================
    
    def _domain_from_url(self, url: str) -> Optional[str]:
        """Extract domain from URL."""
        try:
            host = urlparse(url).netloc
            if not host:
                return None
            host = host.lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return None
    
    def _select_novel_findings(self, findings: List[Dict], today: str) -> List[Dict]:
        """Pick the most novel/unusual findings (unseen URLs + rare domains)."""
        state = self._load_scout_state()
        seen_urls = state.get("seen_urls", {}) if isinstance(state.get("seen_urls", {}), dict) else {}
        seen_domains = state.get("seen_domains", {}) if isinstance(state.get("seen_domains", {}), dict) else {}
        
        scored: List[tuple] = []
        for f in findings:
            url = (f.get("url") or "").strip()
            if not url:
                continue
            domain = self._domain_from_url(url) or ""
            url_is_new = url not in seen_urls
            domain_count = int(seen_domains.get(domain, 0)) if domain else 0
            
            base = float(f.get("score") or 0)
            novelty = 0.0
            if url_is_new:
                novelty += 1.0
            if domain and domain_count <= 1:
                novelty += 0.6
            elif domain and domain_count <= 3:
                novelty += 0.3
            
            composite = (base * 0.7) + (novelty * 0.9)
            scored.append((composite, f))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:5]]
    
    # ========================================
    # SCOUT: Query Planning
    # ========================================
    
    def _default_scout_plan(self) -> List[Dict[str, str]]:
        """Fallback research paths when LLM planning is unavailable."""
        now = datetime.now(EASTERN)
        date_key = now.date().isoformat()
        rng = random.Random(date_key)

        # Pull geography from org_profile; fall back to generic US
        try:
            from core.config_loader import OrgProfile
            _prof = OrgProfile.load()
            _location = _prof.location  # e.g. "Austin, Texas"
            _region = _prof.region      # e.g. "Southwest US"
            _caps = _prof.capabilities
        except Exception:
            _location, _region, _caps = "", "", []

        if _location:
            # Derive state/city tokens from "City, State" string
            parts = [p.strip() for p in _location.split(",")]
            city = parts[0] if parts else ""
            state = parts[1] if len(parts) > 1 else ""
            regions = [state] * 3 + [city] + ([_region] * 2 if _region else []) + ["United States"]
        elif _region:
            regions = [_region] * 4 + ["United States"]
        else:
            regions = ["United States"]
        verticals = [
            "museum", "science center", "aquarium", "planetarium",
            "visitor center", "airport", "university", "public art",
            "festival", "brand activation",
        ]
        angles = [
            "interactive installation", "immersive experience",
            "projection mapping", "wayfinding kiosk", "digital twin",
            "photogrammetry", "LiDAR", "AR", "AI", "spatial computing",
        ]
        procurement_terms = [
            "RFP", "RFQ", "tender", "grant",
            "call for proposals", "open call", "commission",
        ]
        year_tokens = [str(now.year), str(now.year + 1)]
        
        region = rng.choice(regions)
        year = rng.choice(year_tokens)
        angle = rng.choice(angles)
        
        templates: List[Dict[str, str]] = [
            {
                "tag": "Civic Procurement",
                "query": f"{region} {rng.choice(procurement_terms)} {angle} wayfinding {year}",
                "rationale": "Public-sector procurement is recurring and often under-scanned.",
                "perspective": "Civic + fabrication + durable installs",
                "topic": "general",
                "time_range": "month",
                "country": "us",
            },
            {
                "tag": "Culture Grants",
                "query": f"{region} arts council {rng.choice(procurement_terms)} technology interactive {year}",
                "rationale": "Grant cycles surface collaborations and budgeted pilots.",
                "perspective": "Funding-led partnerships",
                "topic": "news",
                "time_range": "month",
                "country": "us",
            },
            {
                "tag": "Adjacent Verticals",
                "query": f"{region} {rng.choice(verticals)} {rng.choice(procurement_terms)} {angle} {year}",
                "rationale": "Adjacent venues reuse the same integrators and budgets as museums.",
                "perspective": "Follow budget owners across venues",
                "topic": "general",
                "time_range": "week",
                "country": "us",
            },
            {
                "tag": "Research Partnerships",
                "query": f"{region} university lab partnership {angle} grant {year}",
                "rationale": "Universities and labs fund prototypes that become exhibit-ready.",
                "perspective": "Prototype-to-deployment pipeline",
                "topic": "general",
                "time_range": "year",
                "country": "us",
            },
            {
                "tag": "Festivals & Calls",
                "query": f"{region} light festival {rng.choice(procurement_terms)} projection mapping {year}",
                "rationale": "Festival calls are fast-moving and can seed repeat clients.",
                "perspective": "Short-cycle commissions",
                "topic": "news",
                "time_range": "week",
                "country": "us",
            },
            {
                "tag": "Experiential Agencies",
                "query": f"experiential agency {rng.choice(procurement_terms)} interactive installation fabricator {year}",
                "rationale": "Agencies outsource build + integration; good inbound channel.",
                "perspective": "Agency subcontracting",
                "topic": "general",
                "time_range": "month",
                "country": "us",
            },
        ]
        
        rng.shuffle(templates)
        plan: List[Dict[str, str]] = []
        for t in templates[:3]:
            plan.append({
                "tag": t["tag"],
                "query": t["query"][:100],
                "rationale": t["rationale"],
                "perspective": t["perspective"],
                "domains": [],
                "depth": "advanced",
                "max_results": 6,
                "topic": t.get("topic") or "general",
                "time_range": t.get("time_range") or "month",
                "country": t.get("country"),
                "auto_parameters": True,
            })
        return plan
    
    def _scout_pop_seed_queries(self, *, max_items: int = 2) -> List[Dict[str, Any]]:
        """Pop up to max_items queued Scout follow-up missions."""
        try:
            state = self._load_scout_state()
            queue = state.get("seed_queue", [])
            if not isinstance(queue, list) or not queue:
                return []
            
            picked: List[Dict[str, Any]] = []
            remaining: List[Any] = []
            for item in queue:
                if isinstance(item, dict) and len(picked) < max_items:
                    picked.append(item)
                else:
                    remaining.append(item)
            
            state["seed_queue"] = remaining
            self._save_scout_state(state)
            return picked
        except Exception:
            return []
    
    def _scout_default_crawl_queries(self) -> List[Dict[str, Any]]:
        """Fallback crawl queries with random sampling for variety."""
        # Derive location tokens from org_profile
        try:
            from core.config_loader import OrgProfile
            _prof = OrgProfile.load()
            loc = _prof.location or ""
            rgn = _prof.region or ""
        except Exception:
            loc, rgn = "", ""

        loc_tag = loc.split(",")[0].strip() if loc else "United States"
        state_tag = loc.split(",")[1].strip() if "," in loc else ""
        region_tag = rgn or "United States"

        pool = [
            {"tag": "Radar", "query": f"{state_tag or region_tag} museum RFP interactive exhibit",
             "depth": "advanced", "topic": "news", "time_range": "month", "country": "us"},
            {"tag": "Radar", "query": f"{state_tag or region_tag} state procurement interactive technology kiosk",
             "depth": "advanced", "topic": "general", "time_range": "month", "country": "us"},
            {"tag": "Radar", "query": f"{loc_tag} museum exhibit renovation",
             "depth": "advanced", "topic": "news", "time_range": "month", "country": "us"},
            {"tag": "Radar", "query": f"{state_tag or region_tag} science center expansion project",
             "depth": "advanced", "topic": "news", "time_range": "month", "country": "us"},
            {"tag": "Radar", "query": f"{region_tag} zoo aquarium RFP visitor experience",
             "depth": "advanced", "topic": "news", "time_range": "month", "country": "us"},
            {"tag": "Radar", "query": f"{state_tag or region_tag} arts council grant technology interactive",
             "depth": "advanced", "topic": "news", "time_range": "month", "country": "us"},
            {"tag": "Crawl", "query": f"museum exhibit design RFP {datetime.now().year + 1} United States",
             "depth": "advanced", "topic": "news", "time_range": "week", "country": "us"},
            {"tag": "Crawl", "query": "science center interactive exhibit vendor call",
             "depth": "advanced", "topic": "news", "time_range": "week", "country": "us"},
        ]
        
        selected = random.sample(pool, min(3, len(pool)))
        for s in selected:
            s.setdefault("max_results", 6)
            s.setdefault("auto_parameters", True)
        return selected
    
    # ========================================
    # SCOUT: LLM-Powered Summary Generation
    # ========================================
    
    async def _generate_scout_summary(self, findings: List[Dict], plan_context: List[Dict]) -> str:
        """Generate summary of web search findings with research-path context."""
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        capabilities_block = org["capabilities"] or "- (no capabilities listed in org_profile.yaml)"
        persona_guidance = load_persona_prompt("scout", org["org_name"]) or "None."

        prompt = ChatPromptTemplate.from_template("""
You are the Scout department of {org_name}'s operations bot.
Summarize today's web search findings for opportunities relevant to {org_name}.

{org_name} specializes in:
{capabilities}

Persona guidance (optional):
{persona_guidance}

Research approaches investigated today:
{plan}

Findings:
{findings}

Summarize in 2-3 sentences:
- Most promising opportunity
- Why it's relevant to {org_name}
- Recommended action

Be direct and actionable.

Summary:""")
        
        findings_text = "\n\n".join([
            f"**{f['title']}**\n{f['snippet']}\nURL: {f['url']}\nRelevance: {f['score']}"
            for f in findings[:5]
        ])
        plan_text = "\n".join([
            f"- {mission.get('tag', 'Path')}: {mission.get('perspective', '')} — {mission.get('rationale', '')}"
            for mission in plan_context[:3]
        ])
        
        if not self.llm_service:
            raise RuntimeError("LLM service is not configured")
        return await self.llm_service.generate(prompt, {
            "findings": findings_text,
            "plan": plan_text,
            "org_name": org["org_name"],
            "capabilities": capabilities_block,
            "persona_guidance": persona_guidance,
        })
    
    async def _enqueue_followup_queries_from_findings(self, novel_findings: List[Dict], today: str) -> None:
        """Use today's novel/unusual results to propose future search queries."""
        if not self.llm_service or not novel_findings:
            return
        
        examples = []
        for f in novel_findings[:4]:
            title = (f.get("title") or "")[:120]
            url = (f.get("url") or "")[:200]
            snippet = (f.get("snippet") or "")[:180]
            examples.append(f"- {title}\n  {url}\n  {snippet}")
        examples_block = "\n".join(examples)

        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        persona_guidance = load_persona_prompt("scout", org["org_name"]) or "None."
        
        prompt = ChatPromptTemplate.from_template("""
You are the Scout department improving tomorrow's web search plan.

Today's MOST UNUSUAL / NEW findings (not previously seen):
{examples}

Task:
- Propose exactly 3 search queries for tomorrow that chase these *unusual veins* without repeating today's exact phrasing.
- Each query must be <= 100 characters.
- Queries should be broad enough to surface fresh sources.
- Make the 3 queries non-overlapping (different angles / verticals / regions).

Persona guidance (optional):
{persona_guidance}

Return STRICT JSON array of 3 objects with keys:
- tag, query, rationale, perspective
- depth: "basic" or "advanced"
- max_results: integer 5-8
- topic: "general" or "news"
- time_range: "week" or "month" or "year"
- country: 2-letter code or null
- auto_parameters: true
""")
        
        try:
            raw = await self.llm_service.generate(prompt, {"examples": examples_block, "persona_guidance": persona_guidance})
            plan = json.loads(raw)
            if not isinstance(plan, list):
                return
            
            normalized: List[Dict] = []
            for item in plan[:3]:
                if not isinstance(item, dict):
                    continue
                q = item.get("query")
                if not isinstance(q, str) or not q.strip():
                    continue
                normalized.append({
                    "tag": item.get("tag", "Novelty Follow-up"),
                    "query": q.strip()[:100],
                    "rationale": item.get("rationale", ""),
                    "perspective": item.get("perspective", ""),
                    "domains": [],
                    "depth": item.get("depth", "advanced"),
                    "max_results": int(item.get("max_results", 6) or 6),
                    "topic": item.get("topic") if item.get("topic") in {"general", "news"} else "general",
                    "time_range": item.get("time_range") if item.get("time_range") in {"week", "month", "year"} else "month",
                    "country": item.get("country") if isinstance(item.get("country"), str) and len(item.get("country").strip()) == 2 else None,
                    "auto_parameters": True,
                    "generated_on": today,
                    "source": "novel_findings",
                })
            
            if not normalized:
                return
            
            state = self._load_scout_state()
            q = state.get("seed_queue")
            if not isinstance(q, list):
                q = []
            q.extend(normalized)
            state["seed_queue"] = q
            state = self._scout_cleanup_state(state, today)
            self._save_scout_state(state)
        except Exception as exc:
            logger.warning(f"Failed to generate follow-up Scout queries: {exc}")
    
    async def _generate_scout_plan(self, day_signature: str) -> List[Dict[str, str]]:
        """Ask the LLM to design varied research paths for Scout."""
        today = day_signature.split("-W", 1)[0]
        from LeisureLLM.core.config_loader import org_context_for_prompts
        org = org_context_for_prompts()
        persona_guidance = load_persona_prompt("scout", org["org_name"]) or "None."
        
        # Check for queued seeds from prior novel findings
        try:
            state = self._load_scout_state()
            queue = state.get("seed_queue", [])
            if isinstance(queue, list) and queue:
                queued = []
                remaining = []
                for item in queue:
                    if isinstance(item, dict) and len(queued) < 2:
                        queued.append(item)
                    else:
                        remaining.append(item)
                state["seed_queue"] = remaining
                self._save_scout_state(state)
                
                base = queued
                if len(base) >= 3:
                    return base[:3]
                
                if not self.llm_service:
                    base.extend(self._default_scout_plan()[: (3 - len(base))])
                    return base
                
                # Let LLM create remaining missions
                seed_hex = hex(random.getrandbits(32))
                prompt = ChatPromptTemplate.from_template("""
You are the Scout department planning today's opportunity hunt.

Already queued from prior novel findings:
{queued}

Design exactly {needed} additional research paths that DO NOT overlap with the queued ones.
Make them broad and non-overlapping.

Return STRICT JSON array (no prose) with objects:
- tag, query (<=100 chars), rationale, perspective
- domains (array), depth (basic|advanced), max_results (5-8)
- topic (general|news), time_range (week|month|year)
- country (2-letter or null), auto_parameters (true)

Persona guidance (optional):
{persona_guidance}

Day signature: {day_signature}
Randomizer: {seed_hex}
JSON:
""")
                plan_text = await self.llm_service.generate(
                    prompt,
                    {
                        "queued": json.dumps(queued, ensure_ascii=False)[:1500],
                        "needed": 3 - len(base),
                        "day_signature": day_signature,
                        "seed_hex": seed_hex,
                        "persona_guidance": persona_guidance,
                    },
                )
                plan = json.loads(plan_text)
                if isinstance(plan, list):
                    normalized = []
                    for item in plan:
                        if not isinstance(item, dict):
                            continue
                        q = item.get("query")
                        if not isinstance(q, str) or not q.strip():
                            continue
                        normalized.append({
                            "tag": item.get("tag", "Path"),
                            "query": q.strip()[:100],
                            "rationale": item.get("rationale", ""),
                            "perspective": item.get("perspective", ""),
                            "domains": item.get("domains") if isinstance(item.get("domains"), list) else [],
                            "depth": item.get("depth", "advanced"),
                            "max_results": int(item.get("max_results", 6) or 6),
                            "topic": item.get("topic") if item.get("topic") in {"general", "news"} else "general",
                            "time_range": item.get("time_range") if item.get("time_range") in {"week", "month", "year"} else "month",
                            "country": item.get("country") if isinstance(item.get("country"), str) and len(item.get("country").strip()) == 2 else None,
                            "auto_parameters": True,
                        })
                    base.extend(normalized[: (3 - len(base))])
                if len(base) < 3:
                    base.extend(self._default_scout_plan()[: (3 - len(base))])
                return base[:3]
        except Exception as exc:
            logger.warning(f"Scout queued-seed planning failed; continuing normally: {exc}")
        
        if not self.llm_service:
            return self._default_scout_plan()
        
        seed_hex = hex(random.getrandbits(32))
        prompt = ChatPromptTemplate.from_template("""
You are the Scout department planning today's opportunity hunt.
Design exactly 3 distinct research paths that avoid repeating yesterday's obvious museum-RFP queries.
Make the paths BROAD and non-overlapping (different regions, different verticals, different sourcing).

Return a STRICT JSON array (no prose, markdown, or comments) of objects with keys:
- tag: short codename
- query: Tavily-ready search string (<= 100 chars)
- rationale: 1 sentence on why this matters now
- perspective: the mental model guiding this search
- domains: array of preferred domains (can be empty)
- depth: "basic" or "advanced"
- max_results: integer 5-8
- topic: "general" or "news"
- time_range: "week" or "month" or "year"
- country: 2-letter country code or null
- auto_parameters: true

Persona guidance (optional):
{persona_guidance}

Day signature: {day_signature}
Randomizer: {seed_hex}
Focus on cultural institutions, NC/Southeast pipelines, AI/spatial edge cases, and unconventional client adjacencies.
JSON:
""")
        
        try:
            plan_text = await self.llm_service.generate(
                prompt,
                {
                    "day_signature": day_signature,
                    "seed_hex": seed_hex,
                    "persona_guidance": persona_guidance,
                },
            )
            plan = json.loads(plan_text)
            if not isinstance(plan, list) or len(plan) != 3:
                raise ValueError("Plan must be a list of 3 items")
            
            normalized = []
            for item in plan:
                query = item.get("query")
                if not isinstance(query, str) or not query.strip():
                    continue
                normalized.append({
                    "tag": item.get("tag", "Path"),
                    "query": query.strip(),
                    "rationale": item.get("rationale", ""),
                    "perspective": item.get("perspective", ""),
                    "domains": item.get("domains") if isinstance(item.get("domains"), list) else [],
                    "depth": item.get("depth", "advanced"),
                    "max_results": int(item.get("max_results", 5) or 5),
                    "topic": item.get("topic") if item.get("topic") in {"general", "news", None} else None,
                    "time_range": item.get("time_range") if item.get("time_range") in {"week", "month", "year", None} else None,
                    "country": item.get("country") if isinstance(item.get("country"), str) and len(item.get("country").strip()) == 2 else None,
                    "auto_parameters": bool(item.get("auto_parameters", True)),
                })
            return normalized if normalized else self._default_scout_plan()
        except Exception as exc:
            logger.warning(f"Scout plan generation failed, using defaults: {exc}")
            return self._default_scout_plan()
