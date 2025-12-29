"""Plugin discovery and registration for Rove.

Plugins are auto-discovered by scanning the plugins directory. Each plugin
must export:
- PLUGIN_NAME: str - The name of the plugin (e.g., "jira")
- PLUGIN_VERSION: str - The version of the plugin
- create_client(config: dict) -> ContextClient - Factory function
"""

import importlib
from pathlib import Path
from typing import Callable

from .base import ContextClient

# Cache of discovered plugins
_plugins: dict[str, Callable[..., ContextClient]] | None = None


def discover_plugins() -> dict[str, Callable[..., ContextClient]]:
    """Discover all plugins in the plugins directory.

    Returns:
        A dictionary mapping plugin names to their create_client factory functions.
    """
    global _plugins

    if _plugins is not None:
        return _plugins

    _plugins = {}
    plugin_dir = Path(__file__).parent

    for item in plugin_dir.iterdir():
        if item.is_dir() and not item.name.startswith("_"):
            try:
                module = importlib.import_module(f".{item.name}", package="rove.plugins")
                if hasattr(module, "create_client") and hasattr(module, "PLUGIN_NAME"):
                    _plugins[module.PLUGIN_NAME] = module.create_client
            except ImportError:
                continue  # Skip invalid plugins

    return _plugins


def get_plugin(name: str) -> Callable[..., ContextClient] | None:
    """Get a specific plugin's factory function by name.

    Args:
        name: The plugin name (e.g., "jira", "slack", "github")

    Returns:
        The create_client factory function, or None if not found.
    """
    plugins = discover_plugins()
    return plugins.get(name.lower())


def list_plugins() -> list[str]:
    """List all available plugin names.

    Returns:
        A list of plugin names.
    """
    return list(discover_plugins().keys())


def get_plugin_info(name: str) -> dict[str, str] | None:
    """Get metadata about a plugin.

    Args:
        name: The plugin name

    Returns:
        A dictionary with plugin info, or None if not found.
    """
    plugin_dir = Path(__file__).parent / name.lower()
    if not plugin_dir.is_dir():
        return None

    try:
        module = importlib.import_module(f".{name.lower()}", package="rove.plugins")
        return {
            "name": getattr(module, "PLUGIN_NAME", name),
            "version": getattr(module, "PLUGIN_VERSION", "unknown"),
            "description": getattr(module, "PLUGIN_DESCRIPTION", ""),
        }
    except ImportError:
        return None


def reload_plugins() -> None:
    """Force reload of plugin cache."""
    global _plugins
    _plugins = None
    discover_plugins()



