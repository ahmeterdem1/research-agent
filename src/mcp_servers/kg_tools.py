"""
Knowledge Graph MCP server (``ra-mcp-kg``).

Exposes the dual-scope (``global`` + ``project``) temporal KG to OpenCode agents
so they can remember who the user is, what they care about globally across all
projects versus locally in the active project, what they have argued, mistakes
they have made, and which papers they have read.

All tools return plain strings (never raise) so the LLM always receives an
actionable message, including validation errors it can self-correct.
"""

from contextvars import ContextVar
from typing import Literal, Optional

from fastmcp import FastMCP

from config import settings
from config.settings import get_logger
from src.kg.reasoning import format_user_profile, get_topic_context, get_user_profile
from src.kg.schema import USER_CLASS_NAME
from src.kg.store import KGError, KGStore
from src.telemetry.store import track_tool

logger = get_logger(__name__)

mcp = FastMCP("KnowledgeGraphTools")

CURRENT_SESSION_ID: ContextVar[Optional[str]] = ContextVar("CURRENT_SESSION_ID", default=None)

_store: Optional[KGStore] = None


def get_store() -> KGStore:
    """Lazily open the global KG DB. Tests replace ``_store`` with a temp-DB store."""
    global _store
    if _store is None:
        _store = KGStore(settings.DB_PATH)
    return _store


@mcp.tool()
@track_tool("ra_kg")
def add_triplet(
    subject: str,
    relation: str,
    object: str,
    object_class: str,
    subject_class: str = USER_CLASS_NAME,
    scope: Literal["global", "project"] = "global",
    confidence: float = 1.0,
    ttl_seconds: Optional[int] = None,
    context: Optional[str] = None,
    object_properties: Optional[dict] = None,
) -> str:
    """
    Records a fact in the long-term Knowledge Graph as (subject) --relation--> (object),
    tagged with either `scope="global"` or `scope="project"`.

    IMPORTANT — Choosing `scope`:
    - `scope="global"` (default): Use for facts that remain true across ALL projects on the user's PC:
      * User background (`has_fact`), e.g. academic role, expertise, institution.
      * Papers the user has read (`has_read`) — reading a paper is a permanent global fact.
      * Enduring research interests (`interested_in`) and general workflow preferences (`prefers`).
      * Foundational conceptual mistakes (`made_mistake` / `corrected_by`).
      * Objective literature relationships (`Paper --covers--> Concept`, `Topic --related_to--> Topic`).
    - `scope="project"`: Use for facts that apply ONLY to the current project/workspace:
      * Working hypotheses or project-specific claims (`argued_that`).
      * Project-specific tool, library, or dataset choices (`prefers`).
      * Narrow topics explored strictly for this project's task (`interested_in`).

    Arguments:
    - subject: Use "User" for the human user. Otherwise a concise canonical entity name.
    - object: A short, canonical, reusable name (e.g. "Reinforcement Learning"). Reuse existing names.
    - confidence: 0.0-1.0. Use < 1.0 when inferred rather than explicitly stated.
    - ttl_seconds: Set ONLY for transient facts (e.g. "preparing for a deadline this week" -> 604800).
    - context: One sentence of justification or a short quote from the user.
    - object_properties: Optional extra attributes, e.g. {"virtual_path": "/ML/attention", "doi": "..."}.
    """
    try:
        triplet, created = get_store().add_triplet(
            subject=subject,
            subject_class=subject_class,
            relation=relation,
            object_=object,
            object_class=object_class,
            scope=scope,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            context=context,
            source_chat=CURRENT_SESSION_ID.get(),
            object_properties=object_properties,
        )
    except KGError as e:
        logger.warning("add_triplet validation error: %s", e)
        return f"Error: {e}"
    verb = "Recorded new fact" if created else "Fact already known; refreshed"
    return f"{verb}: {triplet.to_line()}"


@mcp.tool()
@track_tool("ra_kg")
def query_kg(
    subject: Optional[str] = None,
    relation: Optional[str] = None,
    object: Optional[str] = None,
    subject_class: Optional[str] = None,
    object_class: Optional[str] = None,
    scope: Literal["all", "global", "project"] = "all",
    include_inactive: bool = False,
    limit: int = 50,
) -> str:
    """
    Pattern-matches facts in the Knowledge Graph. Every argument is an optional filter.

    Scope filtering (`scope`):
    - `"all"` (default): Returns both `[GLOBAL]` facts and `[PROJECT: <current>]` facts for the
      active workspace (other projects' local facts are automatically excluded).
    - `"global"`: Returns only cross-project global facts.
    - `"project"`: Returns only facts recorded specifically for the current project.

    Each result is prefixed with its ID (`[#12]`) and scope badge (`[GLOBAL]` or `[PROJECT: ...]`).
    """
    try:
        results = get_store().query(
            subject=subject,
            relation=relation,
            object_=object,
            subject_class=subject_class,
            object_class=object_class,
            scope=scope,
            active_only=not include_inactive,
            limit=limit,
        )
    except KGError as e:
        logger.warning("query_kg error: %s", e)
        return f"Error: {e}"
    if not results:
        return "No matching facts found."
    return f"Found {len(results)} fact(s):\n" + "\n".join(t.to_line() for t in results)


