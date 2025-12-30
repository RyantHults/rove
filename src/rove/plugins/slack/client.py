"""Slack Context Client implementation.

Provides access to Slack messages, channels, and threads.
"""

import asyncio
import secrets
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta

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

logger = get_logger("slack")

# Slack OAuth endpoints
SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_API_BASE = "https://slack.com/api"

# Default OAuth settings - would be configured per installation
DEFAULT_CLIENT_ID = "rove-slack-integration"
DEFAULT_CLIENT_SECRET = ""  # Would be configured
REDIRECT_URI = "http://localhost:8766/callback"
SCOPES = [
    "search:read",
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "users:read",
]


@dataclass
class DirectTokenCredentials:
    """Container for direct User OAuth Token credentials."""

    user_token: str
    team_id: str | None = None
    team_name: str | None = None


class SlackContextClient(ContextClient):
    """Slack implementation of the ContextClient protocol."""

    DEFAULT_CONFIG = {
        "rate_limit": 50,
        "page_size": 100,
        "token_refresh_buffer": 300,
    }

    def __init__(self, config: dict):
        """Initialize the Slack client."""
        self.config = {**self.DEFAULT_CONFIG, **config}
        self._access_token: str | None = None
        self._team_id: str | None = None
        self._team_name: str | None = None
        self._auth_method: str = "oauth"  # "oauth" or "token"
        self._client_id = config.get("client_id", DEFAULT_CLIENT_ID)
        self._client_secret = config.get("client_secret", DEFAULT_CLIENT_SECRET)
        self._load_stored_credentials()

    def _load_stored_credentials(self) -> None:
        """Load credentials from keyring if available."""
        creds = get_credentials("slack")
        if creds:
            self._access_token = creds.get("access_token") or creds.get("user_token")
            self._team_id = creds.get("team_id")
            self._team_name = creds.get("team_name")
            self._auth_method = creds.get("auth_method", "oauth")

    def _save_credentials(self) -> None:
        """Save current tokens to keyring."""
        if self._access_token:
            creds = {
                "team_id": self._team_id,
                "team_name": self._team_name,
                "auth_method": self._auth_method,
            }
            if self._auth_method == "token":
                creds["user_token"] = self._access_token
            else:
                creds["access_token"] = self._access_token
            store_credentials("slack", creds)

    def source_name(self) -> str:
        """Return human-readable name."""
        return "Slack"

    def get_config_schema(self) -> dict:
        """Return JSON schema of required configuration."""
        return {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Slack OAuth client ID",
                },
                "client_secret": {
                    "type": "string",
                    "description": "Slack OAuth client secret",
                },
                "rate_limit": {
                    "type": "integer",
                    "description": "Max requests per minute",
                    "default": 50,
                },
            },
            "required": ["client_id", "client_secret"],
        }

    def get_searchable_fields(self) -> list[SearchableField]:
        """Return list of fields this source can search."""
        return [
            SearchableField(
                name="message_content",
                field_type="text",
                description="Message text content",
            ),
            SearchableField(
                name="channel_name",
                field_type="keyword",
                description="Channel name",
            ),
            SearchableField(
                name="thread_replies",
                field_type="text",
                description="Thread reply messages",
            ),
        ]

    async def authenticate(self, credentials: dict | None = None) -> bool:
        """Authenticate with Slack via OAuth or direct User Token.

        Args:
            credentials: Optional pre-existing credentials dict.
                        For OAuth: {"access_token": ..., "team_id": ..., "team_name": ...}
                        For token: {"user_token": ...}

        Returns:
            True if authentication succeeded.
        """
        if credentials:
            # Check if it's a direct token or OAuth credentials
            if "user_token" in credentials:
                self._access_token = credentials["user_token"]
                self._auth_method = "token"
            else:
                self._access_token = credentials.get("access_token")
                self._auth_method = "oauth"
            self._team_id = credentials.get("team_id")
            self._team_name = credentials.get("team_name")
            self._save_credentials()
            return bool(self._access_token)

        # Interactive authentication - prompt user for method
        import click

        click.echo("\nChoose authentication method:")
        click.echo("1. OAuth (requires Slack app with redirect URL configured)")
        click.echo("2. User Token (simpler - just paste token from Slack app)")
        choice = click.prompt("Enter choice", type=click.Choice(["1", "2"]), default="2")

        if choice == "2":
            # Direct token authentication
            click.echo("\nTo get your User OAuth Token:")
            click.echo("  1. Go to https://api.slack.com/apps")
            click.echo("  2. Select your app (or create one)")
            click.echo("  3. Go to 'OAuth & Permissions'")
            click.echo("  4. Add required scopes: search:read, channels:read, channels:history")
            click.echo("  5. Install the app to your workspace")
            click.echo("  6. Copy the 'User OAuth Token' (starts with xoxp-)")
            click.echo()

            user_token = click.prompt("Enter your User OAuth Token", hide_input=True)

            if not user_token.startswith("xoxp-"):
                click.echo("Warning: Token doesn't start with 'xoxp-'. Make sure you copied the User OAuth Token, not the Bot Token.")

            self._access_token = user_token
            self._auth_method = "token"
            self._save_credentials()
            return True
        else:
            # Check if OAuth is properly configured
            if not self._client_id or self._client_id == DEFAULT_CLIENT_ID:
                click.echo("\nOAuth requires client_id to be configured in settings.toml")
                click.echo("Or use option 2 (User Token) which doesn't require OAuth setup.")
                return False

            # Perform OAuth flow
            tokens = await self._perform_oauth_flow()
            if tokens:
                self._access_token = tokens["access_token"]
                self._team_id = tokens.get("team_id")
                self._team_name = tokens.get("team_name")
                self._auth_method = "oauth"
                self._save_credentials()
                return True
            return False

    async def _perform_oauth_flow(self) -> dict | None:
        """Perform Slack OAuth flow."""
        from aiohttp import web

        state = secrets.token_urlsafe(32)
        code_received: dict = {}

        async def handle_callback(request: web.Request) -> web.Response:
            if request.query.get("state") != state:
                return web.Response(text="Invalid state", status=400)

            if "error" in request.query:
                code_received["error"] = request.query["error"]
                return web.Response(text=f"Error: {request.query['error']}")

            code_received["code"] = request.query.get("code")
            return web.Response(
                text="<html><body><h1>✓ Authentication Successful</h1>"
                "<p>You can close this window.</p></body></html>",
                content_type="text/html",
            )

        app = web.Application()
        app.router.add_get("/callback", handle_callback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8766)
        await site.start()

        # Build auth URL
        params = {
            "client_id": self._client_id,
            "scope": ",".join(SCOPES),
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
        auth_url = f"{SLACK_AUTH_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        print(f"\nOpening browser for Slack authentication...")
        print(f"If browser doesn't open, visit: {auth_url}\n")
        webbrowser.open(auth_url)

        # Wait for callback
        for _ in range(120):
            if code_received:
                break
            await asyncio.sleep(1)

        await runner.cleanup()

        if "error" in code_received:
            print(f"Authentication failed: {code_received['error']}")
            return None

        if "code" not in code_received:
            print("Authentication timed out.")
            return None

        # Exchange code for token
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    SLACK_TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code_received["code"],
                        "redirect_uri": REDIRECT_URI,
                    },
                )
                data = response.json()
                if not data.get("ok"):
                    print(f"Token exchange failed: {data.get('error')}")
                    return None

                return {
                    "access_token": data["access_token"],
                    "team_id": data.get("team", {}).get("id"),
                    "team_name": data.get("team", {}).get("name"),
                }
        except Exception as e:
            print(f"Token exchange failed: {e}")
            return None

    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._access_token is not None

    async def test_connection(self) -> bool:
        """Verify the connection is working."""
        if not self._access_token:
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{SLACK_API_BASE}/auth.test",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
                data = response.json()
                if data.get("ok"):
                    # Populate team info if we didn't have it
                    if not self._team_id:
                        self._team_id = data.get("team_id")
                        self._team_name = data.get("team")
                        self._save_credentials()
                    return True
                return False
        except Exception:
            return False

    async def search(
        self,
        query: str,
        since: datetime | None = None,
        until: datetime | None = None,
        fields: list[str] | None = None,
        **kwargs,
    ) -> list[ContextItem]:
        """Search for Slack messages matching the query."""
        if not self._access_token:
            return []

        try:
            # Apply user exclusions to query
            search_query = query
            excluded_users = self.config.get("excluded_users", [])
            if excluded_users:
                # Quote usernames with spaces for Slack search
                parts = []
                for user in excluded_users:
                    if " " in user:
                        parts.append(f'-from:"{user}"')
                    else:
                        parts.append(f"-from:{user}")
                search_query = f"{query} {' '.join(parts)}"

            async with httpx.AsyncClient() as client:
                params: dict = {
                    "query": search_query,
                    "count": self.config["page_size"],
                    "sort": "timestamp",
                }

                logger.debug(f"Slack search query: {search_query}")
                response = await client.post(
                    f"{SLACK_API_BASE}/search.messages",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    data=params,
                )
                data = response.json()

                if not data.get("ok"):
                    logger.debug(f"Slack search failed: {data.get('error', 'unknown error')}")
                    return []

                items = []
                messages = data.get("messages", {}).get("matches", [])
                logger.debug(f"Slack search returned {len(messages)} messages")

                for msg in messages:
                    timestamp = datetime.fromtimestamp(float(msg.get("ts", 0)))

                    # Apply time filters
                    if since and timestamp < since:
                        continue
                    if until and timestamp > until:
                        continue

                    items.append(
                        ContextItem(
                            source="slack",
                            item_type="message",
                            title=f"Message in #{msg.get('channel', {}).get('name', 'unknown')}",
                            content=msg.get("text", ""),
                            url=msg.get("permalink", ""),
                            timestamp=timestamp,
                            author=msg.get("username", "Unknown"),
                            metadata={
                                "channel_id": msg.get("channel", {}).get("id"),
                                "channel_name": msg.get("channel", {}).get("name"),
                                "thread_ts": msg.get("thread_ts"),
                            },
                        )
                    )

                logger.debug(f"Slack search returning {len(items)} items")
                return items
        except Exception as e:
            logger.debug(f"Slack search failed with exception: {e}")
            return []

    async def get_item_details(self, item_id: str) -> ContextItem | None:
        """Fetch full details of a specific message.

        item_id should be in format: channel_id:message_ts
        """
        if not self._access_token:
            return None

        try:
            parts = item_id.split(":")
            if len(parts) != 2:
                return None

            channel_id, message_ts = parts

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{SLACK_API_BASE}/conversations.history",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    data={
                        "channel": channel_id,
                        "latest": message_ts,
                        "inclusive": True,
                        "limit": 1,
                    },
                )
                data = response.json()

                if not data.get("ok") or not data.get("messages"):
                    return None

                msg = data["messages"][0]
                return ContextItem(
                    source="slack",
                    item_type="message",
                    title=f"Message in channel",
                    content=msg.get("text", ""),
                    url="",
                    timestamp=datetime.fromtimestamp(float(msg.get("ts", 0))),
                    author=msg.get("user", "Unknown"),
                    metadata={
                        "channel_id": channel_id,
                        "thread_ts": msg.get("thread_ts"),
                    },
                )
        except Exception:
            return None

    async def disconnect(self) -> None:
        """Clear stored credentials."""
        delete_credentials("slack")
        self._access_token = None
        self._team_id = None
        self._team_name = None
        self._auth_method = "oauth"

    def supported_reference_types(self) -> list[str]:
        """Return list of reference types this plugin can resolve."""
        return ["message"]

    def extract_references(
        self, items: list[ContextItem]
    ) -> list[tuple[str, str]]:
        """Extract Slack message references from content.

        Finds Slack message permalinks like:
        - https://workspace.slack.com/archives/C01234567/p1234567890123456

        Converts them to the channel_id:message_ts format expected by get_item_details.

        Args:
            items: List of ContextItem objects to scan for references.

        Returns:
            List of (reference_type, reference_id) tuples.
        """
        import re

        references: list[tuple[str, str]] = []
        seen: set[str] = set()

        # Pattern for Slack message permalinks
        # Format: https://workspace.slack.com/archives/CHANNEL_ID/p<timestamp>
        # The timestamp is the message_ts with the decimal removed
        permalink_pattern = re.compile(
            r"https?://[a-zA-Z0-9_-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d+)"
        )

        for item in items:
            text = f"{item.title} {item.content}"

            for match in permalink_pattern.finditer(text):
                channel_id = match.group(1)
                # Slack timestamps have format like "1234567890.123456"
                # Permalinks use "p1234567890123456" (no decimal)
                raw_ts = match.group(2)
                # Convert back to Slack ts format: insert decimal before last 6 digits
                if len(raw_ts) > 6:
                    message_ts = f"{raw_ts[:-6]}.{raw_ts[-6:]}"
                else:
                    message_ts = raw_ts

                ref_id = f"{channel_id}:{message_ts}"

                if ref_id not in seen:
                    references.append(("message", ref_id))
                    seen.add(ref_id)

        return references

