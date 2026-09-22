from fastmcp import FastMCP
from typing import Optional
from duckduckgo_search import DDGS
from semanticscholar import SemanticScholar
import requests
import re
import os

mcp = FastMCP("SearchTools")
sch = SemanticScholar()

@mcp.tool()
def duckduckgo_search(query: str, num_results: int = 5) -> str:
    """
    Perform a free general web search using DuckDuckGo.
    Returns a formatted string containing titles, links, and snippets.
    """
    try:
        results = DDGS().text(query, max_results=num_results)
        if not results:
            return "No results found."
            
        formatted = []
        for r in results:
            title = r.get("title", "No Title")
            href = r.get("href", "No Link")
            body = r.get("body", "No Snippet")
            formatted.append(f"Title: {title}\nURL: {href}\nSnippet: {body}\n---")
            
        return "\n".join(formatted)
    except Exception as e:
        return f"Search failed: {e}"

@mcp.tool()
def scholar_search(query: str, num_results: int = 5) -> str:
    """
    Perform an academic search using the Semantic Scholar API.
    Returns paper metadata including Paper ID, Title, Authors, Year, and Citation Count.
    """
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": num_results,
            "fields": "title,authors,year,citationCount,url,externalIds"
        }
        res = requests.get(url, params=params, timeout=10)
        
        if res.status_code == 429:
            return "Error: Semantic Scholar API Rate Limit Exceeded (429)."
        res.raise_for_status()
        
        data = res.json()
        if "data" not in data or not data["data"]:
            return "No academic papers found."
            
        formatted = []
        for p in data["data"]:
            authors = ", ".join([a.get("name", "") for a in p.get("authors", [])])
            year = p.get("year", "Unknown Year")
            citations = p.get("citationCount", 0)
            url_val = p.get("url", "No URL")
            paper_id = p.get("paperId", "Unknown ID")
            doi = p.get("externalIds", {}).get("DOI", "No DOI")
            
            formatted.append(
                f"Title: {p.get('title')}\n"
                f"Paper ID: {paper_id}\n"
                f"DOI: {doi}\n"
                f"Authors: {authors}\n"
                f"Year: {year} | Citations: {citations}\n"
                f"URL: {url_val}\n---"
            )
            
        return "\n".join(formatted)
    except Exception as e:
        return f"Scholar search failed: {e}"

@mcp.tool()
def get_paper_details(paper_id_or_doi: str) -> str:
    """
    Fetch full details, including the abstract, for a specific paper from Semantic Scholar.
    Input can be a Semantic Scholar Paper ID or a DOI (e.g., '10.1145/12345').
    """
    try:
        query_id = f"DOI:{paper_id_or_doi}" if "/" in paper_id_or_doi else paper_id_or_doi
        url = f"https://api.semanticscholar.org/graph/v1/paper/{query_id}"
        params = {
            "fields": "title,abstract,authors,year,citationCount,url,venue,externalIds,tldr"
        }
        res = requests.get(url, params=params, timeout=10)
        
        if res.status_code == 429:
            return "Error: Semantic Scholar API Rate Limit Exceeded (429)."
        res.raise_for_status()
        
        p = res.json()
        authors = ", ".join([a.get("name", "") for a in p.get("authors", [])])
        year = p.get("year", "Unknown")
        venue = p.get("venue", "Unknown Venue")
        citations = p.get("citationCount", 0)
        abstract = p.get("abstract", "No Abstract Available")
        
        tldr_obj = p.get("tldr")
        tldr = tldr_obj.get("text") if tldr_obj else "No TLDR Available"
        
        return (
            f"Title: {p.get('title', 'Unknown')}\n"
            f"Authors: {authors}\n"
            f"Venue: {venue} ({year})\n"
            f"Citations: {citations}\n"
            f"URL: {p.get('url', '')}\n\n"
            f"TLDR: {tldr}\n\n"
            f"Abstract:\n{abstract}"
        )
    except Exception as e:
        return f"Failed to get paper details: {e}"

@mcp.tool()
def read_online_document(url_or_arxiv_id: str) -> str:
    """
    Fetches the raw text of a document from the web.
    If given an arXiv ID (e.g. '1706.03762' or 'arxiv:1706.03762'), it downloads the PDF and extracts text.
    If given a regular URL, it fetches the raw HTML/text.
    
    WARNING: Before using this tool to fetch an academic paper, ALWAYS use the `search_library` tool 
    first to ensure it hasn't already been downloaded to the local VTree library under a different name!
    """
    # Placeholder for arxiv/url reading logic.
    # We can use the existing `extract_pdf_docling` or `extract_pdf_llm` if we download it to a temp dir.
    import tempfile
    from pathlib import Path
    import urllib.request
    from src.cold_start.extractor import extract_content
    
    try:
        arxiv_match = re.search(r'(\d{4}\.\d{4,5}(v\d+)?)', url_or_arxiv_id)
        if arxiv_match or url_or_arxiv_id.lower().startswith('arxiv:'):
            arxiv_id = arxiv_match.group(1) if arxiv_match else url_or_arxiv_id.split(':')[-1]
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                urllib.request.urlretrieve(pdf_url, tmp.name)
                tmp_path = Path(tmp.name)
                
            content = extract_content(tmp_path)
            tmp_path.unlink() # cleanup
            
            if content:
                return content.text
            return "Failed to extract text from arXiv PDF."
            
        else:
            # Simple HTTP GET for other URLs
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url_or_arxiv_id, headers=headers, timeout=10)
            response.raise_for_status()
            
            # Very naive html stripping for placeholder
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            return soup.get_text(separator='\n', strip=True)
            
    except Exception as e:
        return f"Failed to read online document: {e}"

@mcp.tool()
def walk_citation_graph(start_doi_or_id: str, depth: int = 2) -> str:
    """
    Spawns an autonomous subagent that explores the citation graph forward and backward 
    starting from the given paper ID or DOI. It evaluates abstracts and returns a highly 
    curated bibliography of relevant related work.
    """
    from src.subagents.citation_walker import walk_citation_graph as cw_run
    return cw_run(start_doi_or_id, depth)
