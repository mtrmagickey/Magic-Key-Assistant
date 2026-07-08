import os as _os
from pathlib import Path as _Path

# Pin tiktoken cache inside project to survive temp-dir clean-up
_os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR",
    str(_Path(__file__).resolve().parent.parent / ".tiktoken_cache"),
)

import aiohttp
import openai
import tiktoken


def count_tokens(text: str, model_name: str = "text-embedding-3-large") -> int:
    """Estimate token count using OpenAI's tokenizer."""
    encoding = tiktoken.encoding_for_model(model_name)
    return len(encoding.encode(text))

import asyncio
import hashlib
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

import discord
import pandas as pd
import regex as re
from discord import app_commands
from discord.ext import commands
from langchain_core.prompts import ChatPromptTemplate
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import allowed_channel_ids, directory_path, gpt_key, hash_csv, persist_directory

# Optional Chroma import - will work in degraded mode without it
try:
    from langchain_chroma import Chroma
    CHROMA_AVAILABLE = True
except ImportError:
    Chroma = None
    CHROMA_AVAILABLE = False
    logging.warning("="*60)
    logging.warning("⚠️  WARNING: langchain_chroma not installed.")
    logging.warning("   RAG features (document search) will be DISABLED.")
    logging.warning("   Install with: pip install langchain-chroma")
    logging.warning("="*60)
    logging.warning("langchain_chroma not installed. RAG features disabled.")

import json
import os

# Prompt template 
from pathlib import Path

from langchain_community.document_loaders import JSONLoader, PyPDFLoader, TextLoader, UnstructuredWordDocumentLoader
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI
from services.interaction_memory import _extract_keywords

# 3-Phase Pipeline Router
from services.model_router import BackendConfig, BackendType, ModelRouter, PipelineConfig, PipelineRole, RoleConfig

# ── Shared RAG utilities (no Discord deps — importable by web chat) ──────────
from services.rag_pipeline import (
    GAP_INDICATORS,
    SPARSE_CONTEXT_THRESHOLD,
    coerce_document,
    detect_knowledge_gap,
    extract_source_citations,
    filter_superseded_docs,
    format_docs_for_context,
    get_pipeline_router,
    run_retriever_query,
    set_pipeline_router,
)

# System prompt + bot name are loaded by the shared RAG pipeline module
from services.rag_pipeline import get_bot_name as _get_bot_name
from services.rag_pipeline import prompt as prompt
from services.rag_pipeline import template as template
from ux_helpers import ProgressCard, PublishView, create_error_embed, create_info_embed, create_success_embed

from .FeedbackView import ResponseFeedbackView

_bot_name = _get_bot_name()
# Retriever will be lazy-loaded on first use
# This prevents blocking imports and allows the bot to start even if OpenAI is temporarily unavailable

async def fetch_webpage_text(url):
    try:
        async with aiohttp.ClientSession() as session, session.get(url, timeout=10) as r:
            if r.status == 200:
                return await r.text()
        return ""
    except Exception:
        return ""

# API key validation moved to lazy init on first use to prevent blocking imports
logging.info("LLM.py import complete - resources will be initialized on first use.")

# Pipeline router — delegates to the shared singleton in services.rag_pipeline
_get_pipeline_router = get_pipeline_router  # alias for backward compat


# Add detailed prints to AskQuestion and reply logic for OpenAI debugging

async def AskQuestion(q, message_chain, user=None, web_url=None, retriever=None):
    logger.info(f"[AskQuestion] Called with question: {q}")
    history = ""
    topic_reset = _detect_topic_reset(q)
    if message_chain:
        # Create a copy to avoid modifying the original list in place if it's reused
        chain_copy = list(message_chain)
        if chain_copy:
            logger.debug("[AskQuestion] message_chain exists, popping first element if present.")
            chain_copy.pop(0)
        if chain_copy and not topic_reset:
            logger.debug("[AskQuestion] Building history from message_chain.")
            history = _build_history_from_chain(list(reversed(chain_copy)), topic_reset=False)
    
    web_content = ""
    if web_url:
        logger.info(f"[AskQuestion] Fetching web content from {web_url}")
        web_content = await fetch_webpage_text(web_url)
    
    logger.info("[AskQuestion] Retrieving relevant documents for context...")
    # run_retriever_query is synchronous, but usually fast if just vector search. 
    # Ideally this should be async too, but Chroma client might be sync.
    # We'll wrap it in to_thread just in case it blocks.
    docs = await asyncio.to_thread(run_retriever_query, retriever, q)
    
    # Filter and format docs (same quality as Discord /ask)
    filtered_docs = filter_superseded_docs(docs)
    context_text = format_docs_for_context(filtered_docs)
    if filtered_docs and not _context_matches_query(q, filtered_docs):
        logger.info("[AskQuestion] Dropping context due to topic mismatch.")
        filtered_docs = []
        context_text = ""
    
    # Build the full context including history and web content
    full_context = context_text
    if web_content:
        full_context = f"[Web Content]\n{web_content}\n\n{full_context}"
    if history:
        full_context = f"[Conversation History]\n{history}\n\n{full_context}"
    
    # Try to use 3-phase pipeline if configured
    try:
        router = await _get_pipeline_router()
        if router and router.pipeline:
            logger.info("[AskQuestion] Using 3-phase synthesis pipeline...")
            
            # Get system prompt (the template without placeholders)
            system_prompt_text = template.replace("{context}", "").replace("{question}", "").replace("{history}", "").replace("{user}", user or "unknown-user")
            
            result = await router.generate_pipeline(
                user_prompt=q,
                context=full_context,
                system_prompt=system_prompt_text,
            )
            
            logger.info(f"[AskQuestion] Pipeline complete. Models used: {result.get('models_used', {})}")
            return result["final"]
    except Exception as e:
        logger.warning(f"[AskQuestion] Pipeline failed, falling back to single model: {e}")
    
    # Fallback: single-model approach (gpt-4o-mini)
    logger.info("[AskQuestion] Using single-model fallback (gpt-4o-mini)...")
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2, api_key=gpt_key)
    model = model.bind(max_tokens=4000)
    
    try:
        input_dict = {
            "context": context_text,
            "question": q,
            "history": (web_content + "\n" if web_content else "") + history,
            "user": user or "unknown-user"
        }
        logger.debug("[AskQuestion] Input dict ready.")
        logger.info("[AskQuestion] Invoking LLM chain...")
        chain = prompt | model | StrOutputParser()
        result = await chain.ainvoke(input_dict)
        logger.info(f"[AskQuestion] LLM chain invocation complete. Result length: {len(result)}")
        return result
    except Exception as e:
        logger.error(f"[AskQuestion] ERROR during LLM call: {e}")
        return f"[LLM ERROR] {e}"

#Discord has length limit so break the message
def split_embed_content(text, max_length=2000):
    parts = []
    while len(text) > max_length:
        split_index = text.rfind('\n', 0, max_length)
        if split_index == -1:
            split_index = max_length
        part = text[:split_index].strip()
        parts.append(part)
        text = text[split_index:].strip()
    if text:
        parts.append("...\n"+text)
    return parts

# Use module logger (centralized logging configured in leisureLLM.py)
logger = logging.getLogger(__name__)


async def _is_owner_interaction(interaction: discord.Interaction) -> bool:
    try:
        return await interaction.client.is_owner(interaction.user)
    except Exception:
        return False

logger.info("[BOOT] LLM.py loaded - using centralized logging.")

logger.info("[BOOT] LLM.py loaded - using centralized logging.")


