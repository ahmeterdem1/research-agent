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


_USER_CATEGORY_MAP: dict[str, tuple[str, str, str]] = {
    # category / alias -> (relation, object_class, default_scope)
    "background": ("has_fact", "PersonalFact", "global"),
    "personal_fact": ("has_fact", "PersonalFact", "global"),
    "has_fact": ("has_fact", "PersonalFact", "global"),
    "interest": ("interested_in", "Topic", "global"),
    "topic": ("interested_in", "Topic", "global"),
    "interested_in": ("interested_in", "Topic", "global"),
    "preference": ("prefers", "Preference", "global"),
    "prefers": ("prefers", "Preference", "global"),
    "argument": ("argued_that", "Argument", "project"),
    "hypothesis": ("argued_that", "Argument", "project"),
    "argued_that": ("argued_that", "Argument", "project"),
    "mistake": ("made_mistake", "Mistake", "global"),
    "made_mistake": ("made_mistake", "Mistake", "global"),
    "read_paper": ("has_read", "Paper", "global"),
    "paper": ("has_read", "Paper", "global"),
    "has_read": ("has_read", "Paper", "global"),
}


@mcp.tool()
@track_tool("ra_kg")
def remember_user_fact(
    category: Literal[
        "background",
        "interest",
        "preference",
        "argument",
        "mistake",
        "read_paper",
    ],
    fact: str,
    scope: Optional[Literal["global", "project"]] = None,
    context: Optional[str] = None,
    confidence: float = 1.0,
    ttl_seconds: Optional[int] = None,
    properties: Optional[dict] = None,
) -> str:
    """
    PROACTIVE CONVERSATIONAL MEMORY TOOL — Call this tool AUTOMATICALLY (without waiting for the
    user to ask) whenever the user reveals any information about themselves during the conversation!

    WHEN TO CALL THIS TOOL IMMEDIATELY:
    - `category="background"` (`has_fact`, default `scope="global"`):
      The user mentions their academic role, degree/thesis, expertise, institution, or background
      (e.g., "I am a PhD student in NLP", "I already know PPO and DPO well").
    - `category="interest"` (`interested_in`, default `scope="global"`):
      The user expresses interest in or asks for literature on a research topic
      (e.g., "Mechanistic Interpretability", "Reward Hacking in RLHF").
    - `category="preference"` (`prefers`, default `scope="global"`):
      The user states how they like explanations, tools, or workflows
      (e.g., "Prefers concise mathematical derivations", "Uses PyTorch").
    - `category="argument"` (`argued_that`, default `scope="project"`):
      The user proposes a hypothesis, claim, or design opinion in the current discussion
      (e.g., "Coarse grading rubrics cause reward hacking").
    - `category="mistake"` (`made_mistake`, default `scope="global"`):
      The user had a factual misconception that was clarified during the chat.
    - `category="read_paper"` (`has_read`, default `scope="global"`):
      The user mentions having read a paper, or finishes reading/discussing a paper in this chat.

    Arguments:
    - category: One of `"background"`, `"interest"`, `"preference"`, `"argument"`, `"mistake"`, `"read_paper"`.
    - fact: Concise, canonical statement of the fact, topic, preference, hypothesis, or paper title.
    - scope: Optional `"global"` (applies across all projects) or `"project"` (only this workspace).
      If omitted, defaults automatically (`"project"` for `"argument"`, `"global"` for all others).
    - context: Optional 1-sentence quote or reason from the conversation.
    - confidence: 0.0–1.0 (use `1.0` when stated by the user, `< 1.0` when inferred).
    - ttl_seconds: Optional expiry in seconds ONLY for temporary facts (e.g., `604800` for a 1-week deadline).
    - properties: Optional metadata dict (e.g., `{"doi": "...", "virtual_path": "/..."}` for `"read_paper"`).
    """
    key = (category or "").strip().lower()
    if key not in _USER_CATEGORY_MAP:
        valid = ", ".join(["background", "interest", "preference", "argument", "mistake", "read_paper"])
        return f"Error: Unknown category '{category}'. Choose one of: {valid}."

    relation, object_class, default_scope = _USER_CATEGORY_MAP[key]
    effective_scope = scope or default_scope
    try:
        triplet, created = get_store().add_triplet(
            subject=USER_CLASS_NAME,
            subject_class=USER_CLASS_NAME,
            relation=relation,
            object_=fact,
            object_class=object_class,
            scope=effective_scope,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            context=context,
            source_chat=CURRENT_SESSION_ID.get(),
            object_properties=properties,
        )
    except KGError as e:
        logger.warning("remember_user_fact validation error: %s", e)
        return f"Error: {e}"
    verb = "Recorded new fact" if created else "Fact already known; refreshed"
    return f"{verb}: {triplet.to_line()}"


