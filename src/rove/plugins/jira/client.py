"""JIRA Context Client implementation.

Provides full access to JIRA tickets, comments, and related data.
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx

from ...logging import get_logger
from ..base import (
    ContextClient,
    ContextItem,
    SearchableField,
    delete_credentials,
    get_credentials,
    store_credentials,
)
from .auth import (
    ApiTokenCredentials,
    DEFAULT_CLIENT_ID,
    OAuthTokens,
    perform_oauth_flow,
    refresh_access_token,
)

logger = get_logger("jira")


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
        self._api_credentials: ApiTokenCredentials | None = None
        self._auth_method: str = "oauth"  # "oauth" or "api_token"
        self._client_id = config.get("client_id", DEFAULT_CLIENT_ID)
        self._load_stored_credentials()

    def _load_stored_credentials(self) -> None:
        """Load credentials from keyring if available."""
        creds = get_credentials("jira")
        if creds:
            # Check if it's API token auth or OAuth
            if "email" in creds and "api_token" in creds:
                # API token authentication
                try:
                    self._api_credentials = ApiTokenCredentials(
                        email=creds["email"],
                        api_token=creds["api_token"],
                        site_url=creds["site_url"],
                    )
                    self._auth_method = "api_token"
                except (KeyError, ValueError):
                    self._api_credentials = None
            else:
                # OAuth authentication
                try:
                    self._tokens = OAuthTokens(
                        access_token=creds["access_token"],
                        refresh_token=creds.get("refresh_token"),
                        expires_at=datetime.fromisoformat(creds["expires_at"]),
                        cloud_id=creds["cloud_id"],
                        site_url=creds["site_url"],
                    )
                    self._auth_method = "oauth"
                except (KeyError, ValueError):
                    self._tokens = None

    def _save_credentials(self) -> None:
        """Save current credentials to keyring."""
        if self._auth_method == "api_token" and self._api_credentials:
            store_credentials(
                "jira",
                {
                    "email": self._api_credentials.email,
                    "api_token": self._api_credentials.api_token,
                    "site_url": self._api_credentials.site_url,
                },
            )
        elif self._auth_method == "oauth" and self._tokens:
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
        """Ensure we have valid credentials, refreshing OAuth token if needed.

        Returns:
            True if we have valid credentials.
        """
        if self._auth_method == "api_token":
            return self._api_credentials is not None

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
            # Note: site_url is preserved from existing tokens
            self._save_credentials()
            return True
        except httpx.HTTPStatusError:
            return False

    def _get_api_base(self) -> str:
        """Get the JIRA API base URL."""
        if self._auth_method == "api_token" and self._api_credentials:
            # For API token auth, use the site URL directly
            base = self._api_credentials.site_url.rstrip("/")
            return f"{base}/rest/api/3"
        elif self._auth_method == "oauth" and self._tokens:
            return f"https://api.atlassian.com/ex/jira/{self._tokens.cloud_id}"
        raise RuntimeError("Not authenticated")

    def _get_auth_header(self) -> dict[str, str]:
        """Get the appropriate authorization header for the current auth method."""
        if self._auth_method == "api_token" and self._api_credentials:
            # Basic Auth: email:api_token base64 encoded
            credentials = f"{self._api_credentials.email}:{self._api_credentials.api_token}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        elif self._auth_method == "oauth" and self._tokens:
            return {"Authorization": f"Bearer {self._tokens.access_token}"}
        raise RuntimeError("Not authenticated")

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
        """Authenticate with JIRA via OAuth or API token.

        Args:
            credentials: Optional pre-existing credentials dict.
                        For OAuth: {"access_token": ..., "refresh_token": ..., "expires_at": ..., "cloud_id": ..., "site_url": ...}
                        For API token: {"email": ..., "api_token": ..., "site_url": ...}

        Returns:
            True if authentication succeeded.
        """
        if credentials:
            # Check if it's API token or OAuth credentials
            if "email" in credentials and "api_token" in credentials:
                # API token authentication
                try:
                    self._api_credentials = ApiTokenCredentials(
                        email=credentials["email"],
                        api_token=credentials["api_token"],
                        site_url=credentials["site_url"],
                    )
                    self._auth_method = "api_token"
                    self._save_credentials()
                    return True
                except (KeyError, ValueError):
                    return False
            else:
                # OAuth authentication
                try:
                    self._tokens = OAuthTokens(
                        access_token=credentials["access_token"],
                        refresh_token=credentials.get("refresh_token"),
                        expires_at=datetime.fromisoformat(credentials["expires_at"]),
                        cloud_id=credentials["cloud_id"],
                        site_url=credentials["site_url"],
                    )
                    self._auth_method = "oauth"
                    self._save_credentials()
                    return True
                except (KeyError, ValueError):
                    return False

        # Interactive authentication - prompt user for method
        import click

        click.echo("\nChoose authentication method:")
        click.echo("1. OAuth (requires OAuth app registration)")
        click.echo("2. API Token (simpler, no app registration needed)")
        choice = click.prompt("Enter choice", type=click.Choice(["1", "2"]), default="2")

        if choice == "2":
            # API token authentication
            email = click.prompt("Enter your JIRA email address")
            api_token = click.prompt("Enter your JIRA API token", hide_input=True)
            site_url = click.prompt(
                "Enter your JIRA site URL (e.g., https://yourcompany.atlassian.net)"
            )

            # Normalize site URL
            if not site_url.startswith("http"):
                site_url = f"https://{site_url}"

            self._api_credentials = ApiTokenCredentials(
                email=email, api_token=api_token, site_url=site_url
            )
            self._auth_method = "api_token"
            self._save_credentials()
            return True
        else:
            # Perform OAuth flow
            tokens = await perform_oauth_flow(self._client_id)
            if tokens:
                self._tokens = tokens
                self._auth_method = "oauth"
                self._save_credentials()
                return True
            return False

    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        if self._auth_method == "api_token":
            return self._api_credentials is not None
        return self._tokens is not None

    async def test_connection(self) -> bool:
        """Verify the connection is working."""
        if not await self._ensure_valid_token():
            return False

        try:
            async with httpx.AsyncClient() as client:
                # Use the appropriate endpoint based on auth method
                url = f"{self._get_api_base()}/myself"
                response = await client.get(url, headers=self._get_auth_header())
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

        logger.debug(f"JIRA search query: {jql}")
        try:
            async with httpx.AsyncClient() as client:
                # Use new /search/jql endpoint (Atlassian deprecated /search)
                headers = {
                    **self._get_auth_header(),
                    "Content-Type": "application/json",
                }
                response = await client.post(
                    f"{self._get_api_base()}/search/jql",
                    headers=headers,
                    json={
                        "jql": jql,
                        "maxResults": self.config["page_size"],
                        "fields": [
                            "summary",
                            "description",
                            "comment",
                            "labels",
                            "issuelinks",
                            "subtasks",
                            "parent",
                            "created",
                            "updated",
                            "creator",
                        ],
                    },
                )
                response.raise_for_status()
                data = response.json()

                items = []
                for issue in data.get("issues", []):
                    parsed = self._parse_issue(issue)
                    logger.debug(
                        f"JIRA parsed {issue.get('key')}: "
                        f"{len(parsed)} items (1 ticket + {len(parsed)-1} comments)"
                    )
                    items.extend(parsed)
                logger.debug(f"JIRA search returned {len(items)} total items")
                return items
        except httpx.HTTPError as e:
            logger.error(f"JIRA search failed: {e}")
            return []

    async def get_item_details(self, item_id: str) -> ContextItem | None:
        """Fetch full details of a specific ticket including comments.

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
                    f"{self._get_api_base()}/issue/{item_id}",
                    headers=self._get_auth_header(),
                    params={
                        "fields": "summary,description,comment,labels,issuelinks,subtasks,parent,issuetype,created,updated,creator",
                        "expand": "renderedFields",
                    },
                )
                response.raise_for_status()
                issue = response.json()
                
                # Debug: log what comments JIRA returned
                fields = issue.get("fields", {})
                comment_data = fields.get("comment", {})
                raw_comments = comment_data.get("comments", [])
                logger.debug(
                    f"JIRA API returned {len(raw_comments)} comments for {item_id} "
                    f"(total: {comment_data.get('total', 0)}, maxResults: {comment_data.get('maxResults', 0)})"
                )

                items = self._parse_issue(issue)
                if not items:
                    return None
                
                # Store comments in metadata so they can be extracted by caller
                ticket = items[0]
                if len(items) > 1:
                    ticket.metadata["_comments"] = items[1:]
                    logger.debug(f"Fetched ticket {item_id} with {len(items)-1} comments")
                
                # For Epics and parent issues, also fetch child issues via JQL
                # (subtasks are already in the subtasks field, but Epic children are not)
                child_ids = await self._get_child_issue_ids(client, item_id)
                if child_ids:
                    # Merge with any subtask IDs already found
                    existing_children = ticket.metadata.get("child_ticket_ids", [])
                    all_children = list(set(existing_children + child_ids))
                    ticket.metadata["child_ticket_ids"] = all_children
                    logger.debug(
                        f"Found {len(child_ids)} child issues for {item_id} "
                        f"(total children: {len(all_children)})"
                    )
                
                return ticket
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch ticket {item_id}: {e}")
            return None

    async def _get_child_issue_ids(
        self, client: httpx.AsyncClient, parent_id: str
    ) -> list[str]:
        """Fetch child issue IDs for a parent ticket (Epic children, etc.).

        Uses JQL to find issues where parent = parent_id.

        Args:
            client: The HTTP client to use.
            parent_id: The parent ticket key.

        Returns:
            List of child ticket keys.
        """
        try:
            # Query for issues that have this ticket as their parent
            # This catches Epic children and other parent-child relationships
            jql = f'parent = "{parent_id}"'
            headers = {
                **self._get_auth_header(),
                "Content-Type": "application/json",
            }
            response = await client.post(
                f"{self._get_api_base()}/search/jql",
                headers=headers,
                json={
                    "jql": jql,
                    "maxResults": 50,  # Reasonable limit for children
                    "fields": ["key"],  # Only need the keys
                },
            )
            response.raise_for_status()
            data = response.json()

            child_ids = [
                issue.get("key") for issue in data.get("issues", [])
                if issue.get("key")
            ]
            return child_ids
        except httpx.HTTPError as e:
            logger.debug(f"Failed to fetch child issues for {parent_id}: {e}")
            return []

    async def disconnect(self) -> None:
        """Clear stored credentials."""
        delete_credentials("jira")
        self._tokens = None
        self._api_credentials = None
        self._auth_method = "oauth"

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

        # Extract child ticket IDs (subtasks)
        child_ticket_ids = self._extract_subtask_ids(fields.get("subtasks", []))

        # Extract parent ticket ID if this is a subtask
        parent_ticket_id = self._extract_parent_id(fields.get("parent"))

        # Main ticket item
        items.append(
            ContextItem(
                source="jira",
                item_type="ticket",
                title=f"{key}: {fields.get('summary', '')}",
                content=description,
                url=(
                    f"{self._api_credentials.site_url}/browse/{key}"
                    if self._auth_method == "api_token" and self._api_credentials
                    else f"{self._tokens.site_url}/browse/{key}" if self._tokens else ""
                ),
                timestamp=datetime.fromisoformat(
                    fields.get("updated", fields.get("created", "")).replace("Z", "+00:00")
                ),
                author=self._get_author_name(fields.get("creator")),
                metadata={
                    "ticket_id": key,
                    "labels": fields.get("labels", []),
                    "linked_issues": self._extract_linked_issues(fields.get("issuelinks", [])),
                    "child_ticket_ids": child_ticket_ids,
                    "parent_ticket_id": parent_ticket_id,
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
                    url=(
                        f"{self._api_credentials.site_url}/browse/{key}?focusedCommentId={comment.get('id', '')}"
                        if self._auth_method == "api_token" and self._api_credentials
                        else f"{self._tokens.site_url}/browse/{key}?focusedCommentId={comment.get('id', '')}"
                        if self._tokens
                        else ""
                    ),
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
        """Extract markdown-formatted text from Atlassian Document Format.

        Args:
            adf_content: The content, which may be ADF (dict) or plain text.

        Returns:
            Markdown-formatted text content.
        """
        if not adf_content:
            return ""
        if isinstance(adf_content, str):
            return adf_content

        # Handle Atlassian Document Format
        def extract_from_node(node: dict, list_level: int = 0) -> str:
            node_type = node.get("type", "")
            content = node.get("content", [])

            # Text node - handle marks (bold, italic, etc.)
            if node_type == "text":
                text = node.get("text", "")
                marks = node.get("marks", [])
                for mark in marks:
                    mark_type = mark.get("type", "")
                    if mark_type == "strong":
                        text = f"**{text}**"
                    elif mark_type == "em":
                        text = f"*{text}*"
                    elif mark_type == "code":
                        text = f"`{text}`"
                    elif mark_type == "link":
                        href = mark.get("attrs", {}).get("href", "")
                        text = f"[{text}]({href})"
                return text

            # Block-level nodes
            if node_type == "paragraph":
                inner = "".join(extract_from_node(c, list_level) for c in content)
                return f"{inner}\n\n"

            if node_type == "heading":
                level = node.get("attrs", {}).get("level", 1)
                inner = "".join(extract_from_node(c, list_level) for c in content)
                return f"{'#' * level} {inner}\n\n"

            if node_type == "bulletList":
                items = [extract_from_node(c, list_level + 1) for c in content]
                return "".join(items)

            if node_type == "orderedList":
                # Get starting number from attrs (defaults to 1)
                start = node.get("attrs", {}).get("order", 1)
                lines = []
                indent = "  " * list_level
                for i, c in enumerate(content, start):
                    # Process listItem content directly to avoid bullet formatting
                    if c.get("type") == "listItem":
                        inner_content = c.get("content", [])
                        item_inner = "".join(
                            extract_from_node(ic, list_level + 1)
                            for ic in inner_content
                        )
                        item_inner = item_inner.rstrip()
                        lines.append(f"{indent}{i}. {item_inner}\n")
                    else:
                        # Fallback for unexpected structure
                        lines.append(extract_from_node(c, list_level + 1))
                return "".join(lines)

            if node_type == "listItem":
                indent = "  " * (list_level - 1)
                inner = "".join(extract_from_node(c, list_level) for c in content)
                # Remove trailing newlines from inner content for cleaner list formatting
                inner = inner.rstrip()
                return f"{indent}- {inner}\n"

            if node_type == "codeBlock":
                lang = node.get("attrs", {}).get("language", "")
                inner = "".join(extract_from_node(c, list_level) for c in content)
                return f"```{lang}\n{inner.strip()}\n```\n\n"

            if node_type == "blockquote":
                inner = "".join(extract_from_node(c, list_level) for c in content)
                # Prefix each line with >
                lines = inner.strip().split("\n")
                quoted = "\n".join(f"> {line}" for line in lines)
                return f"{quoted}\n\n"

            if node_type == "rule":
                return "---\n\n"

            if node_type == "hardBreak":
                return "\n"

            if node_type == "mention":
                return f"@{node.get('attrs', {}).get('text', 'user')}"

            if node_type == "emoji":
                return node.get("attrs", {}).get("shortName", "")

            # Container nodes (doc, etc.) - just process children
            if content:
                return "".join(extract_from_node(c, list_level) for c in content)

            return ""

        result = extract_from_node(adf_content).strip()
        # Clean up excessive newlines
        import re

        result = re.sub(r"\n{3,}", "\n\n", result)
        return result

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

    def _extract_subtask_ids(self, subtasks: list[dict]) -> list[str]:
        """Extract subtask keys from subtasks field.

        Args:
            subtasks: List of subtask objects from JIRA API.

        Returns:
            List of subtask ticket keys.
        """
        return [s.get("key", "") for s in subtasks if s.get("key")]

    def _extract_parent_id(self, parent: dict | None) -> str | None:
        """Extract parent ticket ID if this issue is a subtask.

        Args:
            parent: Parent object from JIRA API, or None.

        Returns:
            Parent ticket key, or None if not a subtask.
        """
        if parent:
            return parent.get("key")
        return None

