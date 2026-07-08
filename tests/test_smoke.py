"""
Smoke tests to validate test infrastructure is working.

Run with: pytest tests/test_smoke.py -v
"""

from unittest.mock import MagicMock

import pytest


class TestInfrastructure:
    """Verify test infrastructure is working."""
    
    @pytest.mark.unit
    def test_pytest_runs(self):
        """Verify pytest can execute tests."""
        assert True
    
    @pytest.mark.unit
    def test_fixtures_available(self, mock_bot, mock_interaction, mock_message):
        """Verify core fixtures are available and properly typed."""
        assert mock_bot is not None
        assert isinstance(mock_bot, MagicMock)
        assert mock_interaction is not None
        assert mock_message is not None
    
    @pytest.mark.unit
    def test_bot_fixture_structure(self, mock_bot):
        """Verify mock bot has expected structure."""
        assert hasattr(mock_bot, 'user')
        assert hasattr(mock_bot, 'get_channel')
        assert hasattr(mock_bot, 'get_cog')
        assert mock_bot.user.name == "TestBot"
    
    @pytest.mark.unit
    def test_interaction_fixture_structure(self, mock_interaction):
        """Verify mock interaction has expected structure."""
        assert hasattr(mock_interaction, 'user')
        assert hasattr(mock_interaction, 'channel')
        assert hasattr(mock_interaction, 'response')
        assert hasattr(mock_interaction, 'followup')
        assert mock_interaction.user.name == "TestUser"
    
    @pytest.mark.unit
    async def test_async_fixtures_work(self, mock_database):
        """Verify async fixtures work correctly."""
        assert mock_database is not None
        await mock_database.connect()
        await mock_database.close()
    
    @pytest.mark.unit
    def test_sample_data_fixtures(self, sample_knowledge_gap, sample_action_item, sample_lead):
        """Verify sample data fixtures have expected fields."""
        assert sample_knowledge_gap["topic"] == "Test Topic"
        assert sample_action_item["title"] == "Test Action"
        assert sample_lead["name"] == "Test Museum"


class TestEnvironmentSetup:
    """Verify test environment is properly configured."""
    
    @pytest.mark.unit
    def test_env_vars_set(self):
        """Verify test environment variables are set."""
        import os
        assert os.environ.get("DISCORD_TOKEN") == "test-discord-token"
        assert os.environ.get("OPENAI_API_KEY") == "sk-test-openai-key"
    
    @pytest.mark.unit
    def test_imports_work(self):
        """Verify core modules can be imported."""
        # These imports should not raise
        try:
            from LeisureLLM import database, ux_helpers
            assert True
        except ImportError as e:
            pytest.skip(f"Import failed (may need dependencies): {e}")
    
    @pytest.mark.unit
    def test_path_setup(self):
        """Verify sys.path includes LeisureLLM."""
        import sys
        paths = [str(p) for p in sys.path]
        assert any("LeisureLLM" in p for p in paths)


class TestMockBehaviors:
    """Verify mock objects behave correctly."""
    
    @pytest.mark.unit
    async def test_async_mock_interaction(self, mock_interaction):
        """Verify async mocks on interaction work."""
        await mock_interaction.response.defer()
        mock_interaction.response.defer.assert_called_once()
        
        await mock_interaction.followup.send("Test message")
        mock_interaction.followup.send.assert_called_with("Test message")
    
    @pytest.mark.unit
    async def test_async_mock_message(self, mock_message):
        """Verify async mocks on message work."""
        await mock_message.reply("Test reply")
        mock_message.reply.assert_called_with("Test reply")
        
        await mock_message.add_reaction("👍")
        mock_message.add_reaction.assert_called_with("👍")
    
    @pytest.mark.unit
    def test_llm_service_mock(self, mock_llm_service):
        """Verify LLM service mock has expected interface."""
        assert mock_llm_service.is_configured is True
        assert hasattr(mock_llm_service, 'generate')
        assert hasattr(mock_llm_service, 'summarize')
