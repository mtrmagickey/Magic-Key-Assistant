"""
Workflow configuration loader.

Reads org_profile.yaml and workflows.yaml and provides typed access
to the product configuration surface.  Falls back to sensible defaults
when files are absent (first-run experience).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file, returning {} on any failure."""
    try:
        import yaml  # optional dependency; fall back gracefully
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        logger.warning("PyYAML not installed — using default config (pip install pyyaml)")
        return {}
    except FileNotFoundError:
        logger.info("Config file not found: %s — using defaults", path)
        return {}
    except Exception as e:
        logger.warning("Failed to load %s: %s — using defaults", path, e)
        return {}


# ── Org Profile ───────────────────────────────────────────────

@dataclass
class OrgMember:
    discord_user_id: int
    name: str
    username: str = ""
    roles: List[str] = field(default_factory=lambda: ["contributor"])
    emoji: str = ""


@dataclass
class OrgProfile:
    name: str = "My Company"
    tagline: str = ""
    industry: str = ""
    location: str = ""          # e.g. "Raleigh, North Carolina"
    region: str = ""            # e.g. "Southeast US"
    capabilities: List[str] = field(default_factory=list)
    knowledge_topics: List[str] = field(default_factory=list)
    mode: str = "solo"  # solo | small | team
    timezone: str = "America/New_York"
    members: List[OrgMember] = field(default_factory=list)
    channels: Dict[str, Any] = field(default_factory=dict)
    branding: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "OrgProfile":
        data = _load_yaml(path or CONFIG_DIR / "org_profile.yaml")
        org = data.get("org", {})
        members_raw = data.get("members") or []
        members = []
        for m in members_raw:
            if isinstance(m, dict) and "discord_user_id" in m:
                members.append(OrgMember(**{
                    k: v for k, v in m.items()
                    if k in OrgMember.__dataclass_fields__
                }))
        return cls(
            name=org.get("name", cls.name),
            tagline=org.get("tagline", ""),
            industry=org.get("industry", ""),
            location=org.get("location", ""),
            region=org.get("region", ""),
            capabilities=org.get("capabilities", []),
            knowledge_topics=org.get("knowledge_topics", []),
            mode=data.get("mode", "solo"),
            timezone=data.get("timezone", "America/New_York"),
            members=members,
            channels=data.get("channels", {}),
            branding=data.get("branding", {}),
        )

    @property
    def bot_name(self) -> str:
        return self.branding.get("bot_name", "Magic Key Assistant")

    def prompt_header(self) -> str:
        """One-line org identity for LLM prompts."""
        parts = [self.name]
        if self.tagline:
            parts[0] += f" — {self.tagline}"
        return parts[0]

    def prompt_description(self) -> str:
        """Multi-line org context block for injecting into LLM prompts."""
        lines: List[str] = [self.prompt_header()]
        if self.industry:
            lines.append(f"Industry: {self.industry}")
        if self.location:
            lines.append(f"Location: {self.location}")
        if self.region:
            lines.append(f"Region: {self.region}")
        if self.capabilities:
            lines.append("Capabilities:")
            for cap in self.capabilities:
                lines.append(f"  - {cap}")
        return "\n".join(lines)

    def capability_bullets(self) -> str:
        """Return capabilities as markdown bullet list, or empty string."""
        if not self.capabilities:
            return ""
        return "\n".join(f"- {c}" for c in self.capabilities)


def org_context_for_prompts() -> Dict[str, str]:
    """Load org profile and return a dict of prompt-ready strings.

    Keys returned:
        org_name, bot_name, tagline, industry, location, region,
        capabilities (bullet string), org_header (one-line),
        org_description (multi-line block)
    """
    org = OrgProfile.load()
    return {
        "org_name": org.name,
        "bot_name": org.bot_name,
        "tagline": org.tagline,
        "industry": org.industry,
        "location": org.location,
        "region": org.region,
        "capabilities": org.capability_bullets(),
        "org_header": org.prompt_header(),
        "org_description": org.prompt_description(),
    }


# ── Workflow Config ───────────────────────────────────────────

