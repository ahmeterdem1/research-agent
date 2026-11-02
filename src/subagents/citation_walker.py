"""
Citation Graph traversal helpers.

In the OpenCode architecture, deep multi-hop citation walks are driven by the
native ``@citation-walker`` subagent (``.opencode/agents/citation-walker.md``),
which calls ``get_paper_references`` and ``get_paper_citations`` exposed by
``search_tools.py`` in its own isolated context window.
"""

import requests

from config.settings import get_logger

logger = get_logger(__name__)


def _normalize_paper_id(paper_id_or_doi: str) -> str:
    val = paper_id_or_doi.strip()
    if "/" in val and not val.upper().startswith("DOI:") and not val.upper().startswith("ARXIV:"):
        return f"DOI:{val}"
    return val


def get_references(paper_id: str, limit: int = 10) -> list[dict]:
    """Gets papers cited BY the given paper (backward citation search)."""
    try:
        query_id = _normalize_paper_id(paper_id)
        url = f"https://api.semanticscholar.org/graph/v1/paper/{query_id}"
        res = requests.get(
            url,
            params={"fields": "references,references.title,references.paperId,references.year,references.citationCount"},
            timeout=10,
        )
        if res.status_code == 429:
            logger.warning("Semantic Scholar 429 rate limit in get_references(%s)", paper_id)
            return []
        res.raise_for_status()
        paper = res.json()
        return [
            {
                "id": r.get("paperId"),
                "title": r.get("title"),
                "year": r.get("year"),
                "citationCount": r.get("citationCount"),
            }
            for r in paper.get("references", [])[:limit]
            if r.get("paperId")
        ]
    except Exception as e:
        logger.warning("get_references failed for %s: %s", paper_id, e)
        return []


def get_citations(paper_id: str, limit: int = 10) -> list[dict]:
    """Gets papers citing the given paper (forward citation search)."""
    try:
        query_id = _normalize_paper_id(paper_id)
        url = f"https://api.semanticscholar.org/graph/v1/paper/{query_id}"
        res = requests.get(
            url,
            params={"fields": "citations,citations.title,citations.paperId,citations.year,citations.citationCount"},
            timeout=10,
        )
        if res.status_code == 429:
            logger.warning("Semantic Scholar 429 rate limit in get_citations(%s)", paper_id)
            return []
        res.raise_for_status()
        paper = res.json()
        return [
            {
                "id": c.get("paperId"),
                "title": c.get("title"),
                "year": c.get("year"),
                "citationCount": c.get("citationCount"),
            }
            for c in paper.get("citations", [])[:limit]
            if c.get("paperId")
        ]
    except Exception as e:
        logger.warning("get_citations failed for %s: %s", paper_id, e)
        return []


def walk_citation_graph(start_doi_or_id: str, depth: int = 2) -> str:
    """
    1-hop summary helper (retained for direct tool calls and backward compatibility;
    multi-hop exploration in OpenCode is performed by the @citation-walker subagent).
    """
    refs = get_references(start_doi_or_id, limit=5)
    cits = get_citations(start_doi_or_id, limit=5)

    lines = [f"Citation Walk Summary for {start_doi_or_id}:"]
    if refs:
        lines.append("\nBackward References (cited by this paper):")
        for r in refs:
            lines.append(f"- [{r['id']}] {r.get('title', 'Unknown')}")
    if cits:
        lines.append("\nForward Citations (papers citing this paper):")
        for c in cits:
            lines.append(f"- [{c['id']}] {c.get('title', 'Unknown')}")
    if not refs and not cits:
        lines.append("- No citations or references retrieved (or rate-limited).")

    return "\n".join(lines)
