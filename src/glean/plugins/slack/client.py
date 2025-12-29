"""Slack Context Client implementation.

Provides access to Slack messages, channels, and threads.
"""

import asyncio
import secrets
import webbrowser
from datetime import datetime, timedelta

import httpx

from ..base import (
    ContextClient,
    ContextItem,
    SearchableField,
    delete_credentials,
    get_credentials,
    store_credentials,
)

# Slack OAuth endpoints
SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_API_BASE = "https://slack.com/api"

# Default OAuth settings - would be configured per installation
DEFAULT_CLIENT_ID = "glean-slack-integration"
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
        self._client_id = config.get("client_id", DEFAULT_CLIENT_ID)
        self._client_secret = config.get("client_secret", DEFAULT_CLIENT_SECRET)
        self._load_stored_credentials()

    def _load_stored_credentials(self) -> None:
        """Load credentials from keyring if available."""
        creds = get_credentials("slack")
        if creds:
            self._access_token = creds.get("access_token")
            self._team_id = creds.get("team_id")
            self._team_name = creds.get("team_name")

    def _save_credentials(self) -> None:
        """Save current tokens to keyring."""
        if self._access_token:
            store_credentials(
                "slack",
                {
                    "access_token": self._access_token,
                    "team_id": self._team_id,
                    "team_name": self._team_name,
                },
            )

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
        """Authenticate with Slack via OAuth."""
        if credentials:
            self._access_token = credentials.get("access_token")
            self._team_id = credentials.get("team_id")
            self._team_name = credentials.get("team_name")
            self._save_credentials()
            return bool(self._access_token)

        # Perform OAuth flow
        tokens = await self._perform_oauth_flow()
        if tokens:
            self._access_token = tokens["access_token"]
            self._team_id = tokens.get("team_id")
            self._team_name = tokens.get("team_name")
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
                return data.get("ok", False)
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
            async with httpx.AsyncClient() as client:
                params: dict = {
                    "query": query,
                    "count": self.config["page_size"],
                    "sort": "timestamp",
                }

                response = await client.post(
                    f"{SLACK_API_BASE}/search.messages",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    data=params,
                )
                data = response.json()

                if not data.get("ok"):
                    return []

                items = []
                messages = data.get("messages", {}).get("matches", [])

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

                return items
        except Exception:
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

    def supported_reference_types(self) -> list[str]:
        """Return list of reference types this plugin can resolve."""
        return ["message"]

