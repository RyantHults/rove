"""Tests for the ContextBuilder module."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rove.config import RoveConfig
from rove.context_builder import ContextBuilder
from rove.database import Database
from rove.plugins.base import ContextItem


@pytest.fixture
async def context_builder(test_db: Database, mock_config: RoveConfig) -> ContextBuilder:
    """Create a ContextBuilder for testing."""
    return ContextBuilder(test_db, mock_config)


class TestExtractKeywords:
    """Tests for the _extract_keywords method."""

    @pytest.mark.asyncio
    async def test_extracts_keywords_from_ai(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
        mock_ai_client: MagicMock,
    ):
        """Test keyword extraction using AI."""
        with patch.object(context_builder, "_get_ai_client", return_value=mock_ai_client):
            keywords = await context_builder._extract_keywords(sample_context_items)

        assert len(keywords) > 0
        # Keywords should be lowercase and alphanumeric
        for kw in keywords:
            assert kw.isalnum() or "_" in kw

    @pytest.mark.asyncio
    async def test_fallback_on_ai_error(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
    ):
        """Test fallback keyword extraction when AI fails."""
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=Exception("API error")
        )

        with patch.object(context_builder, "_get_ai_client", return_value=mock_client):
            keywords = await context_builder._extract_keywords(sample_context_items)

        # Should return fallback keywords
        assert len(keywords) > 0


class TestGroupByTopic:
    """Tests for the _group_by_topic method."""

    @pytest.mark.asyncio
    async def test_groups_small_set_by_type(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
    ):
        """Test that small item sets are grouped by type."""
        # With <= 3 items, should use simple type-based grouping
        items = sample_context_items[:2]

        grouped = await context_builder._group_by_topic(items)

        assert len(grouped) > 0
        # Each group should have at least one item
        for topic, group_items in grouped.items():
            assert len(group_items) > 0

    @pytest.mark.asyncio
    async def test_groups_large_set_via_ai(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
    ):
        """Test that larger item sets use AI for grouping."""
        # Create more items to trigger AI grouping
        items = sample_context_items * 3  # 9 items

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"Discussion": [0, 1, 2], "Code": [3, 4, 5]}'

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch.object(context_builder, "_get_ai_client", return_value=mock_client):
            grouped = await context_builder._group_by_topic(items)

        assert "Discussion" in grouped or "Code" in grouped


class TestGenerateMarkdown:
    """Tests for the _generate_markdown method."""

    def test_generates_valid_markdown(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
    ):
        """Test that generated markdown is valid."""
        grouped = {"Primary Ticket": sample_context_items[:1], "Discussion": sample_context_items[1:]}

        markdown = context_builder._generate_markdown("TB-123", sample_context_items, grouped)

        # Check required sections
        assert "# Context:" in markdown
        assert "TB-123" in markdown
        assert "## Primary Ticket" in markdown
        assert "## Discussion" in markdown
        assert "## Sources Consulted" in markdown

    def test_includes_sources_table(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
    ):
        """Test that sources table is included."""
        grouped = {"All": sample_context_items}

        markdown = context_builder._generate_markdown("TB-123", sample_context_items, grouped)

        assert "| Source | Items Found | Last Updated |" in markdown
        assert "| JIRA |" in markdown
        assert "| SLACK |" in markdown
        assert "| GITHUB |" in markdown

    def test_includes_references(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
    ):
        """Test that reference links are included."""
        grouped = {"All": sample_context_items}

        markdown = context_builder._generate_markdown("TB-123", sample_context_items, grouped)

        assert "## References" in markdown
        assert "[1]:" in markdown


class TestBuild:
    """Tests for the main build method."""

    @pytest.mark.asyncio
    async def test_creates_context_file(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
        mock_ai_client: MagicMock,
        tmp_path: Path,
    ):
        """Test that build creates a context file."""
        with patch.object(context_builder, "_get_ai_client", return_value=mock_ai_client):
            filename = await context_builder.build(
                "TB-123",
                sample_context_items,
                output_dir=tmp_path,
            )

        assert filename.startswith("TB-123_")
        assert filename.endswith(".md")
        assert (tmp_path / filename).exists()

    @pytest.mark.asyncio
    async def test_updates_database(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
        mock_ai_client: MagicMock,
        tmp_path: Path,
    ):
        """Test that build updates the database."""
        with patch.object(context_builder, "_get_ai_client", return_value=mock_ai_client):
            filename = await context_builder.build(
                "TB-123",
                sample_context_items,
                output_dir=tmp_path,
            )

        # Check database was updated
        record = await context_builder.db.get_context_file("TB-123")
        assert record is not None
        assert record.filename == filename

    @pytest.mark.asyncio
    async def test_raises_on_empty_items(
        self,
        context_builder: ContextBuilder,
    ):
        """Test that build raises ValueError on empty items."""
        with pytest.raises(ValueError, match="No items to build context from"):
            await context_builder.build("TB-123", [])

    @pytest.mark.asyncio
    async def test_updates_fetch_history(
        self,
        context_builder: ContextBuilder,
        sample_context_items: list[ContextItem],
        mock_ai_client: MagicMock,
        tmp_path: Path,
    ):
        """Test that build updates fetch history for each source."""
        with patch.object(context_builder, "_get_ai_client", return_value=mock_ai_client):
            await context_builder.build(
                "TB-123",
                sample_context_items,
                output_dir=tmp_path,
            )

        # Check fetch history was updated
        record = await context_builder.db.get_context_file("TB-123")
        history = await context_builder.db.get_all_fetch_history(record.id)

        sources = {h.source for h in history}
        assert "jira" in sources
        assert "slack" in sources
        assert "github" in sources