class DiscordConnectionLogger(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_disconnect_time = None
        self.downtime_file = 'discord_downtime.log'

    @commands.Cog.listener()
    async def on_connect(self):
        logging.debug(f"[Discord] on_connect event. last_disconnect_time: {self.last_disconnect_time}")
        # Try to load downtime from file if in-memory is None
        if self.last_disconnect_time is None:
            try:
                with open(self.downtime_file, 'r') as f:
                    ts = float(f.read().strip())
                    self.last_disconnect_time = ts
                    logging.debug(f"[Discord] Loaded last_disconnect_time from file: {ts}")
            except Exception as e:
                logger.warning("on_connect: suppressed %s", e)
        if self.last_disconnect_time:
            downtime = time.time() - self.last_disconnect_time
            logging.info(f"[Discord] Bot connected to Discord gateway. Downtime was {downtime:.2f} seconds.")
            self.last_disconnect_time = None
            try:
                os.remove(self.downtime_file)
            except Exception as e:
                logger.warning("on_connect: suppressed %s", e)
        else:
            logging.info("[Discord] Bot connected to Discord gateway.")

    @commands.Cog.listener()
    async def on_disconnect(self):
        self.last_disconnect_time = time.time()
        logging.warning(f"[Discord] Bot disconnected from Discord gateway. last_disconnect_time set to {self.last_disconnect_time}")
        # Persist disconnect time to file
        try:
            with open(self.downtime_file, 'w') as f:
                f.write(str(self.last_disconnect_time))
        except Exception as e:
            logging.error(f"[Discord] Failed to write downtime file: {e}")

    @commands.Cog.listener()
    async def on_resumed(self):
        logging.debug(f"[Discord] on_resumed event. last_disconnect_time: {self.last_disconnect_time}")
        # Try to load downtime from file if in-memory is None
        if self.last_disconnect_time is None:
            try:
                with open(self.downtime_file, 'r') as f:
                    ts = float(f.read().strip())
                    self.last_disconnect_time = ts
                    logging.debug(f"[Discord] Loaded last_disconnect_time from file: {ts}")
            except Exception as e:
                logger.warning("on_resumed: suppressed %s", e)
        if self.last_disconnect_time:
            downtime = time.time() - self.last_disconnect_time
            logging.info(f"[Discord] Bot resumed session with Discord gateway. Downtime was {downtime:.2f} seconds.")
            self.last_disconnect_time = None
            try:
                os.remove(self.downtime_file)
            except Exception as e:
                logger.warning("on_resumed: suppressed %s", e)
        else:
            logging.info("[Discord] Bot resumed session with Discord gateway.")

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info(f"[Discord] Bot ready as {self.bot.user} (ID: {self.bot.user.id})")


class AskReplyModal(discord.ui.Modal, title="Reply to Assistant"):
    reply_input = discord.ui.TextInput(
        label="Your Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Ask a follow-up or clarify...",
        required=True,
        max_length=2000
    )

    def __init__(self, view, interaction: discord.Interaction):
        super().__init__()
        self.view_ref = view
        self.interaction_ref = interaction

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.view_ref.handle_reply(interaction, str(self.reply_input.value))


class AskConversationView(discord.ui.View):
    def __init__(self, cog, question: str, answer: str, user_id: int):
        super().__init__(timeout=900)  # 15 min timeout
        self.cog = cog
        self.history = f"User: {question}\nAssistant: {answer}\n"
        self.user_id = user_id
        self.turn_count = 1

    @discord.ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="💬")
    async def reply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Start your own conversation with /ask.", ephemeral=True)
            return
        await interaction.response.send_modal(AskReplyModal(self, interaction))

    @discord.ui.button(label="Remember Summary", style=discord.ButtonStyle.secondary, emoji="🧠")
    async def remember_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        doc_cog = interaction.client.get_cog("DocumentAuthor")
        if doc_cog:
            await interaction.response.defer(ephemeral=True)
            # Use LLM to summarize
            summary_prompt = f"Summarize this conversation for the knowledge base:\n{self.history}"
            summary, _srcs = await self.cog.safe_llm_call(summary_prompt, None, user="System")
            
            await doc_cog._remember_logic(
                interaction, 
                summary, 
                author_name=interaction.user.display_name, 
                title=f"Conversation Summary {datetime.now().strftime('%Y-%m-%d')}" 
            )
        else:
             await interaction.followup.send("Memory system not loaded.", ephemeral=True)

    async def handle_reply(self, interaction: discord.Interaction, reply: str):
        self.history += f"User: {reply}\n"
        
        # Route follow-up through the full RAG pipeline so the reply
        # gets the same retrieval, system prompt, and pipeline quality
        # as the initial /ask.  Pass conversation history as context.
        answer, _srcs = await self.cog.safe_llm_call(
            reply,
            self.history,
            getattr(interaction.user, "display_name", "User"),
            user_id=interaction.user.id
        )
        
        if not answer:
            await interaction.followup.send("Failed to generate reply.", ephemeral=True)
            return

        self.history += f"Assistant: {answer}\n"
        self.turn_count += 1
        
        parts = split_embed_content(answer)
        embed = create_info_embed(title=_bot_name, description=parts[0])
        
        if self.turn_count >= 3:
            embed.set_footer(text="Tip: Click 'Remember Summary' to save this conversation.")
        
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)
        for part in parts[1:]:
             await interaction.followup.send(part, ephemeral=True)


# ---------------------------------------------------------------------------
# Intent detection for /ask — routes statements to the right command
# ---------------------------------------------------------------------------
_ACTION_CLUES = [
    "need to", "have to", "should", "must", "remind me", "todo",
    "by friday", "by monday", "by end of", "deadline", "assign",
    "follow up", "follow-up", "don't forget", "make sure",
]
_DECISION_CLUES = [
    "we decided", "agreed to", "going with", "the plan is",
    "decision:", "resolved to", "committed to", "verdict is",
]
_MEETING_CLUES = [
    "meeting notes", "attendees:", "agenda:", "minutes from",
    "discussed with", "met with", "in today's meeting",
]


def _detect_non_question_intent(text: str) -> Optional[str]:
    """Fast keyword scan — returns 'action', 'decision', 'meeting', 'memory', or None."""
    low = text.lower()
    # If it ends with a question mark or starts with a question word, it's a question
    if low.rstrip().endswith("?"):
        return None
    q_words = ("what", "how", "why", "when", "where", "who", "which", "is ", "are ", "can ", "does ", "do ")
    if any(low.lstrip().startswith(w) for w in q_words):
        return None

    for clue in _DECISION_CLUES:
        if clue in low:
            return "decision"
    for clue in _ACTION_CLUES:
        if clue in low:
            return "action"
    for clue in _MEETING_CLUES:
        if clue in low:
            return "meeting"

    # If it's a long statement (not a question), suggest saving as memory
    if len(text) > 120 and "?" not in text:
        return "memory"

    return None


# ---------------------------------------------------------------------------
# Web search intent detection — proactive, not just sparse-context fallback
# ---------------------------------------------------------------------------
_WEB_SEARCH_CLUES = [
    # Temporal / recency signals
    "latest", "current", "recent", "today", "this year", "this month",
    "2024", "2025", "2026",
    "right now", "at the moment", "as of", "up to date",
    "has anything changed", "new rules", "new guidance",
    # External knowledge / benchmarking
    "industry standard", "best practice", "regulation", "competitor",
    "market rate", "benchmark", "typical", "average",
    "what are other", "how do other", "compared to",
    "what do other", "how does the sector", "in the sector",
    "outside our", "elsewhere", "other organisations", "other centers",
    "other centres", "other leisure", "other pools", "other gyms",
    # News / updates
    "news about", "update on", "changes to",
    "any developments", "what's happening with",
    # Pricing / commercial
    "pricing", "cost of", "how much does", "going rate",
    "salary", "wage", "pay scale", "pay rate",
    # Regulatory / compliance
    "legislation", "compliance", "health and safety",
    "ofsted", "quest", "cimspa", "sport england",
    "rlss", "swim england", "ukactive", "uk active",
    "hse", "cqc", "government", "council",
    # Discovery / external lookup
    "who offers", "where can i find", "is there a",
    "what software", "what system", "what platform",
    "recommend", "alternative", "option for", "options for",
    "supplier", "vendor", "provider",
    # General knowledge the corpus likely lacks
    "how to", "what is the best way", "standard approach",
    "example of", "template for", "framework for",
    "definition of", "what does .* mean",
    "explain", "difference between",
]


def _needs_web_search(question: str) -> bool:
    """Detect if a question semantically needs current/external information.

    Returns True when the question contains signals that web search would
    help, even if RAG context is plentiful.  This makes web augmentation
    *proactive* rather than purely a sparse-context fallback.
    """
    low = question.lower()
    return any(clue in low for clue in _WEB_SEARCH_CLUES)


# ---------------------------------------------------------------------------
# Outward vs. inward question classification (#4)
# ---------------------------------------------------------------------------
# Outward = about the world (standards, regulations, market, general knowledge)
# Inward  = about the org (our team, our projects, our decisions)
# When a question is outward-facing AND corpus context is weak, we should
# aggressively prefer web search rather than stretching poor corpus matches.

_OUTWARD_CLUES = [
    # Regulatory / industry external
    "regulation", "legislation", "compliance", "law ", "legal requirement",
    "government", "council", "ofsted", "hse", "cqc",
    "industry standard", "best practice", "benchmark",
    # Market / external comparison
    "market rate", "going rate", "competitor", "other organisations",
    "other centers", "other centres", "other leisure", "how do other",
    "what do other", "compared to", "in the sector", "sector average",
    # General knowledge / how-to
    "how to", "what is the best way", "explain", "definition of",
    "difference between", "what does", "general advice",
    "standard approach", "framework for", "template for",
    # Pricing / salary benchmarking
    "salary", "wage", "pay scale", "pay rate", "cost of",
    "pricing", "going rate", "market price",
    # External discovery
    "who offers", "where can i find", "supplier", "vendor", "provider",
    "software", "platform", "alternative", "recommend",
    # News / current affairs
    "news about", "update on", "what's happening with",
    "latest research", "recent study", "recent report",
]

_INWARD_CLUES = [
    # Org self-references
    "our ", "we ", "us ", "my ", "the team",
    "our team", "our project", "our decision", "our plan",
    "who on our", "who in our", "did we", "have we",
    "what did we decide", "what's our", "internally",
    # Specific project / people references are usually inward but
    # can't be enumerated — keyword overlap in retrieval handles those.
]


def _is_outward_question(question: str) -> bool:
    """Classify a question as outward-facing (about the external world).

    Outward questions should prefer web search over corpus when context
    is weak, since the corpus was never designed to cover them.
    Returns False if strong inward signals are present (even if
    outward clues also match), because the user likely wants org-specific
    context for an external question.
    """
    low = question.lower()
    has_inward = any(clue in low for clue in _INWARD_CLUES)
    has_outward = any(clue in low for clue in _OUTWARD_CLUES)
    # If both signals present, defer to inward (user wants org context
    # applied to an external topic — e.g. "how does our pay compare")
    if has_inward and has_outward:
        return False
    return has_outward


# ---------------------------------------------------------------------------
# Question-type routing hints (#6 extension)
# ---------------------------------------------------------------------------
# Detect question categories and inject LLM hints to prioritise the right
# document metadata types (team_bio, decision, strategy, etc.)

_PEOPLE_CLUES = [
    "who ", "who's", "who is", "who are", "who was", "who were",
    "founding", "founder", "partner", "team member", "team lead",
    "colleague", "staff", "employee", "person", "people",
    "tell me about ", "background on", "bio ",
    "manager", "director", "ceo", "cto", "coo", "cfo",
]

_DECISION_CLUES = [
    "what did we decide", "what was decided", "decision about",
    "agreed on", "signed off", "approved", "committed to",
    "resolution", "outcome of", "conclusion",
]

