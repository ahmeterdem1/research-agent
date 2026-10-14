"""
Knowledge Graph MCP server.

Exposes the temporal KG to the agent so it can remember who the user is,
what they care about, what they have argued, mistakes they have made, and
which papers they have read — across chat sessions.

All tools return plain strings (never raise) so the LLM always receives an
actionable message, including validation errors it can correct.
"""

from contextvars import ContextVar
from typing import Literal, Optional

from fastmcp import FastMCP

from src.kg.reasoning import format_user_profile, get_topic_context, get_user_profile
from src.kg.schema import USER_CLASS_NAME, USER_ENTITY_NAME
from src.kg.store import KGError, KGStore

mcp = FastMCP("KnowledgeGraphTools")

# NOTE (Phase 5): the orchestrator will set this per chat turn so that every
# fact records which session produced it, without the LLM having to pass it.
CURRENT_SESSION_ID: ContextVar[Optional[str]] = ContextVar("CURRENT_SESSION_ID", default=None)

_store: Optional[KGStore] = None


def get_store() -> KGStore:
    """Lazily open the project KG. Tests replace ``_store`` with a temp-DB store."""
    global _store
    if _store is None:
        from config import settings

        _store = KGStore(settings.DB_PATH)
    return _store


@mcp.tool()
def add_triplet(
    subject: str,
    relation: str,
    object: str,
    object_class: str,
    subject_class: str = USER_CLASS_NAME,
    confidence: float = 1.0,
    ttl_seconds: Optional[int] = None,
    context: Optional[str] = None,
    object_properties: Optional[dict] = None,
) -> str:
    """
    Records a fact in the long-term Knowledge Graph as (subject) --relation--> (object).

    Use this whenever the user reveals something worth remembering across sessions:
    - a research interest            -> subject="User", relation="interested_in", object_class="Topic"
    - a claim they argue             -> relation="argued_that", object_class="Argument"
    - a factual mistake they made    -> relation="made_mistake", object_class="Mistake"
    - a preference (tool, style...)  -> relation="prefers", object_class="Preference"
    - background (role, institution) -> relation="has_fact", object_class="PersonalFact"
    - a paper they have read         -> relation="has_read", object_class="Paper"
    Non-user facts are allowed too, e.g. Topic --related_to--> Topic, Paper --covers--> Concept.

    Arguments:
    - subject: Use exactly "User" for the human user. Otherwise a concise canonical name.
    - object: A short, canonical, reusable name (e.g. "Reinforcement Learning", not
      "RL stuff the user mentioned"). Names are case-insensitive; reuse existing names.
      For papers, use the paper title.
    - confidence: 0.0-1.0. Use < 1.0 when the fact is inferred rather than stated.
    - ttl_seconds: Set ONLY for transient facts (e.g. "is preparing for a deadline this week"
      -> 604800). Omit for permanent facts.
    - context: One sentence of justification or a short quote from the user.
    - object_properties: Optional extra attributes, e.g. {"virtual_path": "/ML/attention", "doi": "..."}.

    Asserting a fact that already exists refreshes it instead of creating a duplicate.
    Relations and classes must exist in the ontology (see `get_ontology`); use
    `extend_ontology` first if you genuinely need a new one.
    """
    try:
        triplet, created = get_store().add_triplet(
            subject=subject,
            subject_class=subject_class,
            relation=relation,
            object_=object,
            object_class=object_class,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            context=context,
            source_chat=CURRENT_SESSION_ID.get(),
            object_properties=object_properties,
        )
    except KGError as e:
        return f"Error: {e}"
    verb = "Recorded new fact" if created else "Fact already known; refreshed"
    return f"{verb}: {triplet.to_line()}"


