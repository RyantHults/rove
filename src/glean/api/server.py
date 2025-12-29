"""Unix socket API server for Glean.

Provides REST API endpoints for agent integration.
"""

import asyncio
import os
import signal
import sys

from aiohttp import web

from .. import __version__
from ..config import API_SOCKET, PID_FILE, ensure_glean_home
from ..database import Database
from ..logging import get_logger
from ..scheduler import GleanScheduler

logger = get_logger("server")


class GleanAPIServer:
    """Unix socket API server for agent integration."""

    def __init__(self, scheduler: "GleanScheduler | None" = None):
        """Initialize the API server.

        Args:
            scheduler: Optional scheduler instance for health reporting.
        """
        self.db: Database | None = None
        self.scheduler = scheduler
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Set up API routes."""
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/find/{ticket_id}", self.handle_find)
        self.app.router.add_get("/search", self.handle_search)

    async def start(self) -> None:
        """Start the database connection."""
        self.db = Database()
        await self.db.connect()

    async def stop(self) -> None:
        """Stop the database connection."""
        if self.db:
            await self.db.close()

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint.

        GET /health
        Returns: {"status": "ok", "version": "x.x.x", "scheduler": "running|stopped"}
        """
        scheduler_status = "running" if self.scheduler and self.scheduler._running else "stopped"
        return web.json_response({
            "status": "ok",
            "version": __version__,
            "scheduler": scheduler_status,
        })

    async def handle_find(self, request: web.Request) -> web.Response:
        """Find context file for a ticket.

        GET /find/{ticket_id}
        Returns: {"ticket_id": "...", "filename": "...", ...}
        """
        ticket_id = request.match_info["ticket_id"].upper()

        if not self.db:
            return web.json_response(
                {"error": "Database not connected"},
                status=500,
            )

        record = await self.db.get_context_file(ticket_id)

        if not record:
            return web.json_response(
                {"error": f"No context file found for {ticket_id}"},
                status=404,
            )

        return web.json_response({
            "ticket_id": record.ticket_id,
            "filename": record.filename,
            "keywords": record.keywords,
            "last_updated": record.last_updated.isoformat(),
        })

    async def handle_search(self, request: web.Request) -> web.Response:
        """Search context files by keyword.

        GET /search?q=query
        Returns: {"query": "...", "results": [...]}
        """
        query = request.query.get("q", "")

        if not query:
            return web.json_response(
                {"error": "Missing query parameter 'q'"},
                status=400,
            )

        if not self.db:
            return web.json_response(
                {"error": "Database not connected"},
                status=500,
            )

        records = await self.db.search_context_files(query)

        # Calculate simple relevance scores
        results = []
        query_lower = query.lower()

        for record in records:
            # Simple scoring based on keyword matches
            score = 0.0
            for keyword in record.keywords:
                if keyword.lower() in query_lower or query_lower in keyword.lower():
                    score += 0.3

            if query_lower in record.ticket_id.lower():
                score += 0.5

            if query_lower in record.filename.lower():
                score += 0.2

            score = min(score, 1.0)  # Cap at 1.0

            results.append({
                "ticket_id": record.ticket_id,
                "filename": record.filename,
                "keywords": record.keywords,
                "score": round(score, 2),
            })

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)

        return web.json_response({
            "query": query,
            "results": results,
        })


async def run_server(with_scheduler: bool = True) -> None:
    """Run the API server and optionally the background scheduler.

    Args:
        with_scheduler: If True, also start the background refresh scheduler.
    """
    ensure_glean_home()

    # Remove stale socket
    if API_SOCKET.exists():
        API_SOCKET.unlink()

    # Create and start scheduler if requested
    scheduler: GleanScheduler | None = None
    if with_scheduler:
        scheduler = GleanScheduler()
        await scheduler.start()
        logger.info("Background scheduler started")

    # Create server (pass scheduler for health reporting)
    server = GleanAPIServer(scheduler=scheduler)
    await server.start()

    # Track shutdown state
    shutdown_event = asyncio.Event()

    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    # Create Unix socket runner
    runner = web.AppRunner(server.app)
    await runner.setup()

    # Create Unix socket site
    site = web.UnixSite(runner, str(API_SOCKET))
    await site.start()

    # Set socket permissions (owner only)
    API_SOCKET.chmod(0o600)

    print(f"Glean server running on {API_SOCKET}")
    if with_scheduler:
        print("Background scheduler active")
    print("Press Ctrl+C to stop...")

    # Keep running until shutdown
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        print("\nShutting down...")

        # Stop scheduler first
        if scheduler:
            await scheduler.stop()
            logger.info("Scheduler stopped")

        # Stop API server
        await server.stop()
        await runner.cleanup()

        # Cleanup files
        PID_FILE.unlink(missing_ok=True)
        API_SOCKET.unlink(missing_ok=True)

        logger.info("Server stopped cleanly")

