"""GitHub plugin for Rove.

Provides access to GitHub PRs, issues, and commits.
"""

from .client import GitHubContextClient

PLUGIN_NAME = "github"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "GitHub integration for code context"


def create_client(config: dict | None = None) -> GitHubContextClient:
    """Create a new GitHub client instance.

    Args:
        config: Optional configuration dict.

    Returns:
        A configured GitHubContextClient instance.
    """
    return GitHubContextClient(config or {})



