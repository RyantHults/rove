"""GitHub Context Client implementation.

Provides access to GitHub PRs, issues, commits, and discussions.
"""

import asyncio
import re
import secrets
import webbrowser
from datetime import datetime

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

logger = get_logger("github")

# GitHub OAuth endpoints
GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_BASE = "https://api.github.com"

# Default OAuth settings
DEFAULT_CLIENT_ID = "rove-github-integration"
DEFAULT_CLIENT_SECRET = ""
REDIRECT_URI = "http://localhost:8767/callback"
SCOPES = ["repo", "read:org"]


class GitHubContextClient(ContextClient):
    """GitHub implementation of the ContextClient protocol."""

    DEFAULT_CONFIG = {
        "rate_limit": 60,
        "page_size": 100,
    }

    def __init__(self, config: dict):
        """Initialize the GitHub client."""
        self.config = {**self.DEFAULT_CONFIG, **config}
        self._access_token: str | None = None
        self._username: str | None = None
        self._auth_method: str = "oauth"  # "oauth" or "pat"
        self._client_id = config.get("client_id", DEFAULT_CLIENT_ID)
        self._client_secret = config.get("client_secret", DEFAULT_CLIENT_SECRET)
        self._default_owner = config.get("default_owner")
        self._default_repo = config.get("default_repo")
        self._load_stored_credentials()

    def _load_stored_credentials(self) -> None:
        """Load credentials from keyring if available."""
        creds = get_credentials("github")
        if creds:
            self._access_token = creds.get("access_token")
            self._username = creds.get("username")
            self._auth_method = creds.get("auth_method", "oauth")
            self._default_owner = creds.get("default_owner") or self._default_owner
            self._default_repo = creds.get("default_repo") or self._default_repo

    def _save_credentials(self) -> None:
        """Save current tokens to keyring."""
        if self._access_token:
            store_credentials(
                "github",
                {
                    "access_token": self._access_token,
                    "username": self._username,
                    "auth_method": self._auth_method,
                    "default_owner": self._default_owner,
                    "default_repo": self._default_repo,
                },
            )

    def source_name(self) -> str:
        """Return human-readable name."""
        return "GitHub"

    def get_config_schema(self) -> dict:
        """Return JSON schema of required configuration."""
        return {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "GitHub OAuth client ID",
                },
                "client_secret": {
                    "type": "string",
                    "description": "GitHub OAuth client secret",
                },
                "default_owner": {
                    "type": "string",
                    "description": "Default repository owner/org",
                },
                "default_repo": {
                    "type": "string",
                    "description": "Default repository name",
                },
                "rate_limit": {
                    "type": "integer",
                    "description": "Max requests per minute",
                    "default": 60,
                },
            },
            "required": [],
        }

    def get_searchable_fields(self) -> list[SearchableField]:
        """Return list of fields this source can search."""
        return [
            SearchableField(
                name="pr_title",
                field_type="text",
                description="Pull request title",
            ),
            SearchableField(
                name="pr_description",
                field_type="text",
                description="Pull request description/body",
            ),
            SearchableField(
                name="pr_comments",
                field_type="text",
                description="Pull request review comments",
            ),
            SearchableField(
                name="commit_messages",
                field_type="text",
                description="Commit messages",
            ),
            SearchableField(
                name="issue_title",
                field_type="text",
                description="Issue title",
            ),
            SearchableField(
                name="issue_body",
                field_type="text",
                description="Issue description/body",
            ),
        ]

    async def authenticate(self, credentials: dict | None = None) -> bool:
        """Authenticate with GitHub via OAuth or Personal Access Token.

        Args:
            credentials: Optional pre-existing credentials dict.
                        For OAuth: {"access_token": ..., "username": ...}
                        For PAT: {"access_token": ..., "username": ...} (same format)

        Returns:
            True if authentication succeeded.
        """
        if credentials:
            self._access_token = credentials.get("access_token")
            self._username = credentials.get("username")
            self._auth_method = credentials.get("auth_method", "oauth")
            self._save_credentials()
            return bool(self._access_token)

        # Interactive authentication - prompt user for method
        import click

        click.echo("\nChoose authentication method:")
        click.echo("1. OAuth (requires OAuth app registration)")
        click.echo("2. Personal Access Token (simpler, no app registration needed)")
        choice = click.prompt("Enter choice", type=click.Choice(["1", "2"]), default="2")

        if choice == "2":
            # Personal Access Token authentication
            pat = click.prompt("Enter your GitHub Personal Access Token", hide_input=True)
            self._access_token = pat
            self._auth_method = "pat"

            # Fetch username to verify token
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{GITHUB_API_BASE}/user",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if response.status_code == 200:
                    self._username = response.json().get("login")
                    click.echo(f"✓ Authenticated as {self._username}")
                else:
                    click.echo("✗ Invalid token. Please check your Personal Access Token.")
                    return False

            self._save_credentials()
            return True
        else:
            # Perform OAuth flow
            tokens = await self._perform_oauth_flow()
            if tokens:
                self._access_token = tokens["access_token"]
                self._auth_method = "oauth"

                # Fetch username
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"{GITHUB_API_BASE}/user",
                        headers={
                            "Authorization": f"Bearer {self._access_token}",
                            "Accept": "application/vnd.github+json",
                        },
                    )
                    if response.status_code == 200:
                        self._username = response.json().get("login")

                self._save_credentials()
                return True
            return False

    async def _perform_oauth_flow(self) -> dict | None:
        """Perform GitHub OAuth flow."""
        from aiohttp import web

        state = secrets.token_urlsafe(32)
        code_received: dict = {}

        async def handle_callback(request: web.Request) -> web.Response:
            if request.query.get("state") != state:
                return web.Response(text="Invalid state", status=400)

            if "error" in request.query:
                code_received["error"] = request.query.get(
                    "error_description", request.query["error"]
                )
                return web.Response(text=f"Error: {code_received['error']}")

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
        site = web.TCPSite(runner, "localhost", 8767)
        await site.start()

        # Build auth URL
        params = {
            "client_id": self._client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
        auth_url = f"{GITHUB_AUTH_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        print(f"\nOpening browser for GitHub authentication...")
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
                    GITHUB_TOKEN_URL,
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code_received["code"],
                        "redirect_uri": REDIRECT_URI,
                    },
                )
                data = response.json()
                if "error" in data:
                    print(f"Token exchange failed: {data.get('error_description', data['error'])}")
                    return None

                return {"access_token": data["access_token"]}
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
                response = await client.get(
                    f"{GITHUB_API_BASE}/user",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                return response.status_code == 200
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
        """Search for GitHub issues and PRs matching the query."""
        if not self._access_token:
            logger.debug("No access token available for GitHub search")
            return []

        items: list[ContextItem] = []

        # Build search query with proper GitHub search syntax
        # If it looks like a ticket ID, search in title and body explicitly
        if self._looks_like_ticket_id(query):
            # For ticket IDs, search in title and body
            # Quote the ticket ID to handle special characters
            search_query = f'"{query}" in:title,body'
        else:
            # For regular text queries, search in title and body
            search_query = f"{query} in:title,body"

        # Add time filters
        if since:
            search_query += f" updated:>={since.strftime('%Y-%m-%d')}"
        if until:
            search_query += f" updated:<={until.strftime('%Y-%m-%d')}"

        # If we have a default repo, scope the search to that repo
        if self._default_owner and self._default_repo:
            search_query += f" repo:{self._default_owner}/{self._default_repo}"

        logger.debug(f"GitHub search query: {search_query}")

        try:
            async with httpx.AsyncClient() as client:
                # Search issues and PRs
                response = await client.get(
                    f"{GITHUB_API_BASE}/search/issues",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                    params={
                        "q": search_query,
                        "per_page": self.config["page_size"],
                        "sort": "updated",
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    total_count = data.get("total_count", 0)
                    logger.debug(f"GitHub search returned {total_count} items")
                    
                    for item in data.get("items", []):
                        item_type = "pr" if "pull_request" in item else "issue"
                        items.append(
                            ContextItem(
                                source="github",
                                item_type=item_type,
                                title=item.get("title", ""),
                                content=item.get("body", "") or "",
                                url=item.get("html_url", ""),
                                timestamp=datetime.fromisoformat(
                                    item.get("updated_at", "").replace("Z", "+00:00")
                                ),
                                author=item.get("user", {}).get("login", "Unknown"),
                                metadata={
                                    "number": item.get("number"),
                                    "state": item.get("state"),
                                    "labels": [l["name"] for l in item.get("labels", [])],
                                    "repo": item.get("repository_url", "").split("/")[-1]
                                    if item.get("repository_url")
                                    else None,
                                },
                            )
                        )
                elif response.status_code == 422:
                    # GitHub API validation error - might be invalid query syntax
                    error_data = response.json()
                    logger.warning(f"GitHub search API validation error: {error_data}")
                elif response.status_code == 403:
                    # Rate limit or permission issue
                    logger.warning("GitHub API rate limit or permission denied")
                else:
                    logger.warning(f"GitHub search failed with status {response.status_code}: {response.text}")

                # Also search commits if query looks like a ticket ID
                if self._looks_like_ticket_id(query) and self._default_owner and self._default_repo:
                    commits_query = f'"{query}" repo:{self._default_owner}/{self._default_repo}'
                    logger.debug(f"GitHub commit search query: {commits_query}")
                    
                    commits_response = await client.get(
                        f"{GITHUB_API_BASE}/search/commits",
                        headers={
                            "Authorization": f"Bearer {self._access_token}",
                            "Accept": "application/vnd.github+json",
                        },
                        params={
                            "q": commits_query,
                            "per_page": 20,
                        },
                    )

                    if commits_response.status_code == 200:
                        commits_data = commits_response.json()
                        commit_count = commits_data.get("total_count", 0)
                        logger.debug(f"GitHub commit search returned {commit_count} items")
                        
                        for commit in commits_data.get("items", []):
                            commit_info = commit.get("commit", {})
                            items.append(
                                ContextItem(
                                    source="github",
                                    item_type="commit",
                                    title=commit_info.get("message", "").split("\n")[0],
                                    content=commit_info.get("message", ""),
                                    url=commit.get("html_url", ""),
                                    timestamp=datetime.fromisoformat(
                                        commit_info.get("author", {})
                                        .get("date", "")
                                        .replace("Z", "+00:00")
                                    ),
                                    author=commit_info.get("author", {}).get("name", "Unknown"),
                                    metadata={
                                        "sha": commit.get("sha"),
                                    },
                                )
                            )

        except httpx.HTTPError as e:
            logger.error(f"GitHub search HTTP error: {e}")
        except Exception as e:
            logger.error(f"GitHub search error: {e}", exc_info=True)

        logger.debug(f"GitHub search returning {len(items)} items")
        return items

    async def get_item_details(self, item_id: str) -> ContextItem | None:
        """Fetch full details of a specific PR or issue.

        item_id can be:
        - PR/issue number (uses default repo)
        - owner/repo#number
        """
        if not self._access_token:
            return None

        # Parse item_id
        owner = self._default_owner
        repo = self._default_repo
        number = item_id

        if "/" in item_id:
            parts = item_id.replace("#", "/").split("/")
            if len(parts) >= 3:
                owner, repo, number = parts[0], parts[1], parts[2]
        elif "#" in item_id:
            number = item_id.split("#")[1]

        if not owner or not repo:
            return None

        try:
            async with httpx.AsyncClient() as client:
                # Try as PR first
                response = await client.get(
                    f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{number}",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    return ContextItem(
                        source="github",
                        item_type="pr",
                        title=data.get("title", ""),
                        content=data.get("body", "") or "",
                        url=data.get("html_url", ""),
                        timestamp=datetime.fromisoformat(
                            data.get("updated_at", "").replace("Z", "+00:00")
                        ),
                        author=data.get("user", {}).get("login", "Unknown"),
                        metadata={
                            "number": data.get("number"),
                            "state": data.get("state"),
                            "merged": data.get("merged", False),
                            "repo": f"{owner}/{repo}",
                        },
                    )

                # Try as issue
                response = await client.get(
                    f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{number}",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    return ContextItem(
                        source="github",
                        item_type="issue",
                        title=data.get("title", ""),
                        content=data.get("body", "") or "",
                        url=data.get("html_url", ""),
                        timestamp=datetime.fromisoformat(
                            data.get("updated_at", "").replace("Z", "+00:00")
                        ),
                        author=data.get("user", {}).get("login", "Unknown"),
                        metadata={
                            "number": data.get("number"),
                            "state": data.get("state"),
                            "labels": [l["name"] for l in data.get("labels", [])],
                            "repo": f"{owner}/{repo}",
                        },
                    )

        except Exception:
            pass

        return None

    async def disconnect(self) -> None:
        """Clear stored credentials."""
        delete_credentials("github")
        self._access_token = None
        self._username = None

    def supported_reference_types(self) -> list[str]:
        """Return list of reference types this plugin can resolve."""
        return ["pr", "issue", "commit"]

    def _looks_like_ticket_id(self, query: str) -> bool:
        """Check if query looks like a ticket ID (e.g., TB-214, ABC-123)."""
        return bool(re.match(r"^[A-Z]{2,10}-\d+$", query.upper()))