@mcp.tool()
def query_kg(
    subject: Optional[str] = None,
    relation: Optional[str] = None,
    object: Optional[str] = None,
    subject_class: Optional[str] = None,
    object_class: Optional[str] = None,
    include_inactive: bool = False,
    limit: int = 50,
) -> str:
    """
    Pattern-matches facts in the Knowledge Graph. Every argument is an optional filter;
    omitted arguments match anything. Name filters are case-insensitive exact matches
    (use `search_entities` first if you are unsure of an exact name).

    Examples:
    - All of the user's interests:  query_kg(subject="User", relation="interested_in")
    - Who/what relates to a topic:   query_kg(object="Graph Neural Networks")
    - All papers covering a concept: query_kg(relation="covers", object="Backpropagation")

    By default only live facts are returned (not retracted, not expired). Set
    include_inactive=True to also see historical facts, e.g. how the user's views changed.
    Each result is prefixed with its ID ([#12]) for use with `update_triplet` / `retract_triplet`.
    """
    try:
        results = get_store().query(
            subject=subject,
            relation=relation,
            object_=object,
            subject_class=subject_class,
            object_class=object_class,
            active_only=not include_inactive,
            limit=limit,
        )
    except KGError as e:
        return f"Error: {e}"
    if not results:
        return "No matching facts found."
    return f"Found {len(results)} fact(s):\n" + "\n".join(t.to_line() for t in results)


@mcp.tool()
def update_triplet(
    triplet_id: int,
    confidence: Optional[float] = None,
    context: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    make_permanent: bool = False,
) -> str:
    """
    Adjusts an existing live fact, identified by the ID shown in tool output (e.g. [#12] -> 12).

    - confidence: raise it when the user confirms an inferred fact, lower it when doubtful.
    - context: replace the justification text.
    - ttl_seconds: give the fact a new lifetime counted from now.
    - make_permanent: remove any expiry from the fact.

    To change WHAT a fact says (e.g. the user changed their mind), do not edit it: call
    `retract_triplet` on the old fact and `add_triplet` for the new one, so history is kept.
    """
    if make_permanent and ttl_seconds is not None:
        return "Error: pass either ttl_seconds or make_permanent=True, not both."
    kwargs = {}
    if make_permanent:
        kwargs["ttl_seconds"] = None
    elif ttl_seconds is not None:
        kwargs["ttl_seconds"] = ttl_seconds
    try:
        triplet = get_store().update_triplet(triplet_id, confidence=confidence, context=context, **kwargs)
    except KGError as e:
        return f"Error: {e}"
    return f"Updated fact: {triplet.to_line()}"


@mcp.tool()
def retract_triplet(triplet_id: int, reason: str) -> str:
    """
    Marks a fact as no longer true (e.g. the user abandoned an argument, corrected a
    misunderstanding, or said you recorded something wrongly). The fact is kept in history
    with the reason but disappears from normal queries and from the user profile.

    When a user's mistake gets corrected, prefer also recording the insight:
    add_triplet(subject=<mistake>, subject_class="Mistake", relation="corrected_by",
    object=<correct claim>, object_class="Argument").
    """
    try:
        triplet = get_store().retract_triplet(triplet_id, reason)
    except KGError as e:
        return f"Error: {e}"
    return f"Retracted fact: {triplet.to_line()}"


@mcp.tool()
def get_user_profile_summary() -> str:
    """
    Returns everything currently known about the user, grouped into background,
    interests, preferences, arguments, past mistakes, and reading history.

    Call this at the start of a research task to tailor your answer to the user's
    level, interests, and what they have already read.
    """
    store = get_store()
    store.reap_expired()
    return format_user_profile(get_user_profile(store))


@mcp.tool()
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
def get_related_facts(entity: str, depth: int = 2) -> str:
    """
    Returns live facts within `depth` hops of an entity, walking relations in both
    directions. Useful for gathering everything the user has said or read around a topic
    before answering, e.g. get_related_facts("Diffusion Models").
    """
    depth = max(1, min(depth, 3))
    results = get_topic_context(get_store(), entity, depth=depth)
    if not results:
        return f"No facts connected to '{entity}'. Try `search_entities` to find the exact name."
    return f"{len(results)} related fact(s):\n" + "\n".join(t.to_line() for t in results)


@mcp.tool()
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

    - kind="class": e.g. name="Dataset", description="A benchmark or training dataset."
    - kind="relation": e.g. name="uses_dataset", domain="Paper", range="Dataset".
      domain/range must be existing class names; omit them to allow any class.
    Use snake_case for relations and PascalCase for classes.
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
        return f"Error: {e}"
    return "Error: kind must be 'class' or 'relation'."


if __name__ == "__main__":
    mcp.run()

