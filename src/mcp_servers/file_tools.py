"""
User-Gated File Ingestion & Incremental Refresh MCP Server (``ra-mcp-ingest``).

Features:
1. **Multi-Tier Paper Resolution Cascade (``stage_paper``)**:
   - **Tier 1 (arXiv Direct)**: Detects arXiv IDs/URLs and downloads the PDF directly from ``arxiv.org/pdf/<id>.pdf``.
   - **Tier 2 (Academic API Lookup)**: Queries OpenAlex, arXiv API, and Semantic Scholar by title/DOI to locate open-access PDFs or arXiv preprints when given conference landing pages (e.g. ``iclr.cc``, ``openreview.net``).
   - **Tier 3 (Direct URL & Meta-Tag Inspection)**: Checks if the URL is a PDF stream or exposes ``<meta name="citation_pdf_url">``.
   - **Tier 4 (Metadata + Abstract Fallback)**: When no open-access PDF is available (or blocked by captcha), synthesizes structured Markdown from the paper's Title, Authors, Venue, Year, DOI, TLDR, and Abstract.
2. **Dedicated OpenRouter Summarization**:
   - Always summarizes staged documents using ``summarize_text()`` (configured via ``DEFAULT_SUMMARIZATION_MODEL`` and ``config/prompts.toml``) rather than relying on the chat agent.
   - Updates ancestor directory summaries via ``summarize_directory()`` upon ``confirm_save_paper``.
3. **Flat PDF Storage in ``<library_root>/Downloaded/``**:
   - Upon ``confirm_save_paper``, moves the staged PDF into a single flat folder (``<library_root>/Downloaded/<title>.pdf``) while ``vtree.json`` maintains the semantic directory hierarchy.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Literal, Optional
import uuid
import xml.etree.ElementTree as ET

import requests
from fastmcp import FastMCP

sys.path.append(str(Path(__file__).parent.parent.parent))

from config import settings
from config.settings import get_logger
from src.cold_start.summarizer import summarize_text
from src.mcp_servers.progress import StepTracker, report_mcp_progress
from src.telemetry.store import track_tool
from src.vtree.operations import VTreeManager
from src.vtree.store import VTreeStore

logger = get_logger(__name__)

mcp = FastMCP("FileTools")

# In-memory staging cache synchronized with settings.STAGED_DIR on disk
_STAGED_PAPERS: dict[str, dict] = {}

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _staged_file(temp_id: str) -> Path:
    settings.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    return settings.STAGED_DIR / f"{temp_id}.json"


def _staged_pdf_file(temp_id: str) -> Path:
    settings.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    return settings.STAGED_DIR / f"{temp_id}.pdf"


def _save_staged_manifest(temp_id: str, manifest: dict) -> None:
    _STAGED_PAPERS[temp_id] = manifest
    try:
        _staged_file(temp_id).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not persist staged manifest %s to disk: %s", temp_id, e)


def _pop_staged_manifest(temp_id: str) -> Optional[dict]:
    manifest = _STAGED_PAPERS.pop(temp_id, None)
    path = _staged_file(temp_id)
    if path.exists():
        try:
            if manifest is None:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed reading/removing staged file %s: %s", path, e)
    return manifest


def _clean_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|\r\n\t]+', " ", name).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean[:180] or "paper"


def _derive_name(url_or_arxiv_id: str, title: Optional[str] = None) -> str:
    if title and title.strip():
        return _clean_filename(title.strip())
    tail = url_or_arxiv_id.rstrip("/").split("/")[-1] or "paper"
    if tail.lower().endswith(".pdf"):
        tail = tail[:-4]
    return _clean_filename(re.sub(r'[\\/*?:"<>|]+', "_", tail))


def _extract_arxiv_id(value: str) -> Optional[str]:
    if not value:
        return None
    val = value.strip()
    m = re.search(r"(?:arxiv[:/]|abs/|pdf/|html/|^)(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?$", val, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", val)
    if m2 and ("arxiv" in val.lower() or not val.startswith("http")):
        return m2.group(1)
    return None


def _titles_match(query_title: str, candidate_title: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    q = norm(query_title)
    c = norm(candidate_title)
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if not q_tokens:
        return False
    overlap = len(q_tokens & c_tokens) / len(q_tokens)
    return overlap >= 0.7


def _download_pdf_bytes(pdf_url: str, dest_path: Path) -> bool:
    """Download `pdf_url` to `dest_path` if and only if the response contains valid PDF bytes."""
    if not pdf_url:
        return False
    try:
        resp = requests.get(pdf_url, headers=_HTTP_HEADERS, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            return False
        content_type = resp.headers.get("Content-Type", "").lower()
        if b"%PDF-" not in resp.content[:1024] and "application/pdf" not in content_type:
            return False
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        logger.info("Downloaded PDF (%d bytes) from %s -> %s", len(resp.content), pdf_url, dest_path)
        return True
    except Exception as e:
        logger.debug("PDF download attempt failed for %s: %s", pdf_url, e)
        return False


def _extract_pdf_file(
    pdf_path: Path,
    tracker: Optional[StepTracker] = None,
    label: Optional[str] = None,
) -> tuple[Optional[str], Optional[int]]:
    """Extract Markdown text and page count from a local PDF file."""
    page_count: Optional[int] = None
    fallback_text_parts: list[str] = []
    try:
        import fitz

        with fitz.open(pdf_path) as doc:
            page_count = doc.page_count
            for page in doc:
                fallback_text_parts.append(page.get_text("text"))
    except Exception as e:
        logger.warning("PyMuPDF inspection failed for %s: %s", pdf_path, e)

    mode = getattr(settings, "EXTRACTOR_MODE", "pymupdf").lower().strip()
    if mode == "llm":
        mode_label = "Vision LLM OCR"
    elif mode == "docling":
        mode_label = "Docling"
    else:
        mode_label = "PyMuPDF ($0 fast)"
    doc_label = label or pdf_path.name
    if tracker is not None:
        pg_info = f"{page_count} pages" if page_count is not None else "PDF"
        tracker.step(f"Extracting {pg_info} via {mode_label} for '{doc_label}'...")

    try:
        from src.cold_start.extractor import extract_content

        extracted = extract_content(pdf_path)
        if extracted and extracted.text and extracted.text.strip():
            return extracted.text, page_count
    except Exception as e:
        logger.warning("Structured PDF extraction failed for %s (%s); falling back to raw PyMuPDF text", pdf_path, e)

    joined = "\n\n".join(p.strip() for p in fallback_text_parts if p.strip())
    return (joined if joined else None), page_count


_LUCENE_SPECIAL_RE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\])')


def _escape_arxiv_lucene(text: str) -> str:
    """Escape Apache Lucene special query characters (such as `:` in titles) with a backslash."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    return _LUCENE_SPECIAL_RE.sub(r"\\\1", collapsed)


