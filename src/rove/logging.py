"""Logging configuration for Rove.

Provides structured logging with separate handlers for:
- Error/debug logs: ./.rove/logs/rove.log (in current working directory)
- Performance logs: ./.rove/logs/performance.log (in current working directory)
"""

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import ROVE_HOME

# Log directory
LOGS_DIR = ROVE_HOME / "logs"

# Log files
ERROR_LOG = LOGS_DIR / "rove.log"
PERFORMANCE_LOG = LOGS_DIR / "performance.log"

# Loggers
_main_logger: logging.Logger | None = None
_perf_logger: logging.Logger | None = None


def ensure_logs_dir() -> None:
    """Create the logs directory if it doesn't exist."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def get_logger(name: str = "rove") -> logging.Logger:
    """Get or create the main application logger.

    Args:
        name: Logger name (usually module name).

    Returns:
        Configured logger instance.
    """
    global _main_logger

    if _main_logger is None:
        _main_logger = _setup_main_logger()

    return logging.getLogger(f"rove.{name}" if name != "rove" else "rove")


def get_performance_logger() -> logging.Logger:
    """Get or create the performance logger.

    Performance logs are one-line structured entries for metrics.

    Returns:
        Configured performance logger.
    """
    global _perf_logger

    if _perf_logger is None:
        _perf_logger = _setup_performance_logger()

    return _perf_logger


def _parse_log_level(level_str: str) -> int:
    """Parse a log level string to logging constant.

    Args:
        level_str: Log level name (debug, info, warning, error).

    Returns:
        Corresponding logging level constant.
    """
    levels = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    return levels.get(level_str.lower(), logging.INFO)


def _setup_main_logger() -> logging.Logger:
    """Set up the main application logger."""
    ensure_logs_dir()

    # Import here to avoid circular imports
    from .config import load_config

    config = load_config()
    file_level = _parse_log_level(config.logging.level)
    console_level = _parse_log_level(config.logging.console_level)

    logger = logging.getLogger("rove")
    logger.setLevel(logging.DEBUG)  # Let handlers filter

    # Prevent duplicate handlers
    if logger.handlers:
        return logger

    # File handler - rotating log files (5MB max, keep 3 backups)
    file_handler = RotatingFileHandler(
        ERROR_LOG,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(console_level)
    console_format = logging.Formatter("%(levelname)s: %(message)s")
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    return logger


def _setup_performance_logger() -> logging.Logger:
    """Set up the performance logger."""
    ensure_logs_dir()

    logger = logging.getLogger("rove.performance")
    logger.setLevel(logging.INFO)

    # Prevent duplicate handlers
    if logger.handlers:
        return logger

    # Performance log file - structured one-line entries
    file_handler = RotatingFileHandler(
        PERFORMANCE_LOG,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    # Simple format for easy parsing
    file_format = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    # Don't propagate to parent logger
    logger.propagate = False

    return logger


def log_performance(
    operation: str,
    duration_ms: float,
    **metrics: Any,
) -> None:
    """Log a performance metric.

    Args:
        operation: Name of the operation (e.g., "context_build", "search").
        duration_ms: Duration in milliseconds.
        **metrics: Additional key-value metrics to include.
    """
    logger = get_performance_logger()

    # Build metrics string
    parts = [f"op={operation}", f"duration_ms={duration_ms:.2f}"]
    for key, value in metrics.items():
        parts.append(f"{key}={value}")

    logger.info(" | ".join(parts))


class PerformanceTimer:
    """Context manager for timing operations.

    Usage:
        with PerformanceTimer("context_build", ticket_id="TB-123") as timer:
            # ... do work ...
            timer.add_metric("items_found", 15)
    """

    def __init__(self, operation: str, **initial_metrics: Any):
        """Initialize the timer.

        Args:
            operation: Name of the operation.
            **initial_metrics: Initial metrics to include.
        """
        self.operation = operation
        self.metrics = initial_metrics
        self._start_time: datetime | None = None

    def __enter__(self) -> "PerformanceTimer":
        """Start the timer."""
        self._start_time = datetime.now(UTC)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop the timer and log the metrics."""
        if self._start_time is None:
            return

        duration = (datetime.now(UTC) - self._start_time).total_seconds() * 1000

        if exc_type is not None:
            self.metrics["error"] = exc_type.__name__

        log_performance(self.operation, duration, **self.metrics)

    def add_metric(self, key: str, value: Any) -> None:
        """Add a metric to be logged.

        Args:
            key: Metric name.
            value: Metric value.
        """
        self.metrics[key] = value


def configure_logging(verbose: bool = False) -> None:
    """Configure logging for the application.

    Args:
        verbose: If True, show debug output on console.
    """
    logger = get_logger()

    if verbose:
        # Set console handler to DEBUG level
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stderr:
                handler.setLevel(logging.DEBUG)



