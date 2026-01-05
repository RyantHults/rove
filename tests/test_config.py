"""Tests for configuration module."""

import tempfile
from pathlib import Path

import pytest

from rove.config import (
    RoveConfig,
    parse_duration,
    load_config,
    save_config,
)


def test_parse_duration_seconds():
    """Test parsing seconds."""
    assert parse_duration("30s") == 30


def test_parse_duration_minutes():
    """Test parsing minutes."""
    assert parse_duration("5m") == 300


def test_parse_duration_hours():
    """Test parsing hours."""
    assert parse_duration("6h") == 21600


def test_parse_duration_days():
    """Test parsing days."""
    assert parse_duration("7d") == 604800


def test_parse_duration_invalid():
    """Test parsing invalid duration."""
    with pytest.raises(ValueError):
        parse_duration("5x")


def test_default_config():
    """Test default configuration values."""
    config = RoveConfig()
    assert config.sources.default_ticket_source == "jira"
    assert config.scheduler.refresh_interval == "6h"
    assert config.ai.max_hops == 3


def test_config_round_trip(tmp_path, monkeypatch):
    """Test saving and loading configuration."""
    # Use temp directory for settings
    settings_file = tmp_path / "settings.toml"
    
    monkeypatch.setattr("rove.config.SETTINGS_FILE", settings_file)
    monkeypatch.setattr("rove.config.ROVE_HOME", tmp_path)
    
    # Create and save config
    config = RoveConfig()
    config.sources.default_ticket_source = "github"
    config.ai.model = "gpt-4"
    
    save_config(config)
    
    # Load and verify
    loaded = load_config()
    assert loaded.sources.default_ticket_source == "github"
    assert loaded.ai.model == "gpt-4"


def test_env_overrides_without_config_file(tmp_path, monkeypatch):
    """Test that environment variables work even without a settings.toml."""
    settings_file = tmp_path / "settings.toml"
    # Don't create the file - it shouldn't exist
    
    monkeypatch.setattr("rove.config.SETTINGS_FILE", settings_file)
    monkeypatch.setattr("rove.config.ROVE_HOME", tmp_path)
    
    # Set environment variables
    monkeypatch.setenv("ROVE_AI_API_KEY", "test-api-key")
    monkeypatch.setenv("ROVE_AI_MODEL", "claude-3")
    
    loaded = load_config()
    assert loaded.ai.api_key == "test-api-key"
    assert loaded.ai.model == "claude-3"


def test_env_overrides_config_file(tmp_path, monkeypatch):
    """Test that environment variables take precedence over settings.toml."""
    settings_file = tmp_path / "settings.toml"
    
    monkeypatch.setattr("rove.config.SETTINGS_FILE", settings_file)
    monkeypatch.setattr("rove.config.ROVE_HOME", tmp_path)
    
    # Save a config with one value
    config = RoveConfig()
    config.ai.model = "gpt-4"
    config.ai.api_key = "file-api-key"
    save_config(config)
    
    # Set env var to override
    monkeypatch.setenv("ROVE_AI_API_KEY", "env-api-key")
    
    loaded = load_config()
    # Env var should win
    assert loaded.ai.api_key == "env-api-key"
    # File value preserved for non-overridden setting
    assert loaded.ai.model == "gpt-4"


def test_env_overrides_source_config(tmp_path, monkeypatch):
    """Test environment variable overrides for source-specific settings."""
    settings_file = tmp_path / "settings.toml"
    
    monkeypatch.setattr("rove.config.SETTINGS_FILE", settings_file)
    monkeypatch.setattr("rove.config.ROVE_HOME", tmp_path)
    
    # Set source-specific env vars
    monkeypatch.setenv("ROVE_SOURCES_GITHUB_DEFAULT_OWNER", "my-org")
    monkeypatch.setenv("ROVE_SOURCES_GITHUB_DEFAULT_REPO", "my-repo")
    monkeypatch.setenv("ROVE_SOURCES_JIRA_RATE_LIMIT", "200")
    monkeypatch.setenv("ROVE_SOURCES_SLACK_EXCLUDED_USERS", "bot1, bot2, bot3")
    
    loaded = load_config()
    assert loaded.sources.github.default_owner == "my-org"
    assert loaded.sources.github.default_repo == "my-repo"
    assert loaded.sources.jira.rate_limit == 200
    assert loaded.sources.slack.excluded_users == ["bot1", "bot2", "bot3"]


def test_env_overrides_scheduler_and_logging(tmp_path, monkeypatch):
    """Test environment variable overrides for scheduler and logging settings."""
    settings_file = tmp_path / "settings.toml"
    
    monkeypatch.setattr("rove.config.SETTINGS_FILE", settings_file)
    monkeypatch.setattr("rove.config.ROVE_HOME", tmp_path)
    
    monkeypatch.setenv("ROVE_SCHEDULER_REFRESH_INTERVAL", "12h")
    monkeypatch.setenv("ROVE_SCHEDULER_RETRY_ATTEMPTS", "5")
    monkeypatch.setenv("ROVE_LOGGING_LEVEL", "debug")
    monkeypatch.setenv("ROVE_LOGGING_CONSOLE_LEVEL", "info")
    
    loaded = load_config()
    assert loaded.scheduler.refresh_interval == "12h"
    assert loaded.scheduler.retry_attempts == 5
    assert loaded.logging.level == "debug"
    assert loaded.logging.console_level == "info"



