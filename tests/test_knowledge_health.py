"""Tests for services.knowledge_health — confidence decay & self-healing."""

import math
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from services.knowledge_health import (
    CONFIDENCE_FLOOR,
    CONFIDENCE_HALF_LIFE_DAYS,
    apply_confidence_decay_to_results,
    compute_decayed_confidence,
)

# ── compute_decayed_confidence ────────────────────────────────────────────────

class TestComputeDecayedConfidence:
    def test_no_decay_for_recent_doc(self):
        """A document modified today should have ~100% of original confidence."""
        today = datetime.utcnow().isoformat()
        result = compute_decayed_confidence(0.9, today)
        assert result == pytest.approx(0.9, abs=0.01)

    def test_half_decay_at_half_life(self):
        """At exactly one half-life, confidence should be halved."""
        now = datetime(2024, 6, 15)
        modified = (now - timedelta(days=CONFIDENCE_HALF_LIFE_DAYS)).isoformat()
        result = compute_decayed_confidence(1.0, modified, now=now)
        assert result == pytest.approx(0.5, abs=0.01)

    def test_floor_enforced(self):
        """Very old documents should never go below the floor."""
        ancient = "2010-01-01"
        result = compute_decayed_confidence(0.9, ancient)
        assert result >= CONFIDENCE_FLOOR

    def test_custom_half_life(self):
        now = datetime(2024, 6, 15)
        modified = (now - timedelta(days=90)).isoformat()
        result = compute_decayed_confidence(1.0, modified, half_life_days=90, now=now)
        assert result == pytest.approx(0.5, abs=0.01)

    def test_empty_date_returns_original(self):
        assert compute_decayed_confidence(0.8, "") == 0.8

    def test_none_date_returns_original(self):
        # None not str, but we handle gracefully
        assert compute_decayed_confidence(0.8, None) == 0.8

    def test_bad_date_returns_original(self):
        assert compute_decayed_confidence(0.7, "not-a-date") == 0.7

    def test_zero_age_returns_original(self):
        now = datetime(2024, 6, 15)
        result = compute_decayed_confidence(0.85, "2024-06-15", now=now)
        assert result == pytest.approx(0.85, abs=0.001)

    def test_double_half_life_is_quarter(self):
        """Two half-lives should give ~25% of original."""
        now = datetime(2024, 6, 15)
        modified = (now - timedelta(days=2 * CONFIDENCE_HALF_LIFE_DAYS)).isoformat()
        result = compute_decayed_confidence(1.0, modified, now=now)
        assert result == pytest.approx(0.25, abs=0.01)


# ── apply_confidence_decay_to_results ─────────────────────────────────────────

def _make_doc(confidence=None, file_modified=None, doc_date=None):
    """Create a mock LangChain Document."""
    meta = {}
    if confidence is not None:
        meta["llm_confidence"] = confidence
    if file_modified is not None:
        meta["file_modified"] = file_modified
    if doc_date is not None:
        meta["doc_date"] = doc_date
    return SimpleNamespace(page_content="test", metadata=meta)


class TestApplyConfidenceDecay:
    def test_adds_decayed_metadata(self):
        today = datetime.utcnow().isoformat()
        docs = [_make_doc(confidence=0.9, file_modified=today)]
        result = apply_confidence_decay_to_results(docs)
        assert len(result) == 1
        assert "llm_confidence_decayed" in result[0].metadata
        assert "llm_confidence_original" in result[0].metadata
        assert result[0].metadata["llm_confidence_original"] == 0.9

    def test_skips_docs_without_confidence(self):
        docs = [_make_doc(file_modified="2024-01-01")]
        result = apply_confidence_decay_to_results(docs)
        assert "llm_confidence_decayed" not in result[0].metadata

    def test_skips_docs_without_date(self):
        docs = [_make_doc(confidence=0.8)]
        result = apply_confidence_decay_to_results(docs)
        assert "llm_confidence_decayed" not in result[0].metadata

    def test_skips_docs_with_no_metadata(self):
        doc = SimpleNamespace(page_content="no meta")
        result = apply_confidence_decay_to_results([doc])
        assert len(result) == 1

    def test_uses_doc_date_fallback(self):
        docs = [_make_doc(confidence=0.7, doc_date="2024-01-01")]
        result = apply_confidence_decay_to_results(docs)
        assert "llm_confidence_decayed" in result[0].metadata

    def test_returns_same_list(self):
        """apply_confidence_decay_to_results mutates in-place and returns same list."""
        docs = [_make_doc(confidence=0.5, file_modified="2024-01-01")]
        result = apply_confidence_decay_to_results(docs)
        assert result is docs

    def test_multiple_docs(self):
        today = datetime.utcnow().isoformat()
        old = (datetime.utcnow() - timedelta(days=365)).isoformat()
        docs = [
            _make_doc(confidence=0.9, file_modified=today),
            _make_doc(confidence=0.9, file_modified=old),
        ]
        apply_confidence_decay_to_results(docs)
        # Recent doc should have higher decayed confidence than old doc
        assert docs[0].metadata["llm_confidence_decayed"] > docs[1].metadata["llm_confidence_decayed"]