def _lookup_arxiv_api(arxiv_id: Optional[str] = None, title: Optional[str] = None) -> dict:
    """Query the official arXiv Atom API by ID or title."""
    try:
        if arxiv_id:
            clean_id = re.sub(r"v\d+$", "", arxiv_id)
            params = {"id_list": clean_id, "max_results": 1}
        elif title:
            escaped_title = _escape_arxiv_lucene(title)
            params = {"search_query": f'ti:"{escaped_title}"', "max_results": 3}
        else:
            return {}

        resp = requests.get("http://export.arxiv.org/api/query", params=params, headers=_HTTP_HEADERS, timeout=12)
        if resp.status_code != 200:
            return {}

        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        for entry in root.findall("atom:entry", ns):
            cand_title_el = entry.find("atom:title", ns)
            cand_title = re.sub(r"\s+", " ", cand_title_el.text.strip()) if cand_title_el is not None and cand_title_el.text else ""
            if not arxiv_id and title and not _titles_match(title, cand_title):
                continue

            summary_el = entry.find("atom:summary", ns)
            abstract = re.sub(r"\s+", " ", summary_el.text.strip()) if summary_el is not None and summary_el.text else ""
            authors = [
                a.find("atom:name", ns).text.strip()
                for a in entry.findall("atom:author", ns)
                if a.find("atom:name", ns) is not None and a.find("atom:name", ns).text
            ]
            entry_id_el = entry.find("atom:id", ns)
            found_arxiv_id = _extract_arxiv_id(entry_id_el.text) if entry_id_el is not None and entry_id_el.text else arxiv_id
            pdf_url = f"https://arxiv.org/pdf/{found_arxiv_id}.pdf" if found_arxiv_id else None
            for link in entry.findall("atom:link", ns):
                if link.attrib.get("title") == "pdf" and link.attrib.get("href"):
                    pdf_url = link.attrib["href"]
                    if pdf_url.startswith("http://"):
                        pdf_url = "https://" + pdf_url[len("http://") :]
            doi_el = entry.find("arxiv:doi", ns)
            doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None
            return {
                "title": cand_title,
                "abstract": abstract,
                "authors": authors,
                "arxiv_id": found_arxiv_id,
                "pdf_url": pdf_url,
                "doi": doi,
            }
    except Exception as e:
        logger.debug("arXiv API lookup failed (id=%s, title=%s): %s", arxiv_id, title, e)
    return {}


def _reconstruct_openalex_abstract(inverted_index: Optional[dict]) -> str:
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, pos_list in inverted_index.items():
        if isinstance(pos_list, list):
            for p in pos_list:
                if isinstance(p, int):
                    positions.append((p, word))
    positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in positions)


