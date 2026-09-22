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
