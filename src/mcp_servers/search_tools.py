"""
Web & Academic Search MCP Server (``ra-mcp-search``).

Provides DuckDuckGo web search, Semantic Scholar paper search & metadata,
arXiv/web document reading, and forward/backward citation graph edges.
"""

from pathlib import Path
import os
import re
import sys
import tempfile
from typing import Optional

from duckduckgo_search import DDGS
from fastmcp import FastMCP
import requests
from semanticscholar import SemanticScholar

sys.path.append(str(Path(__file__).parent.parent.parent))

from config import settings
from config.settings import get_logger
from src.mcp_servers.progress import report_mcp_progress
from src.subagents.citation_walker import (
    get_citations,
    get_references,
    walk_citation_graph as cw_run,
)
from src.telemetry.store import track_tool

logger = get_logger(__name__)

mcp = FastMCP("SearchTools")
sch = SemanticScholar()


def _s2_headers() -> dict[str, str]:
    headers = {"User-Agent": "ResearchAgent/1.0"}
    api_key = os.getenv("S2_API_KEY") or os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key.strip()
    return headers


def _reconstruct_openalex_abstract(inv_index: Optional[dict]) -> str:
    if not inv_index or not isinstance(inv_index, dict):
        return ""
    pos_map: dict[int, str] = {}
    for word, positions in inv_index.items():
        if isinstance(positions, list):
            for p in positions:
                if isinstance(p, int):
                    pos_map[p] = word
    if not pos_map:
        return ""
    return " ".join(pos_map[idx] for idx in sorted(pos_map.keys()))


def _openalex_search_fallback(query: str, num_results: int) -> Optional[str]:
    try:
        res = requests.get(
            "https://api.openalex.org/works",
            params={"search": query, "per-page": max(1, min(num_results, 25))},
            headers={"User-Agent": "ResearchAgent/1.0"},
            timeout=10,
        )
        if res.status_code != 200:
            return None
        items = (res.json() or {}).get("results") or []
        if not items:
            return None
        formatted = ["[Note: Semantic Scholar rate-limited (429); served via OpenAlex fallback]"]
        for w in items[:num_results]:
            authors = ", ".join(
                a.get("author", {}).get("display_name", "")
                for a in (w.get("authorships") or [])
                if a.get("author", {}).get("display_name")
            )
            year = w.get("publication_year", "Unknown Year")
            citations = w.get("cited_by_count", 0)
            raw_doi = w.get("doi") or ""
            doi = raw_doi.replace("https://doi.org/", "") if raw_doi else "No DOI"
            ids_dict = w.get("ids") or {}
            raw_arxiv = ids_dict.get("arxiv") or ""
            arxiv_id = raw_arxiv.split("arxiv.org/abs/")[-1] if "arxiv.org/abs/" in raw_arxiv else (raw_arxiv or "No ArXiv ID")
            paper_id = (w.get("id") or "Unknown ID").replace("https://openalex.org/", "")
            oa_url = (w.get("open_access") or {}).get("oa_url") or w.get("id") or "No URL"
            formatted.append(
                f"Title: {w.get('title') or w.get('display_name')}\n"
                f"Paper ID: {paper_id}\n"
                f"DOI: {doi}\n"
                f"ArXiv ID: {arxiv_id}\n"
                f"Authors: {authors}\n"
                f"Year: {year} | Citations: {citations}\n"
                f"URL: {oa_url}\n---"
            )
        return "\n".join(formatted)
    except Exception as e:
        logger.debug("OpenAlex search fallback failed: %s", e)
        return None


@mcp.tool()
@track_tool("ra_search")
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
        logger.warning("duckduckgo_search failed for '%s': %s", query, e)
        return f"Search failed: {e}"


@mcp.tool()
@track_tool("ra_search")
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
            "fields": "title,authors,year,citationCount,url,externalIds",
        }
        res = requests.get(url, params=params, timeout=10)

        if res.status_code == 429:
            fallback = _openalex_search_fallback(query, num_results)
            if fallback:
                return fallback
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
            ext_ids = p.get("externalIds") or {}
            doi = ext_ids.get("DOI", "No DOI")
            arxiv_id = ext_ids.get("ArXiv", "No ArXiv ID")

            formatted.append(
                f"Title: {p.get('title')}\n"
                f"Paper ID: {paper_id}\n"
                f"DOI: {doi}\n"
                f"ArXiv ID: {arxiv_id}\n"
                f"Authors: {authors}\n"
                f"Year: {year} | Citations: {citations}\n"
                f"URL: {url_val}\n---"
            )

        return "\n".join(formatted)
    except Exception as e:
        logger.warning("scholar_search failed for '%s': %s", query, e)
        return f"Scholar search failed: {e}"


