"""Shared pytest fixtures for Glean tests."""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rove.config import RoveConfig
from rove.database import Database
from rove.plugins.base import ContextItem, SearchableField


@pytest.fixture
def mock_config() -> RoveConfig:
    """Create a mock configuration."""
    config = RoveConfig()
    config.ai.api_key = "test-key"
    config.ai.model = "gpt-4o-mini"
    return config


@pytest.fixture
def sample_context_item() -> ContextItem:
    """Create a sample context item for testing."""
    return ContextItem(
        source="jira",
        item_type="ticket",
        title="TB-123: Implement OAuth authentication",
        content="We need to implement OAuth2 authentication for enterprise customers. "
        "This should support PKCE flow and refresh tokens.",
        url="https://example.atlassian.net/browse/TB-123",
        timestamp=datetime(2024, 12, 20, 10, 30, 0),
        author="John Doe",
        metadata={
            "ticket_id": "TB-123",
            "labels": ["authentication", "oauth"],
        },
    )


@pytest.fixture
def sample_context_items(sample_context_item: ContextItem) -> list[ContextItem]:
    """Create a list of sample context items."""
    return [
        sample_context_item,
        ContextItem(
            source="slack",
            item_type="message",
            title="Message in #backend-team",
            content="Discussed the OAuth implementation. See PR #847 for the initial work.",
            url="https://workspace.slack.com/archives/C123/p456",
            timestamp=datetime(2024, 12, 21, 14, 0, 0),
            author="Jane Smith",
            metadata={"channel_name": "backend-team"},
        ),
        ContextItem(
            source="github",
            item_type="pr",
            title="PR #847: Add OAuth2 provider base class",
            content="This PR adds the base class for OAuth2 authentication providers.",
            url="https://github.com/org/repo/pull/847",
            timestamp=datetime(2024, 12, 22, 9, 15, 0),
            author="Jane Smith",
            metadata={"number": 847, "state": "merged"},
        ),
    ]


@pytest.fixture
def mock_ai_response() -> MagicMock:
    """Create a mock AI response."""
    mock_choice = MagicMock()
    mock_choice.message.content = "oauth, authentication, enterprise, pkce"

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.fixture
def mock_ai_client(mock_ai_response: MagicMock) -> MagicMock:
    """Create a mock OpenAI client."""
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_ai_response)
    return mock_client


@pytest.fixture
def mock_source_client(sample_context_item: ContextItem) -> MagicMock:
    """Create a mock source client."""
    mock_client = MagicMock()
    mock_client.source_name.return_value = "MockSource"
    mock_client.is_authenticated.return_value = True
    mock_client.authenticate = AsyncMock(return_value=True)
    mock_client.test_connection = AsyncMock(return_value=True)
    mock_client.get_item_details = AsyncMock(return_value=sample_context_item)
    mock_client.search = AsyncMock(return_value=[sample_context_item])
    mock_client.supported_reference_types.return_value = ["ticket"]
    mock_client.extract_references.return_value = []  # Default: no references found
    mock_client.get_searchable_fields.return_value = [
        SearchableField(
            name="ticket_id",
            field_type="id_reference",
            description="Ticket key",
        ),
    ]
    return mock_client


@pytest.fixture
async def test_db(tmp_path) -> Database:
    """Create a test database."""
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()



