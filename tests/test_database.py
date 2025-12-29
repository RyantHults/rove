"""Tests for database module."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from rove.database import Database


@pytest.fixture
async def db(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_create_context_file(db):
    """Test creating a context file record."""
    record_id = await db.create_context_file(
        ticket_id="TB-123",
        filename="TB-123_oauth_auth.md",
        keywords=["oauth", "authentication"],
    )
    
    assert record_id > 0
    
    record = await db.get_context_file("TB-123")
    assert record is not None
    assert record.ticket_id == "TB-123"
    assert record.filename == "TB-123_oauth_auth.md"
    assert record.keywords == ["oauth", "authentication"]


@pytest.mark.asyncio
async def test_update_context_file(db):
    """Test updating a context file record."""
    await db.create_context_file(
        ticket_id="TB-123",
        filename="TB-123_old.md",
        keywords=["old"],
    )
    
    success = await db.update_context_file(
        ticket_id="TB-123",
        filename="TB-123_new.md",
        keywords=["new", "updated"],
    )
    
    assert success
    
    record = await db.get_context_file("TB-123")
    assert record.filename == "TB-123_new.md"
    assert record.keywords == ["new", "updated"]


@pytest.mark.asyncio
async def test_search_context_files(db):
    """Test searching context files."""
    await db.create_context_file("TB-123", "TB-123_oauth.md", ["oauth", "auth"])
    await db.create_context_file("TB-456", "TB-456_payment.md", ["payment", "stripe"])
    
    results = await db.search_context_files("oauth")
    assert len(results) == 1
    assert results[0].ticket_id == "TB-123"
    
    results = await db.search_context_files("TB")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_task_operations(db):
    """Test task CRUD operations."""
    task_id = await db.create_task("TB-123", "build")
    
    task = await db.get_task(task_id)
    assert task.status == "pending"
    assert task.ticket_id == "TB-123"
    
    await db.update_task_status(task_id, "in_progress")
    task = await db.get_task(task_id)
    assert task.status == "in_progress"
    assert task.started_at is not None
    
    await db.update_task_status(task_id, "completed")
    task = await db.get_task(task_id)
    assert task.status == "completed"
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_fetch_history(db):
    """Test fetch history operations."""
    # Create a context file first
    record_id = await db.create_context_file("TB-123", "TB-123.md", [])
    
    # Update fetch history
    await db.update_fetch_history(record_id, "jira")
    await db.update_fetch_history(record_id, "slack")
    
    # Get history
    history = await db.get_all_fetch_history(record_id)
    assert len(history) == 2
    
    sources = {h.source for h in history}
    assert sources == {"jira", "slack"}



