"""SQLite database operations for Rove.

Stores metadata for context files and fetch history for deduplication.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .config import DATABASE_FILE, ensure_rove_home


def utc_now() -> datetime:
    """Get current time as timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_db_timestamp(timestamp_str: str) -> datetime:
    """Parse a timestamp string from the database to timezone-aware UTC datetime.

    Handles both naive (legacy) and aware timestamps.
    """
    if not timestamp_str:
        return utc_now()

    # Try parsing with timezone info first
    try:
        dt = datetime.fromisoformat(timestamp_str)
        if dt.tzinfo is None:
            # Assume naive timestamps are UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return utc_now()

# SQL schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS context_files (
    id INTEGER PRIMARY KEY,
    ticket_id TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    keywords TEXT NOT NULL,
    last_updated TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ticket_id ON context_files(ticket_id);
CREATE INDEX IF NOT EXISTS idx_filename ON context_files(filename);

CREATE TABLE IF NOT EXISTS fetch_history (
    context_file_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    last_fetched TIMESTAMP NOT NULL,
    PRIMARY KEY (context_file_id, source),
    FOREIGN KEY (context_file_id) REFERENCES context_files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_ticket ON tasks(ticket_id);
"""


@dataclass
class ContextFileRecord:
    """Represents a context file record in the database."""

    id: int
    ticket_id: str
    filename: str
    keywords: list[str]
    last_updated: datetime
    created_at: datetime


@dataclass
class FetchHistoryRecord:
    """Represents a fetch history record."""

    context_file_id: int
    source: str
    last_fetched: datetime


@dataclass
class TaskRecord:
    """Represents a task record."""

    id: int
    ticket_id: str
    task_type: str  # "build" or "refresh"
    status: str  # "pending", "in_progress", "completed", "failed"
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None


class Database:
    """Async database operations for Rove."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DATABASE_FILE
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Connect to the database and initialize schema."""
        ensure_rove_home()
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA)
        await self._connection.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Get the active connection, raising if not connected."""
        if self._connection is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._connection

    # Context file operations

    async def create_context_file(
        self, ticket_id: str, filename: str, keywords: list[str]
    ) -> int:
        """Create a new context file record. Returns the record ID."""
        now = utc_now().isoformat()
        cursor = await self.conn.execute(
            """
            INSERT INTO context_files (ticket_id, filename, keywords, last_updated)
            VALUES (?, ?, ?, ?)
            """,
            (ticket_id, filename, json.dumps(keywords), now),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def get_context_file(self, ticket_id: str) -> ContextFileRecord | None:
        """Get a context file record by ticket ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM context_files WHERE ticket_id = ?", (ticket_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return ContextFileRecord(
            id=row["id"],
            ticket_id=row["ticket_id"],
            filename=row["filename"],
            keywords=json.loads(row["keywords"]),
            last_updated=parse_db_timestamp(row["last_updated"]),
            created_at=parse_db_timestamp(row["created_at"]),
        )

    async def get_context_file_by_filename(self, filename: str) -> ContextFileRecord | None:
        """Get a context file record by filename."""
        cursor = await self.conn.execute(
            "SELECT * FROM context_files WHERE filename = ?", (filename,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return ContextFileRecord(
            id=row["id"],
            ticket_id=row["ticket_id"],
            filename=row["filename"],
            keywords=json.loads(row["keywords"]),
            last_updated=parse_db_timestamp(row["last_updated"]),
            created_at=parse_db_timestamp(row["created_at"]),
        )

    async def update_context_file(
        self, ticket_id: str, filename: str | None = None, keywords: list[str] | None = None
    ) -> bool:
        """Update a context file record. Returns True if updated."""
        record = await self.get_context_file(ticket_id)
        if not record:
            return False

        new_filename = filename if filename is not None else record.filename
        new_keywords = keywords if keywords is not None else record.keywords
        now = utc_now().isoformat()

        await self.conn.execute(
            """
            UPDATE context_files
            SET filename = ?, keywords = ?, last_updated = ?
            WHERE ticket_id = ?
            """,
            (new_filename, json.dumps(new_keywords), now, ticket_id),
        )
        await self.conn.commit()
        return True

    async def delete_context_file(self, ticket_id: str) -> bool:
        """Delete a context file record. Returns True if deleted."""
        cursor = await self.conn.execute(
            "DELETE FROM context_files WHERE ticket_id = ?", (ticket_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def search_context_files(self, query: str) -> list[ContextFileRecord]:
        """Search context files by keyword match."""
        # Simple keyword matching - search in keywords JSON and filename
        cursor = await self.conn.execute(
            """
            SELECT * FROM context_files
            WHERE keywords LIKE ? OR filename LIKE ? OR ticket_id LIKE ?
            ORDER BY last_updated DESC
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        )
        rows = await cursor.fetchall()
        return [
            ContextFileRecord(
                id=row["id"],
                ticket_id=row["ticket_id"],
                filename=row["filename"],
                keywords=json.loads(row["keywords"]),
                last_updated=parse_db_timestamp(row["last_updated"]),
                created_at=parse_db_timestamp(row["created_at"]),
            )
            for row in rows
        ]

    async def list_all_context_files(self) -> list[ContextFileRecord]:
        """List all context file records."""
        cursor = await self.conn.execute(
            "SELECT * FROM context_files ORDER BY last_updated DESC"
        )
        rows = await cursor.fetchall()
        return [
            ContextFileRecord(
                id=row["id"],
                ticket_id=row["ticket_id"],
                filename=row["filename"],
                keywords=json.loads(row["keywords"]),
                last_updated=parse_db_timestamp(row["last_updated"]),
                created_at=parse_db_timestamp(row["created_at"]),
            )
            for row in rows
        ]

    # Fetch history operations

    async def update_fetch_history(
        self, context_file_id: int, source: str, last_fetched: datetime | None = None
    ) -> None:
        """Update the fetch history for a source on a context file."""
        fetched_at = (last_fetched or utc_now()).isoformat()
        await self.conn.execute(
            """
            INSERT INTO fetch_history (context_file_id, source, last_fetched)
            VALUES (?, ?, ?)
            ON CONFLICT (context_file_id, source) DO UPDATE SET last_fetched = ?
            """,
            (context_file_id, source, fetched_at, fetched_at),
        )
        await self.conn.commit()

    async def get_fetch_history(
        self, context_file_id: int, source: str
    ) -> FetchHistoryRecord | None:
        """Get fetch history for a specific source on a context file."""
        cursor = await self.conn.execute(
            """
            SELECT * FROM fetch_history
            WHERE context_file_id = ? AND source = ?
            """,
            (context_file_id, source),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return FetchHistoryRecord(
            context_file_id=row["context_file_id"],
            source=row["source"],
            last_fetched=parse_db_timestamp(row["last_fetched"]),
        )

    async def get_all_fetch_history(self, context_file_id: int) -> list[FetchHistoryRecord]:
        """Get all fetch history for a context file."""
        cursor = await self.conn.execute(
            "SELECT * FROM fetch_history WHERE context_file_id = ?",
            (context_file_id,),
        )
        rows = await cursor.fetchall()
        return [
            FetchHistoryRecord(
                context_file_id=row["context_file_id"],
                source=row["source"],
                last_fetched=parse_db_timestamp(row["last_fetched"]),
            )
            for row in rows
        ]

    # Task operations

    async def create_task(self, ticket_id: str, task_type: str) -> int:
        """Create a new task. Returns the task ID."""
        cursor = await self.conn.execute(
            """
            INSERT INTO tasks (ticket_id, task_type, status)
            VALUES (?, ?, 'pending')
            """,
            (ticket_id, task_type),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def get_task(self, task_id: int) -> TaskRecord | None:
        """Get a task by ID."""
        cursor = await self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def update_task_status(
        self,
        task_id: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Update task status."""
        now = utc_now().isoformat()

        if status == "in_progress":
            await self.conn.execute(
                "UPDATE tasks SET status = ?, started_at = ? WHERE id = ?",
                (status, now, task_id),
            )
        elif status in ("completed", "failed"):
            await self.conn.execute(
                "UPDATE tasks SET status = ?, completed_at = ?, error_message = ? WHERE id = ?",
                (status, now, error_message, task_id),
            )
        else:
            await self.conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )
        await self.conn.commit()

    async def get_pending_tasks(self) -> list[TaskRecord]:
        """Get all pending tasks."""
        cursor = await self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def get_recent_tasks(self, limit: int = 20) -> list[TaskRecord]:
        """Get recent tasks of all statuses."""
        cursor = await self.conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    def _row_to_task(self, row: aiosqlite.Row) -> TaskRecord:
        """Convert a database row to a TaskRecord."""
        return TaskRecord(
            id=row["id"],
            ticket_id=row["ticket_id"],
            task_type=row["task_type"],
            status=row["status"],
            created_at=parse_db_timestamp(row["created_at"]),
            started_at=(
                parse_db_timestamp(row["started_at"]) if row["started_at"] else None
            ),
            completed_at=(
                parse_db_timestamp(row["completed_at"]) if row["completed_at"] else None
            ),
            error_message=row["error_message"],
        )


# Convenience function for context manager usage
async def get_database() -> Database:
    """Get a connected database instance."""
    db = Database()
    await db.connect()
    return db

