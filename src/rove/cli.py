"""Command-line interface for Rove.

Provides all CLI commands for managing sources, building context,
and interacting with the Rove service.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from . import __version__
from .config import (
    API_SOCKET,
    PID_FILE,
    SETTINGS_FILE,
    create_default_config,
    load_config,
    save_config,
)
from .database import Database, get_database
from .logging import configure_logging, get_logger


def _show_welcome_message() -> None:
    """Show welcome message for first-time users."""
    click.echo()
    click.secho("Welcome to Rove!", fg="green", bold=True)
    click.echo()
    click.echo("Rove helps coding agents understand your tickets by gathering")
    click.echo("context from JIRA, Slack, GitHub, and more.")
    click.echo()
    click.echo("Get started:")
    click.echo(f"  1. Edit your config: {SETTINGS_FILE}")
    click.echo("     - Add your AI API key (OpenAI, Ollama, or OpenRouter)")
    click.echo()
    click.echo("  2. Connect your sources:")
    click.echo("     rove --add-source jira")
    click.echo("     rove --add-source slack")
    click.echo("     rove --add-source github")
    click.echo()
    click.echo("  3. Build context for a ticket:")
    click.echo("     rove --ticket TB-123")
    click.echo()
    click.echo("For more information: rove --help")
    click.echo()


def parse_date(date_str: str) -> datetime:
    """Parse a date string into a datetime object.

    Supports:
    - ISO format: 2024-12-28
    - Relative: "30 days ago", "1 week ago"
    """
    # Try ISO format first
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        pass

    # Try relative format
    parts = date_str.lower().split()
    if len(parts) == 3 and parts[2] == "ago":
        try:
            value = int(parts[0])
            unit = parts[1].rstrip("s")  # Remove trailing 's'
            from datetime import timedelta

            if unit == "day":
                return datetime.now(UTC) - timedelta(days=value)
            elif unit == "week":
                return datetime.now(UTC) - timedelta(weeks=value)
            elif unit == "month":
                return datetime.now(UTC) - timedelta(days=value * 30)
            elif unit == "hour":
                return datetime.now(UTC) - timedelta(hours=value)
        except ValueError:
            pass

    raise click.BadParameter(f"Invalid date format: {date_str}")


@click.command()
@click.option("--ticket", "-t", help="Build context for a specific ticket (e.g., TB-123)")
@click.option("--source", "-s", help="Override default ticket source for this operation")
@click.option("--status", is_flag=True, help="Show status of all context building tasks")
@click.option("--source-plugins", is_flag=True, help="List all available source plugins")
@click.option("--add-source", metavar="NAME", help="Add and authenticate a new source")
@click.option("--remove-source", metavar="NAME", help="Remove a source connection")
@click.option("--list-sources", is_flag=True, help="List configured sources")
@click.option("--set-default-source", metavar="NAME", help="Set the default ticket source")
@click.option("--find", metavar="TICKET_ID", help="Find context file for a ticket")
@click.option("--search", "-q", metavar="QUERY", help="Search context files by keyword")
@click.option("--since", metavar="DATE", help="Only include items after this date")
@click.option("--until", metavar="DATE", help="Only include items before this date")
@click.option("--start-server", is_flag=True, help="Start the API server daemon")
@click.option("--stop-server", is_flag=True, help="Stop the API server daemon")
@click.option("--server-status", is_flag=True, help="Check if API server is running")
@click.option("--api-find", metavar="TICKET_ID", help="Find context file via API (for agents)")
@click.option("--api-search", metavar="QUERY", help="Search context files via API (for agents)")
@click.option("--version", is_flag=True, help="Show version information")
def main(
    ticket: str | None,
    source: str | None,
    status: bool,
    source_plugins: bool,
    add_source: str | None,
    remove_source: str | None,
    list_sources: bool,
    set_default_source: str | None,
    find: str | None,
    search: str | None,
    since: str | None,
    until: str | None,
    start_server: bool,
    stop_server: bool,
    server_status: bool,
    api_find: str | None,
    api_search: str | None,
    version: bool,
) -> None:
    """Rove - Context extraction for coding agents.

    Build comprehensive context documents from JIRA, Slack, GitHub and more.

    Examples:

        rove --ticket TB-123              Build context for ticket TB-123

        rove --add-source jira            Add JIRA as a context source

        rove --find TB-123                Find the context file for TB-123

        rove --search "oauth auth"        Search context files

        rove --status                     Show task status
    """
    # Initialize logging
    configure_logging()

    # Ensure config exists, show welcome on first run
    is_first_run = create_default_config()
    if is_first_run:
        _show_welcome_message()

    if version:
        click.echo(f"Rove version {__version__}")
        return

    if source_plugins:
        asyncio.run(cmd_list_plugins())
        return

    if add_source:
        asyncio.run(cmd_add_source(add_source))
        return

    if remove_source:
        asyncio.run(cmd_remove_source(remove_source))
        return

    if list_sources:
        asyncio.run(cmd_list_sources())
        return

    if set_default_source:
        cmd_set_default_source(set_default_source)
        return

    if status:
        asyncio.run(cmd_status())
        return

    if find:
        asyncio.run(cmd_find(find))
        return

    if search:
        asyncio.run(cmd_search(search))
        return

    if ticket:
        since_dt = parse_date(since) if since else None
        until_dt = parse_date(until) if until else None
        asyncio.run(cmd_build_context(ticket, source, since_dt, until_dt))
        return

    if start_server:
        asyncio.run(cmd_start_server())
        return

    if stop_server:
        cmd_stop_server()
        return

    if server_status:
        cmd_server_status()
        return

    if api_find:
        asyncio.run(cmd_api_find(api_find))
        return

    if api_search:
        asyncio.run(cmd_api_search(api_search))
        return

    # No command specified, show help
    ctx = click.get_current_context()
    click.echo(ctx.get_help())


async def cmd_list_plugins() -> None:
    """List all available source plugins."""
    from .plugins import get_plugin_info, list_plugins

    plugins = list_plugins()

    if not plugins:
        click.echo("No plugins found.")
        return

    click.echo("\nAvailable source plugins:\n")
    for name in sorted(plugins):
        info = get_plugin_info(name)
        if info:
            click.echo(f"  {info['name']:<12} v{info['version']:<8} {info['description']}")
        else:
            click.echo(f"  {name}")
    click.echo()


async def cmd_add_source(name: str) -> None:
    """Add and authenticate a new source."""
    from .plugins import get_plugin

    factory = get_plugin(name)
    if not factory:
        click.echo(f"Unknown source plugin: {name}")
        click.echo("Run 'rove --source-plugins' to see available plugins.")
        sys.exit(1)

    click.echo(f"\nAdding {name.upper()} as a context source...")

    # Load config from settings.toml
    config = load_config()
    source_config: dict = {}
    if hasattr(config.sources, name):
        src_cfg = getattr(config.sources, name)
        source_config = {
            "rate_limit": src_cfg.rate_limit,
            "page_size": src_cfg.page_size,
        }
        # Include OAuth credentials if configured
        if src_cfg.client_id:
            source_config["client_id"] = src_cfg.client_id
        if src_cfg.client_secret:
            source_config["client_secret"] = src_cfg.client_secret

    client = factory(source_config)

    # Perform authentication
    try:
        success = await client.authenticate()
    except Exception as e:
        click.echo(f"Authentication failed: {e}")
        sys.exit(1)

    if not success:
        click.echo("Authentication failed.")
        sys.exit(1)

    # Test connection
    click.echo("Testing connection...")
    if await client.test_connection():
        click.echo(f"✓ Successfully connected to {client.source_name()}")
    else:
        click.echo("✗ Connection test failed. Credentials may be invalid.")
        sys.exit(1)


async def cmd_remove_source(name: str) -> None:
    """Remove a source connection."""
    from .plugins import get_plugin

    factory = get_plugin(name)
    if not factory:
        click.echo(f"Unknown source plugin: {name}")
        sys.exit(1)

    client = factory({})
    await client.disconnect()
    click.echo(f"✓ Removed {name.upper()} connection")


async def cmd_list_sources() -> None:
    """List configured sources and their status."""
    from .plugins import get_plugin, list_plugins

    config = load_config()
    plugins = list_plugins()

    click.echo("\nConfigured sources:\n")

    for name in sorted(plugins):
        factory = get_plugin(name)
        if factory:
            client = factory({})
            is_auth = client.is_authenticated()
            status_icon = "✓" if is_auth else "✗"
            default_marker = " (default)" if name == config.sources.default_ticket_source else ""
            click.echo(f"  {status_icon} {name:<12}{default_marker}")

    click.echo()


def cmd_set_default_source(name: str) -> None:
    """Set the default ticket source."""
    from .plugins import get_plugin

    if not get_plugin(name):
        click.echo(f"Unknown source plugin: {name}")
        sys.exit(1)

    config = load_config()
    config.sources.default_ticket_source = name.lower()
    save_config(config)
    click.echo(f"✓ Default ticket source set to {name}")


async def cmd_status() -> None:
    """Show status of all tasks."""
    db = await get_database()
    try:
        tasks = await db.get_recent_tasks(20)

        if not tasks:
            click.echo("\nNo tasks found.\n")
            return

        click.echo("\nRecent tasks:\n")
        click.echo(f"  {'ID':<6} {'Ticket':<12} {'Type':<8} {'Status':<12} {'Created'}")
        click.echo("  " + "-" * 60)

        for task in tasks:
            created = task.created_at.strftime("%Y-%m-%d %H:%M")
            click.echo(
                f"  {task.id:<6} {task.ticket_id:<12} {task.task_type:<8} "
                f"{task.status:<12} {created}"
            )

        click.echo()
    finally:
        await db.close()


async def cmd_find(ticket_id: str) -> None:
    """Find context file for a ticket."""
    db = await get_database()
    try:
        record = await db.get_context_file(ticket_id.upper())
        if record:
            click.echo(record.filename)
        else:
            click.echo(f"No context file found for {ticket_id}", err=True)
            sys.exit(1)
    finally:
        await db.close()


async def cmd_search(query: str) -> None:
    """Search context files by keyword."""
    db = await get_database()
    try:
        results = await db.search_context_files(query)

        if not results:
            click.echo("No matching context files found.", err=True)
            sys.exit(1)

        for record in results:
            click.echo(record.filename)
    finally:
        await db.close()


async def cmd_build_context(
    ticket_id: str,
    source_override: str | None,
    since: datetime | None,
    until: datetime | None,
) -> None:
    """Build context for a ticket."""
    from .context_builder import ContextBuilder
    from .search_agent import SearchAgent

    config = load_config()
    source = source_override or config.sources.default_ticket_source

    click.echo(f"\nBuilding context for {ticket_id} from {source}...")

    db = await get_database()
    try:
        # Create task
        task_id = await db.create_task(ticket_id, "build")
        await db.update_task_status(task_id, "in_progress")

        try:
            # Initialize search agent and context builder
            search_agent = SearchAgent(config)
            context_builder = ContextBuilder(db)

            # Search for context
            click.echo("Searching for context...")
            items = await search_agent.search(
                ticket_id=ticket_id,
                source_override=source,
                since=since,
                until=until,
            )

            if not items:
                click.echo("No context found for this ticket.")
                await db.update_task_status(task_id, "completed")
                return

            click.echo(f"Found {len(items)} context items.")

            # Build context document
            click.echo("Building context document...")
            filename = await context_builder.build(ticket_id, items)

            await db.update_task_status(task_id, "completed")
            click.echo(f"\n✓ Context saved to: .context/{filename}")

        except Exception as e:
            await db.update_task_status(task_id, "failed", str(e))
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
    finally:
        await db.close()


async def cmd_start_server() -> None:
    """Start the API server daemon."""
    from .api.server import run_server

    if PID_FILE.exists():
        click.echo("Server already running. Use --stop-server first.")
        sys.exit(1)

    click.echo(f"Starting API server on {API_SOCKET}...")
    await run_server()


def cmd_stop_server() -> None:
    """Stop the API server daemon."""
    import os
    import signal

    if not PID_FILE.exists():
        click.echo("Server is not running.")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink()
        click.echo("Server stopped.")
    except (ProcessLookupError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        click.echo("Server was not running (stale PID file removed).")


def cmd_server_status() -> None:
    """Check if API server is running."""
    import os
    import signal

    if not PID_FILE.exists():
        click.echo("Server is not running.")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process exists (signal 0 doesn't kill, just checks)
        os.kill(pid, 0)
        click.echo(f"Server is running (PID: {pid})")
        click.echo(f"Socket: {API_SOCKET}")
    except (ProcessLookupError, ValueError):
        click.echo("Server is not running (stale PID file).")


async def cmd_api_find(ticket_id: str) -> None:
    """Find context file via API (for agent use).

    Calls the Unix socket API server to find a context file.
    Falls back to direct database access if server is not running.
    """
    import aiohttp

    if not API_SOCKET.exists():
        # Server not running, fall back to direct DB access
        await cmd_find(ticket_id)
        return

    try:
        connector = aiohttp.UnixConnector(path=str(API_SOCKET))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"http://localhost/find/{ticket_id.upper()}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    click.echo(data["filename"])
                elif response.status == 404:
                    data = await response.json()
                    click.echo(data.get("error", f"Not found: {ticket_id}"), err=True)
                    sys.exit(1)
                else:
                    click.echo(f"API error: {response.status}", err=True)
                    sys.exit(1)
    except aiohttp.ClientError as e:
        # Connection failed, fall back to direct DB access
        await cmd_find(ticket_id)


async def cmd_api_search(query: str) -> None:
    """Search context files via API (for agent use).

    Calls the Unix socket API server to search context files.
    Falls back to direct database access if server is not running.
    """
    import aiohttp

    if not API_SOCKET.exists():
        # Server not running, fall back to direct DB access
        await cmd_search(query)
        return

    try:
        connector = aiohttp.UnixConnector(path=str(API_SOCKET))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"http://localhost/search", params={"q": query}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("results", [])
                    if not results:
                        click.echo("No matching context files found.", err=True)
                        sys.exit(1)
                    for result in results:
                        click.echo(result["filename"])
                else:
                    click.echo(f"API error: {response.status}", err=True)
                    sys.exit(1)
    except aiohttp.ClientError as e:
        # Connection failed, fall back to direct DB access
        await cmd_search(query)


if __name__ == "__main__":
    main()

