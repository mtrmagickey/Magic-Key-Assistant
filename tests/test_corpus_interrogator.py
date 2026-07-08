"""Tests for services.corpus_interrogator — self-interrogation framework."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from services.corpus_interrogator import (
    MAX_STRATEGIC_QUESTIONS,
    InterrogationFinding,
    InterrogationResult,
    _clusters_to_map,
    _format_coverage_map,
    _format_question_frequency,
    _parse_json_response,
    run_drilldown,
    run_strategic_interrogation,
)

# ── JSON parsing ──────────────────────────────────────────────────────────────


class TestParseJsonResponse:
    def test_clean_json(self):
        raw = '{"findings": [{"domain": "safety"}]}'
        result = _parse_json_response(raw)
        assert result["findings"][0]["domain"] == "safety"

    def test_markdown_fenced_json(self):
        raw = '```json\n{"findings": [{"domain": "ops"}]}\n```'
        result = _parse_json_response(raw)
        assert result["findings"][0]["domain"] == "ops"

    def test_trailing_text(self):
        raw = '{"findings": []} Here is some extra text.'
        result = _parse_json_response(raw)
        assert result["findings"] == []

    def test_preamble_text(self):
        raw = 'Here is the analysis:\n{"findings": [{"domain": "hr"}]}'
        result = _parse_json_response(raw)
        assert result["findings"][0]["domain"] == "hr"

    def test_empty_string(self):
        assert _parse_json_response("") == {}

    def test_no_json(self):
        assert _parse_json_response("No JSON here at all") == {}

    def test_nested_braces(self):
        raw = '{"findings": [{"domain": "x", "nested": {"a": 1}}]}'
        result = _parse_json_response(raw)
        assert result["findings"][0]["nested"]["a"] == 1


# ── Data formatting ───────────────────────────────────────────────────────────


class TestFormatCoverageMap:
    def test_empty_corpus(self):
        result = _format_coverage_map({"topics": {}, "summary": {}})
        assert "empty" in result.lower()

    def test_with_topics(self):
        cm = {
            "topics": {
                "Safety Procedures": {
                    "chunks": 12,
                    "primary_sources": 3,
                    "avg_confidence": 0.85,
                    "newest": "2025-01-15",
                    "unique_sources": 3,
                },
                "HR Policies": {
                    "chunks": 5,
                    "primary_sources": 1,
                    "avg_confidence": 0.65,
                    "newest": "2024-06-01",
                    "unique_sources": 1,
                },
            },
            "summary": {"total": 17, "unique_sources": 4},
        }
        result = _format_coverage_map(cm)
        assert "Safety Procedures" in result
        assert "HR Policies" in result
        # Safety should be listed first (more chunks)
        assert result.index("Safety") < result.index("HR")


class TestFormatQuestionFrequency:
    def test_empty(self):
        result = _format_question_frequency({})
        assert "no question frequency" in result.lower()

    def test_with_data(self):
        result = _format_question_frequency({"Pool hours": 5, "Pricing": 3})
        assert "Pool hours" in result
        assert "5" in result


# ── Cluster to map conversion ─────────────────────────────────────────────────


class TestClustersToMap:
    def test_empty_clusters(self):
        result = _clusters_to_map([], None)
        assert result["topics"] == {}

    def test_with_topic_clusters(self):
        cluster = MagicMock()
        cluster.topic = "Aquatics"
        cluster.chunk_count = 8
        cluster.primary_count = 2
        cluster.avg_confidence = 0.75
        cluster.newest_date = "2025-12-01"
        cluster.unique_sources = 2

        result = _clusters_to_map([cluster], {"total": 8})
        assert "Aquatics" in result["topics"]
        assert result["topics"]["Aquatics"]["chunks"] == 8
        assert result["summary"]["total"] == 8


# ── InterrogationResult summary ──────────────────────────────────────────────


class TestInterrogationResult:
    def test_empty_summary(self):
        result = InterrogationResult(run_id="abc123", run_date="2026-03-06")
        summary = result.summary()
        assert "abc123" in summary

    def test_summary_with_findings(self):
        result = InterrogationResult(
            run_id="abc123",
            run_date="2026-03-06",
            strategic_findings=[
                InterrogationFinding(
                    question="What safety procedures are missing?",
                    finding="No emergency evacuation docs found",
                    domain="Safety",
                    severity="critical",
                    action_type="human_review",
                ),
                InterrogationFinding(
                    question="What industry standards apply?",
                    finding="No standards references",
                    domain="Compliance",
                    severity="significant",
                    action_type="web_research",
                ),
            ],
            drilldown_findings=[
                InterrogationFinding(
                    question="What is the fire evacuation plan?",
                    domain="Safety",
                    tier="drill_down",
                )
            ],
            actions_taken={"web_researched": 1, "gaps_created": 1},
        )
        summary = result.summary()
        assert "critical" not in summary.lower()  # uses emoji icons, not text
        assert "🔴" in summary
        assert "🟠" in summary
        assert "web-researched" in summary
        assert "gap(s) created" in summary


# ── Strategic interrogation ──────────────────────────────────────────────────


class TestRunStrategicInterrogation:
    def test_returns_empty_without_llm(self):
        result = asyncio.run(run_strategic_interrogation(None, {}, {}))
        assert result == []

    def test_parses_llm_findings(self):
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=json.dumps({
            "findings": [
                {
                    "domain": "Safety",
                    "question": "What are the emergency procedures?",
                    "finding": "No emergency procedures documented",
                    "severity": "critical",
                    "action_type": "human_review",
                    "confidence": 0.9,
                },
                {
                    "domain": "Finance",
                    "question": "What is the pricing structure?",
                    "finding": "Pricing info scattered across multiple files",
                    "severity": "minor",
                    "action_type": "auto_close",
                    "confidence": 0.6,
                },
            ]
        }))

        result = asyncio.run(run_strategic_interrogation(
            llm,
            {"topics": {"ops": {"chunks": 5}}, "summary": {"total": 5}},
            {"Safety": 8},
        ))
        assert len(result) == 2
        assert result[0].domain == "Safety"
        assert result[0].severity == "critical"
        assert result[0].action_type == "human_review"
        assert result[1].domain == "Finance"

    def test_filters_low_confidence(self):
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=json.dumps({
            "findings": [
                {
                    "domain": "Vague",
                    "question": "Something maybe?",
                    "finding": "Not sure",
                    "severity": "minor",
                    "action_type": "human_review",
                    "confidence": 0.1,
                },
            ]
        }))

        result = asyncio.run(run_strategic_interrogation(llm, {}, {}))
        assert len(result) == 0  # filtered out

    def test_handles_llm_error(self):
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM down"))

        result = asyncio.run(run_strategic_interrogation(llm, {}, {}))
        assert result == []


# ── Drill-down ────────────────────────────────────────────────────────────────


class TestRunDrilldown:
    def test_generates_subquestions(self):
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=json.dumps({
            "drilldowns": [
                {
                    "question": "What is the fire evacuation plan for Building A?",
                    "domain": "Safety",
                    "action_type": "human_review",
                    "rationale": "No evacuation plan documented for main building",
                },
                {
                    "question": "What fire safety regulations apply to leisure centres?",
                    "domain": "Safety / Compliance",
                    "action_type": "web_research",
                    "rationale": "Industry standards should be referenced",
                },
            ]
        }))

        parent = InterrogationFinding(
            question="What safety procedures are missing?",
            finding="No emergency evacuation docs found",
            domain="Safety",
            severity="critical",
            action_type="human_review",
        )

        result = asyncio.run(run_drilldown(llm, parent))
        assert len(result) == 2
        assert result[0].tier == "drill_down"
        assert result[0].action_type == "human_review"
        assert result[1].action_type == "web_research"

    def test_returns_empty_without_llm(self):
        parent = InterrogationFinding(question="test", domain="test")
        result = asyncio.run(run_drilldown(None, parent))
        assert result == []


# ── InterrogationFinding ─────────────────────────────────────────────────────


class TestInterrogationFinding:
    def test_defaults(self):
        f = InterrogationFinding(question="test?")
        assert f.tier == "strategic"
        assert f.severity == "minor"
        assert f.action_type == ""
        assert f.confidence == 0.0
        assert f.parent_id is None
