"""Tests for the SearchAgent module."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rove.config import RoveConfig
from rove.plugins.base import AuthenticationError, ContextItem
from rove.search_agent import SearchAgent


@pytest.fixture
def search_agent(mock_config: RoveConfig) -> SearchAgent:
    """Create a SearchAgent for testing."""
    return SearchAgent(mock_config)


class TestExtractReferences:
    """Tests for the _extract_references method.

    Note: _extract_references now delegates to each plugin's extract_references()
    method instead of using hardcoded patterns. These tests verify the delegation
    and deduplication behavior.
    """

    def test_delegates_to_plugin_extract_references(
        self, search_agent: SearchAgent, mock_source_client: MagicMock
    ):
        """Test that extraction delegates to plugin's extract_references method."""
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

        # Configure mock to return references
        mock_source_client.extract_references.return_value = [
            ("ticket", "TB-123"),
            ("ticket", "ABC-456"),
        ]

        with patch(
            "rove.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_source_client
        ):
            references = search_agent._extract_references(items)

        # Verify plugin's extract_references was called
        mock_source_client.extract_references.assert_called_once_with(items)

        # Check references include the client
        assert len(references) == 2
        ref_types_and_ids = [(r[0], r[1]) for r in references]
        assert ("ticket", "TB-123") in ref_types_and_ids
        assert ("ticket", "ABC-456") in ref_types_and_ids
        # Each reference should include the client
        for ref in references:
            assert ref[2] == mock_source_client

    def test_aggregates_from_multiple_plugins(
        self, search_agent: SearchAgent
    ):
        """Test that references are aggregated from multiple plugins."""
        items = [
            ContextItem(
                source="slack",
                item_type="message",
                title="Test message",
                content="See PR #123 and TB-456",
                url="http://example.com",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            )
        ]

        mock_jira_client = MagicMock()
        mock_jira_client.is_authenticated.return_value = True
        mock_jira_client.extract_references.return_value = [("ticket", "TB-456")]

        mock_github_client = MagicMock()
        mock_github_client.is_authenticated.return_value = True
        mock_github_client.extract_references.return_value = [("pr", "123")]

        def get_client(source: str):
            if source == "jira":
                return mock_jira_client
            elif source == "github":
                return mock_github_client
            return None

        with patch(
            "rove.search_agent.list_plugins", return_value=["jira", "github"]
        ), patch.object(search_agent, "_get_source_client", side_effect=get_client):
            references = search_agent._extract_references(items)

        # Should have references from both plugins
        assert len(references) == 2
        ref_types_and_ids = [(r[0], r[1]) for r in references]
        assert ("ticket", "TB-456") in ref_types_and_ids
        assert ("pr", "123") in ref_types_and_ids

    def test_deduplicates_references_per_plugin(
        self, search_agent: SearchAgent, mock_source_client: MagicMock
    ):
        """Test that duplicate references from the same plugin are removed."""
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
        ]

        # Plugin returns duplicate references (could happen in real implementation)
        mock_source_client.extract_references.return_value = [
            ("ticket", "TB-123"),
            ("ticket", "TB-123"),  # Duplicate
        ]

        with patch(
            "rove.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_source_client
        ):
            references = search_agent._extract_references(items)

        # Should only appear once
        ticket_refs = [(r[0], r[1]) for r in references if r[0] == "ticket" and r[1] == "TB-123"]
        assert len(ticket_refs) == 1

    def test_skips_unauthenticated_clients(
        self, search_agent: SearchAgent
    ):
        """Test that unauthenticated clients are skipped."""
        items = [
            ContextItem(
                source="slack",
                item_type="message",
                title="Test message",
                content="See TB-123",
                url="http://example.com",
                timestamp=datetime.now(),
                author="test",
                metadata={},
            )
        ]

        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = False
        mock_client.extract_references.return_value = [("ticket", "TB-123")]

        with patch(
            "rove.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_client
        ):
            references = search_agent._extract_references(items)

        # Should be empty since client is not authenticated
        assert len(references) == 0
        mock_client.extract_references.assert_not_called()


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
    """Tests for the _expand_reference method.

    Note: _expand_reference now takes the client as a parameter since the
    caller (from _extract_references) already knows which client to use.
    """

    @pytest.mark.asyncio
    async def test_expands_ticket_reference(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
        sample_context_item: ContextItem,
    ):
        """Test expanding a ticket reference using the provided client."""
        result = await search_agent._expand_reference(
            "ticket", "TB-123", mock_source_client
        )

        assert result is not None
        assert result.source == "jira"
        mock_source_client.get_item_details.assert_called_once_with("TB-123")

    @pytest.mark.asyncio
    async def test_returns_none_on_client_error(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
    ):
        """Test that errors during expansion return None."""
        mock_source_client.get_item_details = AsyncMock(
            side_effect=Exception("API error")
        )

        result = await search_agent._expand_reference(
            "ticket", "TB-123", mock_source_client
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_item_not_found(
        self,
        search_agent: SearchAgent,
        mock_source_client: MagicMock,
    ):
        """Test that None is returned when item is not found."""
        mock_source_client.get_item_details = AsyncMock(return_value=None)

        result = await search_agent._expand_reference(
            "ticket", "NOTFOUND-999", mock_source_client
        )

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
            "rove.search_agent.list_plugins", return_value=["jira"]
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

        # Create a separate mock for the secondary source (slack)
        # Primary source (jira) is used for get_item_details, not search
        mock_slack_client = MagicMock()
        mock_slack_client.is_authenticated.return_value = True
        mock_slack_client.search = AsyncMock(return_value=[])
        mock_slack_client.supported_reference_types.return_value = ["message"]

        def get_client(source: str):
            if source == "slack":
                return mock_slack_client
            return mock_source_client

        with patch(
            "rove.search_agent.list_plugins", return_value=["jira", "slack"]
        ), patch.object(
            search_agent, "_get_source_client", side_effect=get_client
        ), patch.object(
            search_agent, "_get_ai_client", return_value=mock_ai_client
        ):
            await search_agent.search("TB-123", since=since, until=until)

        # Verify search was called on the secondary source with time filters
        mock_slack_client.search.assert_called()
        call_kwargs = mock_slack_client.search.call_args.kwargs
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
            "rove.search_agent.list_plugins", return_value=["jira"]
        ), patch.object(
            search_agent, "_get_source_client", return_value=mock_client
        ):
            with pytest.raises(AuthenticationError) as exc_info:
                await search_agent.search("TB-123")

        assert "Failed to authenticate with jira" in str(exc_info.value)
        assert "rove --add-source jira" in str(exc_info.value)



