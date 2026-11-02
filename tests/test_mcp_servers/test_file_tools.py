import pytest
from src.mcp_servers import file_tools
from src.mcp_servers.file_tools import stage_paper, confirm_save_paper, _STAGED_PAPERS


def test_stage_and_confirm_paper(monkeypatch, tmp_path):
    from config import settings
    from src.cold_start import refresher

    _STAGED_PAPERS.clear()
    monkeypatch.setattr(settings, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(tmp_path / "vtree.json"))
    monkeypatch.setattr(file_tools, "summarize_text", lambda txt: "Mock OpenRouter summary")
    monkeypatch.setattr(refresher, "summarize_directory", lambda sums: "Mock OpenRouter dir summary")

    # 1. Stage Paper
    stage_res = stage_paper("http://arxiv.org/123", "/papers", preextracted_text="Sample paper text")
    assert "Paper staged successfully" in stage_res
    assert "STOP EXECUTION" in stage_res

    import re
    match = re.search(r"ID:\s*([0-9a-fA-F-]+)", stage_res)
    assert match is not None
    temp_id = match.group(1)

    assert temp_id in _STAGED_PAPERS
    assert _STAGED_PAPERS[temp_id]["dir"] == "/papers"

    # 2. Confirm Save Paper
    confirm_res = confirm_save_paper(temp_id)
    assert "Successfully saved paper to '/papers'" in confirm_res
    assert temp_id not in _STAGED_PAPERS


def test_confirm_invalid_id():
    _STAGED_PAPERS.clear()
    res = confirm_save_paper("invalid-id")
    assert "Error: No staged paper found" in res


def test_stage_and_confirm_persists_to_project_vtree(monkeypatch, tmp_path):
    import re
    from config import settings
    from src.cold_start import refresher
    from src.vtree.store import VTreeStore

    _STAGED_PAPERS.clear()
    staged_dir = tmp_path / "staged"
    cache_dir = tmp_path / "cache"
    proj_vtree = tmp_path / ".opencode" / "vtree.json"

    monkeypatch.setattr(settings, "STAGED_DIR", staged_dir)
    monkeypatch.setattr(settings, "CACHE_DIR", cache_dir)
    monkeypatch.setenv("RA_VTREE_PATH", str(proj_vtree))
    monkeypatch.setattr(file_tools, "summarize_text", lambda txt: "Introduces the Transformer.")
    monkeypatch.setattr(refresher, "summarize_directory", lambda sums: "Directory of Transformer architecture papers.")

    stage_res = file_tools.stage_paper(
        "https://arxiv.org/abs/1706.03762",
        "/Arch/Transformers",
        target_scope="project",
        title="Attention Is All You Need",
        preextracted_text="# Attention Is All You Need\nSelf-attention mechanism.",
    )
    tid = re.search(r"ID:\s*([0-9a-fA-F-]+)", stage_res).group(1)

    # Simulate cross-process restart by clearing in-memory _STAGED_PAPERS;
    # confirm_save_paper must recover the manifest from disk!
    _STAGED_PAPERS.clear()
    assert (staged_dir / f"{tid}.json").exists()

    confirm_res = file_tools.confirm_save_paper(tid)
    assert "Successfully saved paper to '/Arch/Transformers'" in confirm_res
    assert not (staged_dir / f"{tid}.json").exists()
    assert proj_vtree.exists()

    vtree_data = VTreeStore(proj_vtree).load_as_dict()
    from src.vtree.operations import VTreeManager

    mgr = VTreeManager(vtree_data)
    doc = mgr._get_node("/Arch/Transformers/Attention Is All You Need")
    assert doc is not None
    assert doc["summary"] == "Introduces the Transformer."
    # Verify ancestor directory summaries were updated via OpenRouter (`summarize_directory`)
    parent_dir = mgr._get_node("/Arch/Transformers")
    assert parent_dir["summary"] == "Directory of Transformer architecture papers."


