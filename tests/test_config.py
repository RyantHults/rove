"""Tests for configuration module."""

import tempfile
from pathlib import Path

import pytest

from glean.config import (
    GleanConfig,
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
    config = GleanConfig()
    assert config.sources.default_ticket_source == "jira"
    assert config.scheduler.refresh_interval == "6h"
    assert config.ai.max_hops == 3


def test_config_round_trip(tmp_path, monkeypatch):
    """Test saving and loading configuration."""
    # Use temp directory for settings
    settings_file = tmp_path / "settings.toml"
    
    monkeypatch.setattr("glean.config.SETTINGS_FILE", settings_file)
    monkeypatch.setattr("glean.config.GLEAN_HOME", tmp_path)
    
    # Create and save config
    config = GleanConfig()
    config.sources.default_ticket_source = "github"
    config.ai.model = "gpt-4"
    
    save_config(config)
    
    # Load and verify
    loaded = load_config()
    assert loaded.sources.default_ticket_source == "github"
    assert loaded.ai.model == "gpt-4"



