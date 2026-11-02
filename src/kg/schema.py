"""
SQLite schema and base ontology for the temporal Knowledge Graph.

Design notes:
- Entity and ontology names use ``COLLATE NOCASE`` so that the LLM writing
  "Reinforcement Learning" and "reinforcement learning" hits the same row.
- Triplets carry an explicit ``scope`` ('global' or 'project') and an optional
  ``project_id`` (NULL when scope='global'; the active workspace identifier
  when scope='project'). This lets the agent separate permanent, cross-project
  facts about the user from local project-specific hypotheses and preferences.
- Triplets are never hard-deleted. Retraction and TTL expiry flip
  ``is_active`` to 0 and record ``invalidated_reason``, preserving history.
- All timestamps are UTC strings in SQLite's native ``YYYY-MM-DD HH:MM:SS``
  format so they compare correctly as text.
"""

SCHEMA_VERSION = 2

# Canonical name of the single human user of this local-first app.
USER_ENTITY_NAME = "User"
USER_CLASS_NAME = "User"

VALID_SCOPES = ("global", "project")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_classes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL COLLATE NOCASE,
    description TEXT,
    created_by  TEXT NOT NULL DEFAULT 'system',      -- 'system' or 'llm'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ontology_relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL COLLATE NOCASE,
    domain      TEXT REFERENCES ontology_classes(name), -- NULL = any class
    range       TEXT REFERENCES ontology_classes(name), -- NULL = any class
    description TEXT,
    created_by  TEXT NOT NULL DEFAULT 'system',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL COLLATE NOCASE,
    class       TEXT NOT NULL REFERENCES ontology_classes(name),
    properties  TEXT,                                 -- JSON object
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, class)
);

CREATE TABLE IF NOT EXISTS triplets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id         INTEGER NOT NULL REFERENCES entities(id),
    relation           TEXT NOT NULL REFERENCES ontology_relations(name),
    object_id          INTEGER NOT NULL REFERENCES entities(id),
    scope              TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'project')),
    project_id         TEXT,                          -- NULL when scope='global'; project identifier when scope='project'
    confidence         REAL NOT NULL DEFAULT 1.0,
    source_chat        TEXT,                          -- session UUID that produced the fact
    context            TEXT,                          -- rationale / quote
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    ttl_seconds        INTEGER,                       -- NULL = permanent
    expires_at         TEXT,                          -- NULL = permanent
    is_active          INTEGER NOT NULL DEFAULT 1,
    invalidated_reason TEXT                           -- 'expired', or a retraction reason
);

CREATE INDEX IF NOT EXISTS idx_triplets_subject  ON triplets(subject_id) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_triplets_object   ON triplets(object_id)  WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_triplets_relation ON triplets(relation)   WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_triplets_scope    ON triplets(scope, project_id) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_triplets_expires  ON triplets(expires_at)
    WHERE expires_at IS NOT NULL AND is_active = 1;
"""

# (name, description)
BASE_CLASSES: list[tuple[str, str]] = [
    ("User", "The human user of the system."),
    ("Topic", "A research area or specific scientific discipline."),
    ("Argument", "A specific claim or hypothesis proposed or defended by the user."),
    ("Mistake", "A factual misconception or invalid premise held by the user."),
    ("Preference", "A stated preference (e.g. tools, methods, frameworks, writing style)."),
    ("PersonalFact", "Personal background, research role, institution, or goals."),
    ("Paper", "A specific research paper, preprint, book, or publication."),
    ("Concept", "A technical term, equation, algorithm, or methodology."),
]

# (name, domain, range, description)
BASE_RELATIONS: list[tuple[str, str | None, str | None, str]] = [
    ("has_read", "User", "Paper", "User has read or is familiar with this paper."),
    ("interested_in", "User", "Topic", "User expressed interest in, or requested literature on, this topic."),
    ("argued_that", "User", "Argument", "User asserted or defended this claim."),
    ("made_mistake", "User", "Mistake", "User made this factual error or mistaken assertion."),
    ("prefers", "User", "Preference", "User expressed this preference."),
    ("has_fact", "User", "PersonalFact", "Personal information or background about the user."),
    ("related_to", "Topic", "Topic", "Topical relatedness or hierarchy between two topics."),
    ("references", "Paper", "Paper", "The subject paper cites the object paper."),
    ("covers", "Paper", "Concept", "The paper discusses or introduces this concept."),
    ("contradicts", "Argument", "Argument", "The two arguments conflict with each other."),
    ("corrected_by", "Mistake", "Argument", "The mistake was resolved by this valid insight."),
]
