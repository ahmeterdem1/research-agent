import sqlite3

import pytest

from src.db.migrations import init_db
from src.kg.schema import BASE_CLASSES, BASE_RELATIONS, SCHEMA_VERSION
from src.kg.store import KGError


# --------------------------------------------------------------------------- #
# Schema & seeding
# --------------------------------------------------------------------------- #
def test_init_db_seeds_base_ontology(store):
    class_names = {c.name for c in store.list_classes()}
    relation_names = {r.name for r in store.list_relations()}
    assert class_names == {name for name, _ in BASE_CLASSES}
    assert relation_names == {r[0] for r in BASE_RELATIONS}
    assert store.get_entity("User", "User") is not None


def test_init_db_is_idempotent(store):
    init_db(store.db_path)
    init_db(store.db_path)
    assert len(store.list_classes()) == len(BASE_CLASSES)
    assert len(store.list_relations()) == len(BASE_RELATIONS)
    assert len(store.search_entities("User")) == 1

    conn = sqlite3.connect(store.db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Triplet insertion & validation
# --------------------------------------------------------------------------- #
def test_add_and_query_triplet(store):
    t, created = store.add_triplet(
        "User", "User", "interested_in", "Reinforcement Learning", "Topic",
        confidence=0.9, context="Asked for RL papers.", source_chat="sess-1",
    )
    assert created
    assert t.subject == "User" and t.object == "Reinforcement Learning"
    assert t.confidence == 0.9 and t.source_chat == "sess-1"
    assert t.expires_at is None and t.is_active

    results = store.query(subject="User", relation="interested_in")
    assert [r.id for r in results] == [t.id]


def test_names_are_case_insensitive(store):
    t1, _ = store.add_triplet("User", "User", "interested_in", "Reinforcement Learning", "Topic")
    t2, created = store.add_triplet("user", "user", "INTERESTED_IN", "reinforcement learning", "topic")
    assert not created
    assert t1.id == t2.id
    # Canonical (first-seen) spelling is preserved.
    assert t2.object == "Reinforcement Learning"
    assert len(store.search_entities("reinforcement")) == 1


def test_reasserting_refreshes_instead_of_duplicating(store, clock):
    t1, _ = store.add_triplet("User", "User", "prefers", "PyTorch", "Preference", confidence=0.6)
    clock.advance(minutes=5)
    t2, created = store.add_triplet(
        "User", "User", "prefers", "PyTorch", "Preference", confidence=0.95, context="Said so explicitly."
    )
    assert not created
    assert t2.id == t1.id
    assert t2.confidence == 0.95
    assert t2.context == "Said so explicitly."
    assert t2.updated_at > t1.updated_at
    assert t2.created_at == t1.created_at


def test_unknown_relation_lists_alternatives(store):
    with pytest.raises(KGError, match="Unknown relation 'likes'.*interested_in"):
        store.add_triplet("User", "User", "likes", "Cats", "Topic")


def test_unknown_class_rejected(store):
    with pytest.raises(KGError, match="Unknown class 'Dataset'"):
        store.add_triplet("User", "User", "interested_in", "ImageNet", "Dataset")


def test_domain_and_range_enforced(store):
    with pytest.raises(KGError, match="requires a subject of class 'User'"):
        store.add_triplet("Attention Is All You Need", "Paper", "has_read", "Other Paper", "Paper")
    with pytest.raises(KGError, match="requires an object of class 'Paper'"):
        store.add_triplet("User", "User", "has_read", "Transformers", "Topic")


def test_invalid_confidence_and_ttl(store):
    with pytest.raises(KGError, match="confidence"):
        store.add_triplet("User", "User", "prefers", "Vim", "Preference", confidence=1.5)
    with pytest.raises(KGError, match="ttl_seconds"):
        store.add_triplet("User", "User", "prefers", "Vim", "Preference", ttl_seconds=0)


def test_failed_insert_does_not_leave_orphan_entities(store):
    with pytest.raises(KGError):
        store.add_triplet("User", "User", "has_read", "Ghost Topic", "Topic")
    assert store.search_entities("Ghost Topic") == []


def test_entity_properties_are_merged(store):
    store.add_triplet(
        "User", "User", "has_read", "Attention Is All You Need", "Paper",
        object_properties={"doi": "10.48550/arXiv.1706.03762"},
    )
    store.add_triplet(
        "User", "User", "has_read", "Attention Is All You Need", "Paper",
        object_properties={"virtual_path": "/ML/Attention Is All You Need"},
    )
    e = store.get_entity("Attention Is All You Need", "Paper")
    assert e.properties == {
        "doi": "10.48550/arXiv.1706.03762",
        "virtual_path": "/ML/Attention Is All You Need",
    }


def test_same_name_different_class_are_distinct(store):
    store.add_triplet("User", "User", "interested_in", "Transformers", "Topic")
    store.add_triplet("Attention Is All You Need", "Paper", "covers", "Transformers", "Concept")
    classes = {e.entity_class for e in store.search_entities("Transformers")}
    assert classes == {"Topic", "Concept"}


# --------------------------------------------------------------------------- #
# Temporal behaviour (TTL)
# --------------------------------------------------------------------------- #
def test_ttl_fact_disappears_after_expiry_without_reaper(store, clock):
    t, _ = store.add_triplet(
        "User", "User", "has_fact", "Preparing NeurIPS submission", "PersonalFact", ttl_seconds=3600
    )
    assert t.expires_at == "2026-01-01 13:00:00"
    assert len(store.query(subject="User", relation="has_fact")) == 1

    clock.advance(minutes=59)
    assert len(store.query(subject="User", relation="has_fact")) == 1

    clock.advance(minutes=1)  # exactly at expiry -> no longer live
    assert store.query(subject="User", relation="has_fact") == []
    # Still physically present until reaped.
    assert store.get_triplet(t.id).is_active


def test_reaper_soft_deletes_expired_facts(store, clock):
    expiring, _ = store.add_triplet("User", "User", "prefers", "Short answers", "Preference", ttl_seconds=60)
    permanent, _ = store.add_triplet("User", "User", "prefers", "LaTeX math", "Preference")

    assert store.reap_expired() == 0
    clock.advance(seconds=61)
    assert store.reap_expired() == 1
    assert store.reap_expired() == 0  # idempotent

    reaped = store.get_triplet(expiring.id)
    assert not reaped.is_active and reaped.invalidated_reason == "expired"
    assert store.get_triplet(permanent.id).is_active

    history = store.query(subject="User", relation="prefers", active_only=False)
    assert {t.id for t in history} == {expiring.id, permanent.id}


def test_reasserting_after_expiry_creates_new_fact(store, clock):
    t1, _ = store.add_triplet("User", "User", "interested_in", "Diffusion Models", "Topic", ttl_seconds=60)
    clock.advance(seconds=120)
    t2, created = store.add_triplet("User", "User", "interested_in", "Diffusion Models", "Topic")
    assert created and t2.id != t1.id
    assert [t.id for t in store.query(object_="Diffusion Models")] == [t2.id]


def test_reasserting_permanent_fact_with_ttl_sets_expiry(store):
    store.add_triplet("User", "User", "prefers", "Dark mode", "Preference")
    t, _ = store.add_triplet("User", "User", "prefers", "Dark mode", "Preference", ttl_seconds=10)
    assert t.ttl_seconds == 10 and t.expires_at is not None


# --------------------------------------------------------------------------- #
# Update & retract
# --------------------------------------------------------------------------- #
def test_update_triplet_fields(store, clock):
    t, _ = store.add_triplet("User", "User", "interested_in", "GNNs", "Topic", confidence=0.5)
    updated = store.update_triplet(t.id, confidence=0.9, context="Confirmed by user.")
    assert updated.confidence == 0.9 and updated.context == "Confirmed by user."
    assert updated.expires_at is None

    with_ttl = store.update_triplet(t.id, ttl_seconds=600)
    assert with_ttl.expires_at == "2026-01-01 12:10:00"

    permanent = store.update_triplet(t.id, ttl_seconds=None)
    assert permanent.expires_at is None and permanent.ttl_seconds is None
    assert permanent.confidence == 0.9  # untouched fields preserved


def test_update_rejects_missing_or_inactive(store, clock):
    with pytest.raises(KGError, match="No triplet"):
        store.update_triplet(9999, confidence=0.5)

    t, _ = store.add_triplet("User", "User", "prefers", "Tabs", "Preference", ttl_seconds=5)
    clock.advance(seconds=10)
    with pytest.raises(KGError, match="no longer active"):
        store.update_triplet(t.id, confidence=0.5)


def test_retract_triplet_keeps_history(store):
    t, _ = store.add_triplet("User", "User", "argued_that", "Scaling is all you need", "Argument")
    retracted = store.retract_triplet(t.id, "User changed their mind.")
    assert not retracted.is_active
    assert retracted.invalidated_reason == "User changed their mind."
    assert store.query(subject="User", relation="argued_that") == []
    assert len(store.query(subject="User", relation="argued_that", active_only=False)) == 1

    with pytest.raises(KGError, match="already inactive"):
        store.retract_triplet(t.id, "again")
    with pytest.raises(KGError, match="No triplet"):
        store.retract_triplet(9999, "missing")


# --------------------------------------------------------------------------- #
# Queries, search & traversal
# --------------------------------------------------------------------------- #
def test_query_filters(store):
    store.add_triplet("User", "User", "interested_in", "RL", "Topic", confidence=0.4)
    store.add_triplet("User", "User", "interested_in", "NLP", "Topic", confidence=0.9)
    store.add_triplet("RL", "Topic", "related_to", "Control Theory", "Topic")

    assert {t.object for t in store.query(relation="interested_in")} == {"RL", "NLP"}
    assert {t.object for t in store.query(relation="interested_in", min_confidence=0.5)} == {"NLP"}
    assert {t.relation for t in store.query(object_="RL")} == {"interested_in"}
    assert {t.relation for t in store.query(subject="RL")} == {"related_to"}
    assert len(store.query(subject_class="Topic", object_class="Topic")) == 1
    assert len(store.query(limit=1)) == 1


def test_search_entities(store):
    store.add_triplet("User", "User", "interested_in", "Graph Neural Networks", "Topic")
    store.add_triplet("User", "User", "has_read", "Graph Attention Networks", "Paper")
    assert {e.name for e in store.search_entities("graph")} == {"Graph Neural Networks", "Graph Attention Networks"}
    assert [e.name for e in store.search_entities("graph", entity_class="Paper")] == ["Graph Attention Networks"]


def test_neighbourhood_respects_depth_and_liveness(store, clock):
    store.add_triplet("User", "User", "interested_in", "RL", "Topic")
    store.add_triplet("RL", "Topic", "related_to", "Control Theory", "Topic")
    store.add_triplet("Control Theory", "Topic", "related_to", "Optimization", "Topic")
    store.add_triplet("RL", "Topic", "related_to", "Bandits", "Topic", ttl_seconds=10)

    depth1 = {(t.subject, t.object) for t in store.neighbourhood("RL", depth=1)}
    assert depth1 == {("User", "RL"), ("RL", "Control Theory"), ("RL", "Bandits")}

    depth2 = {(t.subject, t.object) for t in store.neighbourhood("RL", depth=2)}
    assert ("Control Theory", "Optimization") in depth2

    clock.advance(seconds=20)
    after = {(t.subject, t.object) for t in store.neighbourhood("RL", depth=1)}
    assert ("RL", "Bandits") not in after

    assert store.neighbourhood("Nonexistent") == []


# --------------------------------------------------------------------------- #
# Ontology extension
# --------------------------------------------------------------------------- #
def test_extend_ontology_and_use_it(store):
    cls, created = store.add_class("Dataset", "A benchmark or training dataset.")
    assert created and cls.created_by == "llm"
    _, created_again = store.add_class("dataset", "dup")
    assert not created_again

    rel, created = store.add_relation("uses_dataset", "Paper uses dataset.", domain="paper", range_="Dataset")
    assert created and rel.domain == "Paper" and rel.range == "Dataset"

    t, _ = store.add_triplet("ResNet", "Paper", "uses_dataset", "ImageNet", "Dataset")
    assert t.relation == "uses_dataset"

    with pytest.raises(KGError, match="requires a subject of class 'Paper'"):
        store.add_triplet("User", "User", "uses_dataset", "ImageNet", "Dataset")


def test_relation_without_domain_accepts_any_class(store):
    store.add_relation("mentions", "Generic mention.")
    store.add_triplet("User", "User", "mentions", "Transformers", "Concept")
    store.add_triplet("Some Paper", "Paper", "mentions", "Transformers", "Concept")
    assert len(store.query(relation="mentions")) == 2


def test_add_relation_with_unknown_domain_fails(store):
    with pytest.raises(KGError, match="Unknown class 'Lab'"):
        store.add_relation("works_at", "Employment.", domain="User", range_="Lab")
    with pytest.raises(KGError, match="must not be empty"):
        store.add_class("  ", "blank")


def test_persistence_across_store_instances(store, clock):
    from src.kg.store import KGStore

    store.add_triplet("User", "User", "interested_in", "Causality", "Topic")
    reopened = KGStore(store.db_path, now_fn=clock)
    assert [t.object for t in reopened.query(subject="User")] == ["Causality"]

