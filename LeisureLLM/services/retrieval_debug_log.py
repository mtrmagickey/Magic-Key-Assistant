"""
Retrieval debug log — in-memory ring buffer capturing every stage of
the RAG pipeline so you can inspect exactly what happened for each query.

Usage:
    from services.retrieval_debug_log import retrieval_logger

    trace = retrieval_logger.start_trace(query)
    trace.log_stage("hyde_original", docs=[...], scores=[...])
    ...
    trace.finish()

    # Read back
    retrieval_logger.get_traces(limit=20)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

_MAX_TRACES = 100  # keep last N query traces in memory


@dataclass
class RetrievalStage:
    """One stage in the retrieval pipeline."""
    name: str
    timestamp: float
    elapsed_ms: float = 0.0
    doc_count: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    # First few doc previews (content truncated + scores)
    docs_preview: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalTrace:
    """Full trace of one query through the RAG pipeline."""
    trace_id: str
    query: str
    started_at: float
    finished_at: Optional[float] = None
    entry_point: str = "web_chat"  # web_chat | inbox | discord
    stages: List[RetrievalStage] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    _stage_start: float = field(default=0.0, repr=False)

    def begin_stage(self, name: str):
        """Mark the start of a new stage (call log_stage to record it)."""
        self._stage_start = time.time()

    def log_stage(
        self,
        name: str,
        *,
        docs: Optional[list] = None,
        scores: Optional[List[float]] = None,
        detail: Optional[Dict[str, Any]] = None,
        doc_count: Optional[int] = None,
    ):
        """Record a pipeline stage with optional document previews."""
        now = time.time()
        elapsed = (now - self._stage_start) * 1000 if self._stage_start else 0.0

        previews = []
        actual_count = 0
        if docs:
            actual_count = len(docs)
            for i, doc in enumerate(docs[:8]):
                content = ""
                meta = {}
                score = None
                if hasattr(doc, "page_content"):
                    content = doc.page_content[:300]
                    meta = dict(doc.metadata) if doc.metadata else {}
                    score = meta.get("retrieval_score")
                elif isinstance(doc, tuple) and len(doc) == 2:
                    d, s = doc
                    if hasattr(d, "page_content"):
                        content = d.page_content[:300]
                        meta = dict(d.metadata) if d.metadata else {}
                    score = float(s) if s is not None else None

                preview = {"rank": i + 1, "content_preview": content}
                if score is not None:
                    preview["score"] = round(score, 4)
                # Include useful metadata
                for key in ("source", "source_relpath", "llm_content_type",
                            "llm_actionability", "llm_confidence", "status",
                            "doc_date", "retrieval_score"):
                    if key in meta and meta[key] is not None:
                        preview[key] = meta[key]
                previews.append(preview)

            if scores and not any("score" in p for p in previews):
                for j, p in enumerate(previews):
                    if j < len(scores):
                        p["score"] = round(float(scores[j]), 4)

        stage = RetrievalStage(
            name=name,
            timestamp=now,
            elapsed_ms=round(elapsed, 1),
            doc_count=doc_count if doc_count is not None else actual_count,
            detail=detail or {},
            docs_preview=previews,
        )
        self.stages.append(stage)
        self._stage_start = now  # chain for next stage

    def finish(self, **summary_kwargs):
        """Mark the trace as complete."""
        self.finished_at = time.time()
        self.summary.update(summary_kwargs)
        self.summary["total_ms"] = round((self.finished_at - self.started_at) * 1000, 1)
        self.summary["stage_count"] = len(self.stages)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "query": self.query,
            "entry_point": self.entry_point,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "stages": [s.to_dict() for s in self.stages],
        }


class RetrievalDebugLog:
    """Thread-safe ring buffer of retrieval traces."""

    def __init__(self, max_traces: int = _MAX_TRACES):
        self._traces: deque[RetrievalTrace] = deque(maxlen=max_traces)
        self._lock = threading.Lock()
        self._counter = 0

    def start_trace(self, query: str, entry_point: str = "web_chat") -> RetrievalTrace:
        with self._lock:
            self._counter += 1
            trace_id = f"rt-{self._counter:06d}"
        trace = RetrievalTrace(
            trace_id=trace_id,
            query=query,
            started_at=time.time(),
            entry_point=entry_point,
        )
        trace._stage_start = trace.started_at
        with self._lock:
            self._traces.append(trace)
        return trace

    def get_traces(self, limit: int = 20) -> List[dict]:
        with self._lock:
            items = list(self._traces)
        # Newest first
        items.reverse()
        return [t.to_dict() for t in items[:limit]]

    def get_trace(self, trace_id: str) -> Optional[dict]:
        with self._lock:
            for t in self._traces:
                if t.trace_id == trace_id:
                    return t.to_dict()
        return None

    def clear(self):
        with self._lock:
            self._traces.clear()


# Module-level singleton
retrieval_logger = RetrievalDebugLog()
