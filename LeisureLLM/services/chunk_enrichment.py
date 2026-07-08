"""
Chunk Enrichment Service
========================
Uses a local LLM (Ollama) at ingestion time to extract structured metadata
from raw document chunks.  This dramatically improves retrieval quality for
messy, unstructured data — Discord exports, pasted notes, PDFs with no
headings, etc.

Extracted fields
----------------
- summary         : 1-2 sentence description of the chunk's content
- topics          : list of 2-5 topic tags
- content_type    : one of decision / meeting_notes / strategy / reference /
                    casual_discussion / announcement / technical / financial /
                    product_spec / project_proposal / team_bio / brand_marketing /
                    legal_contract / operational_guidance / knowledge_gap_response /
                    creative_work / noise
- participants    : list of usernames/names mentioned (validated against source)
- date_range      : ISO date or range extracted from content
- actionability   : float 0.0-1.0 (how actionable / referenceable)
- key_questions   : 2-4 questions this chunk could answer (HyDE-style)
- entities        : named entities mentioned (validated against source)
- confidence      : float 0.0-1.0 (LLM self-reported extraction confidence)

Anti-hallucination measures
---------------------------
- Extraction prompt explicitly forbids inferring/guessing
- Entities and participants are validated against the source text
- If >50% of extracted names are hallucinated, confidence is auto-reduced
- Low-confidence enrichments are de-weighted at retrieval time
- Provenance tracked (which model enriched each chunk)

Usage
-----
    from services.chunk_enrichment import ChunkEnricher

    enricher = ChunkEnricher()              # auto-picks best local model
    meta = await enricher.enrich(chunk_text) # returns EnrichmentResult
    doc.metadata.update(meta.to_metadata()) # merge into ChromaDB metadata
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── Schema that the LLM must return ──────────────────────────────────────────

ENRICHMENT_SCHEMA = {
    "summary": "string — 1-2 sentence summary. ONLY state facts explicitly present in the text. Never infer, guess, or add context.",
    "topics": "list[string] — 2-5 topic/keyword tags. Use concrete nouns that APPEAR in the text (project names, product names, people, technologies) not abstract labels.",
    "content_type": "string — one of: decision, meeting_notes, strategy, reference, casual_discussion, announcement, technical, financial, product_spec, project_proposal, team_bio, brand_marketing, legal_contract, operational_guidance, knowledge_gap_response, creative_work, noise",
    "participants": "list[string] — usernames or real names of people who APPEAR in the text (not inferred)",
    "date_range": "string — ISO date or date range (YYYY-MM-DD or YYYY-MM-DD/YYYY-MM-DD). ONLY from explicit dates in the text. Empty string if no dates are clearly stated.",
    "actionability": "float — 0.0 (trivia/noise) to 1.0 (critical decision/reference)",
    "key_questions": "list[string] — 2-4 questions this chunk could answer. Think about what someone in this organisation would actually ask.",
    "entities": "list[string] — named entities that APPEAR in the text: project names, product names, company names, venue names, technologies mentioned",
    "confidence": "float — 0.0 to 1.0. How confident are you in the accuracy of your extraction? 1.0 = text is clear and structured. 0.5 = text is ambiguous but reasonable guesses can be made. 0.1 = text is garbled, minimal, or very hard to interpret.",
}

EXTRACTION_PROMPT = """You are a metadata extraction engine for an organisation's knowledge base. Documents may include chat exports, meeting notes, product specs, project proposals, team bios, marketing copy, financial plans, contracts, operational guides, and various messy unstructured data.
{org_context}
Analyse the following document chunk and return a JSON object with exactly these fields:

{schema}

Content type definitions:
- decision: contains a concrete decision, commitment, or resolution
- meeting_notes: meeting minutes, agendas, or recap
- strategy: business strategy, planning, roadmaps, goals
- reference: stable reference material (how-to, specs, docs)
- casual_discussion: informal chat, banter, commentary
- announcement: news, updates, launches communicated to a group
- technical: code, architecture, system design, debugging
- financial: budgets, investments, pricing, revenue, costs
- product_spec: product requirements, feature specs, design docs, roadmaps
- project_proposal: project pitches, proposals, client-facing descriptions
- team_bio: info about a team member — their skills, role, background
- brand_marketing: mission statements, brand voice, marketing copy, website content
- legal_contract: partnership agreements, contracts, legal terms
- operational_guidance: internal processes, calibration docs, philosophy, SOPs
- knowledge_gap_response: answers generated to fill identified knowledge gaps
- creative_work: design assets, music, art, fabrication, creative descriptions
- noise: attachment-only messages, bot spam, broken encoding, no informational value