def _openalex_details_fallback(paper_id_or_doi: str) -> Optional[str]:
    try:
        clean = paper_id_or_doi.strip()
        if "/" in clean:
            clean_doi = clean.replace("https://doi.org/", "").replace("DOI:", "")
            url = f"https://api.openalex.org/works/https://doi.org/{clean_doi}"
            res = requests.get(url, headers={"User-Agent": "ResearchAgent/1.0"}, timeout=10)
        else:
            res = requests.get(
                "https://api.openalex.org/works",
                params={"search": clean, "per-page": 1},
                headers={"User-Agent": "ResearchAgent/1.0"},
                timeout=10,
            )
        if res.status_code != 200:
            return None
        data = res.json() or {}
        w = data["results"][0] if "results" in data and data["results"] else data
        if not w or not w.get("title"):
            return None
        authors = ", ".join(
            a.get("author", {}).get("display_name", "")
            for a in (w.get("authorships") or [])
            if a.get("author", {}).get("display_name")
        )
        year = w.get("publication_year", "Unknown")
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "Unknown Venue"
        citations = w.get("cited_by_count", 0)
        abstract = _reconstruct_openalex_abstract(w.get("abstract_inverted_index")) or "No Abstract Available"
        oa_url = (w.get("open_access") or {}).get("oa_url") or w.get("id") or ""
        return (
            f"Title: {w.get('title', 'Unknown')}\n"
            f"Authors: {authors}\n"
            f"Venue: {venue} ({year})\n"
            f"Citations: {citations}\n"
            f"URL: {oa_url}\n\n"
            f"TLDR: No TLDR Available\n\n"
            f"Abstract:\n{abstract}"
        )
    except Exception:
        return None


@mcp.tool()
@track_tool("ra_search")
def get_paper_details(paper_id_or_doi: str) -> str:
    """
    Fetch full details, including the abstract and TLDR, for a specific paper from Semantic Scholar.
    Input can be a Semantic Scholar Paper ID or a DOI (e.g., '10.1145/12345').
    """
    try:
        query_id = f"DOI:{paper_id_or_doi}" if "/" in paper_id_or_doi else paper_id_or_doi
        url = f"https://api.semanticscholar.org/graph/v1/paper/{query_id}"
        params = {"fields": "title,abstract,authors,year,citationCount,url,venue,externalIds,tldr"}
        res = requests.get(url, params=params, timeout=10)

        if res.status_code == 429:
            fallback = _openalex_details_fallback(paper_id_or_doi)
            if fallback:
                return fallback
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
        ext_ids = p.get("externalIds") or {}
        doi = ext_ids.get("DOI", "No DOI")
        arxiv_id = ext_ids.get("ArXiv", "No ArXiv ID")

        return (
            f"Title: {p.get('title', 'Unknown')}\n"
            f"DOI: {doi} | ArXiv ID: {arxiv_id}\n"
            f"Authors: {authors}\n"
            f"Venue: {venue} ({year})\n"
            f"Citations: {citations}\n"
            f"URL: {p.get('url', '')}\n\n"
            f"TLDR: {tldr}\n\n"
            f"Abstract:\n{abstract}"
        )
    except Exception as e:
        logger.warning("get_paper_details failed for '%s': %s", paper_id_or_doi, e)
        return f"Failed to get paper details: {e}"


@mcp.tool()
@track_tool("ra_search")
def get_paper_references(paper_id_or_doi: str, limit: int = 10) -> str:
    """
    Fetch backward references (papers cited BY the given paper) from Semantic Scholar.
    Used by the @citation-walker subagent or primary researcher to trace foundational work.
    """
    refs = get_references(paper_id_or_doi, limit=limit)
    if not refs:
        return f"No references found for '{paper_id_or_doi}' (or rate-limited)."
    lines = [f"References cited by '{paper_id_or_doi}' ({len(refs)}):"]
    for r in refs:
        lines.append(
            f"- Paper ID: {r['id']} | Title: {r.get('title', 'Unknown')} "
            f"| Year: {r.get('year', '?')} | Citations: {r.get('citationCount', '?')}"
        )
    return "\n".join(lines)


