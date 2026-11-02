"""
Temporal Knowledge Graph store (synchronous SQLite) with Global vs. Project scope separation.

Temporal & Scope semantics
--------------------------
* Every triplet has ``scope`` in ``('global', 'project')`` and an optional
  ``project_id`` (NULL for global facts; the workspace identifier for project facts).
* A triplet is **live** when ``is_active = 1`` AND it has not passed its
  ``expires_at``. Every read path filters on liveness, so an expired fact is
  invisible immediately — even before the reaper runs.
* Queries default to ``scope='all'``, which returns all live ``global`` facts
  PLUS the live ``project`` facts belonging to the active ``project_id`` —
  automatically isolating unrelated projects' local facts.
* Re-asserting an existing live fact within the same ``(scope, project_id)``
  refreshes it (confidence, context, ``updated_at``, TTL) instead of inserting
  a duplicate.
* Retraction is a soft delete with a reason, never a hard delete.
"""

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from config.settings import get_active_project_id, get_logger
from src.db.connection import connect
from src.db.migrations import init_db
from src.kg.models import Entity, OntologyClass, OntologyRelation, Triplet
from src.kg.schema import VALID_SCOPES

logger = get_logger(__name__)

TIME_FMT = "%Y-%m-%d %H:%M:%S"

# Sentinel used to distinguish "leave unchanged" from an explicit None.
_UNSET: Any = object()

