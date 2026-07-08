"""
Pytest configuration and shared fixtures for LeisureLLM tests.

Run tests with:
    pytest                          # Run all tests
    pytest -m unit                  # Run only unit tests
    pytest -m "not slow"            # Skip slow tests
    pytest --cov=LeisureLLM         # With coverage report
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Add LeisureLLM to path for imports
ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

# Set test environment variables BEFORE importing config
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")


# ============================================================
# Event Loop Fixture
# ============================================================

@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ============================================================
# Mock Discord Bot
# ============================================================

@pytest.fixture
def mock_bot() -> MagicMock:
    """Create a mock Discord bot instance."""
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 123456789
    bot.user.name = "TestBot"
    bot.guilds = []
    bot.get_channel = MagicMock(return_value=None)
    bot.get_cog = MagicMock(return_value=None)
    bot.wait_until_ready = AsyncMock()
    bot.is_owner = AsyncMock(return_value=False)
    bot.db = None
    bot.service_container = None
    return bot


@pytest.fixture
def mock_interaction() -> MagicMock:
    """Create a mock Discord interaction."""
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 987654321
    interaction.user.name = "TestUser"
    interaction.user.display_name = "Test User"
    interaction.channel = MagicMock()
    interaction.channel.id = 111222333
    interaction.channel.name = "test-channel"
    interaction.guild = MagicMock()
    interaction.guild.id = 444555666
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.client = MagicMock()
    interaction.client.is_owner = AsyncMock(return_value=False)
    return interaction


@pytest.fixture
def mock_message() -> MagicMock:
    """Create a mock Discord message."""
    message = MagicMock()
    message.id = 999888777
    message.content = "Test message content"
    message.author = MagicMock()
    message.author.id = 987654321
    message.author.name = "TestUser"
    message.author.bot = False
    message.channel = MagicMock()
    message.channel.id = 111222333
    message.channel.name = "test-channel"
    message.channel.send = AsyncMock()
    message.guild = MagicMock()
    message.guild.id = 444555666
    message.reply = AsyncMock()
    message.add_reaction = AsyncMock()
    return message


# ============================================================
# Mock Database
# ============================================================

@pytest.fixture
async def mock_database() -> AsyncGenerator[MagicMock, None]:
    """Create a mock database instance."""
    db = MagicMock()
    db.connect = AsyncMock()
    db.close = AsyncMock()
    db.connection = MagicMock()
    
    # Mock execute that returns async context manager
    async def mock_execute(*args, **kwargs):
        cursor = MagicMock()
        cursor.fetchone = AsyncMock(return_value=None)
        cursor.fetchall = AsyncMock(return_value=[])
        cursor.fetchmany = AsyncMock(return_value=[])
        return cursor
    
    db.connection.execute = mock_execute
    db.connection.executemany = AsyncMock()
    db.connection.commit = AsyncMock()
    
    yield db


# ============================================================
# Mock LLM Service
# ============================================================

@pytest.fixture
def mock_llm_service() -> MagicMock:
    """Create a mock LLM service."""
    llm = MagicMock()
    llm.generate = AsyncMock(return_value="Mock LLM response")
    llm.summarize = AsyncMock(return_value="Mock summary")
    llm.is_configured = True
    return llm


@pytest.fixture
def mock_openai_response() -> dict:
    """Create a mock OpenAI API response."""
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "This is a mock response from the LLM."
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150
        }
    }


# ============================================================
# Mock Service Container
# ============================================================

@pytest.fixture
def mock_service_container(mock_llm_service: MagicMock) -> MagicMock:
    """Create a mock service container."""
    container = MagicMock()
    container.llm = mock_llm_service
    container.tavily = MagicMock()
    container.tavily.is_configured = True
    container.tavily.search = AsyncMock(return_value=[])
    return container


# ============================================================
# Test Data Fixtures
# ============================================================

@pytest.fixture
def sample_knowledge_gap() -> dict:
    """Sample knowledge gap data."""
    return {
        "id": 1,
        "topic": "Test Topic",
        "description": "This is a test knowledge gap",
        "source_question": "What is the test?",
        "priority": "medium",
        "status": "open",
        "created_at": "2026-01-14T12:00:00Z",
    }


@pytest.fixture
def sample_action_item() -> dict:
    """Sample action item data."""
    return {
        "id": 1,
        "title": "Test Action",
        "description": "This is a test action item",
        "assigned_to": "TestUser",
        "due_date": "2026-01-21",
        "status": "open",
        "priority": "normal",
        "created_at": "2026-01-14T12:00:00Z",
    }


@pytest.fixture
def sample_lead() -> dict:
    """Sample lead/opportunity data."""
    return {
        "id": 1,
        "name": "Test Museum",
        "contact_name": "Jane Doe",
        "contact_info": "jane@testmuseum.org",
        "stage": "prospect",
        "value": 50000,
        "notes": "Interested in interactive exhibits",
        "created_at": "2026-01-14T12:00:00Z",
    }


# ============================================================
# Utility Fixtures
# ============================================================

@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database path for testing."""
    return tmp_path / "test_assistant.db"


