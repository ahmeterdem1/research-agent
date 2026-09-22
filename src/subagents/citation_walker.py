import json
from semanticscholar import SemanticScholar
# NOTE: Placeholder for Future Implementation
# In Phase 5, this will likely be a true conversational subagent with its own system prompt,
# powered by the Orchestrator, that uses get_references/get_citations tools iteratively.
# For now, this is just a stub that mimics the planned traversal tools.

import requests

def get_references(paper_id: str, limit: int = 10) -> list:
    """Gets papers cited BY the given paper (backward search)."""
    try:
        url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
        res = requests.get(url, params={"fields": "references,references.title,references.paperId"}, timeout=10)
        if res.status_code == 429:
            return []
        res.raise_for_status()
        paper = res.json()
        return [{"id": r.get("paperId"), "title": r.get("title")} for r in paper.get("references", [])[:limit] if r.get("paperId")]
    except:
        return []

def get_citations(paper_id: str, limit: int = 10) -> list:
    """Gets papers citing the given paper (forward search)."""
    try:
        url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
        res = requests.get(url, params={"fields": "citations,citations.title,citations.paperId"}, timeout=10)
        if res.status_code == 429:
            return []
        res.raise_for_status()
        paper = res.json()
        return [{"id": c.get("paperId"), "title": c.get("title")} for c in paper.get("citations", [])[:limit] if c.get("paperId")]
    except:
        return []

def filter_influential(papers: list) -> list:
    """Placeholder tool: Ask LLM to filter a list of references based on a criteria."""
    pass

def walk_citation_graph(start_doi_or_id: str, depth: int = 2) -> str:
    """
    Kicks off the autonomous subagent traversal.
    Returns the final annotated path summary to the main agent.
    """
    # NOTE: In the future, this will spawn an LLM loop. For now, it's a mock return.
    return f"Citation Walk Summary for {start_doi_or_id}:\n- Found 5 highly relevant papers in the graph.\n- (Future implementation will contain detailed LLM-curated bibliography)."
