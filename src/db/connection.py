"""
SQLite connection helper shared by the KG (Phase 4) and, later, chat/usage
persistence (Phase 6).

Each operation opens a short-lived connection. This is cheap for SQLite and
keeps us thread-safe: FastMCP executes synchronous tools in worker threads,
and ``sqlite3`` connections must not be shared across threads.

NOTE (Phase 6): FastAPI handlers should call store methods through
``asyncio.to_thread`` (or we migrate to ``aiosqlite``) to avoid blocking
the event loop.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success and rolls back on error."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

