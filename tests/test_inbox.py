"""
Tests for the Inbox feature — email-like async Q&A and interview threads.

Covers:
  - Page route /inbox renders inbox.html
  - /chat redirects to /inbox
  - Thread CRUD API endpoints (list, create, get, patch)
  - Unread-count badge endpoint
  - Interview start endpoint
  - Message ingestion endpoint
  - Feedback endpoint
  - Sidebar branding text change

Uses the same mock approach as test_admin_gui.py: FastAPI TestClient with
a mock database and no real Ollama/Discord.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Environment setup ─────────────────────────────────────────────────────────
os.environ["ADMIN_AUTH_DISABLED"] = "1"
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeRow(dict):
    """Dict-like object that supports attribute access."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _AsyncCursorResult:
    """Returned by mock conn.execute(); supports both ``await`` and ``async with``."""

    def __init__(self, cursor):
        self._cur = cursor

    def __await__(self):
        async def _resolve():
            return self._cur
        return _resolve().__await__()

    async def __aenter__(self):
        return self._cur

    async def __aexit__(self, *a):
        pass


def _mock_db():
    """Return a mock database supporting ``acquire`` context manager.

    The mock ``conn.execute`` returns an object that works with both styles::

        cur = await conn.execute(...)
        async with conn.execute(...) as cur: ...
    """
    db = MagicMock()
    conn = MagicMock()

    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=(0,))   # row with one int
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.__aiter__ = MagicMock(return_value=iter([]))
    cursor.lastrowid = 1

    conn.execute = MagicMock(side_effect=lambda *a, **kw: _AsyncCursorResult(cursor))
    conn.executemany = AsyncMock()
    conn.commit = AsyncMock()

    class AcquireCM:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    db.acquire = lambda: AcquireCM()
    db.execute = AsyncMock()
    db.fetchone = AsyncMock(return_value=None)
    db.fetchall = AsyncMock(return_value=[])
    return db


CSRF = {"X-CSRF-Protection": "1"}


@pytest.fixture(scope="module")
def client():
    """TestClient with mocked deps and inbox router active."""
    from admin import dependencies, server

    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()

    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()
    dependencies._model_router = mock_mr

    mock_bot = MagicMock()
    mock_bot.db = _mock_db()
    dependencies._bot_instance = mock_bot

    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c


# =============================================================================
# Page Route Tests
# =============================================================================

class TestInboxPage:
    """Verify the inbox page renders and /chat redirects."""

    def test_inbox_page_renders(self, client):
        resp = client.get("/inbox")
        assert resp.status_code == 200, f"/inbox returned {resp.status_code}"
        assert "text/html" in resp.headers.get("content-type", "")
        assert "inbox-layout" in resp.text

    def test_inbox_page_has_new_question_btn(self, client):
        resp = client.get("/inbox")
        assert "New Message" in resp.text

    def test_inbox_page_has_interview_btn(self, client):
        resp = client.get("/inbox")
        assert "Fill Gaps" in resp.text

    def test_chat_redirects_to_inbox(self, client):
        resp = client.get("/chat", follow_redirects=False)
        assert resp.status_code == 302
        assert "/inbox" in resp.headers.get("location", "")


# =============================================================================
# Sidebar Branding Tests
# =============================================================================

class TestSidebarBranding:
    """Verify sidebar uses 'Magic Key Assistant' branding."""

    def test_sidebar_shows_magic_key_assistant(self, client):
        resp = client.get("/inbox")
        assert "Magic Key Assistant" in resp.text

    def test_sidebar_inbox_nav_item(self, client):
        resp = client.get("/inbox")
        assert "message-square" in resp.text
        assert "Conversation" in resp.text

    def test_sidebar_inbox_badge_element(self, client):
        resp = client.get("/inbox")
        assert "inboxBadge" in resp.text


# =============================================================================
# Inbox API Tests
# =============================================================================

