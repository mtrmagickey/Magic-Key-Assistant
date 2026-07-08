"""
seed_foundational_gaps — Bootstrap a new org with high-value interview questions.

After /setup completes, this module creates 10-15 knowledge gaps tailored to the
org profile and team mode.  Each gap is pre-curated (curation_status='keep') and
high-priority so the interview flow surfaces them immediately.

Goal: within 30 minutes of setup a user can do `/interview` and generate 10+
structured docs — turning natural conversation into a real corpus.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_SEED_GAP_FLAG = ".foundational_gaps_seeded"


def _flag_path(base_dir: Path | None = None) -> Path:
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return base_dir / _SEED_GAP_FLAG


def is_foundational_gaps_seeded(base_dir: Path | None = None) -> bool:
    return _flag_path(base_dir).exists()


# ── Base questions every org needs answered ──────────────────────────────────

_UNIVERSAL_GAPS: List[Dict[str, str]] = [
    {
        "topic": "Typical week",
        "question": (
            "Walk me through a typical week, start to finish. "
            "What happens Monday morning? What are the key milestones during the week? "
            "What does Friday wrap-up look like?"
        ),
    },
    {
        "topic": "Common failures",
        "question": (
            "What's the most common thing that goes wrong in your work? "
            "When it happens, what's the current workaround? "
            "How much time or money does it cost when it slips?"
        ),
    },
    {
        "topic": "Key people and vendors",
        "question": (
            "Who are the key people, vendors, or partners you depend on? "
            "For each, what do they do for you, and how do you reach them in an emergency?"
        ),
    },
    {
        "topic": "Recurring deadlines",
        "question": (
            "What recurring deadlines would hurt you if missed? "
            "Include compliance, financial, client-facing, and internal deadlines. "
            "For each, who is responsible and what's the consequence of missing it?"
        ),
    },
    {
        "topic": "New person onboarding",
        "question": (
            "If a new person started tomorrow, what would they need to know on day one? "
            "What are the first three things they should learn? "
            "What mistake does every new person make?"
        ),
    },
    {
        "topic": "Decision history",
        "question": (
            "What's the most important decision you've made in the last 6 months? "
            "What options did you consider, what did you choose, and why? "
            "Has it worked out as expected?"
        ),
    },
    {
        "topic": "Tribal knowledge",
        "question": (
            "What's something everyone on the team knows but hasn't been written down? "
            "The unwritten rule, the workaround, the 'just ask Sam' answer — "
            "what would be lost if someone left tomorrow?"
        ),
    },
    {
        "topic": "Tools and systems",
        "question": (
            "What tools, apps, and systems do you use every day? "
            "For each, what do you use it for, and who has the login/admin access? "
            "Are there any you're paying for but barely using?"
        ),
    },
    {
        "topic": "Service or product description",
        "question": (
            "Describe what you offer — your core services or products — "
            "in plain language, as if explaining to someone at a dinner party. "
            "What makes you different from alternatives?"
        ),
    },
    {
        "topic": "Current priorities",
        "question": (
            "What are your top 3 priorities right now? "
            "For each, what does 'done' look like, and what's blocking progress?"
        ),
    },
]

# ── Mode-specific questions ──────────────────────────────────────────────────

_SOLO_GAPS: List[Dict[str, str]] = [
    {
        "topic": "Context switching",
        "question": (
            "What are the biggest context switches in your week? "
            "When you pick something back up after a break, what information do you "
            "wish was written down so you didn't have to re-derive it?"
        ),
    },
    {
        "topic": "Client management",
        "question": (
            "How do you keep track of client commitments and follow-ups? "
            "Have you ever dropped the ball on a client deliverable? What happened?"
        ),
    },
]

_SMALL_GAPS: List[Dict[str, str]] = [
    {
        "topic": "Ownership and handoffs",
        "question": (
            "How do you decide who owns what? "
            "When you hand something off to your partner, what information do you include? "
            "Where do handoffs most often break down?"
        ),
    },
    {
        "topic": "Alignment rhythm",
        "question": (
            "How do you and your partner(s) stay aligned week to week? "
            "What's your current sync rhythm — daily standup, weekly check-in, ad-hoc? "
            "What falls through the cracks between syncs?"
        ),
    },
]

_TEAM_GAPS: List[Dict[str, str]] = [
    {
        "topic": "Role clarity",
        "question": (
            "Does everyone on the team know exactly what they're responsible for? "
            "Where is there overlap? Where are there gaps in coverage? "
            "What happens when someone is out sick or on vacation?"
        ),
    },
    {
        "topic": "Knowledge loss risk",
        "question": (
            "If your most experienced team member left tomorrow, what knowledge "
            "would walk out the door? What processes would break? "
            "What's currently only in one person's head?"
        ),
    },
    {
        "topic": "Team communication",
        "question": (
            "How does your team communicate day to day — Slack, Discord, email, meetings? "
            "What important information gets lost in chat? "
            "Is there anything that should be documented but currently lives only in DMs?"
        ),
    },
]

# ── Industry-specific question sets ─────────────────────────────────────────

_INDUSTRY_GAPS: Dict[str, List[Dict[str, str]]] = {
    "technology": [
        {
            "topic": "Technical infrastructure",
            "question": (
                "Describe your current technical stack. "
                "What systems are you running, where are they hosted, "
                "and who has admin access to each?"
            ),
        },
    ],
    "consulting": [
        {
            "topic": "Engagement lifecycle",
            "question": (
                "Walk me through a typical client engagement from first contact to delivery. "
                "What are the key stages, and where does quality or timeline most often slip?"
            ),
        },
    ],
    "nonprofit": [
        {
            "topic": "Institutional memory",
            "question": (
                "What happens to organizational knowledge when a board member rotates off "
                "or a volunteer leaves? How do you currently preserve institutional memory?"
            ),
        },
    ],
    "creative": [
        {
            "topic": "Project workflow",
            "question": (
                "Walk me through a creative project from brief to delivery. "
                "What are the approval gates? Where do scope changes typically happen?"
            ),
        },
    ],
    "trades": [
        {
            "topic": "Job site operations",
            "question": (
                "Describe a typical job from quote to close-out. "
                "What paperwork or checklists are involved? Where do delays happen?"
            ),
        },
    ],
    "agriculture": [
        {
            "topic": "Seasonal calendar",
            "question": (
                "Walk me through your year season by season. "
                "What are the critical windows, and what happens if you miss one?"
            ),
        },
    ],
    "retail": [
        {
            "topic": "Inventory and cycles",
            "question": (
                "How do you manage inventory — ordering, receiving, counting, and restocking? "
                "What are your busiest periods and how do you prepare?"
            ),
        },
    ],
    "museum": [
        {
            "topic": "Exhibit and visitor operations",
            "question": (
                "Describe a typical visitor experience from arrival to departure. "
                "What are the key operational checkpoints? "
                "What technology is involved in daily exhibit operation?"
            ),
        },
    ],
    "education": [
        {
            "topic": "Program delivery",
            "question": (
                "Walk me through how a course or program goes from planning to delivery. "
                "What's the review/approval process? What recurring admin tasks are involved?"
            ),
        },
    ],
}


def _match_industry(industry: str) -> str | None:
    """Fuzzy-match an industry string to our known templates."""
    if not industry:
        return None
    il = industry.lower()
    for key in _INDUSTRY_GAPS:
        if key in il:
            return key
    # Broader matches
    broad = {
        "agency": "creative",
        "design": "creative",
        "studio": "creative",
        "saas": "technology",
        "software": "technology",
        "tech": "technology",
        "farm": "agriculture",
        "maintenance": "trades",
        "construction": "trades",
        "plumbing": "trades",
        "electrical": "trades",
        "hvac": "trades",
        "vending": "retail",
        "shop": "retail",
        "store": "retail",
        "church": "nonprofit",
        "charity": "nonprofit",
        "foundation": "nonprofit",
        "school": "education",
        "university": "education",
        "lab": "education",
        "research": "education",
        "gallery": "museum",
        "exhibit": "museum",
        "leisure": "museum",
    }
    for keyword, mapped in broad.items():
        if keyword in il:
            return mapped
    return None


def build_gap_set(
    mode: str = "solo",
    industry: str = "",
    org_name: str = "",
) -> List[Dict[str, str]]:
    """Assemble the complete set of foundational gaps for a given org profile.

    Returns a list of dicts with 'topic', 'question', and 'context' keys.
    """
    gaps = list(_UNIVERSAL_GAPS)

    # Add mode-specific gaps
    mode_lower = (mode or "solo").lower()
    if mode_lower == "solo":
        gaps.extend(_SOLO_GAPS)
    elif mode_lower == "small":
        gaps.extend(_SMALL_GAPS)
    elif mode_lower == "team":
        gaps.extend(_TEAM_GAPS)

    # Add industry-specific gaps
    matched = _match_industry(industry)
    if matched and matched in _INDUSTRY_GAPS:
        gaps.extend(_INDUSTRY_GAPS[matched])

    # Annotate all with context
    org_label = org_name or "your organisation"
    for gap in gaps:
        gap["context"] = (
            f"Foundational question for {org_label} — seeded during setup. "
            f"Answering this builds the core knowledge base. [depth:0]"
        )

    return gaps


async def seed_foundational_gaps(
    db,
    *,
    mode: str = "solo",
    industry: str = "",
    org_name: str = "",
    base_dir: Path | None = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Insert foundational knowledge gaps into the database.

    Idempotent: checks for a flag file before running.
    Returns a summary dict.
    """
    flag = _flag_path(base_dir)
    if flag.exists() and not force:
        logger.info("Foundational gaps already seeded — skipping")
        return {"skipped": True, "count": 0}

    from cogs.KnowledgeGapTracker import insert_gap

    gaps = build_gap_set(mode=mode, industry=industry, org_name=org_name)
    created = 0

    async with db.acquire() as conn:
        for i, gap in enumerate(gaps):
            priority = max(8, 15 - i)  # highest priority for earliest questions
            try:
                await insert_gap(
                    conn,
                    topic=gap["topic"],
                    question=gap["question"],
                    context=gap["context"],
                    priority_score=priority,
                    curation_status="keep",
                    curation_reason="foundational:setup-seeded",
                )
                created += 1
            except Exception as e:
                logger.warning("Failed to seed gap '%s': %s", gap["topic"], e)

        await conn.commit()

    # Write flag
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
    logger.info("Seeded %d foundational knowledge gaps (mode=%s, industry=%s)", created, mode, industry)

    return {"skipped": False, "count": created, "mode": mode, "industry": industry}
