"""
Tests for ScoutMixin persona.

Tests the Scout persona's web research and novelty detection capabilities.
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "LeisureLLM"))

from cogs.personas.scout import ScoutMixin


class MockScout(ScoutMixin):
    """Test harness that provides Scout's required dependencies."""
    
    def __init__(self, tmp_path: Path):
        self._tmp_path = tmp_path
        self.llm_service = AsyncMock()
        self.tavily_service = MagicMock()
        self.tavily_service.is_configured = True
    
    @property
    def _scout_state_path(self) -> Path:
        """Override to use temp directory."""
        return self._tmp_path / "scout_state.json"


class TestScoutStateManagement:
    """Test Scout state persistence."""
    
    def test_load_empty_state(self, tmp_path):
        """Loading non-existent state returns defaults."""
        scout = MockScout(tmp_path)
        state = scout._load_scout_state()
        
        assert "seen_urls" in state
        assert "seen_domains" in state
        assert "seed_queue" in state
        assert state["seen_urls"] == {}
        assert state["seen_domains"] == {}
        assert state["seed_queue"] == []
    
    def test_save_and_load_state(self, tmp_path):
        """State persists through save/load cycle."""
        scout = MockScout(tmp_path)
        
        state = {
            "seen_urls": {"https://example.com": "2025-01-01"},
            "seen_domains": {"example.com": 1},
            "seed_queue": [{"query": "test"}],
            "last_cleanup": "2025-01-01",
        }
        scout._save_scout_state(state)
        
        loaded = scout._load_scout_state()
        assert loaded["seen_urls"] == {"https://example.com": "2025-01-01"}
        assert loaded["seen_domains"] == {"example.com": 1}
        assert loaded["seed_queue"] == [{"query": "test"}]
    
    def test_cleanup_bounds_seen_urls(self, tmp_path):
        """Cleanup keeps seen_urls bounded."""
        scout = MockScout(tmp_path)
        
        # Create state with >3000 URLs
        state = {
            "seen_urls": {f"https://example{i}.com": f"2025-01-{i % 28 + 1:02d}" for i in range(3500)},
            "seen_domains": {},
            "seed_queue": [],
            "last_cleanup": None,
        }
        
        cleaned = scout._scout_cleanup_state(state, "2025-01-15")
        assert len(cleaned["seen_urls"]) <= 2500
    
    def test_cleanup_bounds_seed_queue(self, tmp_path):
        """Cleanup keeps seed_queue bounded."""
        scout = MockScout(tmp_path)
        
        state = {
            "seen_urls": {},
            "seen_domains": {},
            "seed_queue": [{"query": f"query {i}"} for i in range(100)],
            "last_cleanup": None,
        }
        
        cleaned = scout._scout_cleanup_state(state, "2025-01-15")
        assert len(cleaned["seed_queue"]) <= 50


class TestDomainExtraction:
    """Test URL/domain utilities."""
    
    @pytest.mark.parametrize("url,expected", [
        ("https://example.com/page", "example.com"),
        ("https://www.example.com/page", "example.com"),
        ("http://subdomain.example.com", "subdomain.example.com"),
        ("invalid-url", None),
        ("", None),
    ])
    def test_domain_from_url(self, tmp_path, url, expected):
        """Domain extraction handles various URL formats."""
        scout = MockScout(tmp_path)
        result = scout._domain_from_url(url)
        assert result == expected


class TestNoveltyDetection:
    """Test finding novelty scoring."""
    
    def test_novel_url_scores_higher(self, tmp_path):
        """Never-seen URLs get novelty bonus."""
        scout = MockScout(tmp_path)
        
        # Seed state with one seen URL
        state = {
            "seen_urls": {"https://old.com": "2025-01-01"},
            "seen_domains": {"old.com": 5},
            "seed_queue": [],
            "last_cleanup": None,
        }
        scout._save_scout_state(state)
        
        findings = [
            {"url": "https://old.com", "score": 0.9, "title": "Old"},
            {"url": "https://new.com", "score": 0.8, "title": "New"},
        ]
        
        novel = scout._select_novel_findings(findings, "2025-01-15")
        
        # Novel URL should appear (it gets novelty bonus)
        urls = [f["url"] for f in novel]
        assert "https://new.com" in urls
    
    def test_rare_domain_scores_higher(self, tmp_path):
        """Rarely-seen domains get bonus."""
        scout = MockScout(tmp_path)
        
        state = {
            "seen_urls": {},
            "seen_domains": {"common.com": 10},
            "seed_queue": [],
            "last_cleanup": None,
        }
        scout._save_scout_state(state)
        
        findings = [
            {"url": "https://common.com/page1", "score": 0.9, "title": "Common"},
            {"url": "https://rare.com/page1", "score": 0.85, "title": "Rare"},
        ]
        
        novel = scout._select_novel_findings(findings, "2025-01-15")
        
        # Rare domain should be included due to novelty bonus
        urls = [f["url"] for f in novel]
        assert "https://rare.com/page1" in urls


class TestDefaultPlan:
    """Test fallback Scout plan generation."""
    
    def test_default_plan_returns_three_items(self, tmp_path):
        """Default plan provides 3 research paths."""
        scout = MockScout(tmp_path)
        plan = scout._default_scout_plan()
        
        assert len(plan) == 3
    
    def test_default_plan_has_required_fields(self, tmp_path):
        """Each plan item has required fields."""
        scout = MockScout(tmp_path)
        plan = scout._default_scout_plan()
        
        for item in plan:
            assert "tag" in item
            assert "query" in item
            assert "rationale" in item
            assert "perspective" in item


class TestSeedQueueManagement:
    """Test seed query queue operations."""
    
    def test_pop_seed_queries_empty(self, tmp_path):
        """Popping from empty queue returns empty list."""
        scout = MockScout(tmp_path)
        result = scout._scout_pop_seed_queries(max_items=2)
        assert result == []
    
    def test_pop_seed_queries_returns_items(self, tmp_path):
        """Popping returns queued items and removes them."""
        scout = MockScout(tmp_path)
        
        state = {
            "seen_urls": {},
            "seen_domains": {},
            "seed_queue": [
                {"query": "test1", "tag": "Tag1"},
                {"query": "test2", "tag": "Tag2"},
                {"query": "test3", "tag": "Tag3"},
            ],
            "last_cleanup": None,
        }
        scout._save_scout_state(state)
        
        result = scout._scout_pop_seed_queries(max_items=2)
        assert len(result) == 2
        
        # Check remaining
        reloaded = scout._load_scout_state()
        assert len(reloaded["seed_queue"]) == 1
