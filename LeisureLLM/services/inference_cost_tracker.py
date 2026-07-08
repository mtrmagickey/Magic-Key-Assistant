"""
Inference Cost Tracker — per-request token counting and cost analytics.

This is a core **counter-positioning moat**: by making the cost advantage
of local inference *visible and measurable*, users see exactly how much
they'd pay if they switched to a cloud-only competitor.

Capabilities:
    1. **Token estimation** — estimates tokens per request (prompt + completion)
       for both local and cloud backends.
    2. **Cost calculation** — computes what each request *would have cost*
       on cloud providers, even when run locally for free.
    3. **Savings dashboard data** — cumulative savings ("You've saved $X.XX
       by running locally") — a powerful retention signal.
    4. **Per-backend analytics** — track usage by backend, model, pipeline role.
    5. **Budget alerts** — optional spend caps for cloud backends.

Design:
    - Token counting uses tiktoken for OpenAI, char-based estimation for others.
    - All data stored in SQLite — no cloud dependency.
    - Injected as middleware in the ModelRouter pipeline.
    - Cloud pricing table updated via config file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Pricing Table (per 1M tokens) ─────────────────────────────
# Updated periodically. Users can override via config.
# Prices are in USD per 1 million tokens.
DEFAULT_PRICING = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "o3": {"input": 10.00, "output": 40.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "gpt-5.2": {"input": 3.00, "output": 12.00},

    # Anthropic
    "claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-latest": {"input": 0.80, "output": 4.00},
    "claude-3-opus-latest": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-4": {"input": 0.80, "output": 4.00},

    # OpenRouter (popular models)
    "meta-llama/llama-3.3-70b-instruct": {"input": 0.40, "output": 0.40},
    "google/gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "mistralai/mistral-large-latest": {"input": 2.00, "output": 6.00},

    # Local models — always $0 (the point of the moat)
    "_local_default": {"input": 0.0, "output": 0.0},
}


def _estimate_tokens(text: str) -> int:
    """
    Estimate token count from text.

    Uses a simple heuristic: ~4 characters per token for English text.
    More accurate than word-count, less overhead than loading tiktoken.
    """
    if not text:
        return 0
    # Rough estimate: 1 token ≈ 4 chars for English, 2-3 for code
    return max(1, len(text) // 4)


def _estimate_tokens_tiktoken(text: str, model: str = "gpt-4o") -> int:
    """Try tiktoken for accurate OpenAI token counting, fall back to heuristic."""
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return _estimate_tokens(text)


# ── Data structures ───────────────────────────────────────────

@dataclass
class InferenceRecord:
    """A single LLM inference event."""
    timestamp: str
    backend_type: str            # ollama | openai | anthropic | openrouter
    backend_name: str            # e.g. "My Ollama", "openai"
    model: str
    pipeline_role: str           # initial | critique | synthesize | single
    input_tokens: int
    output_tokens: int
    total_tokens: int
    actual_cost_usd: float       # What it actually cost (0 for local)
    cloud_equiv_cost_usd: float  # What it WOULD have cost on cloud
    savings_usd: float           # Difference = cloud_equiv - actual
    latency_ms: int
    cached: bool = False


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS inference_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    backend_type TEXT NOT NULL,
    backend_name TEXT NOT NULL,
    model TEXT NOT NULL,
    pipeline_role TEXT NOT NULL DEFAULT 'single',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0.0,
    cloud_equiv_cost_usd REAL NOT NULL DEFAULT 0.0,
    savings_usd REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    query_hash TEXT,
    session_id TEXT
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_inference_timestamp
    ON inference_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_inference_backend
    ON inference_log(backend_type, model);
CREATE INDEX IF NOT EXISTS idx_inference_role
    ON inference_log(pipeline_role);
"""