=== ANTI-HALLUCINATION RULES (CRITICAL) ===
- ONLY extract information that is EXPLICITLY STATED in the text below.
- If a field cannot be determined from the text, use the empty/default value (empty string, empty list, or 0.5).
- DO NOT infer, assume, or add context from outside the chunk.
- For "summary": describe ONLY what the text says. Do not interpret, editorialize, or add your own analysis.
- For "participants": include ONLY names/usernames that literally appear in the text. Do not guess who might be involved.
- For "entities": include ONLY named things that literally appear in the text. Do not add entities from your training data.
- For "date_range": include ONLY dates explicitly written in the text. Do not estimate or infer dates.
- For "confidence": be honest. If the text is garbled, very short, or ambiguous, confidence should be LOW.
- If you're uncertain about content_type, use "reference" rather than guessing a more specific type.

Additional rules:
- Output ONLY valid JSON — no markdown fences, no explanation, no preamble.
- For date_range, look for timestamps like [M/D/YYYY H:MM AM/PM], ISO dates, month+year references. Convert to ISO format. Leave empty if none found.
- For content_type, choose the BEST single fit from the list above.
- For key_questions, think: "What would someone in this organisation actually ask that this chunk answers?"
- Keep the summary factual and specific — mention concrete details, names, numbers from the text.
- actionability: 1.0 = contains a decision, commitment, specification, or critical fact. 0.0 = noise/spam. 0.3 = general context. 0.7 = useful reference with specific details.
- For chunks with broken encoding (garbled bytes, corrupted document internals), set content_type to "noise", actionability to 0.0, and confidence to 0.0.

Document chunk:
---
{text}
---

