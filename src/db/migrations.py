"""
Database initialisation, schema upgrades, and base-ontology seeding.

``init_db`` is idempotent: it can be called on every startup (and by every
``KGStore`` instance) without duplicating seed rows, and transparently
upgrades v1 databases to v2 (adding ``scope`` and ``project_id`` to ``triplets``).
"""

from pathlib import Path

from config.settings import get_logger
from src.db.connection import connect
from src.kg.schema import (
    BASE_CLASSES,
    BASE_RELATIONS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    USER_CLASS_NAME,
    USER_ENTITY_NAME,
)

logger = get_logger(__name__)


def _upgrade_v1_to_v2_if_needed(conn) -> None:
    """If an older triplets table exists without scope/project_id, add those columns."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='triplets'"
    ).fetchone()
    if not table_exists:
        return

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(triplets)").fetchall()}
    if "scope" not in cols:
        logger.info("Upgrading triplets table: adding 'scope' column")
        conn.execute(
            "ALTER TABLE triplets ADD COLUMN scope TEXT NOT NULL DEFAULT 'global' "
            "CHECK (scope IN ('global', 'project'))"
        )
    if "project_id" not in cols:
        logger.info("Upgrading triplets table: adding 'project_id' column")
        conn.execute("ALTER TABLE triplets ADD COLUMN project_id TEXT")


def init_db(db_path: str | Path) -> None:
    """Create all tables (if missing), apply column migrations, and seed the base ontology."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with connect(db_path) as conn:
        # WAL lets multiple processes read while an MCP tool is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        _upgrade_v1_to_v2_if_needed(conn)
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

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    logger.debug("Database initialized at %s (schema v%d)", db_path, SCHEMA_VERSION)
