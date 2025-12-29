"""JIRA plugin for Glean.

Provides access to JIRA/Atlassian Cloud tickets, comments, and related data.
"""

from .client import JiraContextClient

PLUGIN_NAME = "jira"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Atlassian JIRA integration for ticket context"


def create_client(config: dict | None = None) -> JiraContextClient:
    """Create a new JIRA client instance.

    Args:
        config: Optional configuration dict with rate_limit, page_size, etc.

    Returns:
        A configured JiraContextClient instance.
    """
    return JiraContextClient(config or {})



