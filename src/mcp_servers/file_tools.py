from fastmcp import FastMCP
from typing import Optional
import json
import uuid

mcp = FastMCP("FileTools")

# Placeholder staging area memory for Phase 3 (in a real DB this would persist)
_STAGED_PAPERS = {}

@mcp.tool()
def stage_paper(url_or_arxiv_id: str, target_virtual_dir: str) -> str:
    """
    Downloads a paper, extracts its text, and generates a summary, placing it in a temporary staging area.
    Returns a 'temp_id' which must be passed to `confirm_save_paper` to finalize the save.
    
    CRITICAL INSTRUCTION: After calling this tool and receiving the summary, you MUST halt your execution,
    show the paper summary to the user in the chat, and explicitly ask if they want to save it. 
    You are FORBIDDEN from calling `confirm_save_paper` until the user replies affirmatively in the next chat turn.
    """
    # NOTE: Placeholder for Future Implementation
    # This should call read_online_document, write to a temp md file, call summarize_text, and store in _STAGED_PAPERS.
    temp_id = str(uuid.uuid4())
    _STAGED_PAPERS[temp_id] = {
        "url": url_or_arxiv_id,
        "dir": target_virtual_dir,
        "summary": "This is a placeholder summary for the staged paper."
    }
    
    return f"Paper staged successfully with ID: {temp_id}\n\nSummary: {_STAGED_PAPERS[temp_id]['summary']}\n\nSTOP EXECUTION NOW and ask the user for permission to use `confirm_save_paper`."

@mcp.tool()
def confirm_save_paper(temp_id: str) -> str:
    """
    Finalizes the saving of a staged paper into the local VTree library.
    Requires explicit user approval before invocation.
    """
    if temp_id not in _STAGED_PAPERS:
        return f"Error: No staged paper found with ID '{temp_id}'."
        
    paper_info = _STAGED_PAPERS.pop(temp_id)
    target_dir = paper_info["dir"]
    
    # NOTE: Placeholder for Future Implementation
    # This should write the document node into vtree.json, move the text to data/cache/texts, 
    # and call update_ancestor_summaries.
    
    return f"Successfully saved paper to '{target_dir}'. Ancestor directory summaries have been updated."