@mcp.tool()
@track_tool("ra_search")
def get_paper_citations(paper_id_or_doi: str, limit: int = 10) -> str:
    """
    Fetch forward citations (newer papers that cite the given paper) from Semantic Scholar.
    Used by the @citation-walker subagent or primary researcher to trace follow-up work.
    """
    cits = get_citations(paper_id_or_doi, limit=limit)
    if not cits:
        return f"No citations found for '{paper_id_or_doi}' (or rate-limited)."
    lines = [f"Papers citing '{paper_id_or_doi}' ({len(cits)}):"]
    for c in cits:
        lines.append(
            f"- Paper ID: {c['id']} | Title: {c.get('title', 'Unknown')} "
            f"| Year: {c.get('year', '?')} | Citations: {c.get('citationCount', '?')}"
        )
    return "\n".join(lines)


@mcp.tool()
@track_tool("ra_search")
def read_online_document(
    url_or_arxiv_id: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
) -> str:
    """
    Fetches and reads the extracted Markdown/text of an online document or arXiv paper
    in paginated line ranges (up to 400 lines per call) with a Document Outline of headings
    and line numbers.

    Pagination & Navigation (`start_line` / `end_line`):
    - On the first call (`start_line=1`), returns lines 1–400 together with a
      `Document Outline (Headings & Line Numbers)` (`L12: # 1. Introduction`, `L145: ## 2. Method`, etc.).
    - To read subsequent sections or jump directly to a specific heading from the outline,
      call `read_online_document(url_or_arxiv_id="...", start_line=401, end_line=800)` (or pass the
      heading's line number as `start_line`).
    - Both arXiv PDFs (`arxiv_<id>.md`) and web URLs (`url_<sha256>.md`) are cached locally in
      `~/.research-agent/cache/online_docs/`, so follow-up paginated reads (`start_line=...`) and
      subsequent `stage_paper` calls are instant (0ms, no re-download).
    - Do NOT call `read_online_document` repeatedly with default arguments (`start_line=1`) on the
      same URL—always advance `start_line` when reading further!

    WARNING: Before using this tool to fetch an academic paper, ALWAYS use the `search_library` tool
    first to ensure it hasn't already been downloaded to the local VTree library under a different name!
    """
    import hashlib
    from src.mcp_servers.pagination import extract_clean_html_markdown, paginate_document_text

    if hasattr(settings, "reload_env_settings"):
        settings.reload_env_settings()

    try:
        online_cache_dir = settings.CACHE_DIR / "online_docs"
        online_cache_dir.mkdir(parents=True, exist_ok=True)
        is_mocked_http = hasattr(requests.get, "assert_called")

        arxiv_match = re.search(r"(\d{4}\.\d{4,5}(v\d+)?)", url_or_arxiv_id)
        if arxiv_match or url_or_arxiv_id.lower().startswith("arxiv:"):
            from src.cold_start.extractor import extract_content

            arxiv_id = arxiv_match.group(1) if arxiv_match else url_or_arxiv_id.split(":")[-1]
            safe_id = re.sub(r"[^a-zA-Z0-9._-]", "_", arxiv_id)
            cached_md_path = online_cache_dir / f"arxiv_{safe_id}.md"
            cached_pdf_path = online_cache_dir / f"arxiv_{safe_id}.pdf"

            full_text = ""
            if cached_md_path.exists() and not is_mocked_http:
                full_text = cached_md_path.read_text(encoding="utf-8")
                if full_text.strip():
                    report_mcp_progress(
                        1,
                        1,
                        f"[read_online_document] Serving arXiv:{arxiv_id} from online_docs cache (0 pages re-parsed)",
                    )

            if not full_text.strip():
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                report_mcp_progress(0, 2, f"[read_online_document] Downloading arXiv PDF {pdf_url}...")
                headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ResearchAgent/1.0"}
                resp = requests.get(pdf_url, headers=headers, timeout=20)
                resp.raise_for_status()
                cached_pdf_path.write_bytes(resp.content)

                report_mcp_progress(1, 2, f"[read_online_document] Extracting Markdown from arXiv:{arxiv_id}...")
                content = extract_content(cached_pdf_path)
                if not content or not content.text.strip():
                    return "Failed to extract text from arXiv PDF."
                full_text = content.text
                cached_md_path.write_text(full_text, encoding="utf-8")
                report_mcp_progress(
                    2,
                    2,
                    f"[read_online_document] Completed extraction for arXiv:{arxiv_id} ({len(full_text):,} chars)",
                )

            return paginate_document_text(
                full_text,
                source_label=f"arXiv:{arxiv_id}",
                tool_name="read_online_document",
                tool_arg_hint=f'url_or_arxiv_id="{url_or_arxiv_id}"',
                start_line=start_line,
                end_line=end_line,
            )

        from src.openreview_client import extract_openreview_forum_id, get_openreview_headers

        or_forum_id = extract_openreview_forum_id(url_or_arxiv_id)
        if or_forum_id:
            from src.cold_start.extractor import extract_content

            safe_or_id = re.sub(r"[^a-zA-Z0-9._-]", "_", or_forum_id)
            cached_md_path = online_cache_dir / f"openreview_{safe_or_id}.md"
            cached_pdf_path = online_cache_dir / f"openreview_{safe_or_id}.pdf"

            full_text = ""
            if cached_md_path.exists() and not is_mocked_http:
                full_text = cached_md_path.read_text(encoding="utf-8")
                if full_text.strip():
                    report_mcp_progress(
                        1,
                        1,
                        f"[read_online_document] Serving OpenReview:{or_forum_id} from online_docs cache",
                    )

            if not full_text.strip():
                pdf_url = f"https://openreview.net/pdf?id={or_forum_id}"
                report_mcp_progress(0, 2, f"[read_online_document] Downloading OpenReview PDF {pdf_url}...")
                resp = requests.get(pdf_url, headers=get_openreview_headers(), timeout=20, allow_redirects=True)
                resp.raise_for_status()
                cached_pdf_path.write_bytes(resp.content)

                report_mcp_progress(1, 2, f"[read_online_document] Extracting Markdown from OpenReview:{or_forum_id}...")
                content = extract_content(cached_pdf_path)
                if not content or not content.text.strip():
                    return f"Failed to extract text from OpenReview PDF ({pdf_url})."
                full_text = content.text
                cached_md_path.write_text(full_text, encoding="utf-8")
                report_mcp_progress(
                    2,
                    2,
                    f"[read_online_document] Completed extraction for OpenReview:{or_forum_id} ({len(full_text):,} chars)",
                )

            return paginate_document_text(
                full_text,
                source_label=f"OpenReview:{or_forum_id}",
                tool_name="read_online_document",
                tool_arg_hint=f'url_or_arxiv_id="{url_or_arxiv_id}"',
                start_line=start_line,
                end_line=end_line,
            )

        url_hash = hashlib.sha256(url_or_arxiv_id.strip().encode("utf-8")).hexdigest()[:24]
        cached_url_md = online_cache_dir / f"url_{url_hash}.md"

        full_text = ""
        if cached_url_md.exists() and not is_mocked_http:
            full_text = cached_url_md.read_text(encoding="utf-8")
            if full_text.strip():
                report_mcp_progress(
                    1,
                    1,
                    f"[read_online_document] Serving cached webpage '{url_or_arxiv_id}' ({len(full_text):,} chars)",
                )

        if not full_text.strip():
            report_mcp_progress(0, 2, f"[read_online_document] Fetching webpage '{url_or_arxiv_id}'...")
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ResearchAgent/1.0"}
            response = requests.get(url_or_arxiv_id, headers=headers, timeout=15)
            response.raise_for_status()
            full_text = extract_clean_html_markdown(response.text)
            if full_text.strip() and not is_mocked_http:
                cached_url_md.write_text(full_text, encoding="utf-8")
            report_mcp_progress(
                2,
                2,
                f"[read_online_document] Extracted {len(full_text):,} chars from '{url_or_arxiv_id}'",
            )

        return paginate_document_text(
            full_text,
            source_label=url_or_arxiv_id,
            tool_name="read_online_document",
            tool_arg_hint=f'url_or_arxiv_id="{url_or_arxiv_id}"',
            start_line=start_line,
            end_line=end_line,
        )

    except Exception as e:
        logger.warning("read_online_document failed for '%s': %s", url_or_arxiv_id, e)
        return f"Failed to read online document: {e}"