def test_stage_paper_tier1_arxiv_pdf_downloads_to_flat_folder(monkeypatch, tmp_path):
    """
    Verify Tier 1 (arXiv ID '2601.18533'):
    - Downloads the PDF into staging
    - Summarizes extracted text via OpenRouter `summarize_text`
    - On `confirm_save_paper`, moves the PDF into the single flat `<library_root>/Downloaded/` folder
      and updates parent directory summaries via `summarize_directory`.
    """
    import re
    import json
    from config import settings
    from src.cold_start import refresher
    from src.vtree.operations import VTreeManager
    from src.vtree.store import VTreeStore

    _STAGED_PAPERS.clear()
    lib_root = tmp_path / "my_library"
    lib_root.mkdir()
    vtree_path = tmp_path / "global_vtree.json"
    vtree_path.write_text(
        json.dumps(
            {
                "version": 1,
                "library_root": str(lib_root),
                "root": {"type": "directory", "name": "Root", "virtual_path": "/", "summary": "", "children": []},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(vtree_path))

    monkeypatch.setattr(
        file_tools,
        "_lookup_arxiv_api",
        lambda arxiv_id=None, title=None: {
            "title": "From Verifiable Dot to Reward Chain",
            "abstract": "We propose RLVRR for verifiable reward chaining.",
            "authors": ["Alice", "Bob"],
            "arxiv_id": "2601.18533",
            "pdf_url": "https://arxiv.org/pdf/2601.18533.pdf",
            "doi": "10.48550/arXiv.2601.18533",
        },
    )

    def fake_download(url: str, dest: pytest.TempPathFactory) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 fake pdf bytes")
        return True

    monkeypatch.setattr(file_tools, "_download_pdf_bytes", fake_download)
    monkeypatch.setattr(
        file_tools,
        "_extract_pdf_file",
        lambda p: ("# From Verifiable Dot to Reward Chain\nFull extracted paper text.", 12),
    )
    monkeypatch.setattr(
        file_tools,
        "summarize_text",
        lambda txt: "OpenRouter summary: Introduces RLVRR to chain verifiable rewards in RLHF.",
    )
    monkeypatch.setattr(
        refresher,
        "summarize_directory",
        lambda sums: "OpenRouter directory summary covering RLVRR reward chaining.",
    )

    res = file_tools.stage_paper("2601.18533", "/nlp/RLHF/RLVRR", target_scope="global")
    assert "tier1_arxiv_pdf" in res
    assert "OpenRouter summary: Introduces RLVRR" in res
    tid = re.search(r"ID:\s*([0-9a-fA-F-]+)", res).group(1)

    confirm_out = file_tools.confirm_save_paper(tid)
    expected_flat_pdf = lib_root / "Downloaded" / "From Verifiable Dot to Reward Chain.pdf"
    assert expected_flat_pdf.exists()
    assert str(expected_flat_pdf) in confirm_out

    vtree = VTreeStore(vtree_path).load_as_dict()
    mgr = VTreeManager(vtree)
    node = mgr._get_node("/nlp/RLHF/RLVRR/From Verifiable Dot to Reward Chain")
    assert node is not None
    assert node["real_path"] == str(expected_flat_pdf)
    assert node["page_count"] == 12
    assert node["file_type"] == "pdf"
    assert node["doi"] == "10.48550/arXiv.2601.18533"
    assert node["text_path"] is not None
    assert node["summary"] == "OpenRouter summary: Introduces RLVRR to chain verifiable rewards in RLHF."
    # Check parent directory summary is no longer "User created directory."
    dir_node = mgr._get_node("/nlp/RLHF/RLVRR")
    assert dir_node["summary"] == "OpenRouter directory summary covering RLVRR reward chaining."


def test_stage_paper_tier2_conference_page_and_tier4_fallback(monkeypatch, tmp_path):
    """
    Verify Tier 2 (conference URL `https://iclr.cc/virtual/2026/poster/10008552` resolved via OpenAlex/arXiv)
    and Tier 4 (fallback to rich abstract + metadata when no PDF is downloadable).
    """
    import re
    from config import settings

    _STAGED_PAPERS.clear()
    monkeypatch.setattr(settings, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")

    # Simulate conference page blocked or returning no PDF, and no PDF downloadable,
    # but OpenAlex returning the paper's abstract & authors (Tier 4 fallback)
    monkeypatch.setattr(file_tools, "_inspect_url_directly", lambda url, dest: {})
    monkeypatch.setattr(file_tools, "_lookup_arxiv_api", lambda arxiv_id=None, title=None: {})
    monkeypatch.setattr(
        file_tools,
        "_lookup_openalex",
        lambda title=None, doi=None: {
            "title": "Rubrics as Rewards",
            "abstract": "We use structured grading rubrics as dense reward signals for LLM alignment.",
            "authors": ["Carol", "Dave"],
            "pdf_url": None,
            "doi": "10.1000/iclr.2026.rubrics",
        },
    )
    monkeypatch.setattr(file_tools, "_lookup_semantic_scholar", lambda title=None, paper_id_or_doi=None: {})

    summarized_inputs = []

    def fake_summarize(txt: str) -> str:
        summarized_inputs.append(txt)
        return "OpenRouter summary: Structured grading rubrics provide dense rewards for RLHF."

    monkeypatch.setattr(file_tools, "summarize_text", fake_summarize)

    res = file_tools.stage_paper(
        "https://iclr.cc/virtual/2026/poster/10008552",
        "/nlp/RLHF/Rubrics as Rewards",
        title="Rubrics as Rewards",
    )
    assert "tier4_metadata_abstract_fallback" in res
    assert "OpenRouter summary: Structured grading rubrics" in res
    assert len(summarized_inputs) == 1
    assert "We use structured grading rubrics as dense reward signals" in summarized_inputs[0]
    tid = re.search(r"ID:\s*([0-9a-fA-F-]+)", res).group(1)
    assert _STAGED_PAPERS[tid]["text_path"] is not None



def test_refresh_library_indexes_only_newly_dropped_files(monkeypatch, tmp_path):
    """
    Verify that `refresh_library` / `refresh_vtree`:
    1. Skips files whose `real_path` is already present anywhere in `vtree.json`
       (even if moved into a custom semantic directory like `/Semantic/Categorized`).
    2. Extracts and summarizes only newly dropped files.
    3. Preserves existing nodes and custom virtual directories.
    4. Returns 'No new files found' on a subsequent run when no new files were added.
    """
    import json
    from config import settings
    from src.cold_start import refresher
    from src.mcp_servers import file_tools
    from src.vtree.operations import VTreeManager
    from src.vtree.store import VTreeStore

    lib_dir = tmp_path / "source_library"
    sub_dir = lib_dir / "SubTopic"
    sub_dir.mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    vtree_path = tmp_path / "global_vtree.json"

    monkeypatch.setattr(settings, "CACHE_DIR", cache_dir)
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(vtree_path))

    existing_file = lib_dir / "ExistingPaper.md"
    existing_file.write_text("# Existing Paper\nAlready indexed content.", encoding="utf-8")

    # Existing VTree where ExistingPaper was already organized into `/Semantic/MovedHere`
    initial_vtree = {
        "version": 1,
        "library_root": str(lib_dir),
        "root": {
            "type": "directory",
            "name": "Root",
            "virtual_path": "/",
            "summary": "Root summary",
            "children": [
                {
                    "type": "directory",
                    "name": "Semantic",
                    "virtual_path": "/Semantic",
                    "summary": "Custom semantic folder",
                    "children": [
                        {
                            "type": "document",
                            "name": "ExistingPaper",
                            "virtual_path": "/Semantic/ExistingPaper",
                            "real_path": str(existing_file),
                            "text_hash": "already_hashed_123",
                            "text_path": "/tmp/existing.md",
                            "summary": "Original summary of ExistingPaper.",
                        }
                    ],
                }
            ],
        },
    }
    vtree_path.write_text(json.dumps(initial_vtree, indent=2), encoding="utf-8")

    # Drop 2 new files into the source folder (one at root, one in SubTopic/)
    new_root_file = lib_dir / "NewPaperA.md"
    new_root_file.write_text("# New Paper A\nNovel sparse attention mechanism.", encoding="utf-8")
    new_sub_file = sub_dir / "NewPaperB.txt"
    new_sub_file.write_text("Reinforcement learning from AI feedback notes.", encoding="utf-8")

    summarize_calls = []

    def fake_summarize_text(text: str) -> str:
        summarize_calls.append(text)
        return f"Mock summary ({len(text)} chars)"

    monkeypatch.setattr(refresher, "summarize_text", fake_summarize_text)
    monkeypatch.setattr(
        refresher,
        "summarize_directory",
        lambda child_sums: f"Dir summary ({len(child_sums)} items)",
    )

    out1 = file_tools.refresh_library(scope="global")
    assert "Scanned: 3 | Already indexed: 1 | Newly added: 2" in out1
    assert "NewPaperA" in out1
    assert "NewPaperB" in out1
    # Only the 2 new files were summarized; ExistingPaper was skipped!
    assert len(summarize_calls) == 2

    # Check VTree structure preserved `/Semantic/ExistingPaper` and added the 2 new nodes
    updated_tree = VTreeStore(vtree_path).load_as_dict()
    mgr = VTreeManager(updated_tree)
    assert mgr._get_node("/Semantic/ExistingPaper") is not None
    assert mgr._get_node("/NewPaperA") is not None
    assert mgr._get_node("/SubTopic/NewPaperB") is not None

    # Second run with no new files should be a fast no-op
    out2 = file_tools.refresh_library(scope="global")
    assert "No new or inconsistent files found" in out2
    assert len(summarize_calls) == 2


def test_refresh_library_heals_corrupted_stub_nodes_and_user_created_dirs(monkeypatch, tmp_path):
    """
    Verify that `refresh_library` (and `ra-cli refresh`) automatically heals existing
    corrupted/inconsistent nodes (`summary: "Staged paper ..."`, `text_path: null`)
    and `"User created directory."` folder summaries without re-summarizing healthy nodes.
    """
    import json
    from config import settings
    from src.cold_start import refresher
    from src.mcp_servers import file_tools
    from src.vtree.operations import VTreeManager
    from src.vtree.store import VTreeStore

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    cache_dir = tmp_path / "cache"
    vtree_path = tmp_path / "vtree.json"

    monkeypatch.setattr(settings, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(settings, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(vtree_path))

    # VTree with 1 healthy node and the 2 corrupted stub nodes from the user's example
    inconsistent_vtree = {
        "version": 1,
        "library_root": str(lib_dir),
        "root": {
            "type": "directory",
            "name": "Root",
            "virtual_path": "/",
            "summary": "Root library overview.",
            "children": [
                {
                    "type": "document",
                    "name": "HealthyPaper",
                    "virtual_path": "/HealthyPaper",
                    "real_path": "/some/healthy.pdf",
                    "text_hash": "healthy123",
                    "text_path": "/some/cache/healthy123.md",
                    "summary": "Well-formed cold-start summary that must not be touched.",
                },
                {
                    "name": "RLVRR",
                    "virtual_path": "/RLVRR",
                    "summary": "User created directory.",
                    "type": "directory",
                    "children": [
                        {
                            "name": "From Verifiable Dot to Reward Chain",
                            "virtual_path": "/RLVRR/From Verifiable Dot to Reward Chain",
                            "summary": "Staged paper 'From Verifiable Dot to Reward Chain' from 2601.18533.",
                            "type": "document",
                            "real_path": "2601.18533",
                            "page_count": None,
                            "file_type": "pdf",
                            "doi": None,
                            "text_hash": None,
                            "text_path": None,
                        }
                    ],
                },
            ],
        },
    }
    vtree_path.write_text(json.dumps(inconsistent_vtree, indent=2), encoding="utf-8")

    monkeypatch.setattr(
        file_tools,
        "_resolve_paper_content",
        lambda url_or_arxiv_id, temp_id, title=None, doi=None, preextracted_text=None: {
            "title": title or "From Verifiable Dot to Reward Chain",
            "doi": "10.48550/arXiv.2601.18533",
            "text": "# From Verifiable Dot to Reward Chain\nResolved full paper text.",
            "pdf_path": None,
            "page_count": 14,
            "resolution_tier": "tier1_arxiv_pdf",
        },
    )

    doc_sum_calls = []
    monkeypatch.setattr(
        refresher,
        "summarize_text",
        lambda txt: (doc_sum_calls.append(txt) or "Healed OpenRouter summary for RLVRR paper."),
    )
    monkeypatch.setattr(
        refresher,
        "summarize_directory",
        lambda sums: "Healed OpenRouter directory overview for RLVRR.",
    )

    out = file_tools.refresh_library(scope="global")
    assert "Repaired: 1" in out
    assert "[REPAIRED] /RLVRR/From Verifiable Dot to Reward Chain" in out
    # HealthyPaper was NOT re-summarized; only the 1 broken node was summarized!
    assert len(doc_sum_calls) == 1

    healed = VTreeStore(vtree_path).load_as_dict()
    mgr = VTreeManager(healed)
    healthy = mgr._get_node("/HealthyPaper")
    assert healthy["summary"] == "Well-formed cold-start summary that must not be touched."

    repaired_doc = mgr._get_node("/RLVRR/From Verifiable Dot to Reward Chain")
    assert repaired_doc["summary"] == "Healed OpenRouter summary for RLVRR paper."
    assert repaired_doc["text_path"] is not None
    assert repaired_doc["text_hash"] is not None
    assert repaired_doc["page_count"] == 14
    assert repaired_doc["doi"] == "10.48550/arXiv.2601.18533"

    repaired_dir = mgr._get_node("/RLVRR")
    assert repaired_dir["summary"] == "Healed OpenRouter directory overview for RLVRR."



