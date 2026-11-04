import re

import pytest

from src.mcp_servers import kg_tools


@pytest.fixture(autouse=True)
def isolated_store(store, monkeypatch):
    """Point the MCP server at the per-test temp DB, never the real project DB."""
    monkeypatch.setattr(kg_tools, "_store", store)
    return store


def _id(text: str) -> int:
    return int(re.search(r"\[#(\d+)\]", text).group(1))


def test_add_triplet_defaults_subject_class_to_user():
    res = kg_tools.add_triplet(subject="User", relation="interested_in", object="Causal Inference", object_class="Topic")
    assert res.startswith("Recorded new fact")
    assert "User (User) --interested_in--> Causal Inference (Topic)" in res

    again = kg_tools.add_triplet(subject="User", relation="interested_in", object="causal inference", object_class="Topic")
    assert again.startswith("Fact already known; refreshed")
    assert _id(res) == _id(again)


def test_add_triplet_records_session_id():
    token = kg_tools.CURRENT_SESSION_ID.set("session-42")
    try:
        res = kg_tools.add_triplet(subject="User", relation="prefers", object="Python", object_class="Preference")
    finally:
        kg_tools.CURRENT_SESSION_ID.reset(token)
    assert kg_tools.get_store().get_triplet(_id(res)).source_chat == "session-42"


def test_add_triplet_errors_are_returned_not_raised():
    res = kg_tools.add_triplet(subject="User", relation="loves", object="X", object_class="Topic")
    assert res.startswith("Error: Unknown relation 'loves'")
    res = kg_tools.add_triplet(subject="User", relation="has_read", object="X", object_class="Topic")
    assert res.startswith("Error:") and "requires an object of class 'Paper'" in res


def test_add_triplet_with_object_properties():
    kg_tools.add_triplet(
        subject="User", relation="has_read", object="Attention Is All You Need", object_class="Paper",
        object_properties={"virtual_path": "/ML/attention"},
    )
    e = kg_tools.get_store().get_entity("Attention Is All You Need", "Paper")
    assert e.properties["virtual_path"] == "/ML/attention"


def test_query_kg():
    assert kg_tools.query_kg(subject="User") == "No matching facts found."
    kg_tools.add_triplet(subject="User", relation="interested_in", object="RL", object_class="Topic")
    kg_tools.add_triplet(subject="User", relation="interested_in", object="NLP", object_class="Topic")
    res = kg_tools.query_kg(subject="User", relation="interested_in")
    assert res.startswith("Found 2 fact(s)")
    assert "RL" in res and "NLP" in res


def test_update_and_retract_flow(clock):
    res = kg_tools.add_triplet(
        subject="User", relation="interested_in", object="GNNs", object_class="Topic", confidence=0.5
    )
    tid = _id(res)

    upd = kg_tools.update_triplet(tid, confidence=0.95, ttl_seconds=3600)
    assert "confidence=0.95" in upd and "expires=" in upd

    perm = kg_tools.update_triplet(tid, make_permanent=True)
    assert "expires=" not in perm

    assert kg_tools.update_triplet(tid, ttl_seconds=10, make_permanent=True).startswith("Error:")

    ret = kg_tools.retract_triplet(tid, "No longer interested.")
    assert "INACTIVE (No longer interested.)" in ret
    assert kg_tools.query_kg(object="GNNs") == "No matching facts found."
    assert "INACTIVE" in kg_tools.query_kg(object="GNNs", include_inactive=True)

    assert kg_tools.retract_triplet(tid, "again").startswith("Error:")
    assert kg_tools.update_triplet(9999, confidence=0.1).startswith("Error:")


def test_get_user_profile_summary_reaps_expired(clock, isolated_store):
    assert "No facts" in kg_tools.get_user_profile_summary()

    res = kg_tools.add_triplet(
        subject="User", relation="has_fact", object="Writing thesis", object_class="PersonalFact", ttl_seconds=60
    )
    kg_tools.add_triplet(subject="User", relation="interested_in", object="Optimization", object_class="Topic")
    summary = kg_tools.get_user_profile_summary()
    assert "## Background" in summary and "Writing thesis" in summary

    clock.advance(seconds=61)
    summary = kg_tools.get_user_profile_summary()
    assert "Writing thesis" not in summary and "Optimization" in summary
    assert isolated_store.get_triplet(_id(res)).invalidated_reason == "expired"


def test_search_entities_tool():
    assert "No entities" in kg_tools.search_entities("diffusion")
    kg_tools.add_triplet(subject="User", relation="interested_in", object="Diffusion Models", object_class="Topic")
    assert "- Diffusion Models (Topic)" in kg_tools.search_entities("diffusion")


def test_get_related_facts_tool():
    assert "No facts connected" in kg_tools.get_related_facts("RL")
    kg_tools.add_triplet(subject="User", relation="interested_in", object="RL", object_class="Topic")
    kg_tools.add_triplet(
        subject="RL", subject_class="Topic", relation="related_to", object="Control", object_class="Topic"
    )
    res = kg_tools.get_related_facts("Control", depth=10)  # depth is clamped to 3
    assert res.startswith("2 related fact(s)")