@mcp.tool()
@track_tool("ra_kg")
def update_triplet(
    triplet_id: int,
    confidence: Optional[float] = None,
    context: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    make_permanent: bool = False,
    scope: Optional[Literal["global", "project"]] = None,
) -> str:
    """
    Adjusts an existing live fact, identified by the ID shown in tool output (e.g. [#12] -> 12).

    - confidence: raise when confirmed by the user, lower when uncertain.
    - context: replace the justification text.
    - ttl_seconds: set a new lifetime in seconds counted from now.
    - make_permanent: remove any expiry from the fact.
    - scope: pass `"global"` to promote a project-local fact to global memory, or `"project"`
      to restrict a global fact to the current project.
    """
    if make_permanent and ttl_seconds is not None:
        return "Error: pass either ttl_seconds or make_permanent=True, not both."
    kwargs = {}
    if make_permanent:
        kwargs["ttl_seconds"] = None
    elif ttl_seconds is not None:
        kwargs["ttl_seconds"] = ttl_seconds
    if scope is not None:
        kwargs["scope"] = scope
    try:
        triplet = get_store().update_triplet(triplet_id, confidence=confidence, context=context, **kwargs)
    except KGError as e:
        logger.warning("update_triplet error for #%d: %s", triplet_id, e)
        return f"Error: {e}"
    return f"Updated fact: {triplet.to_line()}"


@mcp.tool()
@track_tool("ra_kg")
def retract_triplet(triplet_id: int, reason: str) -> str:
    """
    Marks a fact as no longer true (e.g. the user abandoned a project hypothesis, corrected a
    misunderstanding, or said you recorded something wrongly). The fact is kept in history
    with the reason but disappears from normal queries and from the user profile.
    """
    try:
        triplet = get_store().retract_triplet(triplet_id, reason)
    except KGError as e:
        logger.warning("retract_triplet error for #%d: %s", triplet_id, e)
        return f"Error: {e}"
    return f"Retracted fact: {triplet.to_line()}"


@mcp.tool()
@track_tool("ra_kg")
def get_user_profile_summary(scope: Literal["all", "global", "project"] = "all") -> str:
    """
    Returns what is currently known about the user, separated into:
    1. `# Global User Profile (Applies Across All Projects)` — permanent background, reading
       history, long-term interests, and general preferences.
    2. `# Current Project Profile (project: <name>)` — hypotheses, arguments, and preferences
       specific to the active OpenCode workspace.

    Pass `scope="global"` or `scope="project"` to inspect only one layer, or leave `scope="all"`
    (default) to see both clearly separated.
    """
    store = get_store()
    store.reap_expired()
    try:
        profile = get_user_profile(store, scope=scope)
    except KGError as e:
        return f"Error: {e}"
    return format_user_profile(profile)


@mcp.tool()
@track_tool("ra_kg")
def search_entities(query: str, entity_class: Optional[str] = None, limit: int = 20) -> str:
    """
    Finds entities whose name contains `query` (case-insensitive). Use this to discover
    the exact canonical name of a Topic, Paper, Concept, etc. before calling `query_kg`,
    and to reuse existing names in `add_triplet` instead of creating near-duplicates.
    """
    results = get_store().search_entities(query, entity_class=entity_class, limit=limit)
    if not results:
        return f"No entities matching '{query}'."
    return "\n".join(f"- {e.name} ({e.entity_class})" for e in results)


@mcp.tool()
@track_tool("ra_kg")
def get_related_facts(
    entity: str,
    depth: int = 2,
    scope: Literal["all", "global", "project"] = "all",
) -> str:
    """
    Returns live facts within `depth` hops of an entity, walking relations in both
    directions across the requested `scope` (`"all"`, `"global"`, or `"project"`).
    """
    depth = max(1, min(depth, 3))
    try:
        results = get_topic_context(get_store(), entity, depth=depth, scope=scope)
    except KGError as e:
        return f"Error: {e}"
    if not results:
        return f"No facts connected to '{entity}'. Try `search_entities` to find the exact name."
    return f"{len(results)} related fact(s):\n" + "\n".join(t.to_line() for t in results)


@mcp.tool()
@track_tool("ra_kg")
def get_ontology() -> str:
    """
    Lists the classes (entity types) and relations (with their required subject/object
    classes) that facts may use. Check this before inventing a new relation.
    """
    store = get_store()
    lines = ["# Classes"]
    lines += [f"- {c.name}: {c.description or ''}" for c in store.list_classes()]
    lines.append("\n# Relations (subject class -> object class)")
    for r in store.list_relations():
        lines.append(f"- {r.name} ({r.domain or 'Any'} -> {r.range or 'Any'}): {r.description or ''}")
    return "\n".join(lines)


@mcp.tool()
@track_tool("ra_kg")
def extend_ontology(
    kind: Literal["class", "relation"],
    name: str,
    description: str,
    domain: Optional[str] = None,
    range: Optional[str] = None,
) -> str:
    """
    Adds a new class or relation to the ontology when no existing one fits.
    Prefer existing terms (see `get_ontology`); only extend for genuinely new kinds of facts.
    """
    store = get_store()
    try:
        if kind == "class":
            cls, created = store.add_class(name, description)
            return f"{'Added' if created else 'Already exists'}: class '{cls.name}'."
        if kind == "relation":
            rel, created = store.add_relation(name, description, domain=domain, range_=range)
            return (
                f"{'Added' if created else 'Already exists'}: relation '{rel.name}' "
                f"({rel.domain or 'Any'} -> {rel.range or 'Any'})."
            )
    except KGError as e:
        logger.warning("extend_ontology error: %s", e)
        return f"Error: {e}"
    return "Error: kind must be 'class' or 'relation'."


def main() -> None:
    logger.info("Starting KnowledgeGraphTools MCP server (active project: %s)", settings.get_active_project_id())
    mcp.run()


if __name__ == "__main__":
    main()