JSON output:"""

# Max text sent to the LLM per chunk (avoids context window issues on small models)
_MAX_CHUNK_TEXT = 3000

# Valid content_type values
VALID_CONTENT_TYPES = frozenset({
    "decision", "meeting_notes", "strategy", "reference",
    "casual_discussion", "announcement", "technical", "financial",
    "product_spec", "project_proposal", "team_bio", "brand_marketing",
    "legal_contract", "operational_guidance", "knowledge_gap_response",
    "creative_work", "noise",
})


@dataclass
class EnrichmentResult:
    """Parsed result from LLM enrichment."""
    summary: str = ""
    topics: List[str] = field(default_factory=list)
    content_type: str = "unknown"
    participants: List[str] = field(default_factory=list)
    date_range: str = ""
    actionability: float = 0.5
    key_questions: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    confidence: float = 0.5   # LLM self-reported extraction confidence
    enriched: bool = False    # True if LLM enrichment succeeded
    enriched_at: str = ""     # ISO timestamp of enrichment
    enrichment_model: str = ""  # Which model produced this enrichment

    def to_metadata(self) -> Dict[str, Any]:
        """Flatten to ChromaDB-compatible metadata (strings only for lists)."""
        return {
            "llm_summary": self.summary,
            "llm_topics": "|".join(self.topics),  # ChromaDB doesn't support list values
            "llm_content_type": self.content_type,
            "llm_participants": "|".join(self.participants),
            "llm_date_range": self.date_range,
            "llm_actionability": self.actionability,
            "llm_key_questions": "|".join(self.key_questions),
            "llm_entities": "|".join(self.entities),
            "llm_confidence": self.confidence,
            "enriched": self.enriched,
            "enriched_at": self.enriched_at,
            "enrichment_model": self.enrichment_model,
        }


class ChunkEnricher:
    """Calls a local Ollama model to extract structured metadata from chunks."""

    def __init__(
        self,
        model: str | None = None,
        endpoint: str = "http://localhost:11434",
        timeout: float | None = None,
        max_retries: int = 2,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model  # None = auto-detect
        self.timeout = timeout if timeout is not None else self._default_timeout()
        self.max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None
        self._org_context = self._build_org_context()

    @staticmethod
    def _default_timeout() -> float:
        """Read enrichment timeout from the centralised LLMTimeouts config."""
        try:
            from services.model_router import LLMTimeouts
            return LLMTimeouts().enrichment  # 120.0 default
        except Exception:
            return 120.0

    @staticmethod
    def _build_org_context() -> str:
        """Build an org-specific context block for the extraction prompt.

        Pulls from org_profile.yaml (name, industry, capabilities, members)
        and operational_context.txt (free-form org description) so the
        enrichment LLM understands the org's domain vocabulary without
        any hardcoded examples.  Returns empty string if nothing is configured.
        """
        lines: list[str] = []
        try:
            from core.config_loader import OrgProfile
            org = OrgProfile.load()
            if org.name and org.name != "My Company":
                lines.append(f"Organisation: {org.name}")
            if org.industry:
                lines.append(f"Industry: {org.industry}")
            if org.capabilities:
                lines.append("Capabilities: " + ", ".join(org.capabilities))
            if org.members:
                names = [m.name for m in org.members if m.name]
                if names:
                    lines.append("Known team members: " + ", ".join(names))
        except Exception:
            pass  # No config available — that's fine

        # Also try to pull a short excerpt from operational_context.txt
        try:
            ctx_path = Path(__file__).resolve().parent.parent / "prompts" / "operational_context.txt"
            if ctx_path.exists():
                raw = ctx_path.read_text(encoding="utf-8", errors="replace")[:600]
                # Take just the positioning / quick-facts block as domain primer
                lines.append(f"Organisation context (excerpt): {raw.strip()}")
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        if not lines:
            return ""
        header = "\n=== ORGANISATION CONTEXT (for domain vocabulary — NOT for output) ===\n"
        body = "\n".join(lines)
        footer = (
            "\n\nUse this context ONLY to recognise domain-specific names, projects, "
            "and terminology in the document chunk. Do NOT add any of this information "
            "to your output unless it also appears in the chunk text below.\n"
        )
        return header + body + footer

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def _detect_model(self) -> str:
        """Pick the best available local model for enrichment."""
        if self.model:
            return self.model

        # Try dynamic catalog-based selection first
        try:
            from services.model_discovery import pick_best_enrichment_model
            best = await pick_best_enrichment_model()
            if best:
                self.model = best
                logger.info("Auto-selected enrichment model (catalog): %s", self.model)
                return self.model
        except Exception as exc:
            logger.debug("Catalog-based enrichment model selection failed: %s", exc)

        # Fallback: hardcoded substring matching
        try:
            session = await self._get_session()
            async with session.get(f"{self.endpoint}/api/tags") as resp:
                if resp.status != 200:
                    raise RuntimeError("Ollama not reachable")
                data = await resp.json()
                models = [m["name"] for m in data.get("models", [])]
        except Exception as exc:
            raise RuntimeError(f"Cannot detect Ollama models: {exc}") from exc

        if not models:
            raise RuntimeError("No Ollama models installed. Pull a model first.")

        # Prefer smaller/faster models for enrichment (extraction doesn't need 70B)
        # Priority order: qwen2.5 family (great at structured output) > llama > anything
        priority = []
        for m in models:
            ml = m.lower()
            if "qwen" in ml and ("7b" in ml or "8b" in ml or "14b" in ml):
                priority.append((0, m))
            elif "qwen" in ml:
                priority.append((1, m))
            elif "llama" in ml and ("8b" in ml or "7b" in ml):
                priority.append((2, m))
            elif "llama" in ml:
                priority.append((3, m))
            elif "mistral" in ml or "phi" in ml or "gemma" in ml:
                priority.append((4, m))
            else:
                priority.append((5, m))

        priority.sort(key=lambda x: x[0])
        self.model = priority[0][1]
        logger.info("Auto-selected enrichment model (fallback): %s", self.model)
        return self.model

    async def enrich(self, text: str) -> EnrichmentResult:
        """Extract structured metadata from a single chunk of text.

        Returns an EnrichmentResult; on failure returns a default result
        with enriched=False so ingestion is never blocked.
        """
        model = await self._detect_model()
        truncated = text[:_MAX_CHUNK_TEXT]

        schema_str = json.dumps(ENRICHMENT_SCHEMA, indent=2)
        prompt_text = (
            EXTRACTION_PROMPT
            .replace("{schema}", schema_str)
            .replace("{text}", truncated)
            .replace("{org_context}", self._org_context)
        )

        for attempt in range(self.max_retries + 1):
            try:
                raw = await self._call_ollama(model, prompt_text)
                result = self._parse_response(raw, source_text=truncated)
                result.enrichment_model = model  # provenance tracking
                return result
            except Exception as exc:
                if attempt < self.max_retries:
                    logger.debug("Enrichment attempt %d failed: %s — retrying", attempt + 1, exc)
                    await asyncio.sleep(1)
                else:
                    logger.warning("Enrichment failed after %d attempts: %s", self.max_retries + 1, exc)

        return EnrichmentResult()  # graceful fallback

    async def enrich_batch(
        self,
        chunks: List[Dict[str, str]],
        *,
        concurrency: int = 1,
        on_progress: Any = None,
    ) -> List[EnrichmentResult]:
        """Enrich multiple chunks with optional progress callback.

        Parameters
        ----------
        chunks : list of dicts with at least a "text" key
        concurrency : max parallel enrichments (1 = sequential, safest for local GPU)
        on_progress : async callable(done, total, result) called after each chunk

        Returns list of EnrichmentResult in same order as input.
        """
        results: List[Optional[EnrichmentResult]] = [None] * len(chunks)
        sem = asyncio.Semaphore(concurrency)
        total = len(chunks)

        async def _process(idx: int, chunk: Dict[str, str]):
            async with sem:
                result = await self.enrich(chunk["text"])
                results[idx] = result
                if on_progress:
                    await on_progress(idx + 1, total, result)

        tasks = [_process(i, c) for i, c in enumerate(chunks)]
        await asyncio.gather(*tasks)
        return results  # type: ignore[return-value]

    async def _call_ollama(self, model: str, prompt: str) -> str:
        """Send a generation request to Ollama and return raw text."""
        session = await self._get_session()
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.1,   # Low temp for structured extraction
                "num_predict": 800,   # Metadata JSON is small
                "num_ctx": 4096,      # Enough room for chunk text + extraction prompt
                "repeat_penalty": 1.0, # No penalty needed for JSON extraction
                "top_k": 20,          # Tight sampling for deterministic structured output
                "top_p": 0.8,
            },
        }
        async with session.post(
            f"{self.endpoint}/api/generate",
            json=payload,
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Ollama error {resp.status}: {error[:200]}")
            data = await resp.json()
            return data.get("response", "")

    def _parse_response(self, raw: str, *, source_text: str = "") -> EnrichmentResult:
        """Parse LLM JSON output into an EnrichmentResult.

        Handles common LLM quirks: markdown fences, trailing commas, etc.
        Validates extracted fields against the source text to catch hallucinations.
        """
        text = raw.strip()
        # Strip markdown code fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # Find the JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object found in response: {text[:200]}")
        json_str = text[start:end]

        # Fix trailing commas (common LLM mistake)
        json_str = re.sub(r",\s*([}\]])", r"\1", json_str)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc} — raw: {json_str[:300]}") from exc

        confidence = _clamp_float(data.get("confidence", 0.5))

        # ── Anti-hallucination: validate entities/participants against source ──
        source_lower = source_text.lower() if source_text else ""
        if source_lower:
            # Only keep participants whose name actually appears in the source
            raw_participants = _as_str_list(data.get("participants", []))[:10]
            validated_participants = [
                p for p in raw_participants
                if p.lower() in source_lower or any(
                    word.lower() in source_lower
                    for word in p.split() if len(word) > 2
                )
            ]

            # Only keep entities that appear in the source text
            raw_entities = _as_str_list(data.get("entities", []))[:8]
            validated_entities = [
                e for e in raw_entities
                if e.lower() in source_lower or any(
                    word.lower() in source_lower
                    for word in e.split() if len(word) > 2
                )
            ]

            # If we filtered out a lot, reduce confidence
            total_raw = len(raw_participants) + len(raw_entities)
            total_validated = len(validated_participants) + len(validated_entities)
            if total_raw > 0 and total_validated < total_raw * 0.5:
                confidence = min(confidence, 0.3)
                logger.debug(
                    "Reduced confidence: %d/%d entities/participants validated",
                    total_validated, total_raw,
                )
        else:
            validated_participants = _as_str_list(data.get("participants", []))[:10]
            validated_entities = _as_str_list(data.get("entities", []))[:8]

        # Validate and normalise
        result = EnrichmentResult(
            summary=str(data.get("summary", ""))[:500],
            topics=_as_str_list(data.get("topics", []))[:5],
            content_type=_normalise_content_type(data.get("content_type", "")),
            participants=validated_participants,
            date_range=str(data.get("date_range", ""))[:30],
            actionability=_clamp_float(data.get("actionability", 0.5)),
            key_questions=_as_str_list(data.get("key_questions", []))[:4],
            entities=validated_entities,
            confidence=confidence,
            enriched=True,
            enriched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        return result

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _as_str_list(val: Any) -> List[str]:
    """Coerce a value to a list of non-empty strings."""
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    if isinstance(val, str) and val.strip():
        return [v.strip() for v in val.split(",") if v.strip()]
    return []


def _normalise_content_type(raw: str) -> str:
    val = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if val in VALID_CONTENT_TYPES:
        return val
    # Fuzzy match
    for valid in VALID_CONTENT_TYPES:
        if valid in val or val in valid:
            return valid
    return "unknown"


def _clamp_float(val: Any) -> float:
    try:
        f = float(val)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return 0.5


# ── Batch re-enrichment for existing ChromaDB data ──────────────────────────

class ReenrichmentJob:
    """Iterates all chunks in ChromaDB and enriches those lacking metadata.

    Tracks progress so it can be resumed if interrupted, and exposes
    a progress dict for the admin UI.
    """

    def __init__(self, enricher: ChunkEnricher | None = None):
        self.enricher = enricher or ChunkEnricher()
        self.progress: Dict[str, Any] = {
            "status": "idle",       # idle | running | completed | failed
            "total": 0,
            "done": 0,
            "skipped": 0,           # already enriched
            "failed": 0,
            "started_at": None,
            "elapsed_seconds": 0,
            "current_chunk": "",
            "eta_seconds": None,
        }
        self._cancel = False

    def cancel(self):
        self._cancel = True

    async def run(self, *, force: bool = False):
        """Enrich all un-enriched chunks in ChromaDB.

        Parameters
        ----------
        force : if True, re-enrich even already-enriched chunks
        """
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

        from core.chroma_factory import get_vectorstore

        self.progress["status"] = "running"
        self.progress["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        start_time = time.time()

        try:
            db = get_vectorstore()
            raw = db.get(include=["metadatas", "documents"])
            ids = raw["ids"]
            metas = raw["metadatas"]
            docs = raw["documents"]

            self.progress["total"] = len(ids)
            logger.info("Re-enrichment: %d chunks to process", len(ids))

            for i, (chunk_id, meta, text) in enumerate(zip(ids, metas, docs)):
                if self._cancel:
                    logger.info("Re-enrichment cancelled at %d/%d", i, len(ids))
                    self.progress["status"] = "cancelled"
                    break

                # Skip already-enriched unless forced
                if not force and meta.get("enriched"):
                    self.progress["skipped"] += 1
                    self.progress["done"] += 1
                    continue

                self.progress["current_chunk"] = chunk_id[:60]

                try:
                    result = await self.enricher.enrich(text or "")
                    if result.enriched:
                        # Update metadata only — no re-embedding needed
                        new_meta = dict(meta)
                        new_meta.update(result.to_metadata())
                        db._collection.update(
                            ids=[chunk_id],
                            metadatas=[new_meta],
                        )
                    else:
                        self.progress["failed"] += 1
                except Exception as exc:
                    logger.warning("Failed to enrich chunk %s: %s", chunk_id, exc)
                    self.progress["failed"] += 1

                self.progress["done"] += 1
                elapsed = time.time() - start_time
                self.progress["elapsed_seconds"] = round(elapsed)
                remaining = len(ids) - self.progress["done"]
                if self.progress["done"] - self.progress["skipped"] > 0:
                    per_chunk = elapsed / (self.progress["done"] - self.progress["skipped"])
                    self.progress["eta_seconds"] = round(remaining * per_chunk)

                # Small yield to keep event loop responsive
                if i % 5 == 0:
                    await asyncio.sleep(0)

            if self.progress["status"] == "running":
                self.progress["status"] = "completed"
            self.progress["elapsed_seconds"] = round(time.time() - start_time)
            logger.info(
                "Re-enrichment %s: %d done, %d skipped, %d failed in %.0fs",
                self.progress["status"],
                self.progress["done"],
                self.progress["skipped"],
                self.progress["failed"],
                self.progress["elapsed_seconds"],
            )
        except Exception as exc:
            logger.error("Re-enrichment failed: %s", exc)
            self.progress["status"] = "failed"
            self.progress["error"] = str(exc)
        finally:
            await self.enricher.close()


# Module-level singleton for the background job
_active_job: Optional[ReenrichmentJob] = None


def get_active_job() -> Optional[ReenrichmentJob]:
    return _active_job


async def start_reenrichment(*, force: bool = False, model: str | None = None) -> ReenrichmentJob:
    """Start a background re-enrichment job. Returns the job for progress tracking."""
    global _active_job
    if _active_job and _active_job.progress["status"] == "running":
        raise RuntimeError("A re-enrichment job is already running")

    enricher = ChunkEnricher(model=model)
    _active_job = ReenrichmentJob(enricher=enricher)

    async def _run():
        await _active_job.run(force=force)

    asyncio.get_event_loop().create_task(_run())
    return _active_job