@dataclass
class WorkflowConfig:
    """Typed access to workflows.yaml."""
    personas_enabled: bool = False
    memory_enabled: bool = True
    work_enabled: bool = True
    pipeline_enabled: bool = True
    past_clients_enabled: bool = False
    health_enabled: bool = True
    persona_meetings_enabled: bool = False
    artifact_contract_enforce: bool = True
    artifact_contract_warn_only: bool = False
    noise_budget_max_posts: int = 20
    noise_budget_quiet_start: int = 20
    noise_budget_quiet_end: int = 7
    web_search_enabled: bool = False
    # Trust controls
    trust_quiet_hours_enabled: bool = True
    trust_quiet_hours_start: int = 22
    trust_quiet_hours_end: int = 7
    trust_require_change: bool = True
    trust_posts_per_job_per_day: int = 3
    # Sweep defaults
    obligation_sweep_upcoming_days: int = 14
    sop_drift_stale_days: int = 90
    # Corpus quality controls
    cq_auto_improvement_require_review: bool = True
    cq_auto_improvement_max_per_day: int = 3
    cq_auto_improvement_exclude_bad_answer: bool = True
    cq_followup_max_recursion_depth: int = 3
    cq_followup_max_open_questions: int = 5
    cq_memo_min_word_count: int = 50
    cq_memo_require_source_of_truth: bool = True
    cq_memo_require_key_points: int = 3
    # Web-augmented chat
    cq_web_chat_enabled: bool = True
    cq_web_chat_sparse_threshold: int = 80
    # Curator web research
    cq_curator_web_enabled: bool = True
    cq_curator_web_max_daily: int = 2
    cq_curator_web_max_weekly: int = 3
    cq_curator_web_enrich_synthesis: bool = True
    # Autonomous self-improvement
    cq_auto_approve_web_research: bool = True
    cq_immediate_gap_research: bool = True
    cq_auto_close_resolved_gaps: bool = True
    cq_conversation_mining_enabled: bool = True
    cq_conversation_mining_min_turns: int = 4
    cq_conversation_mining_max_per_run: int = 3
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "WorkflowConfig":
        data = _load_yaml(path or CONFIG_DIR / "workflows.yaml")
        personas_section = data.get("personas", {})
        mem = data.get("memory", {})
        work = data.get("work", {})
        pipe = data.get("pipeline", {})
        health = data.get("health", {})
        persona = data.get("persona_meetings", {})
        auto = data.get("automation", {})
        contract = auto.get("artifact_contract", {})
        noise = health.get("noise_budget", {})
        past_clients = pipe.get("past_clients", {}) if isinstance(pipe, dict) else {}
        web = pipe.get("web_search", {}) if isinstance(pipe, dict) else {}
        trust = data.get("trust_controls", {})
        sweeps = data.get("sweeps", {})
        cq = data.get("corpus_quality", {})
        cq_aim = cq.get("auto_improvement_memos", {})
        cq_fg = cq.get("follow_up_gaps", {})
        cq_mq = cq.get("memo_quality", {})
        cq_wc = cq.get("web_augmented_chat", {})
        cq_cw = cq.get("curator_web_research", {})
        cq_auto = cq.get("autonomous", {})
        cq_cm = cq_auto.get("conversation_mining", {})

        return cls(
            personas_enabled=personas_section.get("enabled", False),
            memory_enabled=mem.get("enabled", True),
            work_enabled=work.get("enabled", True),
            pipeline_enabled=pipe.get("enabled", True) if isinstance(pipe, dict) else True,
            past_clients_enabled=past_clients.get("enabled", False),
            health_enabled=health.get("enabled", True),
            persona_meetings_enabled=persona.get("enabled", False),
            artifact_contract_enforce=contract.get("enforce", True),
            artifact_contract_warn_only=contract.get("warn_only", False),
            noise_budget_max_posts=noise.get("max_posts_per_day", 20),
            noise_budget_quiet_start=noise.get("quiet_hours_start", 20),
            noise_budget_quiet_end=noise.get("quiet_hours_end", 7),
            web_search_enabled=web.get("enabled", False),
            trust_quiet_hours_enabled=trust.get("quiet_hours_enabled", True),
            trust_quiet_hours_start=trust.get("quiet_hours_start", 22),
            trust_quiet_hours_end=trust.get("quiet_hours_end", 7),
            trust_require_change=trust.get("require_change", True),
            trust_posts_per_job_per_day=trust.get("posts_per_job_per_day", 3),
            obligation_sweep_upcoming_days=sweeps.get("obligation_upcoming_days", 14),
            sop_drift_stale_days=sweeps.get("sop_stale_days", 90),
            cq_auto_improvement_require_review=cq_aim.get("require_review", True),
            cq_auto_improvement_max_per_day=cq_aim.get("max_per_day", 3),
            cq_auto_improvement_exclude_bad_answer=cq_aim.get("exclude_bad_answer", True),
            cq_followup_max_recursion_depth=cq_fg.get("max_recursion_depth", 3),
            cq_followup_max_open_questions=cq_fg.get("max_open_questions_per_memo", 5),
            cq_memo_min_word_count=cq_mq.get("min_word_count", 50),
            cq_memo_require_source_of_truth=cq_mq.get("require_source_of_truth", True),
            cq_memo_require_key_points=cq_mq.get("require_key_points", 3),
            cq_web_chat_enabled=cq_wc.get("enabled", True),
            cq_web_chat_sparse_threshold=cq_wc.get("sparse_threshold", 80),
            cq_curator_web_enabled=cq_cw.get("enabled", True),
            cq_curator_web_max_daily=cq_cw.get("max_daily_topics", 2),
            cq_curator_web_max_weekly=cq_cw.get("max_weekly_topics", 3),
            cq_curator_web_enrich_synthesis=cq_cw.get("enrich_synthesis", True),
            cq_auto_approve_web_research=cq_auto.get("auto_approve_web_research", True),
            cq_immediate_gap_research=cq_auto.get("immediate_gap_research", True),
            cq_auto_close_resolved_gaps=cq_auto.get("auto_close_resolved_gaps", True),
            cq_conversation_mining_enabled=cq_cm.get("enabled", True),
            cq_conversation_mining_min_turns=cq_cm.get("min_turns", 4),
            cq_conversation_mining_max_per_run=cq_cm.get("max_extracts_per_run", 3),
            raw=data,
        )