def test_get_ontology_and_extend():
    onto = kg_tools.get_ontology()
    assert "- has_read (User -> Paper)" in onto and "- Mistake:" in onto

    assert kg_tools.extend_ontology("class", "Dataset", "A dataset.") == "Added: class 'Dataset'."
    assert kg_tools.extend_ontology("class", "dataset", "dup") == "Already exists: class 'Dataset'."
    assert (
        kg_tools.extend_ontology("relation", "uses_dataset", "Paper uses dataset.", domain="Paper", range="Dataset")
        == "Added: relation 'uses_dataset' (Paper -> Dataset)."
    )
    assert kg_tools.extend_ontology("relation", "bad", "x", domain="Nope").startswith("Error: Unknown class 'Nope'")
    assert kg_tools.extend_ontology("widget", "x", "y").startswith("Error:")
    assert "- uses_dataset (Paper -> Dataset)" in kg_tools.get_ontology()


def test_mcp_dual_scope_workflow(isolated_store):
    isolated_store.default_project_id = "opencode-proj"

    res_global = kg_tools.add_triplet(
        subject="User", relation="has_fact", object="Prefers mathematical rigor",
        object_class="PersonalFact", scope="global",
    )
    assert "[GLOBAL]" in res_global

    res_proj = kg_tools.add_triplet(
        subject="User", relation="argued_that", object="Graph depth 2 is optimal here",
        object_class="Argument", scope="project",
    )
    assert "[PROJECT: opencode-proj]" in res_proj
    proj_tid = _id(res_proj)

    # Query only project scope
    only_proj = kg_tools.query_kg(subject="User", scope="project")
    assert "Graph depth 2 is optimal here" in only_proj
    assert "Prefers mathematical rigor" not in only_proj

    # Query only global scope
    only_glob = kg_tools.query_kg(subject="User", scope="global")
    assert "Prefers mathematical rigor" in only_glob
    assert "Graph depth 2 is optimal here" not in only_glob

    # Profile summary with scope='all' shows both sections
    prof_all = kg_tools.get_user_profile_summary(scope="all")
    assert "# Global User Profile" in prof_all
    assert "# Current Project Profile (project: opencode-proj)" in prof_all

    # Promote project fact to global via update_triplet
    promoted = kg_tools.update_triplet(proj_tid, scope="global")
    assert "[GLOBAL]" in promoted
    assert kg_tools.query_kg(subject="User", scope="project") == "No matching facts found."



def test_tools_registered_on_server():
    import asyncio

    tools = asyncio.run(kg_tools.mcp.list_tools()) if hasattr(kg_tools.mcp, "list_tools") else None
    if tools is None:
        pytest.skip("FastMCP version does not expose list_tools()")
    names = {t.name for t in tools}
    assert {
        "remember_user_fact", "add_triplet", "query_kg", "update_triplet", "retract_triplet",
        "get_user_profile_summary", "search_entities", "get_related_facts", "get_ontology", "extend_ontology",
    } <= names


def test_remember_user_fact_and_auto_inferred_add_triplet(isolated_store):
    """
    Verify:
    1. `remember_user_fact` maps friendly categories (`background`, `interest`, `preference`,
       `argument`, `mistake`, `read_paper`) to their ontology relations and classes without
       requiring `subject`, `subject_class`, or `object_class`.
    2. `argument` defaults to `scope="project"`, while enduring categories default to `scope="global"`.
    3. `add_triplet` automatically infers `subject_class` and `object_class` from `relation`
       when omitted (both for User relations and Paper->Concept relations).
    """
    isolated_store.default_project_id = "interp-proj"

    r_bg = kg_tools.remember_user_fact(
        category="background",
        fact="Graduate researcher focusing on LLM alignment",
        context="Mentioned at start of chat",
    )
    assert "[GLOBAL]" in r_bg
    assert "User (User) --has_fact--> Graduate researcher focusing on LLM alignment (PersonalFact)" in r_bg

    r_arg = kg_tools.remember_user_fact(
        category="argument",
        fact="Coarse grading rubrics cause reward hacking",
    )
    assert "[PROJECT: interp-proj]" in r_arg
    assert "User (User) --argued_that--> Coarse grading rubrics cause reward hacking (Argument)" in r_arg

    r_paper = kg_tools.remember_user_fact(
        category="read_paper",
        fact="The Platonic Representation Hypothesis",
        properties={"arxiv_id": "2405.07987"},
    )
    assert "[GLOBAL]" in r_paper
    assert "User (User) --has_read--> The Platonic Representation Hypothesis (Paper)" in r_paper

    # Auto-inferred classes on add_triplet when object_class and subject_class are omitted
    r_auto_user = kg_tools.add_triplet(
        relation="interested_in",
        object="Mechanistic Interpretability",
    )
    assert "User (User) --interested_in--> Mechanistic Interpretability (Topic)" in r_auto_user

    r_auto_lit = kg_tools.add_triplet(
        subject="The Platonic Representation Hypothesis",
        relation="covers",
        object="Representation Convergence",
    )
    assert (
        "The Platonic Representation Hypothesis (Paper) --covers--> Representation Convergence (Concept)"
        in r_auto_lit
    )


