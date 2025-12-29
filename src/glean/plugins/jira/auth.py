"""OAuth 2.0 authentication for Atlassian Cloud (JIRA).

Uses OAuth 2.0 with PKCE for secure authentication without client secrets.
"""

import asyncio
import base64
import hashlib
import secrets
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

# Atlassian OAuth endpoints
ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# OAuth configuration - these would typically be registered app credentials
# For a real app, these should be configurable or registered
DEFAULT_CLIENT_ID = "glean-jira-integration"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES = [
    "read:jira-work",
    "read:jira-user",
    "offline_access",  # For refresh tokens
]


@dataclass
class OAuthTokens:
    """Container for OAuth tokens."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    cloud_id: str  # Atlassian Cloud site ID
    site_url: str  # The JIRA site URL


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code verifier and challenge.

    Returns:
        A tuple of (code_verifier, code_challenge).
    """
    # Generate a random code verifier (43-128 characters)
    code_verifier = secrets.token_urlsafe(64)

    # Create SHA256 hash and base64url encode it
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    return code_verifier, code_challenge


def build_authorization_url(client_id: str, state: str, code_challenge: str) -> str:
    """Build the OAuth authorization URL.

    Args:
        client_id: The OAuth client ID.
        state: A random state string for CSRF protection.
        code_challenge: The PKCE code challenge.

    Returns:
        The full authorization URL.
    """
    params = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{ATLASSIAN_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_tokens(
    client_id: str, code: str, code_verifier: str
) -> dict:
    """Exchange an authorization code for tokens.

    Args:
        client_id: The OAuth client ID.
        code: The authorization code from the callback.
        code_verifier: The PKCE code verifier.

    Returns:
        The token response dict.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            ATLASSIAN_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
        response.raise_for_status()
        return response.json()


async def refresh_access_token(client_id: str, refresh_token: str) -> dict:
    """Use a refresh token to get a new access token.

    Args:
        client_id: The OAuth client ID.
        refresh_token: The refresh token.

    Returns:
        The new token response dict.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            ATLASSIAN_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        return response.json()


async def get_accessible_resources(access_token: str) -> list[dict]:
    """Get the Atlassian Cloud resources accessible with the token.

    Args:
        access_token: The OAuth access token.

    Returns:
        A list of accessible resource dicts with 'id', 'name', 'url'.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            ATLASSIAN_RESOURCES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


class LocalOAuthServer:
    """Simple local HTTP server to handle OAuth callback."""

    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None

    async def wait_for_callback(self, timeout: int = 120) -> str | None:
        """Start server and wait for OAuth callback.

        Args:
            timeout: Maximum seconds to wait for callback.

        Returns:
            The authorization code, or None if failed.
        """
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/callback", self._handle_callback)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8765)
        await site.start()

        try:
            # Wait for callback with timeout
            for _ in range(timeout):
                if self.code or self.error:
                    break
                await asyncio.sleep(1)
        finally:
            await runner.cleanup()

        return self.code

    async def _handle_callback(self, request: "web.Request") -> "web.Response":
        """Handle the OAuth callback request."""
        from aiohttp import web

        state = request.query.get("state")
        if state != self.expected_state:
            self.error = "Invalid state parameter"
            return web.Response(
                text="Error: Invalid state. Please try again.",
                content_type="text/html",
            )

        if "error" in request.query:
            self.error = request.query.get("error_description", request.query["error"])
            return web.Response(
                text=f"Error: {self.error}",
                content_type="text/html",
            )

        self.code = request.query.get("code")
        if not self.code:
            self.error = "No authorization code received"
            return web.Response(
                text="Error: No authorization code received.",
                content_type="text/html",
            )

        return web.Response(
            text="""
            <html>
            <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                <h1>✓ Authentication Successful</h1>
                <p>You can close this window and return to the terminal.</p>
            </body>
            </html>
            """,
            content_type="text/html",
        )


async def perform_oauth_flow(client_id: str | None = None) -> OAuthTokens | None:
    """Perform the full OAuth flow with PKCE.

    This opens a browser for the user to authenticate, then captures
    the callback and exchanges the code for tokens.

    Args:
        client_id: Optional OAuth client ID. Uses default if not provided.

    Returns:
        OAuthTokens if successful, None if failed.
    """
    client_id = client_id or DEFAULT_CLIENT_ID

    # Generate PKCE pair and state
    code_verifier, code_challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    auth_url = build_authorization_url(client_id, state, code_challenge)

    # Start local server for callback
    server = LocalOAuthServer(state)

    # Open browser
    print(f"\nOpening browser for Atlassian authentication...")
    print(f"If browser doesn't open, visit: {auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for callback
    code = await server.wait_for_callback()

    if not code:
        print(f"Authentication failed: {server.error}")
        return None

    # Exchange code for tokens
    try:
        token_response = await exchange_code_for_tokens(client_id, code, code_verifier)
    except httpx.HTTPStatusError as e:
        print(f"Token exchange failed: {e}")
        return None

    # Get accessible resources to find cloud ID
    access_token = token_response["access_token"]
    try:
        resources = await get_accessible_resources(access_token)
    except httpx.HTTPStatusError as e:
        print(f"Failed to get accessible resources: {e}")
        return None

    if not resources:
        print("No accessible Atlassian sites found.")
        return None

    # Use first available site (could prompt user if multiple)
    site = resources[0]

    # Calculate token expiry
    expires_in = token_response.get("expires_in", 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    return OAuthTokens(
        access_token=access_token,
        refresh_token=token_response.get("refresh_token"),
        expires_at=expires_at,
        cloud_id=site["id"],
        site_url=site["url"],
    )



