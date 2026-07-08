"""
Conversational Onboarding — fluid "get-to-know-you" for your local ops assistant.

Instead of a rigid phase-based wizard, this module drives a **free-form
conversation** that keeps going until the *user* decides they've shared
enough.  Every turn the LLM:

  1. Extracts any **new** structured data (org profile fields, projects,
     action items, concerns) from the latest message.
  2. Applies the changes immediately (org_profile.yaml, workflows.yaml,
     seed artifacts, knowledge gaps).
  3. Confirms what it captured and asks a natural follow-up question.

The conversation is also designed to **introduce the value of local AI** —
the welcome message explains that everything runs on the user's own device,
nothing is sent to the cloud, and the model gets smarter with every
interaction.

Usage
-----
    from core.conversational_onboarding import OnboardingConversation

    onboarding = OnboardingConversation(db=db)
    response = await onboarding.process_message(
        user_input="I run a small design agency...",
        phase="conversation",
    )
    # response.reply          -> conversational response text
    # response.phase          -> "conversation" or "complete"
    # response.extracted      -> structured data extracted this turn
    # response.config_applied -> what was auto-configured this turn
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"


# ═════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═════════════════════════════════════════════════════════════════════════════

class OnboardingPhase(str, Enum):
    WELCOME = "welcome"           # Before conversation starts
    INTRO = "intro"               # (legacy) User describes themselves and their work
    PROJECTS = "projects"         # (legacy) User describes current projects / pipeline
    BRAIN_DUMP = "brain_dump"     # (legacy) User dumps concerns, deadlines, worries
    CONVERSATION = "conversation" # Fluid conversation — the primary mode
    COMPLETE = "complete"         # All done


# ── Session state for optimistic locking ──────────────────────────────────

_ONBOARDING_SESSION_PATH = CONFIG_DIR / "onboarding_session.json"


def _read_session() -> Dict[str, Any]:
    """Read the current onboarding session state."""
    if _ONBOARDING_SESSION_PATH.exists():
        try:
            with open(_ONBOARDING_SESSION_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("_read_session: suppressed %s", e)
    return {"session_id": "", "version": 0, "phase": "welcome"}


def _write_session(session: Dict[str, Any]) -> None:
    """Write session state atomically."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_ONBOARDING_SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(session, f)


def _init_session(session_id: str) -> Dict[str, Any]:
    """Initialise a new session."""
    import secrets
    sid = session_id or secrets.token_hex(8)
    session = {"session_id": sid, "version": 1, "phase": "welcome"}
    _write_session(session)
    return session


def _bump_session(phase: str) -> Dict[str, Any]:
    """Increment version for an applied change."""
    session = _read_session()
    session["version"] = session.get("version", 0) + 1
    session["phase"] = phase
    _write_session(session)
    return session


@dataclass
class ExtractedProfile:
    """Structured data extracted from the intro conversation."""
    org_name: str = ""
    industry: str = ""
    tagline: str = ""
    team_size: str = ""           # "solo", "small", "team"
    team_description: str = ""    # Raw description of team
    location: str = ""
    key_services: List[str] = field(default_factory=list)
    confidence: float = 0.0       # 0-1, how confident the extraction is

    def to_org_profile(self) -> Dict[str, Any]:
        """Convert to org_profile.yaml structure."""
        mode = "solo"
        if self.team_size in ("small", "2-3", "couple", "pair", "partner"):
            mode = "small"
        elif self.team_size in ("team", "4-6", "several", "group"):
            mode = "team"

        return {
            "org_name": self.org_name,
            "mode": mode,
            "timezone": "America/New_York",  # Default, can be refined
            "org": {
                "name": self.org_name,
                "industry": self.industry,
                "tagline": self.tagline,
                "location": self.location,
                "capabilities": self.key_services,
            },
            "branding": {
                "bot_name": "Magic Key Assistant",
            },
        }


@dataclass
class ExtractedProject:
    """A project or pipeline item extracted from conversation."""
    name: str
    description: str = ""
    status: str = "active"        # active | pipeline | idea
    entity_type: str = "rail"     # rail | lead | action
    urgency: str = "normal"       # high | normal | low
    deadline: str = ""            # Free-text deadline if mentioned


@dataclass
class ExtractedConcern:
    """A concern, deadline, or to-do extracted from brain dump."""
    content: str
    entity_type: str = "action"   # action | decision | obligation | knowledge_gap
    urgency: str = "normal"
    owner: str = ""
    deadline: str = ""


@dataclass
class OnboardingResponse:
    """Response from a single onboarding conversation turn."""
    reply: str                                    # Conversational response to show user
    phase: OnboardingPhase                        # Current phase after this turn
    next_phase: Optional[OnboardingPhase] = None  # Next phase (if transitioning)
    extracted: Dict[str, Any] = field(default_factory=dict)
    config_applied: List[str] = field(default_factory=list)
    artifacts_created: List[Dict[str, str]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)  # UI hints
    proposed: Dict[str, Any] = field(default_factory=dict)
    preview: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reply": self.reply,
            "phase": self.phase.value,
            "next_phase": self.next_phase.value if self.next_phase else None,
            "extracted": self.extracted,
            "config_applied": self.config_applied,
            "artifacts_created": self.artifacts_created,
            "suggestions": self.suggestions,
            "proposed": self.proposed,
            "preview": self.preview,
        }


