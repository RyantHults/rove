"""Slack plugin for Rove.

Provides access to Slack messages and threads for context gathering.
"""

from .client import SlackContextClient

PLUGIN_NAME = "slack"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Slack workspace integration for message context"


def create_client(config: dict | None = None) -> SlackContextClient:
    """Create a new Slack client instance.

    Args:
        config: Optional configuration dict.

    Returns:
        A configured SlackContextClient instance.
    """
    return SlackContextClient(config or {})



