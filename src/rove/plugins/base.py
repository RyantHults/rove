"""Base interfaces and data types for Glean plugins.

All plugins must implement the ContextClient protocol.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class AuthenticationError(Exception):
    """Raised when authentication fails or credentials expire."""

    pass


@dataclass
class ContextItem:
    """Standardized format for context returned by any plugin.

    This is the common data structure that all plugins return when
    searching for context. It includes full source attribution.
    """

    source: str  # "jira", "slack", "github"
    item_type: str  # "ticket", "message", "pr", "issue", "comment"
    title: str  # Human-readable title
    content: str  # The actual context text
    url: str  # Link to original item
    timestamp: datetime  # When this was created/updated
    author: str  # Who created this item
    metadata: dict = field(default_factory=dict)  # Source-specific extra data


@dataclass
class SearchableField:
    """Describes a field that can be searched within a source.

    This helps the AI understand what can be searched and how.
    """

    name: str  # "ticket_title", "comments", "channel_messages"
    field_type: str  # "text", "id_reference", "keyword"
    description: str  # Human-readable description for AI context


class ContextClient(Protocol):
    """Interface all plugins must implement.

    This protocol defines the contract between Rove and its plugins.
    Each plugin provides access to a specific data source.
    """

    def source_name(self) -> str:
        """Return human-readable name (e.g., 'JIRA', 'Slack').

        Returns:
            The display name of this source.
        """
        ...

    def get_config_schema(self) -> dict:
        """Return JSON schema of required configuration.

        Returns:
            A JSON Schema dict describing the configuration options.
        """
        ...

    def get_searchable_fields(self) -> list[SearchableField]:
        """Return list of fields this source can search.

        Returns:
            A list of SearchableField objects describing what can be searched.
        """
        ...

    async def authenticate(self, credentials: dict | None = None) -> bool:
        """Authenticate with stored or provided credentials.

        Args:
            credentials: Optional credentials dict. If None, uses stored credentials.

        Returns:
            True if authentication succeeded.
        """
        ...

    def is_authenticated(self) -> bool:
        """Check if currently authenticated with valid credentials.

        Returns:
            True if authenticated and credentials are valid.
        """
        ...

    async def test_connection(self) -> bool:
        """Verify the connection is working.

        Returns:
            True if the connection to the service is working.
        """
        ...

    async def search(
        self,
        query: str,
        since: datetime | None = None,
        until: datetime | None = None,
        fields: list[str] | None = None,
        **kwargs,
    ) -> list[ContextItem]:
        """Search for context matching query.

        Args:
            query: The search query string.
            since: Only return items updated after this time.
            until: Only return items updated before this time.
            fields: Optional list of field names to search in.
            **kwargs: Additional source-specific parameters.

        Returns:
            A list of ContextItem objects matching the search.
        """
        ...

    async def get_item_details(self, item_id: str) -> ContextItem | None:
        """Fetch full details of a specific item.

        Args:
            item_id: The unique identifier for the item.

        Returns:
            A ContextItem with full details, or None if not found.
        """
        ...

    async def disconnect(self) -> None:
        """Clear stored credentials and disconnect."""
        ...

    def supported_reference_types(self) -> list[str]:
        """Return list of reference types this plugin can resolve.

        Reference types are used during multi-hop search to determine
        which plugin should handle expanding a discovered reference.

        Common types:
        - "ticket": JIRA/Linear tickets (e.g., TB-123)
        - "pr": Pull requests (e.g., PR #123)
        - "issue": GitHub/GitLab issues
        - "commit": Git commits
        - "message": Slack/Discord messages

        Returns:
            A list of reference type strings this plugin supports.
        """
        ...


# Credential helper functions using keyring
def store_credentials(source: str, tokens: dict) -> None:
    """Store credentials for a source in the system keyring.

    Args:
        source: The source name (e.g., "jira", "slack")
        tokens: A dict containing the tokens to store
    """
    import json

    import keyring

    # Store as JSON to handle multiple tokens
    keyring.set_password(f"rove-{source}", "tokens", json.dumps(tokens))


def get_credentials(source: str) -> dict | None:
    """Retrieve credentials for a source from the system keyring.

    Args:
        source: The source name (e.g., "jira", "slack")

    Returns:
        A dict containing the stored tokens, or None if not found.
    """
    import json

    import keyring

    try:
        tokens_json = keyring.get_password(f"rove-{source}", "tokens")
        if tokens_json:
            return json.loads(tokens_json)
    except Exception:
        pass
    return None


def delete_credentials(source: str) -> None:
    """Delete credentials for a source from the system keyring.

    Args:
        source: The source name (e.g., "jira", "slack")
    """
    import keyring

    try:
        keyring.delete_password(f"rove-{source}", "tokens")
    except keyring.errors.PasswordDeleteError:
        pass  # Already deleted or never existed