_STRATEGY_CLUES = [
    "strategy", "roadmap", "plan for", "vision", "goals",
    "objectives", "priorities", "initiative", "long-term",
]


def _build_question_type_hint(question: str) -> str:
    """Generate a document-type priority hint for the LLM.

    Detects question categories (people, decisions, strategy) and tells
    the LLM which enriched metadata types to prioritise in the retrieved
    documents.  Returns empty string if no special routing is needed.
    """
    low = question.lower()
    hints = []

    if any(clue in low for clue in _PEOPLE_CLUES):
        hints.append(
            "This question is about PEOPLE or TEAM MEMBERS. "
            "Prioritise documents tagged as 'team_bio', 'decision', or "
            "'meeting_notes' — these are most likely to contain names, "
            "roles, and biographical details. Look for specific names "
            "rather than Discord handles or usernames."
        )

    if any(clue in low for clue in _DECISION_CLUES):
        hints.append(
            "This question is about a DECISION. Prioritise documents "
            "tagged as 'decision' or 'meeting_notes' and look for "
            "explicit conclusions, sign-offs, or commitments."
        )

    if any(clue in low for clue in _STRATEGY_CLUES):
        hints.append(
            "This question is about STRATEGY or PLANNING. Prioritise "
            "documents tagged as 'strategy', 'product_spec', or "
            "'project_proposal'."
        )

    if not hints:
        return ""

    return "[QUESTION TYPE HINT: " + " ".join(hints) + "]"


# Chroma L2 distance threshold — higher values mean the retrieved chunks
# are more distant from the query embedding.  Above this, even if we have
# plenty of text, the content probably doesn't address the actual question.
# Lowered from 1.4 → 1.2 so weak corpus matches don't block web search.
_RETRIEVAL_RELEVANCE_THRESHOLD = 1.2

_MAX_HISTORY_TURNS = 8
_HISTORY_SUMMARY_MAX_CHARS = 800

_TOPIC_RESET_CLUES = [
    "we're talking about",
    "we are talking about",
    "not talking about",
    "not about",
    "i mean",
    "i meant",
    "no, i mean",
    "no i mean",
    "actually, i meant",
    "correction",
    "wrong topic",
    "different topic",
    "not that",
]

_CONTEXT_KEYWORD_MAX_DOCS = 6
_CONTEXT_KEYWORD_MAX_CHARS_PER_DOC = 800
_CONTEXT_KEYWORD_RATIO_THRESHOLD = 0.20


def _context_is_relevant(docs) -> bool:
    """Check whether retrieved documents are semantically relevant.

    Uses the retrieval_score (Chroma L2 distance) embedded in document
    metadata by HyDE retrieval.  If the best (lowest) score exceeds
    the threshold, the context is probably about the wrong topic —
    web search should fire to supplement.

    Returns True if context looks relevant, False if it's weak.
    """
    if not docs:
        return False

    scores = [
        d.metadata.get("retrieval_score", 999)
        for d in docs
        if hasattr(d, "metadata") and d.metadata
    ]
    if not scores:
        return True  # No scores available — assume OK

    best = min(scores)
    # Also check median of top-3 to avoid a single lucky hit masking
    # an otherwise poor retrieval set.
    top3 = sorted(scores)[:3]
    avg_top3 = sum(top3) / len(top3) if top3 else 999

    relevant = best < _RETRIEVAL_RELEVANCE_THRESHOLD and avg_top3 < _RETRIEVAL_RELEVANCE_THRESHOLD + 0.3
    if not relevant:
        logger.debug(
            "Context relevance check FAILED: best=%.3f, avg_top3=%.3f (threshold=%.1f)",
            best, avg_top3, _RETRIEVAL_RELEVANCE_THRESHOLD,
        )
    return relevant


# ---------------------------------------------------------------------------
# Uncertainty admission — borderline retrieval quality marker (#5)
# ---------------------------------------------------------------------------
_BORDERLINE_LOW = 0.9   # below this = confident match
_BORDERLINE_HIGH = 1.2  # above this = weak (already triggers web search)


def _build_retrieval_confidence_note(docs) -> str:
    """Return a context-injection note when retrieval quality is borderline.

    When the best retrieval score is between _BORDERLINE_LOW and
    _BORDERLINE_HIGH, the context *might* be relevant but we're not sure.
    Injecting a note tells the LLM to hedge and consider web/general
    knowledge rather than blindly trusting borderline chunks.

    Returns an empty string when scores are clearly good or clearly bad.
    """
    if not docs:
        return ""

    scores = [
        d.metadata.get("retrieval_score", 999)
        for d in docs
        if hasattr(d, "metadata") and d.metadata
    ]
    if not scores:
        return ""

    best = min(scores)
    if best < _BORDERLINE_LOW or best >= _BORDERLINE_HIGH:
        return ""  # confident or already flagged as weak

    return (
        "[RETRIEVAL NOTE: The retrieved documents are a borderline match "
        "for this question. Still use any relevant facts from them — "
        "they may contain the answer even if the overall topic seems "
        "tangential. Only supplement with web results if the documents "
        "genuinely don't address the question after careful reading.]"
    )


def _detect_topic_reset(text: str) -> bool:
    low = (text or "").lower()
    if any(clue in low for clue in _TOPIC_RESET_CLUES):
        return True
    if re.search(r"\bnot\s+\w+\s+but\b", low):
        return True
    return False


def _summarize_history_lines(lines: list[str]) -> str:
    if not lines:
        return ""

    text = " ".join(lines)
    topics = _extract_keywords(text, max_keywords=6)
    parts = []
    if topics:
        parts.append("Topics: " + ", ".join(topics))

    last_user = next((l for l in reversed(lines) if l.lower().startswith("user:")), "")
    if last_user:
        parts.append("Last user: " + last_user.split(":", 1)[-1].strip()[:160])

    last_assistant = next((l for l in reversed(lines) if l.lower().startswith("assistant:")), "")
    if last_assistant:
        parts.append("Last assistant: " + last_assistant.split(":", 1)[-1].strip()[:160])

    summary = " | ".join(parts).strip()
    return summary[:_HISTORY_SUMMARY_MAX_CHARS]


def _build_history_from_chain(message_chain, topic_reset: bool) -> str:
    if topic_reset or not message_chain:
        return ""
    if not isinstance(message_chain, list):
        return str(message_chain)

    lines = [str(l).strip() for l in message_chain if str(l).strip()]
    if len(lines) <= _MAX_HISTORY_TURNS:
        return "\n".join(lines)

    earlier = lines[:-_MAX_HISTORY_TURNS]
    recent = lines[-_MAX_HISTORY_TURNS:]
    summary = _summarize_history_lines(earlier)
    parts = []
    if summary:
        parts.append("[Earlier Summary]\n" + summary)
    parts.extend(recent)
    return "\n".join(parts)


def _context_matches_query(question: str, docs) -> bool:
    """Legacy boolean wrapper — kept for backward compat."""
    return _context_relevance_score(question, docs) >= 0.15


def _context_relevance_score(question: str, docs) -> float:
    """Return 0.0–1.0 score measuring how well docs cover the question.

    The score is the fraction of question keywords found in the top docs.
    Entity-aware keywords (multi-word proper nouns) are weighted 2× because
    missing a named entity is more damaging than missing a generic word.
    """
    keywords = _extract_keywords(question or "", max_keywords=8)
    if not keywords:
        return 1.0  # nothing to check against

    if not docs:
        return 0.0

    snippets = []
    for doc in docs[:_CONTEXT_KEYWORD_MAX_DOCS]:
        text = (getattr(doc, "page_content", "") or "")[:_CONTEXT_KEYWORD_MAX_CHARS_PER_DOC]
        if text:
            snippets.append(text)

    if not snippets:
        return 0.0

    doc_low = " ".join(snippets).lower()

    # Weighted overlap: multi-word phrases count double
    total_weight = 0.0
    matched_weight = 0.0
    for kw in keywords:
        is_entity = " " in kw  # multi-word = entity phrase
        weight = 2.0 if is_entity else 1.0
        total_weight += weight
        if kw in doc_low:
            matched_weight += weight

    return matched_weight / total_weight if total_weight > 0 else 1.0


def _build_context_quality_note(score: float) -> str:
    """Return a graded LLM instruction based on context relevance score."""
    if score >= 0.6:
        return ""  # good match, no note needed
    if score >= 0.3:
        return (
            "[NOTE: The retrieved documents partially match your question — "
            "some key terms weren't found in the documents. Use whatever "
            "relevant facts they contain, and note any gaps.]"
        )
    if score > 0.0:
        return (
            "[NOTE: The retrieved documents have low overlap with the question's "
            "key terms. They may still contain tangentially useful information. "
            "Prioritise any directly relevant facts but be transparent about "
            "what the documents don't cover.]"
        )
    return (
        "[NOTE: The retrieved documents do not appear to match the question. "
        "Rely on web search results or general knowledge instead, and state "
        "clearly that the knowledge base did not have relevant content.]"
    )


# ---------------------------------------------------------------------------
# Generic answer suppression (#10)
# ---------------------------------------------------------------------------
# Post-generation check: does the response actually address the question's
# key terms?  If not, it's likely a generic/hallucinated answer that sounds
# plausible but doesn't help.
_GENERIC_OVERLAP_THRESHOLD = 0.20  # at least 20% of question keywords in response

# Common preamble phrases that indicate a generic non-answer
_GENERIC_PHRASES = [
    "i don't have specific information",
    "i couldn't find specific",
    "based on general knowledge",
    "in general terms",
    "without specific context",
    "i'm not sure exactly",
    "it's difficult to say",
    "it depends on many factors",
]


