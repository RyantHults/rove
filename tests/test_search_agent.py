"""Tests for the SearchAgent module."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from glean.config import GleanConfig
from glean.plugins.base import AuthenticationError, ContextItem
from glean.search_agent import SearchAgent


@pytest.fixture
def search_agent(mock_config: GleanConfig) -> SearchAgent:
    """Create a SearchAgent for testing."""
    return SearchAgent(mock_config)


class TestExtractReferences:
    """Tests for the _extract_references method."""

    def test_extracts_jira_tickets(self, search_agent: SearchAgent):
        """Test extracting JIRA ticket references."""
        items = [
            ContextItem(
                source="slack",
                item_type="message",
                title="Test message",
                content="Check out TB-123 and ABC-456 for details",
                url="http://example.com",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            )
        ]

        references = search_agent._extract_references(items)

        assert ("ticket", "TB-123") in references
        assert ("ticket", "ABC-456") in references

    def test_extracts_pr_references(self, search_agent: SearchAgent):
        """Test extracting PR references."""
        items = [
            ContextItem(
                source="slack",
                item_type="message",
                title="Test message",
                content="See PR #123 and pull #456 for implementation",
                url="http://example.com",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            )
        ]

        references = search_agent._extract_references(items)

        assert ("pr", "123") in references
        assert ("pr", "456") in references

    def test_extracts_issue_references(self, search_agent: SearchAgent):
        """Test extracting issue references."""
        items = [
            ContextItem(
                source="github",
                item_type="pr",
                title="Test PR",
                content="Fixes issue #789",
                url="http://example.com",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            )
        ]

        references = search_agent._extract_references(items)

        assert ("issue", "789") in references

    def test_deduplicates_references(self, search_agent: SearchAgent):
        """Test that duplicate references are removed."""
        items = [
            ContextItem(
                source="slack",
                item_type="message",
                title="Test 1",
                content="See TB-123 for details",
                url="http://example.com/1",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            ),
            ContextItem(
                source="slack",
                item_type="message",
                title="Test 2",
                content="Also check TB-123",
                url="http://example.com/2",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            ),
        ]

        references = search_agent._extract_references(items)

        # Should only appear once
        ticket_refs = [r for r in references if r == ("ticket", "TB-123")]
        assert len(ticket_refs) == 1


class TestExtractKeywords:
    """Tests for the _extract_keywords method."""

    @pytest.mark.asyncio
    async def test_extracts_keywords_from_ai(
        self,
        search_agent: SearchAgent,
        sample_context_item: ContextItem,
        mock_ai_client: MagicMock,
    ):
        """Test keyword extraction using AI."""
        with patch.object(search_agent, "_get_ai_client", return_value=mock_ai_client):
            keywords = await search_agent._extract_keywords(sample_context_item)

        assert "oauth" in keywords
        assert "authentication" in keywords
        assert len(keywords) >= 3

    @pytest.mark.asyncio
    async def test_fallback_on_ai_error(
        self,
        search_agent: SearchAgent,
        sample_context_item: ContextItem,
    ):
        """Test fallback keyword extraction when AI fails."""
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=Exception("API error")
        )

        with patch.object(search_agent, "_get_ai_client", return_value=mock_client):
            keywords = await search_agent._extract_keywords(sample_context_item)

        # Should return fallback keywords from title
        assert len(keywords) > 0


class TestExpandReference:
    """Tests for the _expand_reference method."""

    @pytest.mark.asyncio
    async def test_expands_ticket_reference(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
        sample_context_item: ContextItem,
    ):
        """Test expanding a ticket reference."""
        with patch(
            "glean.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_source_client
        ):
            result = await search_agent._expand_reference("ticket", "TB-123")

        assert result is not None
        assert result.source == "jira"

    @pytest.mark.asyncio
    async def test_returns_none_for_unsupported_type(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
    ):
        """Test that unsupported reference types return None."""
        # Mock client only supports "ticket", not "unknown"
        with patch(
            "glean.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_source_client
        ):
            result = await search_agent._expand_reference("unknown", "123")

        assert result is None


class TestSearch:
    """Tests for the main search method."""

    @pytest.mark.asyncio
    async def test_search_returns_items(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
        mock_ai_client: MagicMock,
        sample_context_item: ContextItem,
    ):
        """Test that search returns context items."""
        with patch(
            "glean.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_source_client
        ), patch.object(
            search_agent, "_get_ai_client", return_value=mock_ai_client
        ):
            results = await search_agent.search("TB-123")

        assert len(results) >= 1
        assert results[0].source == "jira"

    @pytest.mark.asyncio
    async def test_search_with_time_filters(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
        mock_ai_client: MagicMock,
    ):
        """Test search with since/until filters."""
        since = datetime(2024, 12, 1)
        until = datetime(2024, 12, 31)

        with patch(
            "glean.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_source_client
        ), patch.object(
            search_agent, "_get_ai_client", return_value=mock_ai_client
        ):
            await search_agent.search("TB-123", since=since, until=until)

        # Verify search was called with time filters
        mock_source_client.search.assert_called()
        call_kwargs = mock_source_client.search.call_args.kwargs
        assert call_kwargs.get("since") == since
        assert call_kwargs.get("until") == until

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_auth_failure(
        self,
        search_agent: SearchAgent,
    ):
        """Test that search raises AuthenticationError if authentication fails."""
        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = False
        mock_client.authenticate = AsyncMock(return_value=False)

        with patch(
            "glean.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_client
        ):
            with pytest.raises(AuthenticationError) as exc_info:
                await search_agent.search("TB-123")

        assert "Failed to authenticate with jira" in str(exc_info.value)
        assert "glean --add-source jira" in str(exc_info.value)



