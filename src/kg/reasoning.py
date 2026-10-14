"""
Higher-level KG reasoning: user-profile aggregation and topic context.

``format_user_profile`` produces the compact text block that the Phase 5
dynamic system prompt will inject, so it is intentionally terse.
"""

from collections import defaultdict

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


def get_user_profile(store: KGStore, min_confidence: float = 0.0, limit_per_relation: int = 25) -> dict[str, list[Triplet]]:
    """Return live facts where the User is the subject, grouped by relation."""
    facts = store.query(
        subject=USER_ENTITY_NAME,
        subject_class=USER_CLASS_NAME,
        active_only=True,
        min_confidence=min_confidence,
        limit=1000,
    )
    grouped: dict[str, list[Triplet]] = defaultdict(list)
    for t in facts:
        if len(grouped[t.relation]) < limit_per_relation:
            grouped[t.relation].append(t)
    return dict(grouped)


def format_user_profile(profile: dict[str, list[Triplet]]) -> str:
    if not profile:
        return "No facts about the user have been recorded yet."

    lines: list[str] = []
    known = {rel for rel, _ in PROFILE_SECTIONS}

    def render(heading: str, triplets: list[Triplet]) -> None:
        lines.append(f"## {heading}")
        for t in triplets:
            entry = f"- [#{t.id}] {t.object}"
            if t.context:
                entry += f" — {t.context}"
            if t.confidence < 1.0:
                entry += f" (confidence {t.confidence:.2f})"
            if t.expires_at:
                entry += f" (until {t.expires_at} UTC)"
            lines.append(entry)
        lines.append("")

    for rel, heading in PROFILE_SECTIONS:
        if profile.get(rel):
            render(heading, profile[rel])
    for rel, triplets in profile.items():
        if rel not in known and triplets:
            render(f"Other: {rel}", triplets)

    return "\n".join(lines).rstrip()


def get_topic_context(store: KGStore, entity: str, depth: int = 2) -> list[Triplet]:
    """Live facts within ``depth`` hops of an entity (e.g. a Topic or Paper)."""
    return store.neighbourhood(entity, depth=depth)

