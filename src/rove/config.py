"""Configuration management for Rove.

Settings are loaded from ./.rove/settings.toml (in the current working directory) with the following precedence:
1. CLI flags (highest)
2. Config file
3. Built-in defaults (lowest)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import toml

# Default paths - stored in current working directory
ROVE_HOME = Path.cwd() / ".rove"
SETTINGS_FILE = ROVE_HOME / "settings.toml"
DATABASE_FILE = ROVE_HOME / "rove.db"
API_SOCKET = ROVE_HOME / "api.sock"
PID_FILE = ROVE_HOME / "rove.pid"


@dataclass
class SourceConfig:
    """Configuration for a specific source plugin."""

    rate_limit: int = 100  # requests per minute
    page_size: int = 50  # items per API call
    client_id: str = ""  # OAuth client ID (required for OAuth sources)
    client_secret: str = ""  # OAuth client secret (required for OAuth sources)
    default_owner: str = ""  # GitHub: default org/owner for PR/issue lookups
    default_repo: str = ""  # GitHub: default repo for PR/issue lookups


@dataclass
class SourcesConfig:
    """Sources configuration section."""

    default_ticket_source: str = "jira"
    jira: SourceConfig = field(default_factory=SourceConfig)
    slack: SourceConfig = field(default_factory=lambda: SourceConfig(rate_limit=50, page_size=100))
    github: SourceConfig = field(
        default_factory=lambda: SourceConfig(rate_limit=60, page_size=100)
    )


@dataclass
class SchedulerConfig:
    """Scheduler configuration section."""

    refresh_interval: str = "6h"
    retry_attempts: int = 3
    retry_delay: str = "30m"
    staleness_threshold: str = "7d"


@dataclass
class AIConfig:
    """AI configuration section."""

    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    max_hops: int = 3


@dataclass
class CredentialsConfig:
    """Credentials storage configuration."""

    backend: str = "auto"  # "auto", "keychain", "encrypted_file"


@dataclass
class RoveConfig:
    """Main configuration container."""

    sources: SourcesConfig = field(default_factory=SourcesConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)


def ensure_rove_home() -> None:
    """Create the Rove home directory if it doesn't exist."""
    ROVE_HOME.mkdir(parents=True, exist_ok=True)


def load_config() -> RoveConfig:
    """Load configuration from settings.toml, merging with defaults."""
    config = RoveConfig()

    if not SETTINGS_FILE.exists():
        return config

    try:
        data = toml.load(SETTINGS_FILE)
    except Exception:
        return config

    # Merge sources section
    if "sources" in data:
        sources_data = data["sources"]
        if "default_ticket_source" in sources_data:
            config.sources.default_ticket_source = sources_data["default_ticket_source"]

        for source_name in ["jira", "slack", "github"]:
            if source_name in sources_data:
                source_config = getattr(config.sources, source_name)
                source_data = sources_data[source_name]
                if "rate_limit" in source_data:
                    source_config.rate_limit = source_data["rate_limit"]
                if "page_size" in source_data:
                    source_config.page_size = source_data["page_size"]
                if "client_id" in source_data:
                    source_config.client_id = source_data["client_id"]
                if "client_secret" in source_data:
                    source_config.client_secret = source_data["client_secret"]
                if "default_owner" in source_data:
                    source_config.default_owner = source_data["default_owner"]
                if "default_repo" in source_data:
                    source_config.default_repo = source_data["default_repo"]

    # Merge scheduler section
    if "scheduler" in data:
        sched_data = data["scheduler"]
        for key in ["refresh_interval", "retry_attempts", "retry_delay", "staleness_threshold"]:
            if key in sched_data:
                setattr(config.scheduler, key, sched_data[key])

    # Merge AI section
    if "ai" in data:
        ai_data = data["ai"]
        for key in ["api_base", "api_key", "model", "max_hops"]:
            if key in ai_data:
                setattr(config.ai, key, ai_data[key])

    # Merge credentials section
    if "credentials" in data:
        cred_data = data["credentials"]
        if "backend" in cred_data:
            config.credentials.backend = cred_data["backend"]

    return config


