"""Background task scheduling for Rove.

Handles automatic context refresh and task queue management.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from ..config import DATABASE_FILE, RoveConfig, load_config, parse_duration
from ..context_builder import ContextBuilder
from ..database import Database, utc_now
from ..logging import PerformanceTimer, get_logger
from ..plugins.base import AuthenticationError
from ..search_agent import SearchAgent

logger = get_logger("scheduler")


class RoveScheduler:
    """Background scheduler for context refresh tasks."""

    def __init__(self, config: RoveConfig | None = None):
        """Initialize the scheduler.

        Args:
            config: Optional configuration. Loads default if not provided.
        """
        self.config = config or load_config()
        self._scheduler: AsyncIOScheduler | None = None
        self._db: Database | None = None
        self._running = False

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        # Connect to database
        self._db = Database()
        await self._db.connect()

        # Create scheduler with SQLite job store
        jobstores = {
            "default": SQLAlchemyJobStore(url=f"sqlite:///{DATABASE_FILE}")
        }

        self._scheduler = AsyncIOScheduler(jobstores=jobstores)

        # Schedule the refresh job
        refresh_interval = parse_duration(self.config.scheduler.refresh_interval)
        self._scheduler.add_job(
            self._refresh_stale_contexts,
            "interval",
            seconds=refresh_interval,
            id="refresh_contexts",
            replace_existing=True,
        )

        # Schedule the task processor
        self._scheduler.add_job(
            self._process_pending_tasks,
            "interval",
            seconds=60,  # Check every minute
            id="process_tasks",
            replace_existing=True,
        )

        self._scheduler.start()
        self._running = True
        logger.info(f"Scheduler started. Refresh interval: {self.config.scheduler.refresh_interval}")

    async def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown()
            self._scheduler = None

        if self._db:
            await self._db.close()
            self._db = None

        self._running = False

    async def _refresh_stale_contexts(self) -> None:
        """Refresh context files that are stale."""
        if not self._db:
            return

        staleness_seconds = parse_duration(self.config.scheduler.staleness_threshold)
        now = utc_now()
        staleness_threshold = now - timedelta(seconds=staleness_seconds)

        # Get all context files
        all_files = await self._db.list_all_context_files()

        for record in all_files:
            # Skip if too old (past staleness threshold)
            if record.created_at < staleness_threshold:
                continue

            # Check if needs refresh
            refresh_interval = parse_duration(self.config.scheduler.refresh_interval)
            needs_refresh = (now - record.last_updated).total_seconds() > refresh_interval

            if needs_refresh:
                # Create refresh task
                await self._db.create_task(record.ticket_id, "refresh")

    async def _process_pending_tasks(self) -> None:
        """Process pending tasks from the queue."""
        if not self._db:
            return

        pending_tasks = await self._db.get_pending_tasks()

        for task in pending_tasks:
            logger.debug(f"Processing task {task.id}: {task.task_type} for {task.ticket_id}")

            try:
                await self._db.update_task_status(task.id, "in_progress")

                with PerformanceTimer(
                    f"task_{task.task_type}",
                    task_id=task.id,
                    ticket_id=task.ticket_id,
                ):
                    if task.task_type == "build":
                        await self._build_context(task.ticket_id)
                    elif task.task_type == "refresh":
                        await self._refresh_context(task.ticket_id)

                await self._db.update_task_status(task.id, "completed")
                logger.info(f"Task {task.id} completed: {task.task_type} for {task.ticket_id}")

            except AuthenticationError as e:
                # Auth failures are expected when tokens expire - don't retry
                await self._db.update_task_status(
                    task.id, "failed", f"Authentication required: {e}"
                )
                logger.warning(
                    f"Task {task.id} needs re-authentication: {task.ticket_id}. "
                    "Run 'rove --add-source <source>' to re-authenticate."
                )

            except Exception as e:
                await self._db.update_task_status(task.id, "failed", str(e))
                logger.error(
                    f"Task {task.id} failed: {task.task_type} for {task.ticket_id}",
                    exc_info=True,
                )

    async def _build_context(self, ticket_id: str) -> None:
        """Build context for a ticket.

        Args:
            ticket_id: The ticket ID to build context for.
        """
        if not self._db:
            return

        # Normalize ticket ID to uppercase for consistency
        ticket_id = ticket_id.upper()

        search_agent = SearchAgent(self.config)
        context_builder = ContextBuilder(self._db, self.config)

        # Search for context
        items = await search_agent.search(ticket_id)

        if items:
            # Build context document
            await context_builder.build(ticket_id, items)

    async def _refresh_context(self, ticket_id: str) -> None:
        """Refresh context for an existing ticket.

        Args:
            ticket_id: The ticket ID to refresh.
        """
        if not self._db:
            return

        # Normalize ticket ID to uppercase for consistency
        ticket_id = ticket_id.upper()

        # Get existing record
        record = await self._db.get_context_file(ticket_id)
        if not record:
            # No existing context, do a full build instead
            await self._build_context(ticket_id)
            return

        # Get fetch history for incremental updates
        fetch_history = await self._db.get_all_fetch_history(record.id)

        # Determine the "since" time for each source
        since_times: dict[str, datetime] = {}
        for history in fetch_history:
            since_times[history.source] = history.last_fetched

        # Search for new items since last fetch
        search_agent = SearchAgent(self.config)
        context_builder = ContextBuilder(self._db, self.config)

        # Get the oldest fetch time as our "since" for the search
        if since_times:
            since = min(since_times.values())
        else:
            # If no history, get items from the last refresh interval
            refresh_interval = parse_duration(self.config.scheduler.refresh_interval)
            since = utc_now() - timedelta(seconds=refresh_interval)

        items = await search_agent.search(ticket_id, since=since)

        if items:
            # Rebuild context with all items
            await context_builder.build(ticket_id, items)


async def run_scheduler() -> None:
    """Run the scheduler as a standalone process."""
    scheduler = RoveScheduler()
    await scheduler.start()

    try:
        # Keep running until interrupted
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await scheduler.stop()