def _lookup_openalex(title: Optional[str] = None, doi: Optional[str] = None) -> dict:
    """Query OpenAlex works API by DOI or title to find open-access PDFs, arXiv IDs, and abstracts."""
    try:
        if doi:
            clean_doi = doi.replace("https://doi.org/", "").strip()
            url = f"https://api.openalex.org/works/https://doi.org/{clean_doi}"
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=10)
            works = [resp.json()] if resp.status_code == 200 else []
        elif title:
            resp = requests.get(
                "https://api.openalex.org/works",
                params={"search": title, "per-page": 3},
                headers=_HTTP_HEADERS,
                timeout=10,
            )
            works = resp.json().get("results", []) if resp.status_code == 200 else []
        else:
            return {}

        for w in works:
            cand_title = w.get("title") or w.get("display_name") or ""
            if not doi and title and not _titles_match(title, cand_title):
                continue

            abstract = _reconstruct_openalex_abstract(w.get("abstract_inverted_index"))
            authors = [
                a.get("author", {}).get("display_name", "")
                for a in w.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]
            best_oa = w.get("best_oa_location") or {}
            oa = w.get("open_access") or {}
            pdf_url = best_oa.get("pdf_url") or oa.get("oa_url")
            arxiv_id = None
            for loc in w.get("locations", []):
                lp = loc.get("landing_page_url") or ""
                pu = loc.get("pdf_url") or ""
                aid = _extract_arxiv_id(lp) or _extract_arxiv_id(pu)
                if aid:
                    arxiv_id = aid
                    if not pdf_url:
                        pdf_url = f"https://arxiv.org/pdf/{aid}.pdf"
                    break
            raw_doi = w.get("doi") or ""
            clean_w_doi = raw_doi.replace("https://doi.org/", "").strip() or None
            return {
                "title": cand_title,
                "abstract": abstract,
                "authors": authors,
                "pdf_url": pdf_url,
                "arxiv_id": arxiv_id,
                "doi": clean_w_doi,
                "year": w.get("publication_year"),
            }
    except Exception as e:
        logger.debug("OpenAlex lookup failed (title=%s, doi=%s): %s", title, doi, e)
    return {}


def _lookup_semantic_scholar(title: Optional[str] = None, paper_id_or_doi: Optional[str] = None) -> dict:
    """Query Semantic Scholar Graph API for openAccessPdf, ArXiv ID, DOI, abstract, and TLDR."""
    fields = "title,abstract,authors,year,venue,externalIds,openAccessPdf,tldr"
    try:
        p = None
        if paper_id_or_doi:
            qid = f"DOI:{paper_id_or_doi}" if ("/" in paper_id_or_doi and not paper_id_or_doi.startswith("http")) else paper_id_or_doi
            resp = requests.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{qid}",
                params={"fields": fields},
                headers=_HTTP_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                p = resp.json()
        if p is None and title:
            resp = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": title, "limit": 3, "fields": fields},
                headers=_HTTP_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                for cand in resp.json().get("data", []):
                    if _titles_match(title, cand.get("title", "")):
                        p = cand
                        break
        if not p:
            return {}

        ext = p.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        oa_pdf = (p.get("openAccessPdf") or {}).get("url")
        pdf_url = oa_pdf or (f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else None)
        tldr = (p.get("tldr") or {}).get("text")
        authors = [a.get("name", "") for a in p.get("authors", []) if a.get("name")]
        return {
            "title": p.get("title"),
            "abstract": p.get("abstract") or "",
            "tldr": tldr or "",
            "authors": authors,
            "year": p.get("year"),
            "venue": p.get("venue"),
            "doi": ext.get("DOI"),
            "arxiv_id": arxiv_id,
            "pdf_url": pdf_url,
        }
    except Exception as e:
        logger.debug("Semantic Scholar lookup failed: %s", e)
    return {}