def _is_generic_answer(question: str, response: str) -> bool:
    """Detect whether a response is a generic non-answer to the question.

    Returns True when:
    - The response doesn't mention enough of the question's key terms, OR
    - The response is dominated by generic hedge phrases

    Used to trigger a post-hoc web search retry or confidence flag.
    """
    if not question or not response:
        return False

    # Short responses are likely direct answers
    if len(response.split()) < 20:
        return False

    # Check for generic hedge phrases
    resp_low = response.lower()
    generic_count = sum(1 for p in _GENERIC_PHRASES if p in resp_low)
    if generic_count >= 2:
        return True

    # Check keyword overlap
    keywords = _extract_keywords(question, max_keywords=6)
    if not keywords or len(keywords) < 2:
        return False  # too few keywords to judge

    overlap = sum(1 for kw in keywords if kw in resp_low)
    ratio = overlap / len(keywords)
    if ratio < _GENERIC_OVERLAP_THRESHOLD:
        logger.debug(
            "Generic answer detected: %.0f%% keyword overlap (%d/%d)",
            ratio * 100, overlap, len(keywords),
        )
        return True

    return False


class _IntentRouteView(discord.ui.View):
    """One-click buttons to route /ask input to /remember or /action add."""

    def __init__(self, *, intent: str, content: str, user_id: int, bot):
        super().__init__(timeout=180)
        self.intent = intent
        self.content = content
        self.user_id = user_id
        self.bot = bot

    @discord.ui.button(label="Yes, save it", style=discord.ButtonStyle.success, emoji="✅")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the original user can do this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        # Route to DocumentAuthor._remember_logic which handles all types
        doc_cog = self.bot.get_cog("DocumentAuthor")
        if doc_cog and hasattr(doc_cog, "_remember_logic"):
            try:
                await doc_cog._remember_logic(
                    interaction,
                    self.content,
                    interaction.user.display_name,
                )
                # _remember_logic sends its own followup, so disable buttons
                for item in self.children:
                    item.disabled = True
                try:
                    await interaction.edit_original_response(view=self)
                except Exception as e:
                    logger.warning("save_btn: suppressed %s", e)
                self.stop()
                return
            except Exception as exc:
                logging.warning("Intent route to /remember failed: %s", exc)

        await interaction.followup.send(
            "⚠️ Couldn't route — try `/remember` directly.",
            ephemeral=True,
        )

    @discord.ui.button(label="No thanks", style=discord.ButtonStyle.secondary)
    async def dismiss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class LLM(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.allowed_channel_ids = allowed_channel_ids
        
        # Lazy-loaded resources
        self._retriever = None
        self._embeddings = None
        self._openai_validated = False
        
        logger.info("[LLM Cog] Initialized.")

    def _channel_is_allowed(self, channel) -> bool:
        if not self.allowed_channel_ids:
            return True
        if channel is None:
            return False
        channel_id = getattr(channel, "id", None)
        if channel_id in self.allowed_channel_ids:
            return True
        parent_id = getattr(channel, "parent_id", None)
        if parent_id in self.allowed_channel_ids:
            return True
        parent = getattr(channel, "parent", None)
        if parent and getattr(parent, "id", None) in self.allowed_channel_ids:
            return True
        return False
    
    def _ensure_retriever(self):
        """Lazy-load retriever and vectorstore on first use"""
        if self._retriever is None:
            if not CHROMA_AVAILABLE:
                logger.warning("Chroma not available - RAG features disabled")
                return None
                
            logger.info("Initializing Chroma retriever (first use)...")
            from core.chroma_factory import get_vectorstore
            self._vectorstore = get_vectorstore()
            self._retriever = self._vectorstore.as_retriever(search_kwargs={"k": 20})
            logger.info("Chroma retriever initialized successfully")
        return self._retriever
    
    def _filter_superseded_docs(self, docs):
        """Delegate to module-level filter_superseded_docs."""
        return filter_superseded_docs(docs)

    def _format_docs_for_context(self, docs, max_chars: int = 18000) -> str:
        """Delegate to module-level format_docs_for_context."""
        return format_docs_for_context(docs, max_chars)
    
    async def _validate_openai_once(self):
        """Validate API key once, non-blocking"""
        if not self._openai_validated:
            try:
                await asyncio.to_thread(openai.Model.list)
                self._openai_validated = True
                logger.info("OpenAI API key validated")
            except Exception as e:
                logger.error(f"OpenAI validation failed: {e}")
    
    async def safe_llm_call(self, question: str, message_chain: str = "", user: str = None, web_url: str = None, timeout: int = 45, user_id: int = None, channel_name: str = None, use_pipeline: bool = False, status_callback=None) -> tuple[str, list[str]]:
        """LLM call with timeout and cancellation.

        Returns ``(answer_text, chunk_source_paths)`` so callers can pass
        chunk sources to the feedback view without shared mutable state.
        Set *use_pipeline=True* for 3-phase synthesis.

        *status_callback* is an optional ``async def(status_text: str)``
        that gets called with real-time progress updates (e.g. "Searching
        knowledge base…").  Callers can use this to update a Discord
        message or other UI element.
        """
        async def _emit_status(text: str):
            if status_callback:
                try:
                    await status_callback(text)
                except Exception as e:
                    logger.warning("_emit_status: suppressed %s", e)

        chunk_sources: list[str] = []
        try:
            # Get retriever (also initialises self._vectorstore)
            await _emit_status("🔍 Searching knowledge base…")
            retriever = self._ensure_retriever()

            # ── Start workspace context fetch in parallel with retrieval ──
            # Workspace context (live operational state) is independent of
            # retrieval — start it now so it overlaps with the vectorstore
            # queries rather than waiting until after.
            ws_ctx_task: asyncio.Task | None = None
            try:
                db = getattr(self.bot, "db", None)
                if db:
                    from services.workspace_context import get_workspace_context_builder

                    async def _fetch_ws_ctx():
                        try:
                            ws_builder = get_workspace_context_builder(db)
                            return await ws_builder.get_context()
                        except Exception as exc:
                            logger.debug("Workspace context unavailable for Discord: %s", exc)
                            return ""

                    ws_ctx_task = asyncio.create_task(_fetch_ws_ctx())
            except Exception as e:
                logger.warning("_fetch_ws_ctx: suppressed %s", e)
            
            if retriever is None:
                # No RAG available - direct LLM call
                logger.warning("No retriever available - using LLM without context")
                filtered_docs = []
                context = ""
            else:
                # HyDE retrieval — search with both original question
                # and a hypothetical answer for better recall
                from services.hyde_retrieval import hyde_retrieve, make_generate_fn_from_router

                generate_fn = None
                try:
                    router = await _get_pipeline_router()
                    if router:
                        generate_fn = make_generate_fn_from_router(router)
                except Exception:
                    pass  # fall back to standard retrieval

                # ── Query decomposition (multi-hop planning) ─────────
                # Complex / multi-part questions are broken into targeted
                # sub-queries so retrieval covers all facets.
                sub_queries = None
                try:
                    from services.query_planner import decompose_query
                    sub_queries = await decompose_query(question, generate_fn=generate_fn)
                except Exception as exc:
                    logger.debug("Query decomposition skipped: %s", exc)

                raw_docs = await hyde_retrieve(
                    self._vectorstore,
                    question,
                    generate_fn=generate_fn,
                    k=20,
                    sub_queries=sub_queries,
                )
                filtered_docs = self._filter_superseded_docs(raw_docs)

                # ── Cross-encoder reranking ──────────────────────────
                try:
                    from services.reranker import rerank_documents
                    filtered_docs = await rerank_documents(question, filtered_docs, top_n=12)
                except Exception as exc:
                    logger.debug("Reranking skipped: %s", exc)

                # ── Self-Correcting Retrieval ────────────────────────
                # When initial retrieval is sparse, reformulate the query
                # and retry with alternative phrasings for better recall.
                initial_word_count = sum(
                    len(d.page_content.split()) for d in filtered_docs
                ) if filtered_docs else 0
                if initial_word_count < 80 or len(filtered_docs) < 3:
                    try:
                        from services.self_correcting_retrieval import corrective_retrieve
                        filtered_docs = await corrective_retrieve(
                            self._vectorstore,
                            question,
                            initial_docs=filtered_docs,
                            initial_context_words=initial_word_count,
                            generate_fn=generate_fn,
                        )
                    except Exception as exc:
                        logger.debug("Corrective retrieval skipped: %s", exc)

                # ── Evidence evaluation + gap retrieval ──────────────
                # LLM evaluates which docs are relevant and identifies
                # gaps — then we run targeted retrieval for those gaps.
                _evidence_note = ""
                try:
                    from services.evidence_evaluator import evaluate_evidence
                    eval_result = await evaluate_evidence(
                        question, filtered_docs, generate_fn=generate_fn,
                    )
                    if eval_result.evaluated:
                        _evidence_note = eval_result.evaluation_note
                        # Iterative gap retrieval
                        if eval_result.gap_queries:
                            from services.hyde_retrieval import gap_retrieve
                            gap_docs = await gap_retrieve(
                                self._vectorstore,
                                eval_result.gap_queries,
                                existing_docs=filtered_docs,
                            )
                            if gap_docs:
                                filtered_docs = filtered_docs + gap_docs
                                logger.info(
                                    "Gap retrieval added %d docs for gaps: %s",
                                    len(gap_docs),
                                    eval_result.gap_queries,
                                )
                except Exception as exc:
                    logger.debug("Evidence evaluation skipped: %s", exc)

            # ── Graduated context relevance scoring ────────────────
            _relevance_score = _context_relevance_score(question, filtered_docs) if filtered_docs else 0.0
            _quality_note = _build_context_quality_note(_relevance_score) if filtered_docs else ""
            if _relevance_score < 0.15 and filtered_docs:
                logger.info("[safe_llm_call] Context relevance very low (%.2f) — keeping docs but flagging.", _relevance_score)
            elif _quality_note:
                logger.debug("[safe_llm_call] Context relevance score: %.2f — injecting note.", _relevance_score)
            
            # Capture chunk sources for feedback loop (local, not self)
            chunk_sources = list({
                (d.metadata.get("source_relpath") or d.metadata.get("source") or "")
                for d in filtered_docs if d.metadata
            })
            chunk_sources = [s for s in chunk_sources if s]

            # Check if context is sparse - potential knowledge gap
            context = self._format_docs_for_context(filtered_docs) if filtered_docs else ""
            context_quality = len(context.split()) if context else 0

            # Inject graduated quality note (does NOT drop docs)
            if _quality_note:
                context = _quality_note + "\n\n" + context if context else _quality_note

            # ── Evidence evaluation note ─────────────────────────────
            # LLM-evaluated evidence assessment — more nuanced than
            # keyword-only scoring.  Identifies which docs are relevant
            # and what gaps remain.
            if _evidence_note:
                context = _evidence_note + "\n\n" + context if context else _evidence_note

            # ── Question-type routing hint ───────────────────────────
            # Tell the LLM which doc types to prioritise for this
            # question category (people, decisions, strategy).
            _qtype_hint = _build_question_type_hint(question)
            if _qtype_hint:
                context = _qtype_hint + "\n\n" + context if context else _qtype_hint

            # ── Uncertainty admission (#5) ───────────────────────────
            # When retrieval scores are borderline, inject a note so the
            # LLM knows to hedge rather than blindly trusting weak docs.
            confidence_note = _build_retrieval_confidence_note(filtered_docs)
            if confidence_note:
                context = confidence_note + "\n\n" + context if context else confidence_note

            # ── Freshness gate (#6) ──────────────────────────────────
            # When question asks about current info but docs are old,
            # inject a staleness warning.
            try:
                from services.rag_pipeline import build_freshness_warning
                freshness_note = build_freshness_warning(question, filtered_docs)
                if freshness_note:
                    context = freshness_note + "\n\n" + context if context else freshness_note
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            # ── Workspace context (live operational state) ───────────
            # Await the task we started in parallel before retrieval.
            # This overlaps with HyDE search so usually costs ~0 ms extra.
            if ws_ctx_task is not None:
                try:
                    ws_ctx = await ws_ctx_task
                    if ws_ctx:
                        context = ws_ctx + "\n\n" + context if context else ws_ctx
                        logger.debug(
                            "Discord Q&A: injected %d chars of workspace context",
                            len(ws_ctx),
                        )
                except Exception as exc:
                    logger.debug("Workspace context unavailable for Discord: %s", exc)

            # ── Web-augmented search ────────────────────────────────
            # Triggers on THREE conditions (any is sufficient):
            #  1. RAG context is sparse (< threshold words)
            #  2. Question signals need for current/external information
            #  3. Retrieved context has poor relevance scores (wrong topic)
            # Controlled by corpus_quality.web_augmented_chat config.
            web_augment_block = ""
            context_relevant = _context_is_relevant(filtered_docs)
            outward = _is_outward_question(question)
            needs_web = context_quality < 80 or _needs_web_search(question) or not context_relevant or (outward and context_quality < 200)
            if needs_web:
                try:
                    from core.config_loader import WorkflowConfig
                    wf = WorkflowConfig.load()
                    wac_enabled = wf.cq_web_chat_enabled
                    wac_threshold = wf.cq_web_chat_sparse_threshold
                except Exception:
                    wac_enabled = True
                    wac_threshold = 80

                sparse = context_quality < wac_threshold
                intent = _needs_web_search(question)
                weak_relevance = not context_relevant
                if wac_enabled and (sparse or intent or weak_relevance):
                    service_container = getattr(self.bot, "service_container", None)
                    tavily = getattr(service_container, "tavily", None) if service_container else None
                    if tavily and getattr(tavily, "is_configured", False):
                        # ── Status: tell user WHY we're searching the web ──
                        if weak_relevance and not sparse:
                            await _emit_status("🌐 Knowledge base didn't have a strong match — searching the web…")
                        elif intent:
                            await _emit_status("🌐 Looking up current information on the web…")
                        else:
                            await _emit_status("🌐 Limited local knowledge — searching the web…")

                        try:
                            from services.web_research import chat_web_augment
                            web_augment_block = await chat_web_augment(
                                tavily, question, max_results=4,
                            )
                            if web_augment_block:
                                # Corpus-first: for inward questions, keep corpus
                                # context authoritative and append web as supplement.
                                # For outward questions, web leads.
                                if context and not outward:
                                    context = (
                                        "[INTERNAL KNOWLEDGE BASE — authoritative for org-specific facts]\n"
                                        + context
                                        + "\n\n[WEB SEARCH RESULTS — supplementary]\n"
                                        + web_augment_block
                                    )
                                else:
                                    context = context + "\n\n" + web_augment_block if context else web_augment_block
                                trigger = (
                                    "intent" if intent
                                    else "weak_relevance" if weak_relevance
                                    else "sparse"
                                )
                                logger.info(
                                    "Web-augmented chat (%s): added %d chars of web context (RAG had %d words)",
                                    trigger, len(web_augment_block), context_quality,
                                )

                                await _emit_status("✅ Found web sources — generating answer…")

                                # Cache web results into corpus so we don't search again
                                try:
                                    from services.autonomous_research import cache_web_result
                                    asyncio.create_task(
                                        cache_web_result(
                                            question=question,
                                            web_block=web_augment_block,
                                            bot=self.bot,
                                        )
                                    )
                                except Exception:
                                    pass  # Non-blocking
                            else:
                                await _emit_status("🔄 Web search didn't find anything relevant — using available knowledge…")
                        except Exception as exc:
                            logger.debug("Web augmentation failed (non-fatal): %s", exc)

            await _emit_status("💬 Generating response…")
            # Call AskQuestion with pre-built context
            result = await asyncio.wait_for(
                self._ask_with_context(
                    question, 
                    context,
                    message_chain, 
                    user, 
                    web_url,
                    channel_name,
                    use_pipeline=use_pipeline,
                ),
                timeout=timeout
            )
            
            # ── Generic answer suppression (#10) ─────────────────────
            # If the response doesn't address the question's key terms,
            # append a low-confidence note so the user knows.
            if _is_generic_answer(question, result):
                logger.info("[safe_llm_call] Generic answer detected — appending confidence note")
                result += (
                    "\n\n---\n*Note: This answer may not fully address your specific question. "
                    "Try rephrasing, or ask me to search the web for more targeted information.*"
                )

            # ── LLM self-assessment: detect knowledge gaps via introspection ──
            if user_id:
                try:
                    from services.answer_self_assessment import (
                        assess_answer_quality,
                        find_near_misses,
                        format_near_misses_for_context,
                    )

                    assessment = await assess_answer_quality(
                        question=question,
                        response=result,
                        context=context,
                    )

                    if assessment.gap_detected:
                        gap_tracker = self.bot.get_cog("KnowledgeGapTracker")
                        if gap_tracker:
                            topic = (
                                assessment.suggested_topic
                                or " ".join(w for w in question.split()[:8] if len(w) > 3)
                            )

                            # Near-miss retrieval: find docs that *almost* answer
                            near_misses = await find_near_misses(question)
                            near_miss_text = format_near_misses_for_context(near_misses)

                            gap_context = (
                                f"Self-assessed confidence: {assessment.confidence}/10 | "
                                f"Grounded: {assessment.grounded} | "
                                f"Context quality: {context_quality} words | "
                                f"Retrieved: {len(filtered_docs)} docs"
                            )
                            if assessment.missing_knowledge:
                                gap_context += f"\nMissing: {assessment.missing_knowledge}"
                            if near_miss_text:
                                gap_context += f"\n{near_miss_text}"

                            await gap_tracker.log_knowledge_gap(
                                topic=topic,
                                question=question,
                                context=gap_context,
                                user_id=user_id,
                            )
                            logger.info(
                                "Knowledge gap (confidence %d/10): %s",
                                assessment.confidence,
                                topic[:50],
                            )

                            # ── Immediate autonomous research ──
                            # Fire-and-forget: research the gap NOW via web search
                            # so the answer is available next time someone asks.
                            try:
                                from services.autonomous_research import (
                                    research_gap_immediately,
                                )
                                asyncio.create_task(
                                    research_gap_immediately(
                                        topic=topic,
                                        question=question,
                                        missing_knowledge=assessment.missing_knowledge,
                                        bot=self.bot,
                                    )
                                )
                            except Exception:
                                pass  # Non-blocking
                    else:
                        logger.debug(
                            "Self-assessment passed (confidence %d/10) for: %s",
                            assessment.confidence,
                            question[:60],
                        )

                        # ── Auto-close matching gaps ──
                        # If we now answer confidently, close any matching gap
                        try:
                            from services.autonomous_research import (
                                maybe_auto_close_gap,
                            )
                            asyncio.create_task(
                                maybe_auto_close_gap(
                                    question=question,
                                    confidence=assessment.confidence,
                                    grounded=assessment.grounded,
                                    bot=self.bot,
                                )
                            )
                        except Exception:
                            pass  # Non-blocking
                except Exception as exc:
                    logger.debug("Self-assessment failed (non-fatal): %s", exc)
            
            return result, chunk_sources
        except asyncio.TimeoutError:
            logger.warning(f"LLM call timed out after {timeout}s")
            return "⏱️ Response generation timed out. Try a more specific question.", chunk_sources
    
    async def _ask_with_context(self, question: str, context: str, message_chain: str = "", user: str = None, web_url: str = None, channel_name: str = None, use_pipeline: bool = False) -> str:
        """Internal: ask question with pre-filtered context. use_pipeline=True for 3-phase synthesis."""
        # Build history
        history = ""
        if message_chain:
            topic_reset = _detect_topic_reset(question)
            history = _build_history_from_chain(message_chain, topic_reset=topic_reset)
        
        # Web scraping if URL provided
        if web_url:
            try:
                web_text = await fetch_webpage_text(web_url)
                if web_text:
                    history += f"\n\n[Live web content from {web_url}]:\n{web_text[:2000]}"
            except Exception as e:
                logger.warning(f"Web scraping failed: {e}")

        # ── 3-phase pipeline path (for /deep_consult) ────────────────────────
        if use_pipeline:
            try:
                router = await _get_pipeline_router()
                if router and router.pipeline:
                    logger.info("[_ask_with_context] Using 3-phase pipeline")
                    full_context = context
                    if history:
                        full_context = f"[Conversation History]\n{history}\n\n{full_context}"
                    system_prompt_text = template.replace("{context}", "").replace("{question}", "").replace("{history}", "").replace("{user}", user or "unknown-user")
                    result = await router.generate_pipeline(
                        user_prompt=question,
                        context=full_context,
                        system_prompt=system_prompt_text,
                    )
                    logger.info(f"[_ask_with_context] Pipeline complete. Models: {result.get('models_used', {})}")
                    return result["final"]
            except Exception as e:
                logger.warning(f"[_ask_with_context] Pipeline failed, falling back to single model: {e}")

        # ── Single-model path ────────────────────────────────────────────────
        service_container = getattr(self.bot, "service_container", None)
        llm_service = None
        if service_container:
            llm_service = getattr(service_container, "llm", None) or getattr(service_container, "llm_service", None)
        
        prompt_template = ChatPromptTemplate.from_template(template)
        input_dict = {
            "context": context,
            "history": history,
            "user": user or "unknown-user",
            "question": question,
            "channel": channel_name or "unknown-channel"
        }
        
        try:
            if llm_service:
                return await llm_service.generate(prompt_template, input_dict)
            else:
                logger.warning("LLMService not available, falling back to local ChatOpenAI")
                model = ChatOpenAI(
                    model="gpt-4o-mini",
                    api_key=gpt_key,
                    temperature=0.2
                )
                chain = prompt_template | model | StrOutputParser()
                return await chain.ainvoke(input_dict)
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return f"❌ Error generating response: {str(e)}"

    async def cog_load(self):
        # These app_commands are registered automatically when the cog is added.
        # Manually adding them can cause duplicates (e.g., "Command 'health' already registered").
        logging.info("[LLM Cog] Slash command registration complete.")

    async def cog_unload(self):
        # Removing app_commands explicitly is optional; discord.py will handle it on cog unload.
        # Keep this conservative to avoid errors if commands were never manually added.
        try:
            self.bot.tree.remove_command(self.deep_consult.name, type=self.deep_consult.type)
        except Exception as e:
            logger.warning("cog_unload: suppressed %s", e)
        try:
            self.bot.tree.remove_command(self.health_check.name, type=self.health_check.type)
        except Exception as e:
            logger.warning("cog_unload: suppressed %s", e)
        logging.info("[LLM Cog] Unregistered slash commands.")
    
    @app_commands.command(name="health")
    @app_commands.check(_is_owner_interaction)
    async def health_check(self, interaction: discord.Interaction):
        """Check LLM and retriever health"""
        await interaction.response.defer(ephemeral=True)
        
        status_lines = []
        
        # Check OpenAI API
        try:
            await self._validate_openai_once()
            status_lines.append("✅ OpenAI API: Connected")
        except Exception as e:
            status_lines.append(f"❌ OpenAI API: Failed ({str(e)[:50]})")

        # Check retriever
        try:
            retriever = self._ensure_retriever()
            if retriever:
                test_docs = await asyncio.to_thread(
                    run_retriever_query, retriever, "test"
                )
                status_lines.append(f"✅ Knowledge Base: {len(test_docs)} docs retrievable")
            else:
                status_lines.append("⚠️ Knowledge Base: Chroma not installed - RAG disabled")
        except Exception as e:
            status_lines.append(f"❌ Knowledge Base: Failed ({str(e)[:50]})")

        # Check Database
        if hasattr(self.bot, 'db') and self.bot.db:
            try:
                async with self.bot.db.acquire() as conn:
                    async with conn.execute("SELECT sqlite_version()") as cursor:
                        row = await cursor.fetchone()
                        sqlite_version = row[0] if row else "unknown"
                    status_lines.append(f"✅ Database: SQLite {sqlite_version}")

                    async with conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ) as cursor:
                        row = await cursor.fetchone()
                        table_count = int(row[0]) if row else 0
                    status_lines.append(f"   └─ {table_count} tables initialized")
            except Exception as e:
                status_lines.append(f"❌ Database: Failed ({str(e)[:50]})")
        else:
            status_lines.append("⚠️ Database: Not configured")

        # Check Tavily (if available)
        service_container = getattr(self.bot, "service_container", None)
        tavily = getattr(service_container, "tavily", None) if service_container else None
        if tavily and hasattr(tavily, "is_configured") and tavily.is_configured:
            try:
                healthy = await tavily.health_check() if hasattr(tavily, "health_check") else True
                status_lines.append("✅ Tavily Search: OK" if healthy else "⚠️ Tavily Search: Configured but failing")
            except Exception as e:
                status_lines.append(f"⚠️ Tavily Search: Configured but failing ({str(e)[:50]})")
        else:
            status_lines.append("⚠️ Tavily Search: Not configured")

        status_msg = "\n".join(status_lines)
        await interaction.followup.send(f"**System Health Check**\n{status_msg}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        logger.debug("[LLM Cog] on_message called!")
        logger.debug(f"[on_message] Received message in channel {message.channel.id}: {message.content}")
        if message.author.bot:
            logger.debug("[on_message] Ignoring message from bot user.")
            return
        # Only respond in whitelisted channels if the list is not empty
        if not self._channel_is_allowed(message.channel):
            logger.debug(f"[on_message] Channel {message.channel.id} not in allowed_channel_ids.")
            mentioned = False
            if self.bot.user in message.mentions:
                mentioned = True
            else:
                mention_patterns = [f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"]
                for pat in mention_patterns:
                    if pat in message.content:
                        mentioned = True
                        break
            if mentioned:
                logger.debug("[on_message] Bot was mentioned in a non-allowed channel.")
                try:
                    await message.channel.send(
                        "Sorry, in an effort to reduce clutter, I can't post in this channel. "
                        "Come chat with me in one of my allowed channels!"
                    )
                except Exception as e:
                    logger.warning(f"[on_message] Failed to send whitelist notice: {e}")
            return
        logger.debug("[on_message] Message is in allowed channel.")
        chain = []  # local per-message — no shared state
        current_message = message
        while current_message.reference:
            store = current_message.content.replace(f"<@{self.bot.user.id}>", "").strip()
            if current_message.author.bot:
                chain.append(("Assistant: " + store))
            else:
                chain.append((f"{current_message.author.display_name}: " + store))
            current_message = await current_message.channel.fetch_message(current_message.reference.message_id)
        store = current_message.content.replace(f"<@{self.bot.user.id}>", "").strip()
        if current_message.author.bot:
            chain.append(("Assistant: " + store))
        else:
            chain.append(("User: " + store))
        mentioned = False
        if self.bot.user in message.mentions:
            mentioned = True
        else:
            mention_patterns = [f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"]
            for pat in mention_patterns:
                if pat in message.content:
                    mentioned = True
                    break
        if mentioned:
            logger.debug(f"[on_message] Bot was mentioned by {message.author.display_name} ({message.author.id}) in channel {message.channel.id}")
            
            # Send deprecation notice suggesting slash commands
            try:
                notice_embed = create_info_embed(
                    title="💡 Tip: Use Slash Commands",
                    description=(
                        "I still respond to mentions, but slash commands are now preferred!\n\n"
                        "**Try these instead:**\n"
                        "• `/ask` - Quick Q&A with publish button\n"
                        "• `/deep_consult` - Deep analysis with suggestions\n\n"
                        "_Processing your mention below..._"
                    )
                )
                await message.reply(embed=notice_embed, mention_author=False)
            except Exception as e:
                logging.warning(f"Failed to send deprecation notice: {e}")
            
            prompt = message.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
            if prompt:
                display_name = getattr(message.author, 'display_name', None) or getattr(message.author, 'name', 'unknown-user')
                user_id = getattr(message.author, 'id', None)
                await self.handle_query(message, prompt, display_name, user_id, chain=chain)
        else:
            logger.debug("[on_message] Bot was not mentioned, not responding.")

    async def handle_query(self, message, prompt, user, user_id=None, chain=None):
        logger.debug(f"[handle_query] Handling query: '{prompt}' for user {user} in channel {message.channel.id}")
        
        channel_name = getattr(message.channel, "name", "unknown-channel")
        
        allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
        mention = f"<@{user_id}> " if user_id else ""

        # ── Live status message: shows real-time progress to the user ─────
        status_msg = None
        try:
            status_embed = create_info_embed(
                title="🔍 Working on it…",
                description="Searching knowledge base…",
            )
            status_msg = await message.reply(
                embed=status_embed, mention_author=False,
            )
        except Exception as e:
            logger.warning("handle_query: suppressed %s", e)

        async def _discord_status(text: str):
            nonlocal status_msg
            if status_msg and text:
                try:
                    embed = create_info_embed(
                        title="⏳ Working on it…",
                        description=text,
                    )
                    await status_msg.edit(embed=embed)
                except Exception as e:
                    logger.warning("_discord_status: suppressed %s", e)

        max_retries = 2
        base_delay = 1
        Answer = None
        last_exception = None
        for attempt in range(1, max_retries + 1):
            logger.debug(f"[handle_query] Attempt {attempt} to get LLM answer...")
            try:
                Answer, chunk_sources = await self.safe_llm_call(prompt, chain, user, user_id=user_id, channel_name=channel_name, status_callback=_discord_status)
                logger.debug(f"[handle_query] Got answer: {Answer[:100]}...")
                if not Answer or not isinstance(Answer, str):
                    logger.warning("[handle_query] LLM returned empty or invalid response.")
                    raise ValueError("LLM returned empty or invalid response.")

                # Delete the status message now that we have the answer
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception as e:
                        logger.warning("_discord_status: suppressed %s", e)
                    status_msg = None

                # Always split and send in chunks <= 2000 chars, mention only on first chunk
                split_answer = split_embed_content(Answer)
                previous_message = None
                for idx, part in enumerate(split_answer):
                    if idx == 0:
                        part_to_send = mention + part
                    else:
                        part_to_send = part
                    if previous_message:
                        previous_message = await previous_message.reply(content=part_to_send, allowed_mentions=allowed_mentions)
                    else:
                        previous_message = await message.reply(content=part_to_send, allowed_mentions=allowed_mentions)
                
                # Add feedback buttons to last message (learning loop)
                if previous_message and hasattr(self.bot, 'db') and self.bot.db:
                    feedback_view = ResponseFeedbackView(
                        prompt, Answer, self.bot.db,
                        chunk_sources=chunk_sources,
                    )
                    await previous_message.reply(
                        "Was this helpful?",
                        view=feedback_view,
                        mention_author=False
                    )
                
                logger.debug(f"[handle_query] Thread sent to channel for user: {user}")
                return
            except discord.HTTPException as e:
                logger.warning(f"[handle_query] Discord HTTPException: {e}")
                if attempt < max_retries and (getattr(e, 'status', None) in [429, 500, 502, 503, 504]):
                    await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
                    continue
                else:
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception as delete_err:
                            logger.warning("handle_query: suppressed %s", delete_err)
                    try:
                        await message.reply(content=f"{mention}Sorry, I couldn't send a response due to a Discord error.", allowed_mentions=allowed_mentions)
                    except Exception as notify_err:
                        logger.warning(f"[handle_query] Failed to notify user of Discord error: {notify_err}")
                    return
            except Exception as e:
                logger.warning(f"[handle_query] General Exception: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
                    continue
                else:
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception as delete_err:
                            logger.warning("handle_query: suppressed %s", delete_err)
                    try:
                        await message.reply(content=f"{mention}Sorry, I couldn't generate a response after several attempts. Please try again later.", allowed_mentions=allowed_mentions)
                    except Exception as notify_err:
                        logger.warning(f"[handle_query] Failed to notify user of internal error: {notify_err}")
                    return

    def build_operation_suggestions(self, question: str, answer: str) -> list[str]:
        """Suggest follow-up slash commands that leverage autonomous workflows."""
        suggestions = []
        combined = f"{question}\n{answer}".lower()
        if any(keyword in combined for keyword in ["scout", "market", "opportunit", "research", "tavily"]):
            suggestions.append("Use `/test_scout` to run a fresh Tavily sweep for leads or references.")
        if any(keyword in combined for keyword in ["digest", "summary", "status", "update"]):
            suggestions.append("Trigger `/test_digest` to generate a coordinator-style daily summary in #weekly-meeting-threads.")
        if any(keyword in combined for keyword in ["meeting", "thread", "async", "partners"]):
            suggestions.append("Start `/run_async_meeting` to spin up the four partner threads and capture responses asynchronously.")
        if "channel" in combined or "permissions" in combined or "config" in combined:
            suggestions.append("Run `/dev_status` to verify configured channel IDs, Tavily health, and bot permissions before executing ops.")
        return suggestions

    @app_commands.command(name="ask", description=f"Ask {_bot_name} a question with full knowledge base access.")
    @app_commands.describe(question="What would you like to know?")
    async def ask(
        self,
        interaction: discord.Interaction,
        question: str
    ):
        """Simple Q&A command - ephemeral with publish button.
        Includes intent detection: if the input looks like a task, decision, or
        meeting note rather than a question, we still answer but offer one-click
        routing to the right command.
        """
        if not self._channel_is_allowed(interaction.channel):
            await interaction.response.send_message(
                "Use one of the configured assistant channels (try #bots or #weekly-meeting-threads).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        user_display = getattr(interaction.user, 'display_name', None) or getattr(interaction.user, 'name', 'unknown-user')

        # --- Intent detection (keyword, zero-latency) ---
        detected_intent = _detect_non_question_intent(question)

        # Log question for Steward tracking
        question_id = None
        auto_ops = self.bot.get_cog("AutonomousOps")
        if auto_ops and hasattr(auto_ops, '_steward_log_question'):
            try:
                question_id = await auto_ops._steward_log_question(
                    question_text=question,
                    user_id=interaction.user.id,
                    username=user_display,
                    channel_id=interaction.channel_id,
                    had_sources=False,  # Will update after we get answer
                    source_count=0
                )
            except Exception as e:
                logging.warning(f"Failed to log question for Steward: {e}")

        try:
            answer, chunk_sources = await self.safe_llm_call(
                question, 
                None, 
                user_display, 
                timeout=45, 
                user_id=interaction.user.id,
                channel_name=getattr(interaction.channel, "name", None)
            )
        except Exception as exc:
            logging.error(f"[ask] LLM call failed: {exc}")
            await interaction.followup.send(
                "I couldn't generate a response. Please try again or contact an admin.",
                ephemeral=True,
            )
            return

        if not answer:
            await interaction.followup.send(
                "No response received. Try rephrasing your question.",
                ephemeral=True,
            )
            return

        # Split into chunks if needed
        parts = split_embed_content(answer)
        embed = create_info_embed(
            title=_bot_name,
            description=parts[0]
        )
        embed.set_footer(text=f"Asked by {user_display}")
        
        # Add feedback buttons for learning loop (pass question_id for Steward tracking)
        feedback_view = None
        if hasattr(self.bot, 'db') and self.bot.db:
            feedback_view = ResponseFeedbackView(
                question, answer, self.bot.db,
                question_id=question_id,
                chunk_sources=chunk_sources,
            )
        
        # Use AskConversationView for threaded reply capability
        view = AskConversationView(self, question, answer, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        # Send feedback buttons separately (can't combine views easily)
        if feedback_view:
            await interaction.followup.send(
                "Was this helpful?",
                view=feedback_view,
                ephemeral=True
            )
        
        # Send remaining parts if any
        for part in parts[1:]:
            await interaction.followup.send(part, ephemeral=True)

        # --- Intent routing offer (non-blocking, after answer) ---
        if detected_intent:
            intent_view = _IntentRouteView(
                intent=detected_intent,
                content=question,
                user_id=interaction.user.id,
                bot=self.bot,
            )
            hint = {
                "action": "💡 This looks like a **task**. Want me to create an action item?",
                "decision": "💡 This sounds like a **decision**. Want me to record it?",
                "meeting": "💡 This reads like **meeting notes**. Want me to store them?",
                "memory": "💡 This looks like something worth **remembering**. Want me to save it?",
            }
            await interaction.followup.send(
                hint.get(detected_intent, "💡 Want me to save this?"),
                view=intent_view,
                ephemeral=True,
            )

    @app_commands.command(name="deep_consult", description=f"Ask {_bot_name} to synthesize docs, templates, and new automations.")
    @app_commands.describe(
        question="What do you need help with?",
        source_url="Optional URL to pull live context from",
        include_operations="Suggest follow-up automation commands"
    )
    async def deep_consult(
        self,
        interaction: discord.Interaction,
        question: str,
        source_url: Optional[str] = None,
        include_operations: bool = True,
    ):
        if not self._channel_is_allowed(interaction.channel):
            await interaction.response.send_message(
                "Use one of the configured assistant channels for deep consults (try #bots or #weekly-meeting-threads).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        user_display = getattr(interaction.user, 'display_name', None) or getattr(interaction.user, 'name', 'unknown-user')
        clean_url = source_url.strip() if source_url else None

        try:
            answer, chunk_sources = await self.safe_llm_call(
                question, 
                None, 
                user_display, 
                clean_url, 
                timeout=90, 
                user_id=interaction.user.id,
                channel_name=getattr(interaction.channel, "name", None),
                use_pipeline=True,
            )
        except Exception as exc:
            logging.error(f"[deep_consult] LLM call failed: {exc}")
            await interaction.followup.send(
                "I couldn't generate a response just now—please retry in a moment or ping Colin if it persists.",
                ephemeral=True,
            )
            return

        if not answer:
            await interaction.followup.send(
                "No response came back from the LLM. Try a shorter or clearer question.",
                ephemeral=True,
            )
            return

        if include_operations:
            suggestions = self.build_operation_suggestions(question, answer)
        else:
            suggestions = []

        if suggestions:
            suggestion_block = "\n\n---\nSuggested follow-ups:\n" + "\n".join(f"- {item}" for item in suggestions)
        else:
            suggestion_block = ""

        payload = answer + suggestion_block
        parts = split_embed_content(payload)
        
        # Create embed for first part
        embed = create_success_embed(
            title="Deep Consult Results",
            description=parts[0]
        )
        embed.set_footer(text=f"Consulted by {user_display}")
        
        # Add feedback buttons for learning loop
        feedback_view = None
        if hasattr(self.bot, 'db') and self.bot.db:
            feedback_view = ResponseFeedbackView(
                question, answer, self.bot.db,
                chunk_sources=chunk_sources,
            )
        
        # Send ephemeral with publish button
        # PublishView expects (content, embeds)
        view = PublishView(content="", embeds=[embed])
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        # Send feedback buttons separately
        if feedback_view:
            await interaction.followup.send(
                "Was this helpful?",
                view=feedback_view,
                ephemeral=True
            )
        
        # Send remaining parts if any
        for part in parts[1:]:
            await interaction.followup.send(part, ephemeral=True)
    
    async def Test_Answer(self, message):
        prompt = re.sub(r"<@\w*>", "", message.content).strip()
        prompt = re.sub(r"<@&\w*>", "", prompt).strip()
        
        # Use safe_llm_call instead of legacy AskQuestion
        user_display = getattr(message.author, 'display_name', 'unknown-user')
        Answer, _srcs = await self.safe_llm_call(prompt, None, user=user_display, user_id=message.author.id)
        
        await message.channel.send(
            "---------------------------\n"
            + "Prompt: " + prompt + "\n\n"
            + "Answer: " + Answer + "\n"
            + "---------------------------"
        )


# Update the reloadcogs command to be a slash command

class TourCog(commands.Cog):
    """Quick interactive tour of the bot's capabilities"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="tour", description=f"Quick guided tour of {_bot_name}")
    async def tour(self, interaction: discord.Interaction):
        """Interactive walkthrough of the bot's core capabilities"""
        from ux_helpers import PaginationView
        
        await interaction.response.defer(ephemeral=True)
        
        # Color scheme
        c_brand = discord.Color.from_rgb(147, 51, 234)  # Purple
        c_secondary = discord.Color.from_rgb(59, 130, 246)  # Blue
        
        # Page 1: Welcome / Identity
        p1 = discord.Embed(
            title=f"✨ Welcome to {_bot_name}",
            description=(
                "**I am the studio's institutional operating system.**\n\n"
                "I remember what we've done, what we've decided, and how we build things. "
                "I also run **7 autonomous AI personas** that scan the web for opportunities, "
                "track your pipeline, generate ideas, and keep the team aligned—24/7."
            ),
            color=c_brand
        )
        p1.add_field(
            name="What I Do",
            value=(
                "• **Answer questions** from indexed docs & archives\n"
                "• **Run 7 personas** that work while you sleep\n"
                "• **Track action items** with ownership & due dates\n"
                "• **Learn continuously** from your feedback"
            ),
            inline=False
        )
        p1.set_footer(text="Page 1/7 • Click Next to continue")

        # Page 2: Asking Questions
        p2 = discord.Embed(
            title="🗣️ Asking Questions",
            description="**You don't need special syntax. Just ask.**",
            color=c_brand
        )
        p2.add_field(
            name="`/ask` — Quick Q&A",
            value=(
                "Get a private answer grounded in our knowledge base.\n\n"
                "*Example:* `/ask What was the budget for the Dueling Dinos project?`"
            ),
            inline=False
        )
        p2.add_field(
            name="`/deep_consult` — Deep Analysis",
            value=(
                "For complex questions. Optionally include a URL for live web context.\n\n"
                "*Example:* `/deep_consult Analyze our museum interactive projects and recommend a hardware stack`"
            ),
            inline=False
        )
        p2.set_footer(text="Page 2/7 • RAG retrieval is my superpower")

        # Page 3: The Seven Personas
        p3 = discord.Embed(
            title="🤖 The Seven Personas",
            description=(
                "I run **7 personas** that work around the clock. "
                "They post updates to **#bots-office** so you can see what they're thinking."
            ),
            color=c_secondary
        )
        p3.add_field(
            name="📊 Manager",
            value="Daily standup (9am) & EOD digest (6pm)",
            inline=True
        )
        p3.add_field(
            name="🎯 Chief",
            value="Weekly strategic review (Fri 4pm)",
            inline=True
        )
        p3.add_field(
            name="📋 Coordinator",
            value="Meeting facilitation & summaries",
            inline=True
        )
        p3.add_field(
            name="🔍 Scout",
            value="Daily web search for opportunities",
            inline=True
        )
        p3.add_field(
            name="💭 Dreamer",
            value="Weekly ideation (Tuesdays)",
            inline=True
        )
        p3.add_field(
            name="💰 Rainmaker",
            value="Pipeline management & follow-ups",
            inline=True
        )
        p3.add_field(
            name="🪴 Steward",
            value="Self-monitoring & learning health",
            inline=True
        )
        p3.add_field(
            name="⏰ Hourly Meetings",
            value="Every hour, 2-3 personas meet to discuss actual business context",
            inline=True
        )
        p3.set_footer(text="Page 3/7 • Watch #bots-office to see persona work")

        # Page 4: Teaching Me
        p4 = discord.Embed(
            title="🧠 Teaching Me",
            description=(
                "**The best way to teach me is to just work normally.**\n\n"
                "When you encounter a decision, process, or insight—capture it right from Discord."
            ),
            color=c_brand
        )
        p4.add_field(
            name="`/remember` — Quick Capture",
            value="Save a thought, link, or decision instantly. I'll index it immediately.",
            inline=False
        )
        p4.add_field(
            name="`/teach` — Structured Knowledge",
            value="Add detailed knowledge with categories (decision, process, technical, client) and tags for better retrieval.",
            inline=False
        )
        p4.add_field(
            name="`/parse_meeting` — Meeting Notes",
            value="Paste messy notes → I save them to `docs/meetings/` and make them searchable.",
            inline=False
        )
        p4.set_footer(text="Page 4/7 • You build the brain")

        # Page 5: Action Items & Agenda
        p5 = discord.Embed(
            title="⚡ Action Items & Agenda",
            description="Structured tools to keep work moving.",
            color=c_brand
        )
        p5.add_field(
            name="`/action add` / `/action list`",
            value="Create & manage action items with owners, due dates, and priorities. Rainmaker persona tracks follow-ups.",
            inline=False
        )
        p5.add_field(
            name="`/did`",
            value="Log a quick win or update. Personas reference these in their discussions.",
            inline=False
        )
        p5.add_field(
            name="`/agenda`",
            value="Add a topic you want the personas to discuss in their next meeting.",
            inline=False
        )
        p5.add_field(
            name="`/report`",
            value="Get status reports from personas. Try `/report all` for the full picture.",
            inline=False
        )
        p5.set_footer(text="Page 5/7 • Automating the boring stuff")

        # Page 6: The Learning Loop
        p6 = discord.Embed(
            title="🔄 The Learning Loop",
            description=(
                "**I improve through continuous feedback.** Every interaction teaches me something."
            ),
            color=c_secondary
        )
        p6.add_field(
            name="👍 / 👎 Buttons",
            value="Every response has feedback buttons. **Use them!** Thumbs up reinforces good answers. Thumbs down flags what needs fixing.",
            inline=False
        )
        p6.add_field(
            name="Knowledge Gap Detection",
            value="When I can't answer something well, I log it as a **knowledge gap**. Use `/interview` to fill these gaps through Q&A.",
            inline=False
        )
        p6.add_field(
            name="Steward Monitoring",
            value="The 🪴 Steward persona tracks engagement, question success rates, and learning health. It alerts when things need attention.",
            inline=False
        )
        p6.set_footer(text="Page 6/7 • Your feedback makes me smarter")

        # Page 7: Getting Started
        p7 = discord.Embed(
            title="🚀 Ready to Start?",
            description="Here's what to do next.",
            color=c_brand
        )
        p7.add_field(
            name="1️⃣ Ask a question",
            value="Try `/ask What projects have we done?`",
            inline=False
        )
        p7.add_field(
            name="2️⃣ Check #bots-office",
            value="See what the personas are up to. React to posts that matter.",
            inline=False
        )
        p7.add_field(
            name="3️⃣ Teach me something",
            value="Use `/remember` to save a useful fact or decision.",
            inline=False
        )
        p7.add_field(
            name="4️⃣ Give feedback",
            value="Always click 👍 or 👎 on my responses.",
            inline=False
        )
        p7.add_field(
            name="Admin Tools",
            value=(
                "• `/health` — System status\n"
                "• `/staff` — See all personas (including custom ones)\n"
                "• `/hire` — Create a custom persona\n"
                "• **Admin GUI** — `http://localhost:8000` for model config"
            ),
            inline=False
        )
        p7.set_footer(text="Page 7/7 • Tour Complete ✅")

        pages = [p1, p2, p3, p4, p5, p6, p7]
        view = PaginationView(pages, timeout=300)
        
        await interaction.followup.send(embed=p1, view=view, ephemeral=True)


class ReloadCogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="reloadcogs", description="Reload all bot cogs dynamically.")
    @app_commands.check(_is_owner_interaction)
    async def reload_cogs(self, interaction: discord.Interaction):
        logger.info("Reloading cogs...")
        try:
            for cog in list(self.bot.cogs):
                self.bot.reload_extension(f"cogs.{cog}")
            await interaction.response.send_message("Cogs reloaded successfully.", ephemeral=True)
            logger.info("Cogs reloaded successfully.")
        except Exception as e:
            await interaction.response.send_message(f"Failed to reload cogs: {e}", ephemeral=True)
            logger.error(f"Failed to reload cogs: {e}")

# In the setup function, sync the command tree after adding cogs
async def setup(bot):
    try:
        await bot.add_cog(LLM(bot))
        await bot.add_cog(TourCog(bot))
        logger.info('[setup] LLM cog loaded.')
    except Exception as e:
        logger.error(f'[setup] Failed to load LLM cog: {e}')
