"""
Higher-level KG reasoning: dual-scope (global + project-local) user-profile
aggregation and topic context.

``format_user_profile`` renders global facts and project-local facts in
clearly separated sections so the LLM immediately sees which facts apply
across all projects versus only within the current OpenCode project.
"""

from collections import defaultdict
from typing import Optional

from src.kg.models import Triplet
from src.kg.schema import USER_CLASS_NAME, USER_ENTITY_NAME
from src.kg.store import KGStore

# Display order and headings for well-known user relations. Relations added
# later by the LLM fall into "Other" under their own name.
PROFILE_SECTIONS: list[tuple[str, str]] = [
    ("has_fact", "Background"),
    ("interested_in", "Interests"),
    ("prefers", "Preferences"),
    ("argued_that", "Arguments"),
    ("made_mistake", "Past mistakes"),
    ("has_read", "Reading history"),
]


def get_user_profile(
    store: KGStore,
    min_confidence: float = 0.0,
    limit_per_relation: int = 25,
    scope: str = "all",
    project_id: Optional[str] = None,
) -> dict[str, list[Triplet]]:
    """
    Return live facts where the User is the subject, grouped by relation.
    By default ``scope='all'`` fetches both global facts and the active project's facts.
    """
    facts = store.query(
        subject=USER_ENTITY_NAME,
        subject_class=USER_CLASS_NAME,
        active_only=True,
        min_confidence=min_confidence,
        scope=scope,
        project_id=project_id,
        limit=1000,
    )
    grouped: dict[str, list[Triplet]] = defaultdict(list)
    for t in facts:
        if len(grouped[t.relation]) < limit_per_relation:
            grouped[t.relation].append(t)
    return dict(grouped)


def _render_scope_block(
    grouped: dict[str, list[Triplet]],
    heading_prefix: str = "",
) -> list[str]:
    lines: list[str] = []
    known = {rel for rel, _ in PROFILE_SECTIONS}

    def render(heading: str, triplets: list[Triplet]) -> None:
        full_heading = f"{heading_prefix}{heading}" if heading_prefix else heading
        lines.append(f"## {full_heading}")
        for t in triplets:
            entry = f"- [#{t.id}] {t.scope_tag()} {t.object}"
            if t.context:
                entry += f" — {t.context}"
            if t.confidence < 1.0:
                entry += f" (confidence {t.confidence:.2f})"
            if t.expires_at:
                entry += f" (until {t.expires_at} UTC)"
            lines.append(entry)
        lines.append("")

    for rel, heading in PROFILE_SECTIONS:
        if grouped.get(rel):
            render(heading, grouped[rel])
    for rel, triplets in grouped.items():
        if rel not in known and triplets:
            render(f"Other: {rel}", triplets)

    return lines


def format_user_profile(profile: dict[str, list[Triplet]]) -> str:
    """
    Render the user profile into Markdown, separating Global User Profile facts
    from Current Project Profile facts when both scopes are present.
    """
    if not profile:
        return "No facts about the user have been recorded yet."

    global_grouped: dict[str, list[Triplet]] = defaultdict(list)
    project_grouped: dict[str, dict[str, list[Triplet]]] = defaultdict(lambda: defaultdict(list))

    for rel, triplets in profile.items():
        for t in triplets:
            if t.scope == "project":
                pid = t.project_id or "current"
                project_grouped[pid][rel].append(t)
            else:
                global_grouped[rel].append(t)

    lines: list[str] = []
    if global_grouped:
        lines.append("# Global User Profile (Applies Across All Projects)")
        lines.extend(_render_scope_block(global_grouped))

    for pid, p_grouped in project_grouped.items():
        lines.append(f"# Current Project Profile (project: {pid})")
        # Use "Project " prefix on H2 headings only when Global sections are also rendered,
        # so section headings remain unambiguous.
        prefix = "Project " if global_grouped else ""
        lines.extend(_render_scope_block(p_grouped, heading_prefix=prefix))

    return "\n".join(lines).rstrip()


def get_topic_context(
    store: KGStore,
    entity: str,
    depth: int = 2,
    scope: str = "all",
    project_id: Optional[str] = None,
) -> list[Triplet]:
    """Live facts within ``depth`` hops of an entity (e.g. a Topic or Paper)."""
    return store.neighbourhood(entity, depth=depth, scope=scope, project_id=project_id)