# ═════════════════════════════════════════════════════════════════════════════
# EXTRACTION PROMPTS
# ═════════════════════════════════════════════════════════════════════════════

# Legacy prompts kept for reference — the new unified prompt is below.

UNIFIED_EXTRACTION_PROMPT = """You are an operations assistant helping a new user set up their workspace.
The user is having a casual conversation with you. Extract ANY structured information from their latest message.

Only include fields where the user clearly stated or strongly implied the information.
Leave fields empty/null if the user didn't mention them. Do NOT guess or infer values that weren't stated.

Return ONLY valid JSON with this structure:
{{
  "org_profile": {{
    "org_name": "organisation or business name (or person's name if solo) — or null",
    "industry": "industry category — or null",
    "tagline": "one-line description of what they do — or null",
    "team_size": "solo | small | team — or null",
    "team_description": "how they described their team — or null",
    "location": "city, region, or country — or null",
    "key_services": ["service1", "service2"]
  }},
  "projects": [
    {{
      "name": "short project name",
      "description": "one-line description",
      "status": "active | pipeline | idea",
      "entity_type": "rail | lead | action",
      "urgency": "high | normal | low",
      "deadline": "free-text deadline if mentioned"
    }}
  ],
  "concerns": [
    {{
      "content": "the concern, task, or worry",
      "entity_type": "action | decision | obligation | knowledge_gap",
      "urgency": "high | normal | low",
      "owner": "person responsible if mentioned",
      "deadline": "deadline if mentioned"
    }}
  ],
  "has_new_data": true
}}

Set "has_new_data" to false if the message is just a greeting, acknowledgement, or contains no extractable work information.

Previously extracted context (do NOT re-extract these — only extract NEW information):
{prior_context}

User's latest message:
{user_input}"""

CONVERSATIONAL_REPLY_PROMPT = """You are {bot_name}, a local operations assistant running entirely on this user's device.
You are in a setup conversation — you've just processed the user's message and extracted some information.

Your job:
1. Briefly confirm what you understood from their message (1-2 sentences max)
2. Show what you've configured so far (if anything new was set up)
3. Ask ONE natural follow-up question to learn more about their work
4. Keep it warm but concise — 3-5 sentences total

Style rules:
- Be conversational and encouraging, like a capable colleague getting up to speed
- Reference specifics from what they told you (names, details) to show you were listening
- Don't use emoji
- Don't be sycophantic or overly enthusiastic
- Frame things in terms of what YOU can now do for THEM
- Occasionally remind them this is all running locally on their machine when relevant

What was extracted from this turn: {extracted}
What was configured: {config_actions}
Conversation history summary: {conversation_summary}
User's latest message: {user_input}

If the user seems to be wrapping up or says something like "that's it" or "I think that's enough", acknowledge it warmly and tell them they can always come back to teach you more later. End with something encouraging about the value of having a local AI assistant that knows their work.

Respond ONLY with your conversational reply — no JSON, no system notes."""


# Legacy prompt constants kept for backward compatibility with any external imports.
INTRO_EXTRACTION_PROMPT = UNIFIED_EXTRACTION_PROMPT
PROJECT_EXTRACTION_PROMPT = UNIFIED_EXTRACTION_PROMPT
BRAIN_DUMP_EXTRACTION_PROMPT = UNIFIED_EXTRACTION_PROMPT


# ═════════════════════════════════════════════════════════════════════════════
# CONVERSATION ENGINE
# ═════════════════════════════════════════════════════════════════════════════