@mcp.tool()
@track_tool("ra_search")
def walk_citation_graph(start_doi_or_id: str, depth: int = 2) -> str:
    """
    Explores 1-hop forward and backward citations starting from the given paper ID or DOI.
    For deeper multi-hop exploration, delegate to the `@citation-walker` subagent.
    """
    return cw_run(start_doi_or_id, depth)


@mcp.tool()
@track_tool("ra_search")
def openreview_search(
    query: str,
    venue: Optional[str] = None,
    num_results: int = 5,
) -> str:
    """
    OPTIONAL OPENREVIEW SEARCH — Searches OpenReview (`https://api2.openreview.net/notes/search`)
    for conference submissions, preprints, and workshops (ICLR, NeurIPS, ICML, TMLR, COLM, etc.).

    NOTE: This tool requires the user to have logged into OpenReview via `ra-cli login-openreview`.
    If not configured, it returns a friendly notice so you can fall back to `scholar_search` or
    `duckduckgo_search`.

    Arguments:
    - query: Keywords, paper title, or author name to search on OpenReview.
    - venue: Optional venue filter substring (e.g. `"ICLR"`, `"NeurIPS 2025"`, `"ICML"`, `"TMLR"`).
    - num_results: Maximum number of matching submissions to return (default `5`).
    """
    from src.openreview_client import (
        OpenReviewAuthError,
        is_openreview_configured,
        search_openreview_notes,
    )

    if not is_openreview_configured():
        return (
            "OpenReview API is not configured. OpenReview integration is optional and requires your "
            "personal OpenReview credentials — run `ra-cli login-openreview` in your terminal to enable it "
            "(or use `scholar_search` / `duckduckgo_search` instead)."
        )

    try:
        items = search_openreview_notes(query=query, venue=venue, limit=max(1, min(num_results, 20)))
        if not items:
            return f"No OpenReview papers found matching '{query}'" + (f" (venue='{venue}')." if venue else ".")

        blocks: list[str] = []
        for p in items:
            authors = ", ".join(p.get("authors") or []) or "Anonymous / Unknown"
            year = p.get("year") or "Unknown Year"
            venue_str = p.get("venue") or "OpenReview"
            tldr = p.get("tldr")
            abs_snip = (p.get("abstract") or "")[:350]
            if len(p.get("abstract") or "") > 350:
                abs_snip += "..."

            lines = [
                f"Title: {p.get('title')}",
                f"Forum ID: {p.get('forum_id')}",
                f"Venue: {venue_str} ({year})",
                f"Authors: {authors}",
                f"Forum URL: {p.get('forum_url')}",
                f"PDF URL: {p.get('pdf_url')}",
            ]
            if tldr:
                lines.append(f"TL;DR: {tldr}")
            if abs_snip:
                lines.append(f"Abstract Snippet: {abs_snip}")
            lines.append("---")
            blocks.append("\n".join(lines))

        return "\n".join(blocks)
    except OpenReviewAuthError as e:
        return f"OpenReview authentication required: {e}"
    except Exception as e:
        logger.warning("openreview_search failed for '%s': %s", query, e)
        return f"OpenReview search failed: {e}"


