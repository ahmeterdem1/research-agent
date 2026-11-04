"""
Incremental VTree Refresher & Self-Healer (``src/cold_start/refresher.py``).

Features:
1. **Zero-Cost Cache Relinking**: Automatically restores missing ``text_hash`` / ``text_path``
   fields on Cold-Start ``vtree.json`` nodes from ``cs_02_extract.json`` / ``cs_03_summarize.json``
   without re-parsing or re-summarizing.
2. **Accurate Inconsistency Detection**: Only repairs document nodes whose ``summary`` is missing
   or is a stub placeholder (e.g. ``"Staged paper ..."`` / ``"Summary generation failed."``).
   Healthy documents with valid summaries are **never** re-parsed or re-summarized.
3. **Verbose Pre-Flight Workload & Cost Report**: Before sending any document or page to the
   parser/LLM, computes and logs/prints the exact number of documents, PDF pages, and directory
   summaries that will be processed (and supports ``dry_run=True``).
"""

import concurrent.futures
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Callable, Literal, Optional

from config import settings
from config.settings import get_logger
from src.cold_start.extractor import extract_content
from src.cold_start.scanner import FileInfo, scan_directory
from src.cold_start.splitter import split_book
from src.cold_start.summarizer import summarize_book, summarize_directory, summarize_text
from src.vtree.operations import VTreeManager
from src.vtree.store import VTreeStore

logger = get_logger(__name__)


@dataclass
class RefreshResult:
    vtree_path: Path
    library_path: Path
    scope: str
    scanned_count: int = 0
    already_indexed_count: int = 0
    relinked_cache_count: int = 0
    planned_new_docs: int = 0
    planned_new_pages: int = 0
    planned_repair_docs: int = 0
    planned_repair_pages: int = 0
    new_pages_count: int = 0
    repaired_pages_count: int = 0
    planned_dir_summaries: int = 0
    preflight_report: str = ""
    added_nodes: list[dict] = field(default_factory=list)
    repaired_nodes: list[dict] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)


def _norm_path(p: str | Path) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except Exception:
        return str(p)


def collect_indexed_real_paths(node: dict) -> set[str]:
    """
    Recursively collect all `real_path` values (both raw and resolved) for
    `document` and `book` nodes in a VTree.
    """
    paths: set[str] = set()
    if not isinstance(node, dict):
        return paths

    if node.get("type") in ("document", "book"):
        real_path = node.get("real_path")
        if real_path:
            paths.add(str(real_path))
            paths.add(_norm_path(real_path))

    for child in node.get("children", []):
        paths.update(collect_indexed_real_paths(child))

    return paths


def _save_text_to_cache(text: str) -> tuple[str, Path]:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    text_dir = settings.CACHE_DIR / "texts"
    text_dir.mkdir(parents=True, exist_ok=True)
    text_path = text_dir / f"{text_hash}.md"
    if not text_path.exists():
        text_path.write_text(text, encoding="utf-8")
    return text_hash, text_path


def _infer_virtual_dir(file_real_path: str | Path, library_root: Path, explicit_dir: Optional[str]) -> str:
    if explicit_dir and explicit_dir.strip():
        clean = "/" + "/".join(p for p in explicit_dir.strip().split("/") if p)
        return clean

    try:
        rel_parent = Path(file_real_path).expanduser().resolve().relative_to(library_root.expanduser().resolve()).parent
        if str(rel_parent) == ".":
            return "/"
        return "/" + "/".join(rel_parent.parts)
    except ValueError:
        return "/"