class OnboardingConversation:
    """Manages a fluid conversational onboarding flow.

    Each call to ``process_message()`` is one turn in the conversation.
    There are no rigid phases — the bot keeps talking until the user
    signals they're done.  Extraction and config writes happen
    immediately on every turn.
    """

    def __init__(self, db=None, model_router=None):
        self.db = db
        self.model_router = model_router
        self._accumulated_profile: Dict[str, Any] = {}
        self._accumulated_projects: List[Dict] = []
        self._accumulated_concerns: List[Dict] = []

    # ── Public API ────────────────────────────────────────────────────────

    def get_welcome(self) -> OnboardingResponse:
        """Generate the initial welcome message — frames local AI value."""
        return OnboardingResponse(
            reply=(
                "Hi — I'm your new operations assistant, and I'm running "
                "**entirely on this machine**. Nothing you tell me leaves your "
                "device — no cloud, no third-party servers, just your hardware "
                "doing the thinking.\n\n"
                "The more you tell me about your work, the more useful I get. "
                "So let's just talk — **tell me about yourself and what you do.** "
                "Who are you, what's the business, how big is the team? "
                "There's no form to fill out — just talk naturally and I'll "
                "configure everything from what you say.\n\n"
                "When you feel like I know enough to get started, just say so "
                "and we'll wrap up."
            ),
            phase=OnboardingPhase.WELCOME,
            next_phase=OnboardingPhase.CONVERSATION,
            suggestions=[
                "I run a 3-person design studio focused on museum exhibits",
                "I'm a solo consultant helping nonprofits with grant writing",
                "We're a small construction crew — mostly residential renos",
            ],
        )

    async def process_message(
        self,
        user_input: str,
        phase: str | OnboardingPhase,
        context: Optional[Dict[str, Any]] = None,
        apply_changes: bool = True,
        extracted_override: Optional[Dict[str, Any]] = None,
        selected_artifacts: Optional[List[int]] = None,
        apply_mask: Optional[Dict[str, bool]] = None,
        session_id: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> OnboardingResponse:
        """Process one turn of the fluid onboarding conversation.

        Every message goes through the same pipeline:
        1. Extract new structured data (unified prompt)
        2. Apply config + create artifacts immediately
        3. Generate a conversational reply confirming what was captured
        4. Return — no phase transitions to manage
        """
        # ── Optimistic locking ────────────────────────────────────────────
        if apply_changes and expected_version is not None:
            current = _read_session()
            if current.get("session_id") and session_id != current.get("session_id"):
                return OnboardingResponse(
                    reply="Another session is active. Refresh the page to sync.",
                    phase=OnboardingPhase(phase) if isinstance(phase, str) else phase,
                    extracted={"error": "session_conflict"},
                )
            if current.get("version", 0) != expected_version:
                return OnboardingResponse(
                    reply=(
                        "These changes are stale — someone (or another tab) already "
                        "applied an update. Refresh the page to see the latest state."
                    ),
                    phase=OnboardingPhase(phase) if isinstance(phase, str) else phase,
                    extracted={"error": "stale_version", "current_version": current.get("version", 0)},
                )

        if isinstance(phase, str):
            try:
                phase = OnboardingPhase(phase)
            except ValueError:
                phase = OnboardingPhase.CONVERSATION

        context = context or {}

        # ── Legacy phase support (redirect into unified handler) ──────────
        # Old clients may still send "intro" / "projects" / "brain_dump".
        # Route them all through the unified conversation handler.
        if phase in (
            OnboardingPhase.INTRO,
            OnboardingPhase.PROJECTS,
            OnboardingPhase.BRAIN_DUMP,
            OnboardingPhase.CONVERSATION,
            OnboardingPhase.WELCOME,
        ):
            response = await self._handle_conversation_turn(
                user_input,
                context,
                extracted_override=extracted_override,
            )
        else:
            response = self.get_welcome()

        try:
            self._log_transcript(
                phase="conversation",
                user_input=user_input,
                response=response,
                apply_changes=True,
            )
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # ── Bump session version ──────────────────────────────────────────
        try:
            new_session = _bump_session("conversation")
            response.extracted["_session_version"] = new_session["version"]
            response.extracted["_session_id"] = new_session["session_id"]
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        return response

    # ── Unified conversation handler ──────────────────────────────────────

    async def _handle_conversation_turn(
        self,
        user_input: str,
        context: Dict[str, Any],
        *,
        extracted_override: Optional[Dict[str, Any]] = None,
    ) -> OnboardingResponse:
        """Handle a single turn of the fluid conversation.

        Extracts everything at once, applies immediately, returns a
        conversational reply with confirmation of what was captured.
        """
        from services.alpha_logging import log_alpha_event
        start_ts = time.time()

        # Build a summary of what we already know
        prior_context = self._summarize_prior_context(context)

        # ── Extract ───────────────────────────────────────────────────────
        extracted = extracted_override or await self._extract_with_llm(
            UNIFIED_EXTRACTION_PROMPT.format(
                prior_context=prior_context or "Nothing yet — this is the first message.",
                user_input=user_input,
            ),
        )

        # ── Schema gate (neuro-symbolic) ──────────────────────────────────
        try:
            from core.symbolic_rules import validate_llm_output
            ok, schema_errors = validate_llm_output(extracted, "onboarding_unified")
            if schema_errors:
                logger.info("Unified extraction schema issues: %s",
                            [e.message for e in schema_errors[:3]])
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        has_new_data = extracted.get("has_new_data", True)
        config_actions: List[str] = []
        artifacts_created: List[Dict[str, str]] = []

        if has_new_data:
            # ── Apply org profile ─────────────────────────────────────────
            org_data = extracted.get("org_profile", {})
            if org_data and any(v for v in org_data.values() if v and v != []):
                # Merge with accumulated profile
                for k, v in org_data.items():
                    if v and v != [] and v != "null":
                        self._accumulated_profile[k] = v

                profile = self._parse_profile(self._accumulated_profile)
                profile_dict = profile.to_org_profile()

                try:
                    self._write_org_profile(profile_dict)
                    config_actions.append(
                        "Updated workspace profile"
                        + (f" for **{profile.org_name}**" if profile.org_name else "")
                    )
                except Exception as e:
                    logger.warning("Failed to write org profile: %s", e)

                # Auto-configure workflows based on what we know so far
                try:
                    wf_changes, _ = self._auto_configure_workflows(profile)
                    config_actions.extend(wf_changes)
                except Exception as e:
                    logger.warning("Failed to auto-configure workflows: %s", e)

            # ── Create project artifacts ──────────────────────────────────
            projects = extracted.get("projects", [])
            for proj in projects:
                try:
                    artifact = await self._create_project_artifact(proj)
                    if artifact:
                        artifacts_created.append(artifact)
                except Exception as e:
                    logger.warning("Failed to create project artifact: %s", e)

            # Enable pipeline module if leads were detected
            has_leads = any(p.get("entity_type") == "lead" for p in projects)
            if has_leads:
                try:
                    self._enable_workflow_module("pipeline")
                    config_actions.append("Enabled pipeline tracking (detected prospects)")
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

            if projects:
                names = [p.get("name", "item") for p in projects[:3]]
                config_actions.append(
                    f"Now tracking **{', '.join(names)}**"
                    + (f" and {len(projects) - 3} more" if len(projects) > 3 else "")
                )

            # ── Create concern/action artifacts ───────────────────────────
            concerns = extracted.get("concerns", [])
            for concern in concerns:
                try:
                    artifact = await self._create_concern_artifact(concern)
                    if artifact:
                        artifacts_created.append(artifact)
                except Exception as e:
                    logger.warning("Failed to create concern artifact: %s", e)

            if concerns:
                config_actions.append(f"Captured **{len(concerns)} items** from what you mentioned")

            # ── Save raw content to knowledge base ────────────────────────
            if org_data or projects or concerns:
                try:
                    self._save_conversation_content(user_input)
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

        # ── Build conversation summary for reply generation ───────────────
        conversation_summary = self._build_conversation_summary(context, extracted)

        # ── Generate reply ────────────────────────────────────────────────
        reply = await self._generate_reply(
            user_input=user_input,
            extracted=extracted,
            config_actions=config_actions,
            conversation_summary=conversation_summary,
        )

        # ── Build suggestions based on what we DON'T know yet ─────────────
        suggestions = self._generate_suggestions(context, extracted)

        log_alpha_event(
            "onboarding_turn",
            {
                "message": user_input,
                "reply": reply,
                "extracted": extracted,
                "config_actions": config_actions,
                "artifacts_count": len(artifacts_created),
                "suggestions": suggestions,
                "elapsed_ms": int((time.time() - start_ts) * 1000),
            },
        )

        return OnboardingResponse(
            reply=reply,
            phase=OnboardingPhase.CONVERSATION,
            next_phase=OnboardingPhase.CONVERSATION,
            extracted=extracted,
            config_applied=config_actions,
            artifacts_created=artifacts_created,
            suggestions=suggestions,
        )

    def _summarize_prior_context(self, context: Dict[str, Any]) -> str:
        """Build a text summary of what we already know from prior turns."""
        parts = []
        for phase_key, data in context.items():
            if isinstance(data, dict):
                # Org profile fields
                org = data.get("org_profile", data)
                if org.get("org_name"):
                    parts.append(f"Organisation: {org['org_name']}")
                if org.get("industry"):
                    parts.append(f"Industry: {org['industry']}")
                if org.get("team_size"):
                    parts.append(f"Team size: {org['team_size']}")

                # Projects
                for proj in data.get("projects", []):
                    parts.append(f"Project already captured: {proj.get('name', '?')}")

                # Concerns
                for concern in data.get("concerns", []):
                    parts.append(f"Concern already captured: {concern.get('content', '?')}")
        return "\n".join(parts) if parts else ""

    def _build_conversation_summary(
        self, context: Dict[str, Any], current_extracted: Dict[str, Any]
    ) -> str:
        """Build a summary of the full conversation state for reply generation."""
        parts = []
        # From prior context
        prior = self._summarize_prior_context(context)
        if prior:
            parts.append(f"Previously known:\n{prior}")

        # From current extraction
        org = current_extracted.get("org_profile", {})
        if org and any(v for v in org.values() if v and v != []):
            parts.append(f"Just learned about org: {json.dumps({k: v for k, v in org.items() if v}, default=str)}")

        projects = current_extracted.get("projects", [])
        if projects:
            parts.append(f"Just captured {len(projects)} project(s)")

        concerns = current_extracted.get("concerns", [])
        if concerns:
            parts.append(f"Just captured {len(concerns)} concern(s)/action(s)")

        return "\n".join(parts) if parts else "First message — nothing captured yet."

    def _generate_suggestions(
        self, context: Dict[str, Any], current_extracted: Dict[str, Any]
    ) -> List[str]:
        """Generate contextual suggestion chips based on what's missing."""
        known = set()
        for phase_data in context.values():
            if isinstance(phase_data, dict):
                org = phase_data.get("org_profile", phase_data)
                if org.get("org_name"):
                    known.add("org")
                if phase_data.get("projects"):
                    known.add("projects")
                if phase_data.get("concerns"):
                    known.add("concerns")

        # Check current extraction too
        if current_extracted.get("org_profile", {}).get("org_name"):
            known.add("org")
        if current_extracted.get("projects"):
            known.add("projects")
        if current_extracted.get("concerns"):
            known.add("concerns")

        suggestions = []
        if "org" not in known:
            suggestions.extend([
                "I run a 3-person design studio",
                "We're a solo consulting practice",
            ])
        elif "projects" not in known:
            suggestions.extend([
                "Right now I'm working on a website redesign and two proposals",
                "We've got a big client project due next month",
            ])
        elif "concerns" not in known:
            suggestions.extend([
                "I keep forgetting to follow up with leads",
                "Quarterly reports are due and I haven't started",
            ])
        else:
            suggestions.extend([
                "That covers the basics — let's go",
                "One more thing I should mention...",
            ])
        return suggestions[:3]

    def _save_conversation_content(self, content: str) -> Path:
        """Save a conversation turn to the knowledge base."""
        import config as app_config

        docs_root = Path(app_config.directory_path)
        out_dir = docs_root / "onboarding"
        out_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{datetime.utcnow().strftime('%Y-%m-%d')}_onboarding_notes.md"
        filepath = out_dir / filename

        # Append to existing file for the day
        if filepath.exists():
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n## Additional Notes\n\n{content}\n")
        else:
            frontmatter = "\n".join([
                "---",
                "title: Onboarding Conversation Notes",
                "category: onboarding",
                "source: conversational_onboarding",
                f"created_at: {datetime.utcnow().isoformat()}Z",
                "---",
                "",
            ])
            body = f"## Onboarding Conversation\n\n{content}\n"
            filepath.write_text(frontmatter + body, encoding="utf-8")

        return filepath

    # ── LLM interaction ───────────────────────────────────────────────────

    async def _get_ollama_model(self) -> Optional[str]:
        """Discover the first available Ollama chat model.

        Queries the Ollama tags endpoint and returns the name of the
        first model that isn't an embedding model, or None if Ollama
        isn't running or has no models.
        """
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session, session.get(
                "http://localhost:11434/api/tags",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                models = data.get("models", [])
                # Prefer smaller chat models for onboarding speed
                EMBED_KEYWORDS = {"embed", "nomic", "mxbai", "bge"}
                chat_models = [
                    m["name"] for m in models
                    if not any(k in m["name"].lower() for k in EMBED_KEYWORDS)
                ]
                # Sort by size ascending — prefer smaller/faster for setup
                sized = [
                    (m["name"], m.get("size", float("inf")))
                    for m in models
                    if m["name"] in chat_models
                ]
                sized.sort(key=lambda x: x[1])
                return sized[0][0] if sized else (chat_models[0] if chat_models else None)
        except Exception:
            return None

    async def _extract_with_llm(self, prompt: str) -> Dict[str, Any]:
        """Call the LLM to extract structured data from user input.

        Tries the model router first, falls back to a simple rules-based
        extraction if no LLM is available (so setup can work without a model).
        """
        # Try model router
        if self.model_router:
            try:
                response = await self.model_router.generate(
                    messages=[
                        {"role": "system", "content": "You extract structured data from natural language. Return ONLY valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=1000,
                )
                return self._parse_json_response(response)
            except Exception as e:
                logger.warning("Model router extraction failed: %s", e)

        # Try Ollama directly
        try:
            ollama_model = await self._get_ollama_model()
            if ollama_model:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    payload = {
                        "model": ollama_model,
                        "messages": [
                            {"role": "system", "content": "You extract structured data from natural language. Return ONLY valid JSON."},
                            {"role": "user", "content": prompt},
                        ],
                        "stream": False,
                        "keep_alive": "30m",
                        "options": {
                            "temperature": 0.1,
                            "num_ctx": 4096,
                            "num_predict": 1000,
                            "repeat_penalty": 1.0,
                            "top_k": 20,
                            "top_p": 0.8,
                        },
                    }
                    async with session.post(
                        "http://localhost:11434/api/chat",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            content = data.get("message", {}).get("content", "")
                            return self._parse_json_response(content)
        except Exception as e:
            logger.debug("Ollama extraction failed: %s", e)

        # Fallback: try OpenAI if available
        try:
            from services.secrets import get_secrets_manager
            secrets = get_secrets_manager()
            api_key = secrets.get("openai")
            if api_key:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    payload = {
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": "You extract structured data from natural language. Return ONLY valid JSON."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 1000,
                    }
                    async with session.post(
                        "https://api.openai.com/v1/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            content = data["choices"][0]["message"]["content"]
                            return self._parse_json_response(content)
        except Exception as e:
            logger.debug("OpenAI extraction failed: %s", e)

        # Last resort: return empty extraction (the user's input is still saved)
        logger.warning("No LLM available for extraction — returning empty result")
        return {}

    async def _generate_reply(
        self,
        user_input: str,
        extracted: Dict[str, Any],
        config_actions: List[str],
        conversation_summary: str,
    ) -> str:
        """Generate a conversational reply using LLM, with fallback templates."""
        try:
            from core.config_loader import OrgProfile
            _bn = OrgProfile.load().bot_name
        except Exception:
            _bn = "Magic Key Assistant"
        prompt = CONVERSATIONAL_REPLY_PROMPT.format(
            bot_name=_bn,
            user_input=user_input[:500],
            extracted=json.dumps(extracted, indent=2)[:500],
            config_actions=", ".join(config_actions) if config_actions else "Nothing yet",
            conversation_summary=conversation_summary[:500],
        )

        # Try LLM for natural reply
        for generate_fn in [self._try_model_router, self._try_ollama_direct, self._try_openai_direct]:
            try:
                reply = await generate_fn(prompt)
                if reply:
                    return reply
            except Exception:
                continue

        # Fallback: template-based reply (still good, just not personalised)
        return self._template_reply(extracted, config_actions)

    async def _try_model_router(self, prompt: str) -> Optional[str]:
        if not self.model_router:
            return None
        return await self.model_router.generate(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300,
        )

    async def _try_ollama_direct(self, prompt: str) -> Optional[str]:
        ollama_model = await self._get_ollama_model()
        if not ollama_model:
            return None
        import aiohttp
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": ollama_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": 0.7,
                    "num_ctx": 4096,
                    "num_predict": 300,
                    "repeat_penalty": 1.1,
                    "top_k": 40,
                    "top_p": 0.9,
                },
            }
            async with session.post(
                "http://localhost:11434/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("message", {}).get("content", "")
        return None

    async def _try_openai_direct(self, prompt: str) -> Optional[str]:
        from services.secrets import get_secrets_manager
        secrets = get_secrets_manager()
        api_key = secrets.get("openai")
        if not api_key:
            return None
        import aiohttp
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 300,
            }
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
        return None

    @staticmethod
    def _template_reply(
        extracted: Dict[str, Any],
        config_actions: List[str],
    ) -> str:
        """Generate a decent reply without an LLM."""
        parts = []

        # Acknowledge org info
        org = extracted.get("org_profile", {})
        name = org.get("org_name", "")
        industry = org.get("industry", "")
        if name:
            parts.append(f"Got it — I've set things up for **{name}**")
            if industry:
                parts[-1] += f" in {industry}"
            parts[-1] += "."

        # Acknowledge projects
        projects = extracted.get("projects", [])
        if projects:
            names = [p.get("name", "item") for p in projects[:3]]
            parts.append(f"I'm now tracking **{', '.join(names)}**" +
                        (f" and {len(projects) - 3} more" if len(projects) > 3 else "") + ".")

        # Acknowledge concerns
        concerns = extracted.get("concerns", [])
        if concerns:
            parts.append(f"Captured **{len(concerns)} item(s)** from what you mentioned.")

        # Config actions
        if config_actions:
            parts.append(" ".join(config_actions[:2]) + ".")

        if not parts:
            parts.append("Thanks for sharing that.")

        parts.append("\n\nWhat else should I know about your work? Or if that covers the basics, just let me know and we'll wrap up.")
        return " ".join(parts)

    # ── Artifact creation ─────────────────────────────────────────────────

    async def _create_project_artifact(self, proj: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Create a Rail or Lead from an extracted project."""
        if not self.db:
            return None

        entity_type = proj.get("entity_type", "rail")
        name = proj.get("name", "Untitled")

        if entity_type == "lead":
            from core.services import LeadService
            svc = LeadService(self.db)
            lead_id = await svc.create(
                name,
                source="onboarding",
                notes=proj.get("description", ""),
            )
            return {"type": "lead", "name": name, "id": str(lead_id)}

        elif entity_type == "action":
            from core.services import ActionService
            svc = ActionService(self.db)
            action_id = await svc.create(
                name,
                due_date=proj.get("deadline") or None,
                priority="high" if proj.get("urgency") == "high" else "medium",
            )
            return {"type": "action", "name": name, "id": str(action_id)}

        else:
            # Default: create a Rail (project/venture)
            from core.services import RailsService
            svc = RailsService(self.db)
            phase_map = {"active": "operate", "pipeline": "launch", "idea": "validate"}
            phase = phase_map.get(proj.get("status", "active"), "validate")
            rail_id = await svc.create_rail(
                name,
                phase,
                description=proj.get("description", ""),
                use_default_stages=True,
            )
            return {"type": "rail", "name": name, "id": str(rail_id)}

    async def _create_concern_artifact(self, concern: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Create an appropriate artifact from an extracted concern."""
        if not self.db:
            return None

        entity_type = concern.get("entity_type", "action")
        content = concern.get("content", "")

        if entity_type == "action":
            from core.services import ActionService
            svc = ActionService(self.db)
            priority = "high" if concern.get("urgency") == "high" else "medium"
            action_id = await svc.create(
                content[:200],
                due_date=concern.get("deadline") or None,
                priority=priority,
                owner=concern.get("owner") or None,
            )
            return {"type": "action", "name": content[:80], "id": str(action_id)}

        elif entity_type == "decision":
            from core.services import DecisionService
            svc = DecisionService(self.db)
            dec_id = await svc.create(
                content[:200],
                content,
                rationale="Captured during onboarding — needs decision",
            )
            return {"type": "decision", "name": content[:80], "id": str(dec_id)}

        elif entity_type == "obligation":
            from core.services import ObligationService
            svc = ObligationService(self.db)
            obl_id = await svc.create(
                content[:200],
                frequency="unknown",
                category="onboarding",
            )
            return {"type": "obligation", "name": content[:80], "id": str(obl_id)}

        elif entity_type == "knowledge_gap":
            # Save as a knowledge gap for interview follow-up
            try:
                from cogs.KnowledgeGapTracker import insert_gap
                gap_id = await insert_gap(
                    self.db,
                    topic="Onboarding",
                    question=content,
                    source="onboarding_brain_dump",
                    curation_status="keep",
                    priority=12,
                )
                return {"type": "knowledge_gap", "name": content[:80], "id": str(gap_id)}
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        return None

    # ── Config management ─────────────────────────────────────────────────

    def _write_org_profile(self, data: Dict[str, Any]) -> None:
        """Write or merge org_profile.yaml."""
        import yaml

        org_path = CONFIG_DIR / "org_profile.yaml"
        existing = {}
        if org_path.exists():
            with open(org_path, encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}

        before = yaml.dump(existing, default_flow_style=False, allow_unicode=True)

        # Merge: new data takes priority, but preserve anything existing
        for key, val in data.items():
            if isinstance(val, dict) and isinstance(existing.get(key), dict):
                existing[key].update(val)
            elif val:  # Only overwrite if new value is non-empty
                existing[key] = val

        after = yaml.dump(existing, default_flow_style=False, allow_unicode=True)

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(org_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)

        try:
            self._record_diff("org_profile.yaml", before, after)
        except Exception as e:
            logger.warning("_write_org_profile: suppressed %s", e)

    def _auto_configure_workflows(
        self,
        profile: ExtractedProfile,
        *,
        dry_run: bool = False,
    ) -> tuple[List[str], Dict[str, Any]]:
        """Auto-configure workflows.yaml based on the extracted profile."""
        import yaml

        wf_path = CONFIG_DIR / "workflows.yaml"
        existing = {}
        if wf_path.exists():
            with open(wf_path, encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}

        before = yaml.dump(existing, default_flow_style=False, allow_unicode=True)

        changes = []

        # Always enable memory and work
        if "memory" not in existing:
            existing["memory"] = {}
        existing.setdefault("memory", {})["enabled"] = True
        changes.append("Enabled memory module (knowledge base + RAG)")

        if "work" not in existing:
            existing["work"] = {}
        existing.setdefault("work", {})["enabled"] = True
        changes.append("Enabled work module (actions + meetings)")

        # Enable pipeline if they mentioned clients, prospects, sales
        pipeline_keywords = {"client", "customer", "prospect", "lead", "pipeline",
                            "sales", "proposal", "bid", "contract", "deal", "rfp"}
        services_lower = " ".join(profile.key_services).lower()
        tagline_lower = profile.tagline.lower()
        if any(kw in services_lower or kw in tagline_lower for kw in pipeline_keywords):
            existing.setdefault("pipeline", {})["enabled"] = True
            changes.append("Enabled pipeline module (detected client/sales activity)")

        # Enable health for teams
        if profile.team_size in ("small", "team"):
            existing.setdefault("health", {})["enabled"] = True
            changes.append("Enabled health module (team engagement tracking)")

        # Personas off by default
        existing.setdefault("personas", {})["enabled"] = False

        if not dry_run:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(wf_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)
            after = yaml.dump(existing, default_flow_style=False, allow_unicode=True)
            try:
                self._record_diff("workflows.yaml", before, after)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        return changes, existing

    def _enable_workflow_module(
        self,
        module: str,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Enable a single workflow module in workflows.yaml."""
        import yaml

        wf_path = CONFIG_DIR / "workflows.yaml"
        existing = {}
        if wf_path.exists():
            with open(wf_path, encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}

        before = yaml.dump(existing, default_flow_style=False, allow_unicode=True)

        existing.setdefault(module, {})["enabled"] = True

        if not dry_run:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(wf_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)
            after = yaml.dump(existing, default_flow_style=False, allow_unicode=True)
            try:
                self._record_diff("workflows.yaml", before, after)
            except Exception as e:
                logger.warning("_enable_workflow_module: suppressed %s", e)

        return existing

    async def _seed_gaps(self) -> int:
        """Seed foundational knowledge gaps based on the configured org profile."""
        if not self.db:
            return 0

        from core.seed_foundational_gaps import (
            is_foundational_gaps_seeded,
            seed_foundational_gaps,
        )

        if is_foundational_gaps_seeded():
            return 0

        try:
            from core.config_loader import OrgProfile
            org = OrgProfile.load()
            count = await seed_foundational_gaps(
                self.db,
                mode=org.mode,
                industry=org.industry,
                org_name=org.name,
            )
            return count
        except Exception as e:
            logger.warning("Failed to seed foundational gaps: %s", e)
            return 0

    def _save_brain_dump_doc(self, content: str) -> Path:
        """Save the brain dump as a knowledge document for RAG."""
        import config as app_config

        docs_root = Path(app_config.directory_path)
        out_dir = docs_root / "onboarding"
        out_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{datetime.utcnow().strftime('%Y-%m-%d')}_brain_dump.md"
        filepath = out_dir / filename

        frontmatter = "\n".join([
            "---",
            "title: Onboarding Brain Dump",
            "category: onboarding",
            "source: conversational_onboarding",
            f"created_at: {datetime.utcnow().isoformat()}Z",
            "---",
            "",
        ])
        body = f"## Initial Brain Dump\n\n{content}\n"
        filepath.write_text(frontmatter + body, encoding="utf-8")
        logger.info("Saved onboarding brain dump: %s", filepath)
        return filepath

    # ── Transcript + diff logging ─────────────────────────────────────────

    # Patterns that should be redacted before writing to logs.
    _REDACT_PATTERNS: list = []  # Populated lazily below

    @staticmethod
    def _redact_sensitive(text: str) -> str:
        """Remove probable secrets / PII from a string before logging.

        Covers: API keys (sk-*, key-*, ghp_*, xox*), bearer tokens,
        e-mail addresses, long hex tokens, and generic "password = ..." lines.
        """
        import re

        if not text:
            return text

        patterns = [
            # OpenAI / Anthropic / generic API keys
            (r'\b(sk-[A-Za-z0-9_-]{20,})', '[REDACTED_API_KEY]'),
            (r'\b(key-[A-Za-z0-9_-]{20,})', '[REDACTED_API_KEY]'),
            # GitHub personal access tokens
            (r'\b(ghp_[A-Za-z0-9]{36,})', '[REDACTED_GH_TOKEN]'),
            # Slack tokens
            (r'\b(xox[bpras]-[A-Za-z0-9-]{10,})', '[REDACTED_SLACK_TOKEN]'),
            # Bearer tokens
            (r'(?i)(bearer\s+)[A-Za-z0-9_.~+/=-]{20,}', r'\1[REDACTED_BEARER]'),
            # Generic long hex strings (≥32 chars, likely tokens/hashes)
            (r'\b([0-9a-f]{32,})\b', '[REDACTED_HEX_TOKEN]'),
            # E-mail addresses
            (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[REDACTED_EMAIL]'),
            # password = "...", password: "...", etc.
            (r'(?i)(password|passwd|secret|token|api_key|apikey)\s*[:=]\s*\S+',
             r'\1=[REDACTED]'),
        ]
        for pat, repl in patterns:
            text = re.sub(pat, repl, text)
        return text

    def _log_transcript(
        self,
        *,
        phase: str,
        user_input: str,
        response: OnboardingResponse,
        apply_changes: bool,
    ) -> None:
        """Append a transcript entry for later inspection."""
        import json

        redact = self._redact_sensitive

        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "phase": phase,
            "apply_changes": apply_changes,
            "user_input": redact(user_input),
            "extracted": json.loads(redact(json.dumps(response.extracted, default=str))),
            "proposed": json.loads(redact(json.dumps(response.proposed, default=str))) if response.proposed else None,
            "config_applied": response.config_applied,
            "artifacts_created": response.artifacts_created,
        }

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        transcript_path = CONFIG_DIR / "onboarding_transcript.jsonl"
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def _record_diff(self, filename: str, before: str, after: str) -> None:
        """Record a unified diff to an onboarding diff log."""
        import difflib
        import json

        diff = "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"before/{filename}",
                tofile=f"after/{filename}",
                lineterm="",
            )
        )
        if not diff.strip():
            return

        diff = self._redact_sensitive(diff)

        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "file": filename,
            "diff": diff,
        }

        diff_path = CONFIG_DIR / "onboarding_diff.json"
        existing: List[Dict[str, Any]] = []
        if diff_path.exists():
            try:
                with open(diff_path, encoding="utf-8") as f:
                    existing = json.load(f) or []
            except Exception:
                existing = []

        existing.append(entry)
        with open(diff_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

    # ── JSON parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_json_response(text: str) -> Dict[str, Any]:
        """Extract JSON from an LLM response that may contain markdown fences."""
        if not text:
            return {}
        # Strip markdown code fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        # Try parsing
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find JSON object in the text
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError as e:
                    logger.warning("_parse_json_response: suppressed %s", e)
        logger.debug("Failed to parse JSON from LLM response: %s", text[:200])
        return {}

    @staticmethod
    def _parse_profile(extracted: Dict[str, Any]) -> ExtractedProfile:
        """Convert raw extracted JSON to an ExtractedProfile."""
        return ExtractedProfile(
            org_name=extracted.get("org_name", ""),
            industry=extracted.get("industry", ""),
            tagline=extracted.get("tagline", ""),
            team_size=extracted.get("team_size", "solo"),
            team_description=extracted.get("team_description", ""),
            location=extracted.get("location", ""),
            key_services=extracted.get("key_services", []),
            confidence=float(extracted.get("confidence", 0.5)),
        )


# ═════════════════════════════════════════════════════════════════════════════
# Transcript retention cleanup
# ═════════════════════════════════════════════════════════════════════════════

def cleanup_old_transcripts(max_age_days: int = 30) -> Dict[str, Any]:
    """Delete transcript/diff entries older than *max_age_days*.

    Returns a summary of what was removed.
    """
    from datetime import timedelta
    removed: Dict[str, int] = {"transcript_lines": 0, "diff_entries": 0}
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)

    # --- transcript.jsonl ---
    transcript_path = CONFIG_DIR / "onboarding_transcript.jsonl"
    if transcript_path.exists():
        kept_lines: list[str] = []
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(entry["timestamp"].rstrip("Z"))
                    if ts >= cutoff:
                        kept_lines.append(line.rstrip("\n"))
                    else:
                        removed["transcript_lines"] += 1
                except Exception:
                    kept_lines.append(line.rstrip("\n"))
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept_lines) + ("\n" if kept_lines else ""))

    # --- diff.json ---
    diff_path = CONFIG_DIR / "onboarding_diff.json"
    if diff_path.exists():
        try:
            with open(diff_path, encoding="utf-8") as f:
                entries = json.load(f) or []
        except Exception:
            entries = []
        kept = []
        for entry in entries:
            try:
                ts = datetime.fromisoformat(entry["timestamp"].rstrip("Z"))
                if ts >= cutoff:
                    kept.append(entry)
                else:
                    removed["diff_entries"] += 1
            except Exception:
                kept.append(entry)
        with open(diff_path, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2)

    return removed
