import pytest
from src.mcp_servers.file_tools import stage_paper, confirm_save_paper, _STAGED_PAPERS

def test_stage_and_confirm_paper():
    # Clear state
    _STAGED_PAPERS.clear()
    
    # 1. Stage Paper
    stage_res = stage_paper("http://arxiv.org/123", "/papers")
    assert "Paper staged successfully" in stage_res
    assert "STOP EXECUTION" in stage_res
    
    # Extract temp_id from response (simple parse)
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
    from src.mcp_servers import file_tools
    from src.vtree.store import VTreeStore

    _STAGED_PAPERS.clear()
    staged_dir = tmp_path / "staged"
    cache_dir = tmp_path / "cache"
    proj_vtree = tmp_path / ".opencode" / "vtree.json"

    monkeypatch.setattr(settings, "STAGED_DIR", staged_dir)
    monkeypatch.setattr(settings, "CACHE_DIR", cache_dir)
    monkeypatch.setenv("RA_VTREE_PATH", str(proj_vtree))

    stage_res = file_tools.stage_paper(
        "https://arxiv.org/abs/1706.03762",
        "/Arch/Transformers",
        target_scope="project",
        title="Attention Is All You Need",
        preextracted_text="# Attention Is All You Need\nSelf-attention mechanism.",
        precomputed_summary="Introduces the Transformer.",
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