def _load_cold_start_cache_map() -> dict[str, dict]:
    """
    Build a lookup map `real_path -> {"text_hash": ..., "text_path": ...}` from any existing
    `cs_03_summarize.json` or `cs_02_extract.json` files in `~/.research-agent/cache/` or `data/cache/`.
    """
    mapping: dict[str, dict] = {}
    candidate_dirs = [
        settings.CACHE_DIR,
        settings.PROJECT_ROOT / "data" / "cache",
    ]
    for cdir in candidate_dirs:
        for fname in ("cs_03_summarize.json", "cs_02_extract.json"):
            fpath = cdir / fname
            if not fpath.exists():
                continue
            try:
                items = json.loads(fpath.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    rp = item.get("real_path")
                    th = item.get("text_hash")
                    tp = item.get("text_path")
                    if rp and th and tp:
                        entry = {"text_hash": th, "text_path": tp}
                        mapping[str(rp)] = entry
                        mapping[_norm_path(rp)] = entry
                    for child in item.get("children", []):
                        if isinstance(child, dict) and child.get("real_path") and child.get("text_hash"):
                            c_entry = {"text_hash": child["text_hash"], "text_path": child.get("text_path")}
                            mapping[str(child["real_path"])] = c_entry
                            mapping[_norm_path(child["real_path"])] = c_entry
            except Exception as e:
                logger.debug("Could not read cache index %s: %s", fpath, e)
    return mapping


def _relink_cached_text_paths(node: dict, cache_map: dict[str, dict]) -> int:
    """
    Populate missing `text_hash` and `text_path` on healthy Cold-Start `document` nodes
    using `cache_map` at zero extraction/LLM cost.
    """
    if not isinstance(node, dict) or not cache_map:
        return 0
    relinked = 0
    if node.get("type") == "document" and not node.get("text_path") and not node.get("text_hash"):
        rp = node.get("real_path")
        if rp:
            hit = cache_map.get(str(rp)) or cache_map.get(_norm_path(rp))
            if hit:
                node["text_hash"] = hit["text_hash"]
                node["file_hash"] = hit["text_hash"]
                # Prefer relocated global cache path if available
                global_md = settings.CACHE_DIR / "texts" / f"{hit['text_hash']}.md"
                node["text_path"] = str(global_md) if global_md.exists() else hit["text_path"]
                relinked += 1

    for child in node.get("children", []):
        relinked += _relink_cached_text_paths(child, cache_map)
    return relinked


def _is_inconsistent_doc_node(node: dict) -> bool:
    """
    Detect document nodes left in an incomplete/stub state.

    IMPORTANT: A document node with a valid Cold-Start summary is NEVER marked inconsistent
    even if `text_path` was omitted by an earlier `vtree_builder.py` run. Only nodes whose
    `summary` is missing or matches a stub placeholder (`"Staged paper ..."`,
    `"Summary generation failed."`) are flagged for repair.
    """
    if not isinstance(node, dict) or node.get("type") != "document":
        return False
    summary = (node.get("summary") or "").strip()
    if (
        not summary
        or summary.startswith("Staged paper ")
        or summary.startswith("Paper '")
        or summary == "Summary generation failed."
    ):
        return True
    return False


def _find_inconsistent_doc_nodes(node: dict) -> list[dict]:
    """Collect all inconsistent `document` nodes in the VTree without mutating them."""
    found: list[dict] = []
    if not isinstance(node, dict):
        return found
    if _is_inconsistent_doc_node(node):
        found.append(node)
        return found
    if node.get("type") == "directory":
        for child in node.get("children", []):
            found.extend(_find_inconsistent_doc_nodes(child))
    return found


def _count_dirty_directories(node: dict) -> int:
    if not isinstance(node, dict) or node.get("type") != "directory":
        return 0
    count = 0
    if node.get("_needs_summary_update") or (
        node.get("summary") in (None, "", "User created directory.") and bool(node.get("children"))
    ):
        count += 1
    for child in node.get("children", []):
        if child.get("type") == "directory":
            count += _count_dirty_directories(child)
    return count


def _estimate_node_pages(node: dict) -> int:
    pc = node.get("page_count")
    if isinstance(pc, int) and pc > 0:
        return pc
    rp = node.get("real_path")
    if rp:
        p = Path(rp).expanduser()
        if p.exists() and p.is_file() and p.suffix.lower() == ".pdf":
            try:
                import fitz

                with fitz.open(p) as doc:
                    return doc.page_count
            except Exception:
                pass
    return 0


def _build_preflight_report(
    scope: str,
    library_path: Path,
    already_indexed_count: int,
    relinked_count: int,
    unindexed: list[FileInfo],
    inconsistent_nodes: list[dict],
    dirty_dir_count: int,
) -> tuple[str, int, int]:
    """
    Build a human-readable pre-flight cost and workload breakdown before any
    extraction or LLM calls are made.
    Returns `(report_text, new_pages_total, repair_pages_total)`.
    """
    extractor_mode = getattr(settings, "EXTRACTOR_MODE", "docling").lower()
    vision_model = settings.normalize_litellm_model(
        getattr(settings, "VISION_MODEL", settings.DEFAULT_SUMMARIZATION_MODEL),
        default="openrouter/google/gemini-2.5-flash",
    )
    sum_model = settings.normalize_litellm_model(
        getattr(settings, "DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat"),
        default="openrouter/deepseek/deepseek-chat",
    )
    parser_desc = f"LLM Vision OCR ({vision_model})" if extractor_mode == "llm" else "Local Docling (free)"

    new_pages = sum((f.page_count or 1) for f in unindexed)
    repair_pages = sum(_estimate_node_pages(n) for n in inconsistent_nodes)
    total_docs_to_parse = len(unindexed) + len(inconsistent_nodes)
    total_pages_to_parse = new_pages + repair_pages
    total_sum_calls = len(unindexed) + len(inconsistent_nodes) + dirty_dir_count

    lines = [
        f"=== VTree Refresh Pre-Flight Plan [{scope.upper()}] ===",
        f"Source Library             : {library_path}",
        f"PDF Parser Mode            : {parser_desc}",
        f"Summarization Model        : {sum_model}",
        f"Healthy Indexed (Skipped)  : {already_indexed_count} file(s) (0 pages to parse)",
    ]
    if relinked_count:
        lines.append(f"Cache Relinked (Zero-Cost) : {relinked_count} document(s) linked from existing cache")

    lines.append(f"New Files on Disk to Parse : {len(unindexed)} file(s) ({new_pages} page(s))")
    for f in unindexed:
        pg_label = f"{f.page_count} pages" if f.page_count is not None else "text file"
        lines.append(f"  + [NEW] {f.name} ({pg_label})")

    lines.append(
        f"Inconsistent Nodes to Heal : {len(inconsistent_nodes)} node(s) "
        f"({repair_pages if repair_pages else 'unknown (remote)'} known local page(s))"
    )
    for n in inconsistent_nodes:
        vpath = n.get("virtual_path") or n.get("name", "unnamed")
        rp = n.get("real_path", "unknown")
        lines.append(f"  ~ [REPAIR] {vpath} (source: {rp})")

    lines.append(f"Dirty Directories to Update: {dirty_dir_count} directory summary call(s)")
    lines.append("-" * 54)
    lines.append(
        f"Estimated API Workload     : {total_docs_to_parse} document(s) to parse "
        f"(~{total_pages_to_parse} known PDF page(s)) | {total_sum_calls} summarization LLM call(s)"
    )
    return "\n".join(lines), new_pages, repair_pages


def _process_new_file(finfo: FileInfo, book_threshold: int) -> Optional[dict]:
    """Extract and summarize a single newly discovered file or splittable book."""
    pg_str = f"{finfo.page_count} pages" if finfo.page_count is not None else f"{finfo.file_type}"
    logger.info("Parsing new file '%s' (%s)...", finfo.name, pg_str)
    if finfo.is_book:
        if not finfo.is_splittable:
            logger.warning("Skipping unsplittable book during refresh: %s", finfo.real_path)
            return None

        split_res = split_book(finfo, limit=book_threshold)
        if not split_res.is_split or not split_res.sections:
            logger.warning("Could not split book '%s': %s", finfo.name, split_res.warning)
            return None

        children: list[dict] = []
        for sec in split_res.sections:
            try:
                content = extract_content(sec.real_path)
                if not content or not content.text.strip():
                    continue
                text_hash, text_path = _save_text_to_cache(content.text)
                try:
                    sec_summary = summarize_text(content.text)
                except Exception as e:
                    logger.warning("Failed summarizing section '%s': %s", sec.name, e)
                    sec_summary = "Summary generation failed."

                children.append(
                    {
                        "type": "document",
                        "name": sec.name,
                        "real_path": finfo.real_path,
                        "page_count": sec.page_count,
                        "file_hash": text_hash,
                        "text_hash": text_hash,
                        "text_path": str(text_path),
                        "summary": sec_summary,
                    }
                )
            finally:
                if _norm_path(sec.real_path) != _norm_path(finfo.real_path):
                    Path(sec.real_path).unlink(missing_ok=True)

        if not children:
            return None

        child_summaries = [f"{c['name']}: {c.get('summary', '')}" for c in children]
        try:
            book_sum = summarize_book(child_summaries)
            summary = f"A book containing multiple sections. {book_sum}"
        except Exception as e:
            logger.warning("Failed summarizing book '%s': %s", finfo.name, e)
            summary = "A book containing multiple sections. Summary generation failed."

        return {
            "type": "book",
            "name": finfo.name,
            "real_path": finfo.real_path,
            "page_count": finfo.page_count,
            "summary": summary,
            "children": children,
        }

    content = extract_content(finfo.real_path)
    if not content or not content.text.strip():
        logger.warning("No extractable text found in %s", finfo.real_path)
        return None

    text_hash, text_path = _save_text_to_cache(content.text)
    try:
        summary = summarize_text(content.text)
    except Exception as e:
        logger.warning("Failed summarizing document '%s': %s", finfo.name, e)
        summary = "Summary generation failed."

    node: dict = {
        "type": "document",
        "name": finfo.name,
        "real_path": finfo.real_path,
        "file_hash": text_hash,
        "text_hash": text_hash,
        "text_path": str(text_path),
        "summary": summary,
    }
    if finfo.page_count is not None:
        node["page_count"] = finfo.page_count
    return node


def _refresh_dirty_directory_summaries(node: dict) -> None:
    """Recompute directory summaries for directories marked `_needs_summary_update` or lacking a summary."""
    if not isinstance(node, dict) or node.get("type") != "directory":
        return

    for child in node.get("children", []):
        if child.get("type") == "directory":
            _refresh_dirty_directory_summaries(child)

    needs_update = bool(node.get("_needs_summary_update")) or (
        node.get("summary") in (None, "", "User created directory.") and bool(node.get("children"))
    )
    if needs_update:
        child_sums = [
            f"{c.get('name', 'unnamed')}: {c.get('summary', '')}"
            for c in node.get("children", [])
            if c.get("summary") and c.get("summary") != "User created directory."
        ]
        if child_sums:
            try:
                node["summary"] = summarize_directory(child_sums)
            except Exception as e:
                logger.warning("Directory summary update failed for '%s': %s", node.get("virtual_path"), e)
        node.pop("_needs_summary_update", None)


def _repair_nodes_list(inconsistent_nodes: list[dict], vtree_dict: dict, mgr: VTreeManager) -> list[dict]:
    """Heal the pre-identified list of inconsistent `document` nodes in place."""
    if not inconsistent_nodes:
        return []

    import uuid
    from src.mcp_servers.file_tools import (
        _move_staged_pdf_to_flat_folder,
        _resolve_paper_content,
    )

    repaired: list[dict] = []
    for node in inconsistent_nodes:
        real_path = node.get("real_path") or ""
        title = node.get("name")
        doi = node.get("doi")
        logger.info("Repairing inconsistent VTree document node '%s' (source=%s)", title, real_path)
        try:
            local_candidate = Path(real_path).expanduser() if real_path else None
            if local_candidate and local_candidate.exists() and local_candidate.is_file():
                content = extract_content(local_candidate)
                text_content = content.text if content and content.text else ""
                saved_pdf_path = str(local_candidate.resolve()) if local_candidate.suffix.lower() == ".pdf" else None
                page_count = node.get("page_count")
            else:
                temp_id = f"repair-{uuid.uuid4()}"
                resolved = _resolve_paper_content(
                    url_or_arxiv_id=real_path or title or "",
                    temp_id=temp_id,
                    title=title,
                    doi=doi,
                )
                text_content = resolved["text"]
                saved_pdf_path = _move_staged_pdf_to_flat_folder(
                    resolved.get("pdf_path"),
                    title or resolved["title"],
                    vtree_dict,
                )
                page_count = resolved.get("page_count")
                if resolved.get("doi") and not node.get("doi"):
                    node["doi"] = resolved["doi"]

            if text_content.strip():
                text_hash, text_path = _save_text_to_cache(text_content)
                node["text_hash"] = text_hash
                node["file_hash"] = text_hash
                node["text_path"] = str(text_path)
                if saved_pdf_path:
                    node["real_path"] = saved_pdf_path
                    node["file_type"] = "pdf"
                if page_count is not None:
                    node["page_count"] = page_count
                node["summary"] = summarize_text(text_content)
                if node.get("virtual_path"):
                    mgr.update_ancestor_summaries(node["virtual_path"], node["summary"])
                repaired.append(node)
        except Exception as e:
            logger.warning("Failed to repair inconsistent node '%s': %s", title, e)
    return repaired


def refresh_vtree(
    scope: Literal["global", "project"] = "global",
    library_path: Optional[str | Path] = None,
    vtree_path: Optional[str | Path] = None,
    target_virtual_dir: Optional[str] = None,
    book_threshold: int = settings.BOOK_PAGE_THRESHOLD,
    update_dir_summaries: bool = True,
    reorganize: bool = False,
    dry_run: bool = False,
    on_preflight: Optional[Callable[[str], None]] = None,
) -> RefreshResult:
    """
    Incrementally scan `library_path` to index any newly dropped files AND automatically
    heal any existing inconsistent/stub nodes or placeholder directory summaries in `vtree.json`.

    Before parsing any document or calling any LLM, computes and emits a verbose Pre-Flight
    Workload & Cost Plan listing the exact document and page counts to be processed.
    """
    if vtree_path:
        target_vtree = Path(vtree_path).expanduser().resolve()
    elif scope == "project":
        target_vtree = settings.get_project_vtree_path(must_exist=False)
    else:
        target_vtree = settings.get_global_vtree_path()

    store = VTreeStore(target_vtree)
    vtree_dict: Optional[dict] = None
    if target_vtree.exists():
        vtree_dict = store.load_as_dict()

    candidate_lib = (
        str(library_path).strip()
        if library_path
        else (vtree_dict.get("library_root", "") if vtree_dict else "") or settings.LIBRARY_ROOT_PATH
    )
    if not candidate_lib:
        raise ValueError(
            "No library_path provided and no library_root recorded in VTree or LIBRARY_ROOT_PATH."
        )

    resolved_lib = Path(candidate_lib).expanduser().resolve()
    if not resolved_lib.exists() or not resolved_lib.is_dir():
        raise ValueError(f"Library directory does not exist: {resolved_lib}")

    if vtree_dict is None:
        target_vtree.parent.mkdir(parents=True, exist_ok=True)
        vtree_dict = {
            "version": 1,
            "library_root": str(resolved_lib),
            "root": {
                "type": "directory",
                "name": "Root",
                "virtual_path": "/",
                "summary": "",
                "children": [],
            },
        }
    elif not vtree_dict.get("library_root"):
        vtree_dict["library_root"] = str(resolved_lib)

    mgr = VTreeManager(vtree_dict)

    # Step 0: Zero-cost cache relinking for Cold-Start nodes missing `text_path`/`text_hash`
    cache_map = _load_cold_start_cache_map()
    relinked_count = _relink_cached_text_paths(vtree_dict.get("root", {}), cache_map)

    # Step 1: Pre-flight discovery (no extraction or LLM calls yet)
    inconsistent_nodes = _find_inconsistent_doc_nodes(vtree_dict.get("root", {}))
    indexed_paths = collect_indexed_real_paths(vtree_dict.get("root", {}))
    scanned_files = scan_directory(resolved_lib, book_threshold=book_threshold)

    unindexed: list[FileInfo] = []
    already_count = 0
    for f in scanned_files:
        if f.real_path in indexed_paths or _norm_path(f.real_path) in indexed_paths:
            already_count += 1
        else:
            unindexed.append(f)

    dirty_dir_count = _count_dirty_directories(vtree_dict.get("root", {})) if update_dir_summaries else 0
    preflight_text, new_pages, repair_pages = _build_preflight_report(
        scope=scope,
        library_path=resolved_lib,
        already_indexed_count=already_count,
        relinked_count=relinked_count,
        unindexed=unindexed,
        inconsistent_nodes=inconsistent_nodes,
        dirty_dir_count=dirty_dir_count,
    )

    for line in preflight_text.splitlines():
        logger.info(line)
    if on_preflight:
        on_preflight(preflight_text)

    result = RefreshResult(
        vtree_path=target_vtree,
        library_path=resolved_lib,
        scope=scope,
        scanned_count=len(scanned_files),
        already_indexed_count=already_count,
        relinked_cache_count=relinked_count,
        planned_new_docs=len(unindexed),
        planned_new_pages=new_pages,
        planned_repair_docs=len(inconsistent_nodes),
        planned_repair_pages=repair_pages,
        new_pages_count=new_pages,
        repaired_pages_count=repair_pages,
        planned_dir_summaries=dirty_dir_count,
        preflight_report=preflight_text,
    )

    if dry_run:
        logger.info("Dry run requested (--dry-run); skipping extraction and LLM calls.")
        return result

    # Step 2: Heal inconsistent nodes (only those with broken/stub summaries)
    result.repaired_nodes = _repair_nodes_list(inconsistent_nodes, vtree_dict, mgr)

    # Step 3: Parse & summarize newly dropped files on disk
    if unindexed:
        max_workers = max(1, int(getattr(settings, "MAX_CONCURRENT_REQUESTS", 5)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_finfo = {
                executor.submit(_process_new_file, finfo, book_threshold): finfo
                for finfo in unindexed
            }
            for future in concurrent.futures.as_completed(future_to_finfo):
                finfo = future_to_finfo[future]
                try:
                    node_dict = future.result()
                    if node_dict is None:
                        result.failed_files.append(finfo.real_path)
                        continue

                    vdir = _infer_virtual_dir(finfo.real_path, resolved_lib, target_virtual_dir)
                    inserted = mgr.add_document(vdir, node_dict)
                    result.added_nodes.append(inserted)
                except Exception as e:
                    logger.error("Failed processing new file '%s' during refresh: %s", finfo.real_path, e)
                    result.failed_files.append(finfo.real_path)

    # Step 4: Refresh dirty directory summaries & persist VTree if anything changed
    dirty_dirs_after = update_dir_summaries and (_count_dirty_directories(vtree_dict.get("root", {})) > 0)
    if result.added_nodes or result.repaired_nodes or dirty_dirs_after or relinked_count > 0:
        if update_dir_summaries:
            _refresh_dirty_directory_summaries(vtree_dict["root"])
        if reorganize:
            from src.cold_start.vtree_organizer import organize_vtree

            vtree_dict = organize_vtree(vtree_dict)
        store.save_dict(vtree_dict)
        logger.info(
            "Saved refreshed VTree (%s): %d new node(s), %d repaired node(s), %d relinked -> %s",
            scope,
            len(result.added_nodes),
            len(result.repaired_nodes),
            relinked_count,
            target_vtree,
        )

    return result