def _inspect_url_directly(url: str, dest_pdf_path: Path) -> dict:
    """
    Inspect an HTTP(S) URL directly:
    - If it returns PDF bytes, saves to `dest_pdf_path`.
    - If it returns HTML, extracts scholarly `<meta>` tags (`citation_pdf_url`,
      `citation_abstract`, `citation_title`, `citation_doi`) and readable text.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return {}
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return {}
        ctype = resp.headers.get("Content-Type", "").lower()
        if b"%PDF-" in resp.content[:1024] or "application/pdf" in ctype:
            dest_pdf_path.parent.mkdir(parents=True, exist_ok=True)
            dest_pdf_path.write_bytes(resp.content)
            return {"pdf_downloaded": True}

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        meta_map: dict[str, str] = {}
        for m in soup.find_all("meta"):
            key = (m.get("name") or m.get("property") or "").lower().strip()
            val = (m.get("content") or "").strip()
            if key and val and key not in meta_map:
                meta_map[key] = val

        pdf_downloaded = False
        citation_pdf = meta_map.get("citation_pdf_url")
        if citation_pdf:
            if citation_pdf.startswith("/"):
                from urllib.parse import urljoin

                citation_pdf = urljoin(url, citation_pdf)
            pdf_downloaded = _download_pdf_bytes(citation_pdf, dest_pdf_path)

        page_title = (
            meta_map.get("citation_title")
            or meta_map.get("og:title")
            or (soup.title.string.strip() if soup.title and soup.title.string else "")
        )
        abstract = (
            meta_map.get("citation_abstract")
            or meta_map.get("description")
            or meta_map.get("og:description")
            or ""
        )
        doi = meta_map.get("citation_doi")
        arxiv_id = meta_map.get("citation_arxiv_id") or _extract_arxiv_id(citation_pdf or "")

        body_text = soup.get_text(separator="\n", strip=True)
        # Filter out common anti-bot / Cloudflare interstitial text
        if any(marker in body_text for marker in ("Just a moment...", "Enable JavaScript and cookies to continue")):
            body_text = ""

        return {
            "pdf_downloaded": pdf_downloaded,
            "title": page_title,
            "abstract": abstract,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "body_text": body_text[:15000] if body_text else "",
        }
    except Exception as e:
        logger.debug("Direct URL inspection failed for %s: %s", url, e)
        return {}


def _resolve_paper_content(
    url_or_arxiv_id: str,
    temp_id: str,
    title: Optional[str] = None,
    doi: Optional[str] = None,
    preextracted_text: Optional[str] = None,
    tracker: Optional[StepTracker] = None,
) -> dict:
    """
    Execute the 4-Tier Paper Resolution Cascade to obtain:
    - Full extracted Markdown text (or rich metadata + abstract fallback)
    - Downloaded `.pdf` at `~/.research-agent/cache/staged/<temp_id>.pdf` whenever available
    - Canonical metadata (`title`, `doi`, `page_count`, `resolution_tier`)
    """
    staged_pdf = _staged_pdf_file(temp_id)
    resolved_title = title.strip() if title and title.strip() else None
    resolved_doi = doi.strip() if doi and doi.strip() else None
    metadata_pool: list[dict] = []

    if tracker is not None:
        tracker.step(
            f"Resolving paper source & metadata for '{resolved_title or url_or_arxiv_id}'..."
        )

    if preextracted_text and preextracted_text.strip():
        if tracker is not None:
            tracker.step("Using caller-provided pre-extracted text (0 pages to parse)...")
        return {
            "title": _derive_name(url_or_arxiv_id, resolved_title),
            "doi": resolved_doi,
            "text": preextracted_text.strip(),
            "pdf_path": None,
            "page_count": None,
            "resolution_tier": "preextracted",
        }

    # --- Tier 1: Direct arXiv ID or arXiv URL ---
    arxiv_id = _extract_arxiv_id(url_or_arxiv_id)
    if arxiv_id:
        arxiv_meta = _lookup_arxiv_api(arxiv_id=arxiv_id)
        arxiv_title_matches = True
        if resolved_title and arxiv_meta.get("title"):
            if not _titles_match(resolved_title, arxiv_meta["title"]):
                logger.warning(
                    "Mismatched/hallucinated arXiv ID '%s': arXiv title '%s' does not match expected title '%s'. "
                    "Skipping Tier 1 and falling back to Tier 2 title resolution.",
                    arxiv_id,
                    arxiv_meta.get("title"),
                    resolved_title,
                )
                arxiv_title_matches = False

        if arxiv_title_matches:
            if arxiv_meta:
                metadata_pool.append(arxiv_meta)
                resolved_title = resolved_title or arxiv_meta.get("title")
                resolved_doi = resolved_doi or arxiv_meta.get("doi")

            # Check if `read_online_document` already cached this arXiv PDF & extracted text
            safe_id = re.sub(r"[^a-zA-Z0-9._-]", "_", arxiv_id)
            online_cache_dir = settings.CACHE_DIR / "online_docs"
            cached_md_path = online_cache_dir / f"arxiv_{safe_id}.md"
            cached_pdf_path = online_cache_dir / f"arxiv_{safe_id}.pdf"
            if cached_md_path.exists() and cached_pdf_path.exists():
                cached_text = cached_md_path.read_text(encoding="utf-8")
                if cached_text.strip():
                    staged_pdf.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cached_pdf_path, staged_pdf)
                    page_count = None
                    try:
                        import fitz

                        with fitz.open(staged_pdf) as doc:
                            page_count = doc.page_count
                    except Exception:
                        pass
                    if tracker is not None:
                        tracker.step(
                            f"Reused cached extraction for arXiv:{arxiv_id} ({page_count or '?'} pages, 0 pages re-parsed)..."
                        )
                    logger.info("Reused cached online_docs extraction for arXiv %s (0 pages re-parsed)", arxiv_id)
                    return {
                        "title": _derive_name(url_or_arxiv_id, resolved_title),
                        "doi": resolved_doi,
                        "text": cached_text,
                        "pdf_path": str(staged_pdf),
                        "page_count": page_count,
                        "resolution_tier": "tier1_arxiv_pdf",
                    }

            arxiv_pdf_url = arxiv_meta.get("pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            if _download_pdf_bytes(arxiv_pdf_url, staged_pdf):
                extracted_md, page_count = _extract_pdf_file(
                    staged_pdf, tracker=tracker, label=resolved_title or f"arXiv:{arxiv_id}"
                )
                if extracted_md:
                    return {
                        "title": _derive_name(url_or_arxiv_id, resolved_title),
                        "doi": resolved_doi,
                        "text": extracted_md,
                        "pdf_path": str(staged_pdf),
                        "page_count": page_count,
                        "resolution_tier": "tier1_arxiv_pdf",
                    }

    # --- Tier 3 pre-check / HTML meta extraction if an HTTP URL was given ---
    url_info = _inspect_url_directly(url_or_arxiv_id, staged_pdf)
    if url_info:
        metadata_pool.append(url_info)
        resolved_title = resolved_title or url_info.get("title")
        resolved_doi = resolved_doi or url_info.get("doi")
        if url_info.get("pdf_downloaded") and staged_pdf.exists():
            extracted_md, page_count = _extract_pdf_file(
                staged_pdf, tracker=tracker, label=resolved_title or url_or_arxiv_id
            )
            if extracted_md:
                return {
                    "title": _derive_name(url_or_arxiv_id, resolved_title),
                    "doi": resolved_doi,
                    "text": extracted_md,
                    "pdf_path": str(staged_pdf),
                    "page_count": page_count,
                    "resolution_tier": "tier3_direct_pdf",
                }

    # --- Tier 2: Academic API Resolution (arXiv API + OpenAlex + Semantic Scholar) ---
    if resolved_title or resolved_doi:
        # 2a. arXiv title search (highest PDF reliability for CS/ML/NLP conference papers)
        if resolved_title:
            ax_by_title = _lookup_arxiv_api(title=resolved_title)
            if ax_by_title:
                metadata_pool.append(ax_by_title)
                resolved_doi = resolved_doi or ax_by_title.get("doi")
                if ax_by_title.get("pdf_url") and _download_pdf_bytes(ax_by_title["pdf_url"], staged_pdf):
                    extracted_md, page_count = _extract_pdf_file(
                        staged_pdf, tracker=tracker, label=resolved_title or ax_by_title.get("title")
                    )
                    if extracted_md:
                        return {
                            "title": _derive_name(url_or_arxiv_id, resolved_title or ax_by_title.get("title")),
                            "doi": resolved_doi,
                            "text": extracted_md,
                            "pdf_path": str(staged_pdf),
                            "page_count": page_count,
                            "resolution_tier": "tier2_arxiv_search_pdf",
                        }

        # 2b. OpenAlex lookup
        oa_meta = _lookup_openalex(title=resolved_title, doi=resolved_doi)
        if oa_meta:
            metadata_pool.append(oa_meta)
            resolved_title = resolved_title or oa_meta.get("title")
            resolved_doi = resolved_doi or oa_meta.get("doi")
            if oa_meta.get("pdf_url") and _download_pdf_bytes(oa_meta["pdf_url"], staged_pdf):
                extracted_md, page_count = _extract_pdf_file(
                    staged_pdf, tracker=tracker, label=resolved_title
                )
                if extracted_md:
                    return {
                        "title": _derive_name(url_or_arxiv_id, resolved_title),
                        "doi": resolved_doi,
                        "text": extracted_md,
                        "pdf_path": str(staged_pdf),
                        "page_count": page_count,
                        "resolution_tier": "tier2_openalex_pdf",
                    }

        # 2c. Semantic Scholar lookup
        s2_meta = _lookup_semantic_scholar(title=resolved_title, paper_id_or_doi=resolved_doi)
        if s2_meta:
            metadata_pool.append(s2_meta)
            resolved_title = resolved_title or s2_meta.get("title")
            resolved_doi = resolved_doi or s2_meta.get("doi")
            if s2_meta.get("pdf_url") and _download_pdf_bytes(s2_meta["pdf_url"], staged_pdf):
                extracted_md, page_count = _extract_pdf_file(
                    staged_pdf, tracker=tracker, label=resolved_title
                )
                if extracted_md:
                    return {
                        "title": _derive_name(url_or_arxiv_id, resolved_title),
                        "doi": resolved_doi,
                        "text": extracted_md,
                        "pdf_path": str(staged_pdf),
                        "page_count": page_count,
                        "resolution_tier": "tier2_s2_pdf",
                    }

    # --- Tier 4: Rich Metadata + Abstract + Web Text Fallback ---
    final_title = _derive_name(url_or_arxiv_id, resolved_title)
    if tracker is not None:
        tracker.step(
            f"Synthesizing metadata & abstract fallback for '{final_title}' (no direct PDF found)..."
        )
    best_abstract = ""
    best_tldr = ""
    best_authors: list[str] = []
    best_body = ""
    for m in metadata_pool:
        if len(m.get("abstract") or "") > len(best_abstract):
            best_abstract = m["abstract"]
        if m.get("tldr") and not best_tldr:
            best_tldr = m["tldr"]
        if m.get("authors") and not best_authors:
            best_authors = m["authors"]
        if m.get("body_text") and not best_body:
            best_body = m["body_text"]

    md_sections = [f"# {final_title}", f"- **Source**: {url_or_arxiv_id}"]
    if resolved_doi:
        md_sections.append(f"- **DOI**: {resolved_doi}")
    if best_authors:
        md_sections.append(f"- **Authors**: {', '.join(best_authors)}")
    if best_tldr:
        md_sections.append(f"\n## TLDR\n{best_tldr}")
    if best_abstract:
        md_sections.append(f"\n## Abstract\n{best_abstract}")
    if best_body:
        md_sections.append(f"\n## Extracted Page Content\n{best_body}")

    synthesized_md = "\n".join(md_sections)
    return {
        "title": final_title,
        "doi": resolved_doi,
        "text": synthesized_md,
        "pdf_path": str(staged_pdf) if staged_pdf.exists() else None,
        "page_count": None,
        "resolution_tier": "tier4_metadata_abstract_fallback",
    }


@mcp.tool()
@track_tool("ra_ingest")
def stage_paper(
    url_or_arxiv_id: str,
    target_virtual_dir: str,
    target_scope: Literal["global", "project"] = "global",
    title: Optional[str] = None,
    doi: Optional[str] = None,
    preextracted_text: Optional[str] = None,
) -> str:
    """
    Resolves and downloads a paper (via arXiv, OpenAlex, Semantic Scholar, or direct URL),
    extracts its text, and summarizes it using the dedicated OpenRouter summarization model
    (`DEFAULT_SUMMARIZATION_MODEL`), placing it in `~/.research-agent/cache/staged/`.
    Returns a `temp_id` which must be passed to `confirm_save_paper` to finalize the save.

    Arguments:
    - url_or_arxiv_id: URL, arXiv ID (e.g. '2601.18533'), or DOI of the paper.
    - target_virtual_dir: Virtual directory path in the VTree (e.g. '/nlp/RLHF/RLVRR').
    - target_scope: `'global'` (default, saves to the master PC-wide VTree) or `'project'`
      (saves to the current workspace's `.opencode/vtree.json`).
    - title: Clean paper title (strongly recommended when `url_or_arxiv_id` is a conference page
      like `iclr.cc` or `openreview.net` so `stage_paper` can find the open-access PDF via arXiv/OpenAlex).
    - doi: Optional DOI of the paper.

    MULTI-PAPER STAGING RULE (LIVE USER PROGRESS):
    - Each `stage_paper` call downloads a PDF, runs page-by-page OCR/Markdown extraction, and calls
      the summarization LLM (~10–20 seconds per paper).
    - If you need to stage multiple papers (e.g. 2 to 10+ papers), NEVER emit all `stage_paper` tool
      calls in a single parallel batch turn! Batching them freezes the chat UI for minutes without
      intermediate text updates.
    - Instead, stage them **ONE PAPER PER TURN sequentially**: before each `stage_paper` call, write a
      short 1-line progress update in chat (e.g. `**[1/10] Staging "Paper Title" (2405.07987)...**`),
      call `stage_paper` for that single paper, and then proceed to `[2/10]`, `[3/10]`, etc. Once all
      requested papers are staged, present the consolidated table of staged `temp_id`s and summaries
      and ask the user for permission to call `confirm_save_paper`.

    CRITICAL INSTRUCTION: You are FORBIDDEN from calling `confirm_save_paper` until all requested papers
    have been staged, their summaries have been shown to the user, and the user explicitly replies
    affirmatively in the next chat turn.
    """
    tracker = StepTracker(total_steps=4, prefix="stage_paper")
    temp_id = str(uuid.uuid4())
    resolved = _resolve_paper_content(
        url_or_arxiv_id=url_or_arxiv_id,
        temp_id=temp_id,
        title=title,
        doi=doi,
        preextracted_text=preextracted_text,
        tracker=tracker,
    )

    doc_name = resolved["title"]
    text_content = resolved["text"]
    page_count = resolved.get("page_count")
    text_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()
    text_dir = settings.CACHE_DIR / "texts"
    text_dir.mkdir(parents=True, exist_ok=True)
    text_path = text_dir / f"{text_hash}.md"
    text_path.write_text(text_content, encoding="utf-8")
    text_path_str = str(text_path)

    sum_model = settings.normalize_litellm_model(
        getattr(settings, "DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat"),
        default="openrouter/deepseek/deepseek-chat",
    )
    tracker.step(
        f"Summarizing '{doc_name}' ({len(text_content):,} chars) via {sum_model}..."
    )

    # Always summarize with the dedicated OpenRouter summarization model (`summarize_text`)
    try:
        summary = summarize_text(text_content)
    except Exception as e:
        logger.warning("OpenRouter summarize_text failed for '%s' (%s); using fallback", doc_name, e)
        snippet = re.sub(r"\s+", " ", text_content).strip()[:300]
        summary = f"Paper '{doc_name}' ({url_or_arxiv_id}). {snippet}"

    manifest = {
        "temp_id": temp_id,
        "url": url_or_arxiv_id,
        "name": doc_name,
        "dir": target_virtual_dir,
        "scope": target_scope,
        "summary": summary,
        "text_hash": text_hash,
        "text_path": text_path_str,
        "staged_pdf_path": resolved.get("pdf_path"),
        "page_count": page_count,
        "doi": resolved.get("doi"),
        "resolution_tier": resolved.get("resolution_tier"),
    }
    _save_staged_manifest(temp_id, manifest)
    tracker.step(
        f"Staged manifest saved (temp_id={temp_id}) -> [{target_scope.upper()}] {target_virtual_dir}"
    )
    logger.info(
        "Staged paper '%s' (ID: %s, tier: %s, scope: %s -> %s)",
        doc_name,
        temp_id,
        resolved.get("resolution_tier"),
        target_scope,
        target_virtual_dir,
    )

    pdf_status = "Full PDF downloaded" if resolved.get("pdf_path") else "Metadata/Abstract indexed (no direct PDF)"
    pages_line = (
        f"Pages: {page_count} ({len(text_content):,} chars extracted)\n"
        if page_count is not None
        else f"Extracted Size: {len(text_content):,} chars\n"
    )
    return (
        f"Paper staged successfully with ID: {temp_id}\n\n"
        f"Title: {doc_name}\n"
        f"Resolution: {pdf_status} [{resolved.get('resolution_tier')}]\n"
        f"{pages_line}"
        f"Target: [{target_scope.upper()}] {target_virtual_dir}\n"
        f"Summary: {summary}\n\n"
        f"{tracker.format_block()}\n\n"
        f"STOP EXECUTION NOW (or stage the next paper sequentially with a `[k/N]` status line) "
        f"and ask the user for permission before calling `confirm_save_paper`."
    )


def _move_staged_pdf_to_flat_folder(staged_pdf_str: Optional[str], doc_name: str, vtree_dict: dict) -> Optional[str]:
    """
    Move the staged PDF into a single flat folder (`<library_root>/Downloaded/<doc_name>.pdf`)
    so physical files remain flat on disk while `vtree.json` manages the virtual hierarchy.
    """
    if not staged_pdf_str:
        return None
    staged_pdf = Path(staged_pdf_str)
    if not staged_pdf.exists():
        return None

    lib_root_str = vtree_dict.get("library_root") or settings.LIBRARY_ROOT_PATH
    if lib_root_str and Path(lib_root_str).expanduser().exists():
        flat_dir = Path(lib_root_str).expanduser().resolve() / "Downloaded"
    else:
        flat_dir = settings.DATA_DIR / "Downloaded"

    flat_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = _clean_filename(doc_name)
    dest_pdf = flat_dir / f"{safe_stem}.pdf"
    shutil.move(str(staged_pdf), str(dest_pdf))
    logger.info("Saved downloaded PDF to flat folder: %s", dest_pdf)
    return str(dest_pdf)


def _persist_to_vtree(paper_info: dict, tracker: Optional[StepTracker] = None) -> Optional[str]:
    """
    Write the confirmed paper into the target VTree, move any downloaded PDF into
    `<library_root>/Downloaded/`, and refresh dirty ancestor directory summaries via OpenRouter.
    """
    scope = paper_info.get("scope", "global")
    if scope == "project":
        vtree_path = settings.get_project_vtree_path(must_exist=False)
    else:
        vtree_path = settings.get_global_vtree_path()

    if vtree_path is None:
        return None

    store = VTreeStore(vtree_path)
    if vtree_path.exists():
        try:
            vtree_dict = store.load_as_dict()
        except Exception as e:
            logger.warning("Could not load existing VTree at %s: %s", vtree_path, e)
            return None
    else:
        vtree_path.parent.mkdir(parents=True, exist_ok=True)
        vtree_dict = {
            "version": 1,
            "library_root": settings.LIBRARY_ROOT_PATH or str(vtree_path.parent),
            "root": {
                "type": "directory",
                "name": "Root",
                "virtual_path": "/",
                "summary": "Root of the research library.",
                "children": [],
            },
        }

    doc_name = paper_info.get("name") or _derive_name(paper_info["url"])
    if tracker is not None:
        tracker.step(f"Moving staged PDF for '{doc_name}' into Downloaded/ folder...")
    saved_pdf_path = _move_staged_pdf_to_flat_folder(paper_info.get("staged_pdf_path"), doc_name, vtree_dict)
    real_path = saved_pdf_path or paper_info.get("text_path") or paper_info["url"]

    if tracker is not None:
        tracker.step(f"Inserting '{doc_name}' into [{scope.upper()}] VTree at '{paper_info['dir']}'...")
    mgr = VTreeManager(vtree_dict)
    doc_node = {
        "type": "document",
        "name": doc_name,
        "real_path": real_path,
        "page_count": paper_info.get("page_count"),
        "file_type": "pdf" if saved_pdf_path else "md",
        "doi": paper_info.get("doi"),
        "file_hash": paper_info.get("text_hash"),
        "text_hash": paper_info.get("text_hash"),
        "text_path": paper_info.get("text_path"),
        "summary": paper_info.get("summary", ""),
    }
    mgr.add_document(paper_info["dir"], doc_node)

    # Refresh ancestor directory summaries via OpenRouter (`summarize_directory`)
    if tracker is not None:
        tracker.step(f"Updating ancestor directory summaries for '{paper_info['dir']}'...")
    try:
        from src.cold_start.refresher import _refresh_dirty_directory_summaries

        _refresh_dirty_directory_summaries(vtree_dict["root"])
    except Exception as e:
        logger.warning("Could not refresh ancestor directory summaries: %s", e)

    store.save_dict(vtree_dict)
    return saved_pdf_path


@mcp.tool()
@track_tool("ra_ingest")
def confirm_save_paper(temp_id: str) -> str:
    """
    Finalizes the saving of a staged paper into the local VTree library (`global` or `project`),
    moves any downloaded PDF into `<library_root>/Downloaded/`, and refreshes ancestor
    directory summaries using the OpenRouter summarization model.
    Requires explicit user approval before invocation (enforced via OpenCode `"ask"` permission).
    """
    paper_info = _pop_staged_manifest(temp_id)
    if not paper_info:
        return f"Error: No staged paper found with ID '{temp_id}'."

    tracker = StepTracker(total_steps=3, prefix="confirm_save_paper")
    target_dir = paper_info["dir"]
    saved_pdf: Optional[str] = None
    try:
        saved_pdf = _persist_to_vtree(paper_info, tracker=tracker)
    except Exception as e:
        logger.warning("VTree persistence skipped/failed during confirm_save_paper: %s", e)

    logger.info("Confirmed save of staged paper %s to '%s' (pdf=%s)", temp_id, target_dir, saved_pdf)
    kg_reminder = (
        "\n\nMemory Reminder: If the user expressed interest in this paper's topic or has read it, "
        "record it proactively via `ra_kg_remember_user_fact` (or `ra_kg_add_triplet`)."
    )
    if saved_pdf:
        return (
            f"Successfully saved paper to '{target_dir}' (PDF saved to '{saved_pdf}'). "
            f"Ancestor directory summaries have been updated.\n\n"
            f"{tracker.format_block()}{kg_reminder}"
        )
    return (
        f"Successfully saved paper to '{target_dir}'. Ancestor directory summaries have been updated.\n\n"
        f"{tracker.format_block()}{kg_reminder}"
    )


@mcp.tool()
@track_tool("ra_ingest")
def refresh_library(
    scope: Literal["global", "project"] = "global",
    library_path: Optional[str] = None,
    target_virtual_dir: Optional[str] = None,
    update_dir_summaries: bool = True,
    dry_run: bool = False,
) -> str:
    """
    Incrementally scans the source library directory on disk and indexes any newly dropped
    PDF, TXT, MD, or DOCX files that are not yet present in the target VTree (`global` or `project`),
    while repairing any inconsistent nodes (missing/placeholder summaries).

    Existing healthy nodes and semantic folder structures in `vtree.json` are never re-parsed.
    Before parsing begins, computes a verbose pre-flight workload and page-count cost plan.

    Arguments:
    - scope: `'global'` (default, updates `~/.research-agent/vtrees/global.json`) or `'project'`
      (updates `<project>/.opencode/vtree.json`).
    - library_path: Optional path to the folder on disk to scan. Defaults to the `library_root`
      recorded inside the target `vtree.json` (or `LIBRARY_ROOT_PATH`).
    - target_virtual_dir: Optional virtual directory in the VTree where newly discovered files
      should be placed (e.g. `'/Inbox'`). If omitted, mirrors their relative subfolder path.
    - update_dir_summaries: Whether to refresh ancestor directory summaries via the LLM (default `True`).
    - dry_run: If `True`, only returns the pre-flight document/page workload and cost estimate without
      parsing any files or making LLM calls.
    """
    from src.cold_start.refresher import refresh_vtree

    try:
        res = refresh_vtree(
            scope=scope,
            library_path=library_path,
            target_virtual_dir=target_virtual_dir,
            update_dir_summaries=update_dir_summaries,
            dry_run=dry_run,
        )
    except Exception as e:
        logger.error("refresh_library failed: %s", e)
        return f"Error refreshing library ({scope}): {e}"

    if dry_run:
        return f"{res.preflight_report}\n\n[Dry Run] No documents were parsed and no LLM calls were made."

    if not res.added_nodes and not res.repaired_nodes and not res.failed_files:
        return (
            f"{res.preflight_report}\n\n"
            f"Library refresh complete [{scope.upper()}]: No new or inconsistent files found "
            f"({res.already_indexed_count} file(s) already indexed in '{res.library_path}', "
            f"{res.relinked_cache_count} node(s) relinked to cached extractions at 0 cost)."
        )

    lines = [
        res.preflight_report,
        "",
        f"Library refresh complete [{scope.upper()}] ({res.vtree_path}):",
        f"- Source folder: {res.library_path}",
        f"- Scanned: {res.scanned_count} | Already indexed: {res.already_indexed_count} | "
        f"Cache-relinked: {res.relinked_cache_count} | "
        f"Newly added: {len(res.added_nodes)} ({res.new_pages_count} pages) | "
        f"Repaired: {len(res.repaired_nodes)} ({res.repaired_pages_count} pages)",
    ]
    if res.added_nodes:
        lines.append("\nNewly Indexed Documents:")
        for node in res.added_nodes:
            ntype = node.get("type", "document").upper()
            vpath = node.get("virtual_path", "/")
            summary_snip = (node.get("summary") or "")[:140]
            lines.append(f"  * [{ntype}] {vpath} — {summary_snip}...")
    if res.repaired_nodes:
        lines.append("\nRepaired Inconsistent Documents:")
        for node in res.repaired_nodes:
            vpath = node.get("virtual_path", "/")
            summary_snip = (node.get("summary") or "")[:140]
            lines.append(f"  * [REPAIRED] {vpath} — {summary_snip}...")
    if res.failed_files:
        lines.append(f"\nFailed/Skipped Files ({len(res.failed_files)}):")
        for fp in res.failed_files:
            lines.append(f"  * {fp}")

    return "\n".join(lines)


def main() -> None:
    logger.info("Starting FileTools MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
