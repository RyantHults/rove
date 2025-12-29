"""JIRA Context Client implementation.

Provides full access to JIRA tickets, comments, and related data.
"""

from datetime import UTC, datetime, timedelta

import httpx

from ..base import (
    ContextClient,
    ContextItem,
    SearchableField,
    delete_credentials,
    get_credentials,
    store_credentials,
)
from .auth import (
    DEFAULT_CLIENT_ID,
    OAuthTokens,
    perform_oauth_flow,
    refresh_access_token,
)


class JiraContextClient(ContextClient):
    """JIRA implementation of the ContextClient protocol."""

    DEFAULT_CONFIG = {
        "rate_limit": 100,  # requests per minute
        "page_size": 50,  # items per API call
        "token_refresh_buffer": 300,  # refresh 5 min before expiry
    }

    def __init__(self, config: dict):
        """Initialize the JIRA client.

        Args:
            config: Configuration dict with optional overrides.
        """
        self.config = {**self.DEFAULT_CONFIG, **config}
        self._tokens: OAuthTokens | None = None
        self._client_id = config.get("client_id", DEFAULT_CLIENT_ID)
        self._load_stored_credentials()

    def _load_stored_credentials(self) -> None:
        """Load credentials from keyring if available."""
        creds = get_credentials("jira")
        if creds:
            try:
                self._tokens = OAuthTokens(
                    access_token=creds["access_token"],
                    refresh_token=creds.get("refresh_token"),
                    expires_at=datetime.fromisoformat(creds["expires_at"]),
                    cloud_id=creds["cloud_id"],
                    site_url=creds["site_url"],
                )
            except (KeyError, ValueError):
                self._tokens = None

    def _save_credentials(self) -> None:
        """Save current tokens to keyring."""
        if self._tokens:
            store_credentials(
                "jira",
                {
                    "access_token": self._tokens.access_token,
                    "refresh_token": self._tokens.refresh_token,
                    "expires_at": self._tokens.expires_at.isoformat(),
                    "cloud_id": self._tokens.cloud_id,
                    "site_url": self._tokens.site_url,
                },
            )

    async def _ensure_valid_token(self) -> bool:
        """Ensure we have a valid access token, refreshing if needed.

        Returns:
            True if we have a valid token.
        """
        if not self._tokens:
            return False

        # Check if token needs refresh
        buffer = timedelta(seconds=self.config["token_refresh_buffer"])
        if datetime.now(UTC) + buffer < self._tokens.expires_at:
            return True  # Token is still valid

        # Try to refresh
        if not self._tokens.refresh_token:
            return False

        try:
            token_response = await refresh_access_token(
                self._client_id, self._tokens.refresh_token
            )
            expires_in = token_response.get("expires_in", 3600)
            self._tokens = OAuthTokens(
                access_token=token_response["access_token"],
                refresh_token=token_response.get(
                    "refresh_token", self._tokens.refresh_token
                ),
                expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                cloud_id=self._tokens.cloud_id,
                site_url=self._tokens.site_url,
            )
            self._save_credentials()
            return True
        except httpx.HTTPStatusError:
            return False

    def _get_api_base(self) -> str:
        """Get the JIRA API base URL."""
        if not self._tokens:
            raise RuntimeError("Not authenticated")
        return f"https://api.atlassian.com/ex/jira/{self._tokens.cloud_id}"

    def source_name(self) -> str:
        """Return human-readable name."""
        return "JIRA"

    def get_config_schema(self) -> dict:
        """Return JSON schema of required configuration."""
        return {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "OAuth client ID (optional, uses default)",
                },
                "rate_limit": {
                    "type": "integer",
                    "description": "Max requests per minute",
                    "default": 100,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Items per API call",
                    "default": 50,
                },
            },
            "required": [],
        }

    def get_searchable_fields(self) -> list[SearchableField]:
        """Return list of fields this source can search."""
        return [
            SearchableField(
                name="ticket_id",
                field_type="id_reference",
                description="JIRA ticket key (e.g., TB-123)",
            ),
            SearchableField(
                name="ticket_title",
                field_type="text",
                description="Ticket summary/title",
            ),
            SearchableField(
                name="ticket_description",
                field_type="text",
                description="Full ticket description",
            ),
            SearchableField(
                name="comments",
                field_type="text",
                description="Ticket comments and discussions",
            ),
            SearchableField(
                name="labels",
                field_type="keyword",
                description="Ticket labels",
            ),
            SearchableField(
                name="related_tickets",
                field_type="id_reference",
                description="Linked tickets",
            ),
        ]

    async def authenticate(self, credentials: dict | None = None) -> bool:
        """Authenticate with JIRA via OAuth.

        Args:
            credentials: Optional pre-existing credentials dict.

        Returns:
            True if authentication succeeded.
        """
        if credentials:
            # Use provided credentials
            try:
                self._tokens = OAuthTokens(
                    access_token=credentials["access_token"],
                    refresh_token=credentials.get("refresh_token"),
                    expires_at=datetime.fromisoformat(credentials["expires_at"]),
                    cloud_id=credentials["cloud_id"],
                    site_url=credentials["site_url"],
                )
                self._save_credentials()
                return True
            except (KeyError, ValueError):
                return False

        # Perform OAuth flow
        tokens = await perform_oauth_flow(self._client_id)
        if tokens:
            self._tokens = tokens
            self._save_credentials()
            return True
        return False

    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._tokens is not None

    async def test_connection(self) -> bool:
        """Verify the connection is working."""
        if not await self._ensure_valid_token():
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._get_api_base()}/rest/api/3/myself",
                    headers={"Authorization": f"Bearer {self._tokens.access_token}"},
                )
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def search(
        self,
        query: str,
        since: datetime | None = None,
        until: datetime | None = None,
        fields: list[str] | None = None,
        **kwargs,
    ) -> list[ContextItem]:
        """Search for JIRA tickets matching the query.

        Args:
            query: Search query (JQL or text search).
            since: Only return items updated after this time.
            until: Only return items updated before this time.
            fields: Optional list of field names to search in.
            **kwargs: Additional parameters.

        Returns:
            A list of ContextItem objects.
        """
        if not await self._ensure_valid_token():
            return []

        # Build JQL query
        jql_parts = []

        # Check if query looks like a ticket ID
        if self._looks_like_ticket_id(query):
            jql_parts.append(f'key = "{query}"')
        else:
            # Text search
            jql_parts.append(f'text ~ "{query}"')

        # Add time filters
        if since:
            jql_parts.append(f'updated >= "{since.strftime("%Y-%m-%d")}"')
        if until:
            jql_parts.append(f'updated <= "{until.strftime("%Y-%m-%d")}"')

        jql = " AND ".join(jql_parts)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._get_api_base()}/rest/api/3/search",
                    headers={"Authorization": f"Bearer {self._tokens.access_token}"},
                    params={
                        "jql": jql,
                        "maxResults": self.config["page_size"],
                        "fields": "summary,description,comment,labels,issuelinks,created,updated,creator",
                    },
                )
                response.raise_for_status()
                data = response.json()

                items = []
                for issue in data.get("issues", []):
                    items.extend(self._parse_issue(issue))
                return items
        except httpx.HTTPError:
            return []

    async def get_item_details(self, item_id: str) -> ContextItem | None:
        """Fetch full details of a specific ticket.

        Args:
            item_id: The ticket key (e.g., TB-123).

        Returns:
            A ContextItem with full details, or None if not found.
        """
        if not await self._ensure_valid_token():
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._get_api_base()}/rest/api/3/issue/{item_id}",
                    headers={"Authorization": f"Bearer {self._tokens.access_token}"},
                    params={
                        "fields": "summary,description,comment,labels,issuelinks,created,updated,creator",
                        "expand": "renderedFields",
                    },
                )
                response.raise_for_status()
                issue = response.json()

                items = self._parse_issue(issue)
                return items[0] if items else None
        except httpx.HTTPError:
            return None

    async def disconnect(self) -> None:
        """Clear stored credentials."""
        delete_credentials("jira")
        self._tokens = None

    def supported_reference_types(self) -> list[str]:
        """Return list of reference types this plugin can resolve."""
        return ["ticket"]

    def _looks_like_ticket_id(self, query: str) -> bool:
        """Check if query looks like a JIRA ticket ID."""
        import re

        return bool(re.match(r"^[A-Z]+-\d+$", query.upper()))

    def _parse_issue(self, issue: dict) -> list[ContextItem]:
        """Parse a JIRA issue into ContextItem objects.

        Returns the main ticket plus any comments as separate items.
        """
        items = []
        fields = issue.get("fields", {})
        key = issue.get("key", "")

        # Parse description (handle Atlassian Document Format)
        description = self._extract_text(fields.get("description"))

        # Main ticket item
        items.append(
            ContextItem(
                source="jira",
                item_type="ticket",
                title=f"{key}: {fields.get('summary', '')}",
                content=description,
                url=f"{self._tokens.site_url}/browse/{key}" if self._tokens else "",
                timestamp=datetime.fromisoformat(
                    fields.get("updated", fields.get("created", "")).replace("Z", "+00:00")
                ),
                author=self._get_author_name(fields.get("creator")),
                metadata={
                    "ticket_id": key,
                    "labels": fields.get("labels", []),
                    "linked_issues": self._extract_linked_issues(fields.get("issuelinks", [])),
                },
            )
        )

        # Parse comments
        comments = fields.get("comment", {}).get("comments", [])
        for comment in comments:
            comment_text = self._extract_text(comment.get("body"))
            items.append(
                ContextItem(
                    source="jira",
                    item_type="comment",
                    title=f"Comment on {key}",
                    content=comment_text,
                    url=f"{self._tokens.site_url}/browse/{key}?focusedCommentId={comment.get('id', '')}"
                    if self._tokens
                    else "",
                    timestamp=datetime.fromisoformat(
                        comment.get("updated", comment.get("created", "")).replace(
                            "Z", "+00:00"
                        )
                    ),
                    author=self._get_author_name(comment.get("author")),
                    metadata={
                        "ticket_id": key,
                        "comment_id": comment.get("id"),
                    },
                )
            )

        return items

    def _extract_text(self, adf_content: dict | str | None) -> str:
        """Extract plain text from Atlassian Document Format.

        Args:
            adf_content: The content, which may be ADF (dict) or plain text.

        Returns:
            Plain text content.
        """
        if not adf_content:
            return ""
        if isinstance(adf_content, str):
            return adf_content

        # Handle Atlassian Document Format
        def extract_from_node(node: dict) -> str:
            text_parts = []
            if node.get("type") == "text":
                text_parts.append(node.get("text", ""))
            for child in node.get("content", []):
                text_parts.append(extract_from_node(child))
            return " ".join(text_parts)

        return extract_from_node(adf_content).strip()

    def _get_author_name(self, author: dict | None) -> str:
        """Extract author display name from author dict."""
        if not author:
            return "Unknown"
        return author.get("displayName", author.get("name", "Unknown"))

    def _extract_linked_issues(self, issuelinks: list[dict]) -> list[str]:
        """Extract linked issue keys from issuelinks."""
        linked = []
        for link in issuelinks:
            if "outwardIssue" in link:
                linked.append(link["outwardIssue"].get("key", ""))
            if "inwardIssue" in link:
                linked.append(link["inwardIssue"].get("key", ""))
        return [k for k in linked if k]

