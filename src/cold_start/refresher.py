"""
Incremental VTree Refresher (``src/cold_start/refresher.py``).

Scans a source directory on disk, compares discovered files against all ``real_path``
entries already present in an existing ``vtree.json`` (regardless of where those nodes
have been moved inside the virtual tree), processes **only** the newly added files
(Docling/text extraction + LLM summarization), and inserts them into the VTree without
disturbing existing nodes or semantic folder organization.
"""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Literal, Optional

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


def _process_new_file(finfo: FileInfo, book_threshold: int) -> Optional[dict]:
    """Extract and summarize a single newly discovered file or splittable book."""
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
                        "text_path": str(text_path),
                        "summary": sec_summary,
                    }
                )
            finally:
                # Remove temporary split chapter PDF if it is not the original book file
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

    # Standard document (.pdf, .txt, .md, .docx)
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


def _has_dirty_directories(node: dict) -> bool:
    if not isinstance(node, dict) or node.get("type") != "directory":
        return False
    if node.get("_needs_summary_update") or (
        node.get("summary") in (None, "", "User created directory.") and bool(node.get("children"))
    ):
        return True
    return any(_has_dirty_directories(c) for c in node.get("children", []) if c.get("type") == "directory")


def _is_inconsistent_doc_node(node: dict) -> bool:
    """
    Detect document nodes left in an incomplete/stub state (e.g. missing `text_path`
    or carrying placeholder summaries like `"Staged paper ..."`).
    """
    if not isinstance(node, dict) or node.get("type") != "document":
        return False
    summary = (node.get("summary") or "").strip()
    if not summary or summary.startswith("Staged paper ") or summary == "Summary generation failed.":
        return True
    if not node.get("text_path") and not node.get("file_hash") and not node.get("text_hash"):
        return True
    return False


def _repair_inconsistent_nodes(vtree_dict: dict, mgr: VTreeManager) -> list[dict]:
    """
    Walk `vtree_dict` and heal only inconsistent `document` nodes in place without
    re-summarizing healthy nodes.
    """
    import uuid
    from src.mcp_servers.file_tools import (
        _move_staged_pdf_to_flat_folder,
        _resolve_paper_content,
    )

    repaired: list[dict] = []

    def _walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        if _is_inconsistent_doc_node(node):
            real_path = node.get("real_path") or ""
            title = node.get("name")
            doi = node.get("doi")
            logger.info("Repairing inconsistent VTree document node '%s' (real_path=%s)", title, real_path)
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
            return

        # Only recurse into directories (skip healthy book children unless broken)
        if node.get("type") == "directory":
            for child in node.get("children", []):
                _walk(child)

    _walk(vtree_dict.get("root", {}))
    return repaired


def refresh_vtree(
    scope: Literal["global", "project"] = "global",
    library_path: Optional[str | Path] = None,
    vtree_path: Optional[str | Path] = None,
    target_virtual_dir: Optional[str] = None,
    book_threshold: int = settings.BOOK_PAGE_THRESHOLD,
    update_dir_summaries: bool = True,
    reorganize: bool = False,
) -> RefreshResult:
    """
    Incrementally scan `library_path` to index any newly dropped files AND automatically
    heal any existing inconsistent/incomplete nodes or placeholder directory summaries in `vtree.json`.

    Healthy nodes and virtual folder structures in `vtree.json` are preserved untouched.
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

    # Resolve source library path from argument -> vtree["library_root"] -> settings.LIBRARY_ROOT_PATH
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

    # Pass 1: Heal any inconsistent/stub document nodes already in the VTree
    repaired_nodes = _repair_inconsistent_nodes(vtree_dict, mgr)

    # Pass 2: Scan `resolved_lib` (excluding the flat `Downloaded/` subfolder if already indexed)
    indexed_paths = collect_indexed_real_paths(vtree_dict.get("root", {}))
    scanned_files = scan_directory(resolved_lib, book_threshold=book_threshold)

    unindexed: list[FileInfo] = []
    already_count = 0
    for f in scanned_files:
        if f.real_path in indexed_paths or _norm_path(f.real_path) in indexed_paths:
            already_count += 1
        else:
            unindexed.append(f)

    result = RefreshResult(
        vtree_path=target_vtree,
        library_path=resolved_lib,
        scope=scope,
        scanned_count=len(scanned_files),
        already_indexed_count=already_count,
        repaired_nodes=repaired_nodes,
    )

    for finfo in unindexed:
        try:
            node_dict = _process_new_file(finfo, book_threshold=book_threshold)
            if node_dict is None:
                result.failed_files.append(finfo.real_path)
                continue

            vdir = _infer_virtual_dir(finfo.real_path, resolved_lib, target_virtual_dir)
            inserted = mgr.add_document(vdir, node_dict)
            result.added_nodes.append(inserted)
        except Exception as e:
            logger.error("Failed processing new file '%s' during refresh: %s", finfo.real_path, e)
            result.failed_files.append(finfo.real_path)

    dirty_dirs = update_dir_summaries and _has_dirty_directories(vtree_dict.get("root", {}))
    if result.added_nodes or result.repaired_nodes or dirty_dirs:
        if update_dir_summaries:
            _refresh_dirty_directory_summaries(vtree_dict["root"])
        if reorganize:
            from src.cold_start.vtree_organizer import organize_vtree

            vtree_dict = organize_vtree(vtree_dict)
        store.save_dict(vtree_dict)
        logger.info(
            "Saved refreshed VTree (%s): %d new node(s), %d repaired node(s) -> %s",
            scope,
            len(result.added_nodes),
            len(result.repaired_nodes),
            target_vtree,
        )
    else:
        logger.info(
            "VTree refresh (%s): all %d file(s) in '%s' are already indexed and consistent.",
            scope,
            len(scanned_files),
            resolved_lib,
        )

    return result