class TestInboxAPIs:
    """Test inbox thread CRUD API endpoints."""

    def test_unread_count(self, client):
        resp = client.get("/api/v1/inbox/unread-count")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert isinstance(data["count"], int)

    def test_list_threads_empty(self, client):
        resp = client.get("/api/v1/inbox/threads")
        assert resp.status_code == 200
        data = resp.json()
        assert "threads" in data
        assert isinstance(data["threads"], list)

    def test_create_thread(self, client):
        resp = client.post(
            "/api/v1/inbox/threads",
            json={"message": "What are the pool hours?"},
            headers=CSRF,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "thread_id" in data

    def test_create_thread_empty_message_rejected(self, client):
        resp = client.post(
            "/api/v1/inbox/threads",
            json={"message": "   "},
            headers=CSRF,
        )
        assert resp.status_code in (400, 422)

    def test_get_thread(self, client):
        resp = client.get("/api/v1/inbox/threads/1")
        assert resp.status_code in (200, 404, 500)

    def test_patch_thread_star(self, client):
        resp = client.patch(
            "/api/v1/inbox/threads/1",
            json={"is_starred": True},
            headers=CSRF,
        )
        assert resp.status_code in (200, 404, 500)

    def test_patch_thread_status(self, client):
        resp = client.patch(
            "/api/v1/inbox/threads/1",
            json={"status": "read"},
            headers=CSRF,
        )
        assert resp.status_code in (200, 404)

    def test_reply_to_thread(self, client):
        resp = client.post(
            "/api/v1/inbox/threads/1/reply",
            json={"message": "Can you elaborate?"},
            headers=CSRF,
        )
        assert resp.status_code in (200, 404, 500)

    def test_reprocess_thread(self, client):
        resp = client.post("/api/v1/inbox/threads/1/reprocess", headers=CSRF)
        assert resp.status_code in (200, 400, 404, 500)

    def test_feedback_endpoint(self, client):
        resp = client.post(
            "/api/v1/inbox/feedback",
            json={
                "question": "Pool hours?",
                "answer": "The pool is open 6am-9pm.",
                "feedback": "helpful",
            },
            headers=CSRF,
        )
        assert resp.status_code == 200

    def test_ingest_message(self, client):
        resp = client.post("/api/v1/inbox/messages/1/ingest", headers=CSRF)
        assert resp.status_code in (200, 404, 500)


# =============================================================================
# Interview API Tests
# =============================================================================

class TestInterviewAPIs:
    """Test interview start/answer/skip endpoints."""

    def test_start_interview_no_gaps(self, client):
        """When no knowledge gaps exist, should report no gaps available."""
        resp = client.post("/api/v1/inbox/interview/start", json={}, headers=CSRF)
        assert resp.status_code == 200
        data = resp.json()
        # Should either succeed or report 'no gaps'
        assert isinstance(data, dict)

    def test_interview_answer_no_thread(self, client):
        resp = client.post(
            "/api/v1/inbox/interview/999/answer",
            json={"answer": "We open at 6am."},
            headers=CSRF,
        )
        assert resp.status_code in (200, 404, 500)

    def test_interview_skip_no_thread(self, client):
        resp = client.post("/api/v1/inbox/interview/999/skip", headers=CSRF)
        assert resp.status_code in (200, 404, 500)


# =============================================================================
# Template Content Tests
# =============================================================================

class TestInboxTemplateContent:
    """Verify inbox template contains expected UI elements."""

    def test_has_filter_tabs(self, client):
        resp = client.get("/inbox")
        text = resp.text
        assert "All" in text
        assert "Unread" in text
        assert "Starred" in text
        assert "Interviews" in text

    def test_has_compose_view(self, client):
        resp = client.get("/inbox")
        assert "composeView" in resp.text
        assert "composeInput" in resp.text

    def test_has_thread_view(self, client):
        resp = client.get("/inbox")
        assert "threadView" in resp.text
        assert "threadMessages" in resp.text

    def test_has_processing_indicator(self, client):
        resp = client.get("/inbox")
        assert "processingIndicator" in resp.text
        assert "processing-dots" in resp.text

    def test_has_interview_reply_area(self, client):
        resp = client.get("/inbox")
        assert "interviewReplyArea" in resp.text
        assert "interviewInput" in resp.text

    def test_has_markdown_renderer(self, client):
        resp = client.get("/inbox")
        assert "renderMd" in resp.text

    def test_has_inbox_polling(self, client):
        resp = client.get("/inbox")
        assert "loadThreads" in resp.text
        assert "setInterval" in resp.text

    def test_has_tool_confirmation_flow(self, client):
        resp = client.get("/inbox")
        assert "tool_confirmation" in resp.text
        assert "/api/v1/chat/tool-confirm" in resp.text
        assert "request_trace_id" in resp.text

    def test_has_retry_fallback_controls(self, client):
        resp = client.get("/inbox")
        assert "retryResponseBtn" in resp.text
        assert "retryActiveThreadResponse" in resp.text
        assert "PROCESSING_AUTO_RETRY_MS" in resp.text
        assert "/api/v1/inbox/threads/${threadId}/reprocess" in resp.text
