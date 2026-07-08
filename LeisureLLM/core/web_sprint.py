"""
Web Sprint — Web-compatible capture sprint for first-run knowledge building.

Mirrors the 3-step capture sprint from onboarding_sprint.py but works
through HTTP APIs instead of Discord interactions.  Each step:

  1. Capture a recent decision  → decision record + knowledge doc
  2. Add an upcoming deadline   → obligation record
  3. Describe a key process     → SOP / knowledge doc

The sprint creates real artifacts using the same service layer as the
conversational onboarding and chat pipeline.

Usage
-----
    from core.web_sprint import WebSprintProcessor

    processor = WebSprintProcessor(db=db)
    result = await processor.capture_step(
        step_number=1,
        user_text="We decided to move from Zoom to Teams because...",
    )
    # result.success      -> True
    # result.artifacts     -> [{"type": "decision", "name": "Move from Zoom to Teams"}]
    # result.feedback      -> "Great — I've saved that as a decision record."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"


# ═════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class SprintStepInfo:
    """Metadata about one sprint step, for the UI."""
    step_number: int
    title: str
    emoji: str
    prompt: str
    hint: str
    entity_type: str
    example: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_number": self.step_number,
            "title": self.title,
            "emoji": self.emoji,
            "prompt": self.prompt,
            "hint": self.hint,
            "entity_type": self.entity_type,
            "example": self.example,
        }


@dataclass
class CaptureResult:
    """Result from capturing one sprint step."""
    success: bool
    step_number: int
    feedback: str
    artifacts: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "step_number": self.step_number,
            "feedback": self.feedback,
            "artifacts": self.artifacts,
            "error": self.error,
        }


# ═════════════════════════════════════════════════════════════════════════════
# SPRINT STEP DEFINITIONS
# ═════════════════════════════════════════════════════════════════════════════

SPRINT_STEPS = [
    SprintStepInfo(
        step_number=1,
        title="Capture a recent decision",
        emoji="⚖️",
        prompt=(
            "Think of a decision you've made recently — big or small. "
            "Type it as naturally as you would in conversation."
        ),
        hint="What did you decide, and why?",
        entity_type="decision",
        example=(
            "We decided to switch from weekly in-person meetings to async "
            "standups because scheduling conflicts were causing us to miss "
            "every other week."
        ),
    ),
    SprintStepInfo(
        step_number=2,
        title="Add an upcoming deadline",
        emoji="📅",
        prompt=(
            "What's a recurring deadline or upcoming obligation you can't "
            "afford to miss?"
        ),
        hint="What's due, when, and what happens if you miss it?",
        entity_type="obligation",
        example=(
            "Quarterly tax filing is due every 3 months. Missing it means "
            "penalties and interest. We use our accountant but I need to "
            "send them the books 2 weeks early."
        ),
    ),
    SprintStepInfo(
        step_number=3,
        title="Describe a key process",
        emoji="📋",
        prompt=(
            "Think of something your team does regularly that only exists "
            "in someone's head. Walk me through the steps."
        ),
        hint="What happens first, then what?",
        entity_type="sop",
        example=(
            "When a new client signs up: create their folder in Drive, add "
            "them to the invoicing system, schedule a kickoff call, and "
            "assign a project lead."
        ),
    ),
]


# ═════════════════════════════════════════════════════════════════════════════
# SPRINT PROCESSOR
# ═════════════════════════════════════════════════════════════════════════════

class WebSprintProcessor:
    """Process web-based sprint captures: classifies text and creates artifacts."""

    def __init__(self, db=None, model_router=None):
        self.db = db
        self.model_router = model_router

    def get_steps(self) -> List[SprintStepInfo]:
        """Return all sprint step definitions."""
        return SPRINT_STEPS

    async def capture_step(
        self,
        step_number: int,
        user_text: str,
    ) -> CaptureResult:
        """Process one sprint step capture.

        Takes the user's natural-language input and creates the
        appropriate artifact (decision, obligation, or SOP doc).
        """
        if not user_text.strip():
            return CaptureResult(
                success=False,
                step_number=step_number,
                feedback="Please type something to capture.",
                error="empty_input",
            )

        step = next((s for s in SPRINT_STEPS if s.step_number == step_number), None)
        if not step:
            return CaptureResult(
                success=False,
                step_number=step_number,
                feedback="Unknown step number.",
                error="invalid_step",
            )

        try:
            if step.entity_type == "decision":
                return await self._capture_decision(step, user_text)
            elif step.entity_type == "obligation":
                return await self._capture_obligation(step, user_text)
            elif step.entity_type == "sop":
                return await self._capture_sop(step, user_text)
            else:
                return await self._capture_generic(step, user_text)
        except Exception as e:
            logger.error("Sprint capture step %d failed: %s", step_number, e, exc_info=True)
            return CaptureResult(
                success=False,
                step_number=step_number,
                feedback="Something went wrong saving that — you can always use the Chat page later.",
                error=str(e),
            )

    async def _capture_decision(self, step: SprintStepInfo, text: str) -> CaptureResult:
        """Extract and save a decision."""
        # Try LLM-powered extraction first
        title, rationale = await self._extract_decision_parts(text)

        artifacts = []
        if self.db:
            try:
                from core.services import DecisionService
                svc = DecisionService(self.db)
                decision = svc.create_decision(
                    title=title or "Decision from onboarding sprint",
                    description=text,
                    rationale=rationale or text,
                    decided_by="user",
                    status="decided",
                )
                artifacts.append({
                    "type": "Decision",
                    "name": title or "Decision",
                    "id": str(getattr(decision, "id", "")),
                })
            except Exception as e:
                logger.warning("Decision service save failed, falling back to doc: %s", e)

        # Also save as knowledge document
        doc_path = self._save_as_document(step, text, title)
        if doc_path:
            artifacts.append({"type": "Document", "name": doc_path.name})

        return CaptureResult(
            success=True,
            step_number=step.step_number,
            feedback=(
                f"Got it — saved as a decision record"
                f"{f': **{title}**' if title else ''}. "
                "Your assistant can now answer questions about this."
            ),
            artifacts=artifacts,
        )

    async def _capture_obligation(self, step: SprintStepInfo, text: str) -> CaptureResult:
        """Extract and save an obligation / deadline."""
        title = await self._extract_title(text, "obligation")

        artifacts = []
        if self.db:
            try:
                from core.services import ObligationService
                svc = ObligationService(self.db)
                obligation = svc.create_obligation(
                    title=title or "Obligation from onboarding sprint",
                    description=text,
                    frequency="",
                    owner="user",
                )
                artifacts.append({
                    "type": "Obligation",
                    "name": title or "Obligation",
                    "id": str(getattr(obligation, "id", "")),
                })
            except Exception as e:
                logger.warning("Obligation service save failed, falling back to doc: %s", e)

        doc_path = self._save_as_document(step, text, title)
        if doc_path:
            artifacts.append({"type": "Document", "name": doc_path.name})

        return CaptureResult(
            success=True,
            step_number=step.step_number,
            feedback=(
                f"Tracked — saved as an obligation"
                f"{f': **{title}**' if title else ''}. "
                "The system will surface this when it's approaching."
            ),
            artifacts=artifacts,
        )

    async def _capture_sop(self, step: SprintStepInfo, text: str) -> CaptureResult:
        """Save a process description as a knowledge doc / SOP."""
        title = await self._extract_title(text, "process")

        artifacts = []
        doc_path = self._save_as_document(step, text, title)
        if doc_path:
            artifacts.append({"type": "SOP", "name": doc_path.name})

        return CaptureResult(
            success=True,
            step_number=step.step_number,
            feedback=(
                f"Saved as a process document"
                f"{f': **{title}**' if title else ''}. "
                "This is now searchable in your knowledge base."
            ),
            artifacts=artifacts,
        )

    async def _capture_generic(self, step: SprintStepInfo, text: str) -> CaptureResult:
        """Fallback: save as a knowledge doc."""
        doc_path = self._save_as_document(step, text)
        return CaptureResult(
            success=True,
            step_number=step.step_number,
            feedback="Saved to your knowledge base.",
            artifacts=[{"type": "Document", "name": doc_path.name}] if doc_path else [],
        )

    # ── LLM EXTRACTION HELPERS ──────────────────────────────────────────

    async def _extract_decision_parts(self, text: str) -> tuple[str, str]:
        """Try to extract a decision title and rationale from user text."""
        if not self.model_router:
            return self._heuristic_title(text), ""

        prompt = (
            "Extract the decision title and rationale from this text. "
            "Return ONLY valid JSON: {\"title\": \"short decision title\", \"rationale\": \"why\"}\n\n"
            f"Text: {text}"
        )
        try:
            result = await self._llm_call(prompt)
            import json
            data = json.loads(result)
            return data.get("title", ""), data.get("rationale", "")
        except Exception:
            return self._heuristic_title(text), ""

    async def _extract_title(self, text: str, entity_type: str) -> str:
        """Try to extract a short title from user text."""
        if not self.model_router:
            return self._heuristic_title(text)

        prompt = (
            f"Extract a short title (5-10 words) for this {entity_type} from the text. "
            "Return ONLY the title text, nothing else.\n\n"
            f"Text: {text}"
        )
        try:
            result = await self._llm_call(prompt)
            # Clean up LLM response
            title = result.strip().strip('"\'').strip()
            if len(title) > 100:
                title = title[:97] + "…"
            return title
        except Exception:
            return self._heuristic_title(text)

    async def _llm_call(self, prompt: str) -> str:
        """Make an LLM call using whatever backend is available."""
        if self.model_router:
            try:
                result = await self.model_router.generate(prompt, temperature=0.3)
                if isinstance(result, dict):
                    return result.get("text", result.get("response", ""))
                return str(result)
            except Exception as e:
                logger.warning("_llm_call: suppressed %s", e)

        # Fallback: try Ollama direct
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "http://localhost:11434/api/generate",
                    json={"model": "llama3.1:8b", "prompt": prompt, "stream": False},
                )
                if resp.status_code == 200:
                    return resp.json().get("response", "")
        except Exception as e:
            logger.warning("_llm_call: suppressed %s", e)

        return ""

    @staticmethod
    def _heuristic_title(text: str) -> str:
        """Generate a simple title from the first sentence."""
        # Take first sentence up to 60 chars
        for end in (".", "!", "?", "\n"):
            idx = text.find(end)
            if 0 < idx < 100:
                return text[:idx].strip()
        return text[:60].strip() + ("…" if len(text) > 60 else "")

    # ── DOCUMENT PERSISTENCE ─────────────────────────────────────────────

    def _save_as_document(
        self,
        step: SprintStepInfo,
        content: str,
        title: str = "",
    ) -> Optional[Path]:
        """Save capture as a markdown file in docs/onboarding/."""
        try:
            import config as app_config
            docs_root = Path(app_config.directory_path)
        except Exception:
            docs_root = Path(__file__).resolve().parent.parent / "docs"

        out_dir = docs_root / "onboarding"
        out_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        slug = step.entity_type
        filename = f"{now.strftime('%Y-%m-%d')}_{slug}.md"
        filepath = out_dir / filename

        # Avoid overwriting
        counter = 1
        while filepath.exists():
            filename = f"{now.strftime('%Y-%m-%d')}_{slug}_{counter}.md"
            filepath = out_dir / filename
            counter += 1

        frontmatter = "\n".join([
            "---",
            f"title: {title or step.title}",
            f"category: {step.entity_type}",
            "source: web_sprint",
            f"created_at: {now.isoformat()}",
            "tags: onboarding, sprint",
            "---",
            "",
        ])
        body = f"## {title or step.title}\n\n{content}\n"
        filepath.write_text(frontmatter + body, encoding="utf-8")
        logger.info("Saved web sprint capture: %s", filepath)
        return filepath