def save_config(config: RoveConfig) -> None:
    """Save configuration to settings.toml."""
    ensure_rove_home()

    data: dict[str, Any] = {
        "sources": {
            "default_ticket_source": config.sources.default_ticket_source,
            "jira": {
                "rate_limit": config.sources.jira.rate_limit,
                "page_size": config.sources.jira.page_size,
                "client_id": config.sources.jira.client_id,
            },
            "slack": {
                "rate_limit": config.sources.slack.rate_limit,
                "page_size": config.sources.slack.page_size,
                "client_id": config.sources.slack.client_id,
                "client_secret": config.sources.slack.client_secret,
            },
            "github": {
                "rate_limit": config.sources.github.rate_limit,
                "page_size": config.sources.github.page_size,
                "client_id": config.sources.github.client_id,
                "client_secret": config.sources.github.client_secret,
                "default_owner": config.sources.github.default_owner,
                "default_repo": config.sources.github.default_repo,
            },
        },
        "scheduler": {
            "refresh_interval": config.scheduler.refresh_interval,
            "retry_attempts": config.scheduler.retry_attempts,
            "retry_delay": config.scheduler.retry_delay,
            "staleness_threshold": config.scheduler.staleness_threshold,
        },
        "ai": {
            "api_base": config.ai.api_base,
            "api_key": config.ai.api_key,
            "model": config.ai.model,
            "max_hops": config.ai.max_hops,
        },
        "credentials": {
            "backend": config.credentials.backend,
        },
    }

    with open(SETTINGS_FILE, "w") as f:
        toml.dump(data, f)


def create_default_config() -> bool:
    """Create a default settings.toml if it doesn't exist.

    Returns:
        True if a new config was created (first run), False if it already existed.
    """
    ensure_rove_home()

    if SETTINGS_FILE.exists():
        return False

    # Write a well-commented config file for new users
    commented_config = '''# Rove Configuration
# This file configures the Rove context extraction service.
# For more information, see: https://github.com/your-org/rove

[sources]
# The primary source for fetching ticket details
default_ticket_source = "jira"

# =============================================================================
# IMPORTANT: Authentication Options
# =============================================================================
# JIRA and GitHub support two authentication methods:
#
# 1. API Token (Recommended - simpler, no app registration needed)
#    - JIRA: Use your email + API token (create at https://id.atlassian.com/manage-profile/security/api-tokens)
#    - GitHub: Use a Personal Access Token (create at https://github.com/settings/tokens)
#    - When you run "rove --add-source jira" or "rove --add-source github",
#      choose option 2 to use API token authentication
#
# 2. OAuth (Requires app registration)
#    - JIRA (Atlassian Cloud):
#      1. Go to https://developer.atlassian.com/console/myapps/
#      2. Create an OAuth 2.0 app with "read:jira-work" scope
#      3. Set callback URL to: http://localhost:8765/callback
#      4. Provide client_id below (client_secret not needed for PKCE flow)
#
#    - GitHub:
#      1. Go to https://github.com/settings/developers
#      2. Create an OAuth App
#      3. Set callback URL to: http://localhost:8767/callback
#      4. Provide client_id and client_secret below
#
#    - Slack:
#      1. Go to https://api.slack.com/apps and create an app
#      2. Add OAuth scopes: search:read, channels:read, channels:history
#      3. Set redirect URL to: http://localhost:8766/callback
#      4. Provide client_id and client_secret below
# =============================================================================

[sources.jira]
rate_limit = 100  # requests per minute
page_size = 50    # items per API call
# OAuth authentication (optional - API token is simpler)
# client_id = "your-atlassian-client-id"
# client_secret = ""  # Not needed for PKCE flow
# Note: For API token auth, credentials are stored securely via keyring

[sources.slack]
rate_limit = 50
page_size = 100
# client_id = "your-slack-client-id"
# client_secret = "your-slack-client-secret"

[sources.github]
rate_limit = 60
page_size = 100
# OAuth authentication (optional - Personal Access Token is simpler)
# client_id = "your-github-client-id"
# client_secret = "your-github-client-secret"
# Note: For Personal Access Token auth, credentials are stored securely via keyring
# default_owner = "your-org"  # Default org/owner for PR lookups
# default_repo = "your-repo"  # Default repo for PR lookups

[scheduler]
# How often to refresh context files
refresh_interval = "6h"

# Retry settings for failed tasks
retry_attempts = 3
retry_delay = "30m"

# Stop refreshing tickets older than this
staleness_threshold = "7d"

[ai]
# OpenAI-compatible API endpoint
# For local models (Ollama): http://localhost:11434/v1
# For OpenRouter: https://openrouter.ai/api/v1
api_base = "https://api.openai.com/v1"

# Your API key (required for most providers)
api_key = ""

# Model to use for AI-assisted search and grouping
model = "gpt-4o-mini"

# Maximum search depth for following references
max_hops = 3

[credentials]
# Credential storage backend: "auto", "keychain", "encrypted_file"
# "auto" selects the best available option for your OS
backend = "auto"
'''

    SETTINGS_FILE.write_text(commented_config)
    return True


def parse_duration(duration: str) -> int:
    """Parse a duration string like '6h', '30m', '7d' into seconds."""
    unit = duration[-1].lower()
    value = int(duration[:-1])

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }

    if unit not in multipliers:
        raise ValueError(f"Invalid duration unit: {unit}")

    return value * multipliers[unit]