@pytest.fixture
def mock_chroma_collection() -> MagicMock:
    """Create a mock Chroma collection."""
    collection = MagicMock()
    collection.query = MagicMock(return_value={
        "ids": [["doc1", "doc2"]],
        "documents": [["Document 1 content", "Document 2 content"]],
        "metadatas": [[{"source": "test.txt"}, {"source": "test2.txt"}]],
        "distances": [[0.1, 0.2]]
    })
    collection.add = MagicMock()
    collection.delete = MagicMock()
    collection.count = MagicMock(return_value=100)
    return collection


@pytest.fixture
def solo_web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_loop: asyncio.AbstractEventLoop):
    """Create a real web-only TestClient with isolated docs and DB paths.

    This fixture is intended for product-claim regression tests around
    operational continuity in solo/web mode and does not require Discord.
    """
    db_path = tmp_path / "solo_web_mode.db"
    docs_path = tmp_path / "docs"
    persist_path = tmp_path / "chroma"
    hash_csv_path = tmp_path / "hashes_v3.csv"
    docs_path.mkdir(parents=True, exist_ok=True)
    persist_path.mkdir(parents=True, exist_ok=True)
    hash_csv_path.write_text("path,sha256\n", encoding="utf-8")

    monkeypatch.setenv("ADMIN_AUTH_DISABLED", "0")
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OPERATION_MODE", "solo")

    import admin.dependencies as dependencies
    import admin.server as server
    from database import Database

    import config as root_config
    import LeisureLLM.config as llm_config

    previous_db = getattr(dependencies, "_standalone_db", None)
    previous_bot = getattr(dependencies, "_bot_instance", None)
    previous_router = getattr(dependencies, "_model_router", None)

    monkeypatch.setattr(llm_config, "directory_path", str(docs_path), raising=False)
    monkeypatch.setattr(llm_config, "persist_directory", str(persist_path), raising=False)
    monkeypatch.setattr(llm_config, "hash_csv", str(hash_csv_path), raising=False)
    monkeypatch.setattr(root_config, "directory_path", str(docs_path), raising=False)
    monkeypatch.setattr(root_config, "persist_directory", str(persist_path), raising=False)
    monkeypatch.setattr(root_config, "hash_csv", str(hash_csv_path), raising=False)

    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()
    server._login_attempts.clear()

    db = Database(str(db_path))
    event_loop.run_until_complete(db.connect())

    dependencies._standalone_db = db
    dependencies._bot_instance = None

    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()
    dependencies._model_router = mock_mr

    try:
        with patch.object(server, "_ensure_admin_token", return_value="bootstrap-secret"):
            with TestClient(server.app, raise_server_exceptions=False) as client:
                yield {
                    "client": client,
                    "db": db,
                    "docs_path": docs_path,
                    "persist_path": persist_path,
                    "hash_csv_path": hash_csv_path,
                }
    finally:
        dependencies._standalone_db = previous_db
        dependencies._bot_instance = previous_bot
        dependencies._model_router = previous_router
        event_loop.run_until_complete(db.close())


@pytest.fixture
def bootstrap_admin_session():
    """Return a helper that boots the first web admin and leaves the session authenticated."""

    def _bootstrap(client: TestClient, *, username: str = "owner", password: str = "OwnerPass123", display_name: str = "Owner Admin"):
        response = client.post(
            "/api/v1/auth/bootstrap",
            json={
                "bootstrap_token": "bootstrap-secret",
                "username": username,
                "password": password,
                "display_name": display_name,
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert response.status_code == 200
        return response.json()

    return _bootstrap
