from src.kg.reasoning import format_user_profile, get_topic_context, get_user_profile


def test_empty_profile(store):
    assert get_user_profile(store) == {}
    assert "No facts" in format_user_profile({})


def test_profile_groups_and_orders_sections(store):
    store.add_triplet("User", "User", "has_read", "Attention Is All You Need", "Paper")
    store.add_triplet("User", "User", "interested_in", "Transformers", "Topic", context="Asked about attention.")
    store.add_triplet("User", "User", "has_fact", "PhD student", "PersonalFact")
    store.add_triplet("User", "User", "made_mistake", "Thinks BERT is autoregressive", "Mistake", confidence=0.7)
    # Non-user facts must not leak into the profile.
    store.add_triplet("Transformers", "Topic", "related_to", "Attention", "Topic")

    profile = get_user_profile(store)
    assert set(profile) == {"has_read", "interested_in", "has_fact", "made_mistake"}

    text = format_user_profile(profile)
    order = [text.index(h) for h in ("## Background", "## Interests", "## Past mistakes", "## Reading history")]
    assert order == sorted(order)
    assert "Transformers — Asked about attention." in text
    assert "(confidence 0.70)" in text
    assert "Attention Is All You Need" in text
    assert "related_to" not in text


def test_profile_excludes_expired_and_retracted(store, clock):
    store.add_triplet("User", "User", "prefers", "Concise answers", "Preference", ttl_seconds=60)
    t, _ = store.add_triplet("User", "User", "argued_that", "P = NP", "Argument")
    store.add_triplet("User", "User", "interested_in", "Complexity", "Topic")

    store.retract_triplet(t.id, "Joke.")
    clock.advance(seconds=120)

    profile = get_user_profile(store)
    assert set(profile) == {"interested_in"}


def test_profile_renders_llm_defined_relations(store):
    store.add_class("Lab", "A research lab.")
    store.add_relation("works_at", "Employment.", domain="User", range_="Lab")
    store.add_triplet("User", "User", "works_at", "DeepMind", "Lab")
    text = format_user_profile(get_user_profile(store))
    assert "## Other: works_at" in text and "DeepMind" in text


def test_profile_shows_expiry(store):
    store.add_triplet("User", "User", "has_fact", "On vacation", "PersonalFact", ttl_seconds=3600)
    assert "(until 2026-01-01 13:00:00 UTC)" in format_user_profile(get_user_profile(store))


def test_profile_limit_per_relation(store):
    for i in range(5):
        store.add_triplet("User", "User", "interested_in", f"Topic {i}", "Topic")
    profile = get_user_profile(store, limit_per_relation=3)
    assert len(profile["interested_in"]) == 3


def test_topic_context(store):
    store.add_triplet("User", "User", "interested_in", "RL", "Topic")
    store.add_triplet("Sutton & Barto", "Paper", "covers", "TD Learning", "Concept")
    store.add_triplet("User", "User", "has_read", "Sutton & Barto", "Paper")
    facts = get_topic_context(store, "Sutton & Barto", depth=2)
    assert {t.relation for t in facts} == {"covers", "has_read", "interested_in"}


def test_dual_scope_profile_formatting(store):
    store.add_triplet("User", "User", "has_fact", "ML Researcher", "PersonalFact", scope="global")
    store.add_triplet(
        "User", "User", "argued_that", "Local hypothesis A", "Argument",
        scope="project", project_id="repo-alpha",
    )
    store.add_triplet(
        "User", "User", "argued_that", "Other repo hypothesis", "Argument",
        scope="project", project_id="repo-beta",
    )

    profile = get_user_profile(store, scope="all", project_id="repo-alpha")
    text = format_user_profile(profile)
    assert "# Global User Profile (Applies Across All Projects)" in text
    assert "# Current Project Profile (project: repo-alpha)" in text
    assert "[GLOBAL] ML Researcher" in text
    assert "[PROJECT: repo-alpha] Local hypothesis A" in text
    assert "Other repo hypothesis" not in text