@mcp.tool()
@track_tool("ra_kg")
def add_triplet(
    subject: str = USER_CLASS_NAME,
    relation: str = "interested_in",
    object: str = "",
    object_class: Optional[str] = None,
    subject_class: Optional[str] = None,
    scope: Literal["global", "project"] = "global",
    confidence: float = 1.0,
    ttl_seconds: Optional[int] = None,
    context: Optional[str] = None,
    object_properties: Optional[dict] = None,
) -> str:
    """
    PROACTIVE MEMORY & LITERATURE GRAPH TOOL — Records a structured fact `(subject) --relation--> (object)`
    in the long-term Knowledge Graph (`scope="global"` or `scope="project"`).

    Call this tool (or `remember_user_fact`) AUTOMATICALLY during conversation whenever:
    1. The user shares personal background, research interests, preferences, hypotheses, mistakes,
       or papers they have read (`subject="User"`).
    2. You discover important objective relationships between papers, concepts, and topics
       (`Paper --covers--> Concept`, `Paper --references--> Paper`, `Topic --related_to--> Topic`).

    BUILT-IN RELATIONS (Note: `subject_class` and `object_class` are AUTO-INFERRED if omitted!):
    - User Facts (`subject="User"`, `subject_class="User"`):
      * `has_fact`      -> `object_class="PersonalFact"` (background, role, expertise — `scope="global"`)
      * `interested_in` -> `object_class="Topic"`        (research topics of interest — `scope="global"`)
      * `prefers`       -> `object_class="Preference"`   (workflow/style/tool preferences — `scope="global"` or `"project"`)
      * `argued_that`   -> `object_class="Argument"`     (user's hypothesis or claim — usually `scope="project"`)
      * `made_mistake`  -> `object_class="Mistake"`      (misconception held by user — `scope="global"`)
      * `has_read`      -> `object_class="Paper"`        (paper the user has read — `scope="global"`)
    - Literature & Concept Facts (`scope="global"`):
      * `covers`        (`subject_class="Paper"`    -> `object_class="Concept"`)
      * `references`    (`subject_class="Paper"`    -> `object_class="Paper"`)
      * `related_to`    (`subject_class="Topic"`    -> `object_class="Topic"`)
      * `contradicts`   (`subject_class="Argument"` -> `object_class="Argument"`)
      * `corrected_by`  (`subject_class="Mistake"`  -> `object_class="Argument"`)

    Arguments:
    - subject: Defaults to `"User"`. For literature facts, pass the Paper, Topic, or Argument name.
    - relation: One of the relation names above (or a custom relation added via `extend_ontology`).
    - object: Short, canonical, reusable entity name (e.g. `"Reinforcement Learning"`).
    - object_class: Optional! Automatically inferred from `relation` for all built-in relations above.
    - subject_class: Optional! Automatically inferred (`"User"` when `subject="User"`, or from `relation`).
    - scope: `"global"` (default, true across all projects) or `"project"` (only current workspace).
    - confidence: 0.0–1.0 (`1.0` when explicit, `< 1.0` when inferred).
    - ttl_seconds: Set ONLY for transient facts (e.g. `604800` for a 1-week deadline).
    - context: One sentence of justification or a short quote from the user.
    - object_properties: Optional extra attributes, e.g. `{"virtual_path": "/ML/attention", "doi": "..."}`.
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
    PROACTIVE KG LOOKUP TOOL — Pattern-matches facts in the Knowledge Graph (supporting both exact
    and partial/keyword entity matching on `subject` and `object`).

    WHEN TO CALL THIS TOOL:
    - **Before Recommending or Fetching Papers**: Call `query_kg(subject="User", relation="has_read")`
      (or `object="<topic>"`) to check which papers the user has already read in past sessions so you
      can build on their reading history instead of repeating basics.
    - **When Discussing a Hypothesis or Argument**: Call `query_kg(relation="argued_that")` or
      `query_kg(relation="contradicts")` to check prior claims and see if a new claim aligns with or
      contradicts earlier arguments.
    - **When Exploring a Concept or Topic**: Call `query_kg(relation="covers", object="<concept>")`
      to find papers linked to that concept across previous sessions.
    - **Before Calling `update_triplet` or `retract_triplet`**: If the triplet `[#id]` is not already
      visible in the current chat context, call `query_kg` to look up its ID.
    *(Note: Do not re-query facts that were already loaded or recorded in the current chat window.)*

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
    SESSION-START & PERSONALIZATION MEMORY LOADER — Loads everything known about the user from
    previous sessions!

    WHEN TO CALL THIS TOOL:
    1. **At Session Start (Turn 1)**: Call this tool on your very first turn in a conversation—even
       if your first turn also calls `search_library` or `scholar_search`!
    2. **Before Tailoring Explanations or Paper Recommendations**: If you have not called
       `get_user_profile_summary` yet in the current chat session, call it before explaining complex
       concepts or suggesting papers so you respect the user's `Background`, `Preferences`, `Past mistakes`,
       `Arguments`, and `Reading history` (`has_read`).
    *(Once called in a session, its output stays in your chat context, so you do not need to call it
    again in the same session. Note: this tool only returns `User` edges; to inspect `Paper`, `Concept`,
    or `Topic` connections, call `get_related_facts` or `query_kg`.)*

    Returns the user's profile separated into:
    1. `# Global User Profile (Applies Across All Projects)` — permanent background, reading
       history, long-term interests, and general preferences.
    2. `# Current Project Profile (project: <name>)` — hypotheses, arguments, and preferences
       specific to the active OpenCode workspace.
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
    Searches entities in the Knowledge Graph by keyword or multi-word phrase (case-insensitive,
    with automatic multi-token fallback).

    WHEN TO CALL THIS TOOL:
    - When you want to browse existing `Topic`, `Paper`, `Concept`, or `Argument` entities in the KG
      (e.g. `search_entities(query="representation", entity_class="Concept")`) to reuse canonical
      entity names in `add_triplet` instead of creating near-duplicate nodes.
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
    TOPIC & LITERATURE GRAPH CONTEXT LOADER — Returns live facts within `depth` hops of a Topic,
    Paper, Concept, or Argument (walking edges in both directions, with automatic partial/keyword
    entity matching).

    WHEN TO CALL THIS TOOL:
    - **When Switching to or Exploring a Research Topic / Concept / Paper**: Remember that
      `get_user_profile_summary` only loads `User` facts—it does NOT load `Paper --covers--> Concept`,
      `Paper --references--> Paper`, `Topic --related_to--> Topic`, or `Argument --contradicts--> Argument`!
      Whenever the user asks about a topic, concept, or paper, call `get_related_facts(entity="<topic or paper>")`
      to recall literature connections and arguments recorded across past sessions.
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