_TRIPLET_SELECT = """
SELECT t.*, s.name AS subject, s.class AS subject_class,
       o.name AS object,  o.class AS object_class
FROM triplets t
JOIN entities s ON s.id = t.subject_id
JOIN entities o ON o.id = t.object_id
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KGError(ValueError):
    """Raised for invalid KG operations (unknown ontology terms, domain/scope violations, ...)."""


class KGStore:
    def __init__(
        self,
        db_path: str | Path,
        now_fn: Callable[[], datetime] = _utcnow,
        default_project_id: Optional[str] = None,
    ):
        self.db_path = Path(db_path)
        self._now_fn = now_fn
        self.default_project_id = default_project_id
        init_db(self.db_path)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def now(self) -> str:
        return self._now_fn().astimezone(timezone.utc).strftime(TIME_FMT)

    def resolve_project_id(self, project_id: Optional[str] = None) -> str:
        pid = (project_id or self.default_project_id or get_active_project_id()).strip()
        if not pid:
            raise KGError("Could not determine an active project_id for project-scoped KG operation.")
        return pid

    def _resolve_write_scope(self, scope: str, project_id: Optional[str] = None) -> tuple[str, Optional[str]]:
        s = (scope or "global").strip().lower()
        if s not in VALID_SCOPES:
            raise KGError(f"Invalid scope '{scope}'. Must be 'global' or 'project'.")
        if s == "global":
            return "global", None
        return "project", self.resolve_project_id(project_id)

    def _scope_filter_clause(
        self, scope: str = "all", project_id: Optional[str] = None, alias: str = "t"
    ) -> tuple[str, list[Any]]:
        s = (scope or "all").strip().lower()
        if s not in ("all", "global", "project"):
            raise KGError(f"Invalid query scope '{scope}'. Must be 'all', 'global', or 'project'.")
        if s == "global":
            return f"{alias}.scope = 'global'", []

        pid = self.resolve_project_id(project_id)
        if s == "project":
            if pid == "*":
                return f"{alias}.scope = 'project'", []
            return f"({alias}.scope = 'project' AND {alias}.project_id = ?)", [pid]

        # s == "all"
        if pid == "*":
            return "1 = 1", []
        return f"({alias}.scope = 'global' OR ({alias}.scope = 'project' AND {alias}.project_id = ?))", [pid]

    def _expiry(self, ttl_seconds: Optional[int]) -> Optional[str]:
        if ttl_seconds is None:
            return None
        if ttl_seconds <= 0:
            raise KGError("ttl_seconds must be a positive integer (omit it for a permanent fact).")
        dt = self._now_fn().astimezone(timezone.utc) + timedelta(seconds=ttl_seconds)
        return dt.strftime(TIME_FMT)

    @staticmethod
    def _row_to_triplet(row) -> Triplet:
        keys = row.keys()
        return Triplet(
            id=row["id"],
            subject=row["subject"],
            subject_class=row["subject_class"],
            relation=row["relation"],
            object=row["object"],
            object_class=row["object_class"],
            scope=row["scope"] if "scope" in keys and row["scope"] else "global",
            project_id=row["project_id"] if "project_id" in keys else None,
            confidence=row["confidence"],
            source_chat=row["source_chat"],
            context=row["context"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ttl_seconds=row["ttl_seconds"],
            expires_at=row["expires_at"],
            is_active=bool(row["is_active"]),
            invalidated_reason=row["invalidated_reason"],
        )

    @staticmethod
    def _row_to_entity(row) -> Entity:
        return Entity(
            id=row["id"],
            name=row["name"],
            entity_class=row["class"],
            properties=json.loads(row["properties"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _live_clause(self, alias: str = "t") -> tuple[str, list[Any]]:
        return (
            f"{alias}.is_active = 1 AND ({alias}.expires_at IS NULL OR {alias}.expires_at > ?)",
            [self.now()],
        )

    # ------------------------------------------------------------------ #
    # Ontology
    # ------------------------------------------------------------------ #
    def list_classes(self) -> list[OntologyClass]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT name, description, created_by FROM ontology_classes ORDER BY id"
            ).fetchall()
        return [OntologyClass(**dict(r)) for r in rows]

    def list_relations(self) -> list[OntologyRelation]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT name, domain, range, description, created_by FROM ontology_relations ORDER BY id"
            ).fetchall()
        return [OntologyRelation(**dict(r)) for r in rows]

    def _canonical_class(self, conn, name: Optional[str]) -> Optional[str]:
        if name is None:
            return None
        row = conn.execute("SELECT name FROM ontology_classes WHERE name = ?", (name.strip(),)).fetchone()
        if row is None:
            known = ", ".join(r["name"] for r in conn.execute("SELECT name FROM ontology_classes"))
            raise KGError(f"Unknown class '{name}'. Known classes: {known}. Use extend_ontology to add one.")
        return row["name"]

    def _get_relation(self, conn, name: str):
        row = conn.execute(
            "SELECT name, domain, range FROM ontology_relations WHERE name = ?", (name.strip(),)
        ).fetchone()
        if row is None:
            known = ", ".join(r["name"] for r in conn.execute("SELECT name FROM ontology_relations"))
            raise KGError(f"Unknown relation '{name}'. Known relations: {known}. Use extend_ontology to add one.")
        return row

    def add_class(self, name: str, description: str, created_by: str = "llm") -> tuple[OntologyClass, bool]:
        """Add an ontology class. Returns (class, created). Existing classes are left untouched."""
        name = name.strip()
        if not name:
            raise KGError("Class name must not be empty.")
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ontology_classes (name, description, created_by, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, description, created_by, self.now()),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                "SELECT name, description, created_by FROM ontology_classes WHERE name = ?", (name,)
            ).fetchone()
        if created:
            logger.info("Added ontology class '%s' (by %s)", row["name"], created_by)
        return OntologyClass(**dict(row)), created

    def add_relation(
        self,
        name: str,
        description: str,
        domain: Optional[str] = None,
        range_: Optional[str] = None,
        created_by: str = "llm",
    ) -> tuple[OntologyRelation, bool]:
        """Add an ontology relation. Domain/range must be existing classes (or None for 'any')."""
        name = name.strip()
        if not name:
            raise KGError("Relation name must not be empty.")
        with connect(self.db_path) as conn:
            domain_c = self._canonical_class(conn, domain)
            range_c = self._canonical_class(conn, range_)
            cur = conn.execute(
                "INSERT OR IGNORE INTO ontology_relations (name, domain, range, description, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, domain_c, range_c, description, created_by, self.now()),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                "SELECT name, domain, range, description, created_by FROM ontology_relations WHERE name = ?",
                (name,),
            ).fetchone()
        if created:
            logger.info(
                "Added ontology relation '%s' (%s -> %s, by %s)",
                row["name"],
                domain_c or "Any",
                range_c or "Any",
                created_by,
            )
        return OntologyRelation(**dict(row)), created

    # ------------------------------------------------------------------ #
    # Entities
    # ------------------------------------------------------------------ #
    def _get_or_create_entity_id(
        self, conn, name: str, entity_class: str, properties: Optional[dict] = None
    ) -> int:
        name = name.strip()
        if not name:
            raise KGError("Entity name must not be empty.")
        now = self.now()
        row = conn.execute(
            "SELECT id, properties FROM entities WHERE name = ? AND class = ?", (name, entity_class)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO entities (name, class, properties, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (name, entity_class, json.dumps(properties or {}), now, now),
            )
            return cur.lastrowid
        if properties:
            merged = json.loads(row["properties"] or "{}")
            merged.update(properties)
            conn.execute(
                "UPDATE entities SET properties = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged), now, row["id"]),
            )
        return row["id"]

    def upsert_entity(self, name: str, entity_class: str, properties: Optional[dict] = None) -> Entity:
        with connect(self.db_path) as conn:
            cls = self._canonical_class(conn, entity_class)
            eid = self._get_or_create_entity_id(conn, name, cls, properties)
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        return self._row_to_entity(row)

    def get_entity(self, name: str, entity_class: Optional[str] = None) -> Optional[Entity]:
        sql, params = "SELECT * FROM entities WHERE name = ?", [name.strip()]
        if entity_class:
            sql += " AND class = ?"
            params.append(entity_class)
        with connect(self.db_path) as conn:
            row = conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        return self._row_to_entity(row) if row else None

    def search_entities(self, query: str, entity_class: Optional[str] = None, limit: int = 20) -> list[Entity]:
        """Case-insensitive substring search over entity names."""
        sql, params = "SELECT * FROM entities WHERE name LIKE ?", [f"%{query.strip()}%"]
        if entity_class:
            sql += " AND class = ?"
            params.append(entity_class)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_entity(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Triplets
    # ------------------------------------------------------------------ #
    def add_triplet(
        self,
        subject: str,
        subject_class: str,
        relation: str,
        object_: str,
        object_class: str,
        confidence: float = 1.0,
        ttl_seconds: Optional[int] = None,
        context: Optional[str] = None,
        source_chat: Optional[str] = None,
        subject_properties: Optional[dict] = None,
        object_properties: Optional[dict] = None,
        scope: str = "global",
        project_id: Optional[str] = None,
    ) -> tuple[Triplet, bool]:
        """
        Assert a fact in either the 'global' or 'project' scope. Returns (triplet, created).

        If an identical live (subject, relation, object) fact already exists in the
        same (scope, project_id) it is refreshed in place and ``created`` is False.
        """
        if not 0.0 <= confidence <= 1.0:
            raise KGError("confidence must be between 0.0 and 1.0.")
        resolved_scope, resolved_pid = self._resolve_write_scope(scope, project_id)
        expires_at = self._expiry(ttl_seconds)
        now = self.now()

        with connect(self.db_path) as conn:
            rel = self._get_relation(conn, relation)
            s_cls = self._canonical_class(conn, subject_class)
            o_cls = self._canonical_class(conn, object_class)

            if rel["domain"] and rel["domain"].lower() != s_cls.lower():
                raise KGError(
                    f"Relation '{rel['name']}' requires a subject of class '{rel['domain']}', got '{s_cls}'."
                )
            if rel["range"] and rel["range"].lower() != o_cls.lower():
                raise KGError(
                    f"Relation '{rel['name']}' requires an object of class '{rel['range']}', got '{o_cls}'."
                )

            s_id = self._get_or_create_entity_id(conn, subject, s_cls, subject_properties)
            o_id = self._get_or_create_entity_id(conn, object_, o_cls, object_properties)

            live_sql, live_params = self._live_clause("t")
            existing = conn.execute(
                f"SELECT id FROM triplets t WHERE subject_id = ? AND relation = ? AND object_id = ? "
                f"AND scope = ? AND COALESCE(project_id, '') = ? AND {live_sql}",
                [s_id, rel["name"], o_id, resolved_scope, resolved_pid or "", *live_params],
            ).fetchone()

            if existing:
                tid = existing["id"]
                conn.execute(
                    "UPDATE triplets SET confidence = ?, context = COALESCE(?, context), "
                    "source_chat = COALESCE(?, source_chat), ttl_seconds = ?, expires_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (confidence, context, source_chat, ttl_seconds, expires_at, now, tid),
                )
                created = False
            else:
                cur = conn.execute(
                    "INSERT INTO triplets (subject_id, relation, object_id, scope, project_id, confidence, "
                    "source_chat, context, created_at, updated_at, ttl_seconds, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        s_id,
                        rel["name"],
                        o_id,
                        resolved_scope,
                        resolved_pid,
                        confidence,
                        source_chat,
                        context,
                        now,
                        now,
                        ttl_seconds,
                        expires_at,
                    ),
                )
                tid = cur.lastrowid
                created = True

            row = conn.execute(_TRIPLET_SELECT + " WHERE t.id = ?", (tid,)).fetchone()

        triplet = self._row_to_triplet(row)
        logger.info(
            "%s triplet #%d %s: %s --%s--> %s",
            "Created" if created else "Refreshed",
            triplet.id,
            triplet.scope_tag(),
            triplet.subject,
            triplet.relation,
            triplet.object,
        )
        return triplet, created

    def get_triplet(self, triplet_id: int) -> Optional[Triplet]:
        with connect(self.db_path) as conn:
            row = conn.execute(_TRIPLET_SELECT + " WHERE t.id = ?", (triplet_id,)).fetchone()
        return self._row_to_triplet(row) if row else None

    def update_triplet(
        self,
        triplet_id: int,
        confidence: Optional[float] = None,
        context: Optional[str] = None,
        ttl_seconds: Any = _UNSET,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Triplet:
        """
        Update a live triplet.
        - ``ttl_seconds``: omitted -> unchanged, ``None`` -> make permanent, int -> new TTL counted from now.
        - ``scope``: 'global' or 'project' to promote a project fact to global or restrict a global fact to a project.
        """
        existing = self.get_triplet(triplet_id)
        if existing is None:
            raise KGError(f"No triplet with id {triplet_id}.")
        if not existing.is_active or (existing.expires_at and existing.expires_at <= self.now()):
            raise KGError(f"Triplet {triplet_id} is no longer active; add a new fact instead.")

        sets, params = ["updated_at = ?"], [self.now()]
        if confidence is not None:
            if not 0.0 <= confidence <= 1.0:
                raise KGError("confidence must be between 0.0 and 1.0.")
            sets.append("confidence = ?")
            params.append(confidence)
        if context is not None:
            sets.append("context = ?")
            params.append(context)
        if ttl_seconds is not _UNSET:
            sets += ["ttl_seconds = ?", "expires_at = ?"]
            params += [ttl_seconds, self._expiry(ttl_seconds)]
        if scope is not None:
            new_scope, new_pid = self._resolve_write_scope(scope, project_id or existing.project_id)
            sets += ["scope = ?", "project_id = ?"]
            params += [new_scope, new_pid]

        with connect(self.db_path) as conn:
            conn.execute(f"UPDATE triplets SET {', '.join(sets)} WHERE id = ?", [*params, triplet_id])
        updated = self.get_triplet(triplet_id)
        logger.info("Updated triplet #%d (%s)", triplet_id, updated.scope_tag())
        return updated

    def retract_triplet(self, triplet_id: int, reason: str) -> Triplet:
        """Soft-delete a fact, keeping it in history with the given reason."""
        existing = self.get_triplet(triplet_id)
        if existing is None:
            raise KGError(f"No triplet with id {triplet_id}.")
        if not existing.is_active:
            raise KGError(f"Triplet {triplet_id} is already inactive ({existing.invalidated_reason}).")
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE triplets SET is_active = 0, invalidated_reason = ?, updated_at = ? WHERE id = ?",
                (reason or "retracted", self.now(), triplet_id),
            )
        retracted = self.get_triplet(triplet_id)
        logger.info("Retracted triplet #%d (reason: %s)", triplet_id, reason)
        return retracted

    def query(
        self,
        subject: Optional[str] = None,
        relation: Optional[str] = None,
        object_: Optional[str] = None,
        subject_class: Optional[str] = None,
        object_class: Optional[str] = None,
        active_only: bool = True,
        min_confidence: float = 0.0,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[Triplet]:
        """
        Pattern match on triplets. Name filters are case-insensitive exact matches.
        ``scope`` may be 'all' (global + active project), 'global', or 'project'.
        """
        where, params = [], []
        scope_sql, scope_params = self._scope_filter_clause(scope, project_id, alias="t")
        where.append(scope_sql)
        params.extend(scope_params)

        if subject:
            where.append("s.name = ?")
            params.append(subject.strip())
        if relation:
            where.append("t.relation = ? COLLATE NOCASE")
            params.append(relation.strip())
        if object_:
            where.append("o.name = ?")
            params.append(object_.strip())
        if subject_class:
            where.append("s.class = ? COLLATE NOCASE")
            params.append(subject_class)
        if object_class:
            where.append("o.class = ? COLLATE NOCASE")
            params.append(object_class)
        if min_confidence > 0:
            where.append("t.confidence >= ?")
            params.append(min_confidence)
        if active_only:
            live_sql, live_params = self._live_clause("t")
            where.append(live_sql)
            params += live_params

        sql = _TRIPLET_SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?"
        params.append(limit)

        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_triplet(r) for r in rows]

    def reap_expired(self) -> int:
        """Soft-delete all facts past their expiry. Returns the number reaped."""
        now = self.now()
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE triplets SET is_active = 0, invalidated_reason = 'expired', updated_at = ? "
                "WHERE is_active = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
                (now, now),
            )
            count = cur.rowcount
        if count:
            logger.info("Reaped %d expired triplet(s)", count)
        return count

    def neighbourhood(
        self,
        entity: str,
        entity_class: Optional[str] = None,
        depth: int = 1,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[Triplet]:
        """
        Breadth-first walk over live triplets in both directions starting at
        ``entity``, filtered by ``scope`` ('all' = global + current project).
        """
        start = self.get_entity(entity, entity_class)
        if start is None:
            return []

        live_sql, live_params = self._live_clause("t")
        scope_sql, scope_params = self._scope_filter_clause(scope, project_id, alias="t")
        seen_nodes, seen_edges = {start.id}, {}
        frontier = deque([(start.id, 0)])

        with connect(self.db_path) as conn:
            while frontier and len(seen_edges) < limit:
                node_id, d = frontier.popleft()
                if d >= depth:
                    continue
                rows = conn.execute(
                    _TRIPLET_SELECT
                    + f" WHERE (t.subject_id = ? OR t.object_id = ?) AND {scope_sql} AND {live_sql}",
                    [node_id, node_id, *scope_params, *live_params],
                ).fetchall()
                for row in rows:
                    if row["id"] not in seen_edges:
                        seen_edges[row["id"]] = self._row_to_triplet(row)
                    for nxt in (row["subject_id"], row["object_id"]):
                        if nxt not in seen_nodes:
                            seen_nodes.add(nxt)
                            frontier.append((nxt, d + 1))
        return list(seen_edges.values())[:limit]
