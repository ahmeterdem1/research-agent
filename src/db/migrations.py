"""
Database initialisation and base-ontology seeding.

``init_db`` is idempotent: it can be called on every startup (and by every
``KGStore`` instance) without duplicating seed rows.
"""

from pathlib import Path

from src.db.connection import connect
from src.kg.schema import (
    BASE_CLASSES,
    BASE_RELATIONS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    USER_CLASS_NAME,
    USER_ENTITY_NAME,
)


def init_db(db_path: str | Path) -> None:
    """Create all tables (if missing) and seed the base ontology."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with connect(db_path) as conn:
        # WAL lets the API read while an MCP tool is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)

        conn.executemany(
            "INSERT OR IGNORE INTO ontology_classes (name, description, created_by) "
            "VALUES (?, ?, 'system')",
            BASE_CLASSES,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO ontology_relations (name, domain, range, description, created_by) "
            "VALUES (?, ?, ?, ?, 'system')",
            BASE_RELATIONS,
        )
        conn.execute(
            "INSERT OR IGNORE INTO entities (name, class, properties) VALUES (?, ?, '{}')",
            (USER_ENTITY_NAME, USER_CLASS_NAME),
        )

        # NOTE (Phase 6): chat_sessions, chat_messages and usage tables will be
        # created here as well, bumping SCHEMA_VERSION.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