_CREATE_BUDGET_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cost_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_name TEXT NOT NULL,
    period TEXT NOT NULL CHECK (period IN ('daily', 'weekly', 'monthly')),
    budget_usd REAL NOT NULL,
    current_spend_usd REAL NOT NULL DEFAULT 0.0,
    period_start TEXT NOT NULL,
    alert_threshold REAL NOT NULL DEFAULT 0.8,
    alert_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(backend_name, period)
);
"""


class InferenceCostTracker:
    """
    Tracks token usage and costs across all LLM backends.

    Usage::

        tracker = InferenceCostTracker(db)
        await tracker.ensure_tables()

        # After an LLM call:
        await tracker.record_inference(
            backend_type="ollama",
            backend_name="Local Ollama",
            model="qwen2.5:32b",
            pipeline_role="initial",
            input_text=prompt,
            output_text=response,
            latency_ms=3200,
        )

        # Show savings to user:
        stats = await tracker.get_savings_summary()
        # → {"total_savings_usd": 14.37, "total_requests": 892, ...}
    """

    def __init__(self, db: Any, pricing: Optional[Dict[str, Dict[str, float]]] = None):
        self.db = db
        self.pricing = pricing or dict(DEFAULT_PRICING)
        self._tables_ensured = False

    async def ensure_tables(self) -> None:
        if self._tables_ensured:
            return
        try:
            async with self.db.acquire() as conn:
                await conn.executescript(
                    _CREATE_TABLE_SQL + _CREATE_INDEX_SQL + _CREATE_BUDGET_TABLE_SQL
                )
                await conn.commit()
            self._tables_ensured = True
        except Exception as exc:
            logger.warning("Failed to ensure cost tracker tables: %s", exc)

    # ── Cost calculation ───────────────────────────────────────

    def _get_price(self, model: str) -> Dict[str, float]:
        """Look up pricing for a model. Falls back to closest match."""
        # Exact match
        if model in self.pricing:
            return self.pricing[model]
        # Prefix match (e.g., "gpt-4o-2024-05-13" → "gpt-4o")
        for key in sorted(self.pricing.keys(), key=len, reverse=True):
            if model.startswith(key):
                return self.pricing[key]
        # Local/unknown → free
        return self.pricing["_local_default"]

    def _calculate_cost(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Calculate USD cost for a given model and token counts."""
        price = self._get_price(model)
        cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000
        return round(cost, 6)

    def _get_cloud_equivalent_model(self, local_model: str) -> str:
        """
        Map a local model to its approximate cloud equivalent for
        savings calculation.  This is the key insight that makes
        savings visible.
        """
        model_lower = local_model.lower()

        # Large models → GPT-4o equivalent
        if any(x in model_lower for x in ["70b", "72b", "65b", "34b", "32b"]):
            return "gpt-4o"
        # Medium models → GPT-4o-mini equivalent
        if any(x in model_lower for x in ["14b", "13b", "8b", "7b"]):
            return "gpt-4o-mini"
        # Small models → GPT-4.1-nano equivalent
        if any(x in model_lower for x in ["3b", "2b", "1b", "1.5b"]):
            return "gpt-4.1-nano"
        # Default to GPT-4o-mini for unknown local models
        return "gpt-4o-mini"

    # ── Recording ──────────────────────────────────────────────

    async def record_inference(
        self,
        backend_type: str,
        backend_name: str,
        model: str,
        pipeline_role: str,
        input_text: str,
        output_text: str,
        latency_ms: int,
        *,
        cached: bool = False,
        query_hash: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> InferenceRecord:
        """Record an inference event with cost tracking."""
        await self.ensure_tables()

        # Estimate tokens
        if backend_type == "openai":
            input_tokens = _estimate_tokens_tiktoken(input_text, model)
            output_tokens = _estimate_tokens_tiktoken(output_text, model)
        else:
            input_tokens = _estimate_tokens(input_text)
            output_tokens = _estimate_tokens(output_text)

        total_tokens = input_tokens + output_tokens

        # Calculate costs
        is_local = backend_type in ("ollama",)
        if is_local:
            actual_cost = 0.0
            cloud_model = self._get_cloud_equivalent_model(model)
            cloud_equiv = self._calculate_cost(cloud_model, input_tokens, output_tokens)
        else:
            actual_cost = self._calculate_cost(model, input_tokens, output_tokens)
            cloud_equiv = actual_cost

        savings = cloud_equiv - actual_cost

        record = InferenceRecord(
            timestamp=datetime.now().isoformat(),
            backend_type=backend_type,
            backend_name=backend_name,
            model=model,
            pipeline_role=pipeline_role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            actual_cost_usd=actual_cost,
            cloud_equiv_cost_usd=cloud_equiv,
            savings_usd=savings,
            latency_ms=latency_ms,
            cached=cached,
        )

        # Persist
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO inference_log
                       (backend_type, backend_name, model, pipeline_role,
                        input_tokens, output_tokens, total_tokens,
                        actual_cost_usd, cloud_equiv_cost_usd, savings_usd,
                        latency_ms, cached, query_hash, session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        backend_type, backend_name, model, pipeline_role,
                        input_tokens, output_tokens, total_tokens,
                        actual_cost, cloud_equiv, savings,
                        latency_ms, int(cached), query_hash, session_id,
                    ),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to record inference: %s", exc)

        # Check budget alerts
        if actual_cost > 0:
            await self._check_budget_alerts(backend_name, actual_cost)

        return record

    # ── Analytics ──────────────────────────────────────────────

    async def get_savings_summary(self, days: int = 30) -> Dict[str, Any]:
        """
        Get a savings summary for display to the user.

        This is the key retention metric: "You've saved $X.XX by running
        locally this month."
        """
        await self.ensure_tables()
        try:
            row = await self.db.fetch_one_dict(
                """SELECT
                        COUNT(*) as total_requests,
                        SUM(input_tokens) as total_input_tokens,
                        SUM(output_tokens) as total_output_tokens,
                        SUM(total_tokens) as total_tokens,
                        SUM(actual_cost_usd) as total_actual_cost,
                        SUM(cloud_equiv_cost_usd) as total_cloud_equiv,
                        SUM(savings_usd) as total_savings,
                        AVG(latency_ms) as avg_latency_ms,
                        SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END) as cache_hits
                       FROM inference_log
                       WHERE timestamp >= datetime('now', ? || ' days')""",
                f"-{days}",
            )

            if not row or row["total_requests"] == 0:
                return {
                    "period_days": days,
                    "total_requests": 0,
                    "total_savings_usd": 0.0,
                    "message": "No inference data yet. Start chatting to see savings!",
                }

            return {
                "period_days": days,
                "total_requests": row["total_requests"],
                "total_input_tokens": row["total_input_tokens"] or 0,
                "total_output_tokens": row["total_output_tokens"] or 0,
                "total_tokens": row["total_tokens"] or 0,
                "total_actual_cost_usd": round(row["total_actual_cost"] or 0, 4),
                "total_cloud_equivalent_usd": round(row["total_cloud_equiv"] or 0, 4),
                "total_savings_usd": round(row["total_savings"] or 0, 4),
                "avg_latency_ms": round(row["avg_latency_ms"] or 0, 0),
                "cache_hit_rate": round((row["cache_hits"] or 0) / max(row["total_requests"], 1) * 100, 1),
                "message": f"You've saved ${row['total_savings'] or 0:.2f} by running locally in the last {days} days!",
            }
        except Exception as exc:
            logger.warning("Failed to get savings summary: %s", exc)
            return {"error": str(exc)}

    async def get_usage_by_model(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get per-model usage breakdown."""
        await self.ensure_tables()
        try:
            return await self.db.fetch_dicts(
                """SELECT
                        backend_type, model,
                        COUNT(*) as requests,
                        SUM(total_tokens) as tokens,
                        SUM(actual_cost_usd) as actual_cost,
                        SUM(savings_usd) as savings,
                        AVG(latency_ms) as avg_latency
                       FROM inference_log
                       WHERE timestamp >= datetime('now', ? || ' days')
                       GROUP BY backend_type, model
                       ORDER BY requests DESC""",
                f"-{days}",
            )
        except Exception as exc:
            logger.warning("Failed to get usage by model: %s", exc)
            return []

    async def get_usage_by_role(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get per-pipeline-role usage breakdown."""
        await self.ensure_tables()
        try:
            return await self.db.fetch_dicts(
                """SELECT
                        pipeline_role,
                        COUNT(*) as requests,
                        SUM(total_tokens) as tokens,
                        SUM(actual_cost_usd) as actual_cost,
                        SUM(savings_usd) as savings,
                        AVG(latency_ms) as avg_latency
                       FROM inference_log
                       WHERE timestamp >= datetime('now', ? || ' days')
                       GROUP BY pipeline_role""",
                f"-{days}",
            )
        except Exception as exc:
            logger.warning("Failed to get usage by role: %s", exc)
            return []

    async def get_daily_trend(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get daily token/cost trend for charting."""
        await self.ensure_tables()
        try:
            return await self.db.fetch_dicts(
                """SELECT
                        DATE(timestamp) as day,
                        COUNT(*) as requests,
                        SUM(total_tokens) as tokens,
                        SUM(actual_cost_usd) as actual_cost,
                        SUM(savings_usd) as savings
                       FROM inference_log
                       WHERE timestamp >= datetime('now', ? || ' days')
                       GROUP BY DATE(timestamp)
                       ORDER BY day""",
                f"-{days}",
            )
        except Exception as exc:
            logger.warning("Failed to get daily trend: %s", exc)
            return []

    # ── Recent entries (for Inference Log viewer) ────────────────

    async def get_recent_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent individual inference log entries."""
        await self.ensure_tables()
        try:
            return await self.db.fetch_dicts(
                """SELECT id, timestamp, backend_type, backend_name,
                              model, pipeline_role, input_tokens, output_tokens,
                              total_tokens, actual_cost_usd, cloud_equiv_cost_usd,
                              savings_usd, latency_ms, cached
                       FROM inference_log
                       ORDER BY id DESC
                       LIMIT ?""",
                limit,
            )
        except Exception as exc:
            logger.warning("Failed to get recent entries: %s", exc)
            return []

    # ── Budget management ──────────────────────────────────────

    async def set_budget(
        self, backend_name: str, period: str, budget_usd: float, alert_threshold: float = 0.8
    ) -> None:
        """Set a spending budget for a backend."""
        await self.ensure_tables()
        now = datetime.now()
        if period == "daily":
            period_start = now.strftime("%Y-%m-%d")
        elif period == "weekly":
            # Start of current week
            from datetime import timedelta
            start = now - timedelta(days=now.weekday())
            period_start = start.strftime("%Y-%m-%d")
        else:
            period_start = now.strftime("%Y-%m-01")

        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO cost_budgets
                       (backend_name, period, budget_usd, period_start, alert_threshold)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(backend_name, period) DO UPDATE SET
                           budget_usd = excluded.budget_usd,
                           alert_threshold = excluded.alert_threshold""",
                    (backend_name, period, budget_usd, period_start, alert_threshold),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to set budget: %s", exc)

    async def _check_budget_alerts(self, backend_name: str, cost: float) -> None:
        """Check if spending has exceeded budget thresholds."""
        try:
            async with self.db.acquire() as conn:
                async with conn.execute(
                    """SELECT id, period, budget_usd, current_spend_usd, alert_threshold, alert_sent
                       FROM cost_budgets WHERE backend_name = ?""",
                    (backend_name,),
                ) as cur:
                    budgets = await cur.fetchall()

                for b in budgets:
                    b = dict(b)
                    new_spend = b["current_spend_usd"] + cost
                    await conn.execute(
                        "UPDATE cost_budgets SET current_spend_usd = ? WHERE id = ?",
                        (new_spend, b["id"]),
                    )

                    # Check threshold
                    if not b["alert_sent"] and new_spend >= b["budget_usd"] * b["alert_threshold"]:
                        pct = round(new_spend / b["budget_usd"] * 100, 0)
                        logger.warning(
                            "BUDGET ALERT: %s %s budget is at %.0f%% ($%.4f / $%.2f)",
                            backend_name, b["period"], pct, new_spend, b["budget_usd"],
                        )
                        await conn.execute(
                            "UPDATE cost_budgets SET alert_sent = 1 WHERE id = ?",
                            (b["id"],),
                        )

                await conn.commit()
        except Exception as exc:
            logger.debug("Budget check failed: %s", exc)

    # ── Pricing management ─────────────────────────────────────

    def update_pricing(self, model: str, input_per_1m: float, output_per_1m: float) -> None:
        """Update pricing for a model."""
        self.pricing[model] = {"input": input_per_1m, "output": output_per_1m}

    def get_pricing_table(self) -> Dict[str, Dict[str, float]]:
        """Get the current pricing table."""
        return dict(self.pricing)