@mcp.tool()
@track_tool("ra_search")
def get_openreview_paper(
    forum_id_or_url: str,
    include_reviews: bool = True,
) -> str:
    """
    Fetches full submission details, abstract, TL;DR, PDF link, and (when `include_reviews=True`)
    official peer reviews, ratings/scores, meta-reviews, decisions, and author rebuttals for an
    OpenReview paper.

    Arguments:
    - forum_id_or_url: OpenReview Forum ID (e.g. `'abc123XYZ'`) or URL
      (`'https://openreview.net/forum?id=abc123XYZ'`).
    - include_reviews: When `True` (default), includes official peer reviews, ratings, meta-review,
      and author responses from the paper's OpenReview forum.
    """
    from src.openreview_client import (
        OpenReviewAuthError,
        get_openreview_forum_details,
    )

    try:
        details = get_openreview_forum_details(forum_id_or_url, include_reviews=include_reviews)
        if not details or not details.get("title"):
            return f"No OpenReview paper found for '{forum_id_or_url}'."

        authors = ", ".join(details.get("authors") or []) or "Anonymous / Unknown"
        year = details.get("year") or "Unknown"
        venue = details.get("venue") or "OpenReview"
        tldr = details.get("tldr") or "No TL;DR provided"
        abstract = details.get("abstract") or "No Abstract Available"

        out = [
            f"Title: {details.get('title')}",
            f"Forum ID: {details.get('forum_id')}",
            f"Authors: {authors}",
            f"Venue: {venue} ({year})",
            f"Forum URL: {details.get('forum_url')}",
            f"PDF URL: {details.get('pdf_url')}",
            "",
            f"TL;DR: {tldr}",
            "",
            f"Abstract:\n{abstract}",
        ]

        reviews = details.get("reviews") or []
        if include_reviews:
            if reviews:
                out.append(f"\n## OpenReview Forum Replies & Peer Reviews ({len(reviews)}):")
                for idx, rev in enumerate(reviews, start=1):
                    out.append(f"\n### [{idx}] {rev['kind']} (by {rev['author']})\n{rev['body']}")
            else:
                out.append("\n## OpenReview Forum Replies & Peer Reviews:\nNo public reviews or comments found.")

        return "\n".join(out)
    except OpenReviewAuthError as e:
        return f"OpenReview authentication required: {e}"
    except Exception as e:
        logger.warning("get_openreview_paper failed for '%s': %s", forum_id_or_url, e)
        return f"Failed to fetch OpenReview paper: {e}"


def main() -> None:
    logger.info("Starting SearchTools MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
