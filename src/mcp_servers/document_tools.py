import json
from fastmcp import FastMCP
from typing import List, Optional
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).parent.parent.parent))

from config import settings
from src.vtree.store import VTreeStore
from src.vtree.operations import VTreeManager

mcp = FastMCP("DocumentTools")

# Helper to get active tree manager
def get_manager() -> VTreeManager:
    store = VTreeStore(settings.VTREE_PATH)
    vtree_dict = store.load_as_dict()
    return VTreeManager(vtree_dict)

@mcp.tool()
def get_dir_summary(virtual_path: str) -> str:
    """Gets the summary of a specific directory."""
    manager = get_manager()
    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found."
    if node.get("type") != "directory":
        return f"Error: '{virtual_path}' is not a directory."
    
    return node.get("summary", "No summary available.")

@mcp.tool()
def list_doc_summaries(virtual_path: str) -> str:
    """Lists summaries of all immediate items in a directory."""
    manager = get_manager()
    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found."
    if node.get("type") not in ("directory", "book"):
        return f"Error: '{virtual_path}' is not a directory or book."
        
    children = node.get("children", [])
    if not children:
        return "Empty."
        
    result = []
    for c in children:
        name = c.get("name", "unknown")
        summary = c.get("summary", "No summary.")
        result.append(f"- **{name}**: {summary}")
        
    return "\n".join(result)

@mcp.tool()
def list_tree(virtual_path: str, include_summaries: bool = False, max_depth: int = 3) -> str:
    """Lists the tree structure starting from the given path up to max_depth."""
    manager = get_manager()
    start_node = manager._get_node(virtual_path)
    if not start_node:
        return f"Error: Path '{virtual_path}' not found."
        
    def _walk(node, current_depth, prefix=""):
        if current_depth > max_depth:
            return ""
            
        res = f"{prefix}- {node.get('name', 'unnamed')} ({node.get('type')})"
        if include_summaries and node.get("summary"):
            res += f": {node.get('summary')}"
        res += "\n"
        
        if "children" in node:
            for child in node["children"]:
                res += _walk(child, current_depth + 1, prefix + "  ")
        return res
        
    return _walk(start_node, 1).strip()

@mcp.tool()
def get_document(virtual_path: str) -> str:
    """Gets the full text and headings of a local document by its virtual path."""
    manager = get_manager()
    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found."
    if node.get("type") not in ("document", "book"):
        return f"Error: '{virtual_path}' is not a document."
        
    text_path = node.get("text_path")
    if not text_path or not Path(text_path).exists():
        return f"Error: Text content not found for {virtual_path}. It may not have been extracted properly during cold start."
        
    try:
        content = Path(text_path).read_text(encoding="utf-8")
        return content
    except Exception as e:
        return f"Error reading document text: {e}"

@mcp.tool()
def get_documents(virtual_paths: List[str]) -> str:
    """Batch retrieve text for multiple documents."""
    results = []
    for vp in virtual_paths:
        res = get_document(vp)
        results.append(f"--- Document: {vp} ---\n{res}\n")
    return "\n".join(results)

@mcp.tool()
def search_library(query: str) -> str:
    """
    Instantly searches the entire local virtual library for documents, books, or directories 
    matching the given keyword query. The search is case-insensitive and matches against 
    the file name, DOI, and the LLM-generated summaries of every item.
    
    Use this tool FIRST before deciding to fetch a paper from the web. By searching 
    for keywords related to the paper (e.g., 'attention is all you need transformer'), 
    you can discover if the paper was already ingested under a different file name, 
    thus preventing duplicate downloads.
    
    Returns a formatted list of matches containing their virtual path, type, and summary.
    """
    manager = get_manager()
    results = manager.search_library(query)
    
    if not results:
        return f"No local documents found matching query: '{query}'"
        
    formatted = []
    for r in results:
        vpath = r.get("virtual_path", "unknown")
        rtype = r.get("type", "unknown")
        name = r.get("name", "unknown")
        summary = r.get("summary", "")
        formatted.append(f"Match Found [{rtype}]: '{name}'\nPath: {vpath}\nSummary: {summary}\n---")
        
    return "\n".join(formatted)

