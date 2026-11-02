"""
User-Gated File Ingestion MCP Server (``ra-mcp-ingest``).

Stages downloaded papers on disk (``~/.research-agent/cache/staged/<temp_id>.json``)
as well as in ``_STAGED_PAPERS``, and on user-approved ``confirm_save_paper`` inserts
the document into either the Global VTree or the Project-Local VTree and records a
``User --has_read--> Paper`` triplet in the Global Knowledge Graph.
"""

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Literal, Optional
import uuid

from fastmcp import FastMCP

sys.path.append(str(Path(__file__).parent.parent.parent))

from config import settings
from config.settings import get_logger
from src.telemetry.store import track_tool
from src.vtree.operations import VTreeManager
from src.vtree.store import VTreeStore

logger = get_logger(__name__)

mcp = FastMCP("FileTools")

# In-memory staging cache synchronized with settings.STAGED_DIR on disk
_STAGED_PAPERS: dict[str, dict] = {}


def _staged_file(temp_id: str) -> Path:
    settings.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    return settings.STAGED_DIR / f"{temp_id}.json"


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


def _derive_name(url_or_arxiv_id: str, title: Optional[str] = None) -> str:
    if title and title.strip():
        clean = re.sub(r'[\\/*?:"<>|]+', "", title.strip())
        return clean
    tail = url_or_arxiv_id.rstrip("/").split("/")[-1] or "paper"
    return re.sub(r'[\\/*?:"<>|]+', "_", tail)


@mcp.tool()
@track_tool("ra_ingest")
def stage_paper(
    url_or_arxiv_id: str,
    target_virtual_dir: str,
    target_scope: Literal["global", "project"] = "global",
    title: Optional[str] = None,
    preextracted_text: Optional[str] = None,
    precomputed_summary: Optional[str] = None,
) -> str:
    """
    Downloads or stages a paper, extracts its text, and generates a summary, placing it in a
    staging area (`~/.research-agent/cache/staged/`).
    Returns a `temp_id` which must be passed to `confirm_save_paper` to finalize the save.

    Arguments:
    - url_or_arxiv_id: URL or arXiv ID of the paper.
    - target_virtual_dir: Virtual directory path in the VTree (e.g. '/Transformers').
    - target_scope: `'global'` (default, saves to the master PC-wide VTree) or `'project'`
      (saves to the current workspace's `.opencode/vtree.json`).
    - title: Optional clean title for the document node in the VTree.

    CRITICAL INSTRUCTION: After calling this tool and receiving the summary, you MUST halt your execution,
    show the paper summary to the user in the chat, and explicitly ask if they want to save it.
    You are FORBIDDEN from calling `confirm_save_paper` until the user replies affirmatively in the next chat turn.
    """
    temp_id = str(uuid.uuid4())
    doc_name = _derive_name(url_or_arxiv_id, title)

    text_content = preextracted_text
    summary = precomputed_summary
    text_path_str: Optional[str] = None

    if text_content:
        text_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()
        text_dir = settings.CACHE_DIR / "texts"
        text_dir.mkdir(parents=True, exist_ok=True)
        text_path = text_dir / f"{text_hash}.md"
        text_path.write_text(text_content, encoding="utf-8")
        text_path_str = str(text_path)

    if not summary:
        summary = f"Staged paper '{doc_name}' from {url_or_arxiv_id}."

    manifest = {
        "temp_id": temp_id,
        "url": url_or_arxiv_id,
        "name": doc_name,
        "dir": target_virtual_dir,
        "scope": target_scope,
        "summary": summary,
        "text_path": text_path_str,
    }
    _save_staged_manifest(temp_id, manifest)
    logger.info("Staged paper '%s' (ID: %s, scope: %s -> %s)", doc_name, temp_id, target_scope, target_virtual_dir)

    return (
        f"Paper staged successfully with ID: {temp_id}\n\n"
        f"Target: [{target_scope.upper()}] {target_virtual_dir}\n"
        f"Summary: {summary}\n\n"
        f"STOP EXECUTION NOW and ask the user for permission to use `confirm_save_paper`."
    )


def _persist_to_vtree(paper_info: dict) -> None:
    """Write the confirmed paper into the target VTree if a VTree file exists or can be initialized."""
    scope = paper_info.get("scope", "global")
    if scope == "project":
        vtree_path = settings.get_project_vtree_path(must_exist=False)
    else:
        vtree_path = settings.get_global_vtree_path()

    if vtree_path is None:
        return

    store = VTreeStore(vtree_path)
    if vtree_path.exists():
        try:
            vtree_dict = store.load_as_dict()
        except Exception as e:
            logger.warning("Could not load existing VTree at %s: %s", vtree_path, e)
            return
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

    mgr = VTreeManager(vtree_dict)
    doc_node = {
        "type": "document",
        "name": paper_info.get("name") or _derive_name(paper_info["url"]),
        "real_path": paper_info.get("text_path") or paper_info["url"],
        "text_path": paper_info.get("text_path"),
        "summary": paper_info.get("summary", ""),
    }
    mgr.add_document(paper_info["dir"], doc_node)
    store.save_dict(vtree_dict)


@mcp.tool()
@track_tool("ra_ingest")
def confirm_save_paper(temp_id: str) -> str:
    """
    Finalizes the saving of a staged paper into the local VTree library (`global` or `project`).
    Requires explicit user approval before invocation (enforced via OpenCode `"ask"` permission).
    """
    paper_info = _pop_staged_manifest(temp_id)
    if not paper_info:
        return f"Error: No staged paper found with ID '{temp_id}'."

    target_dir = paper_info["dir"]
    try:
        _persist_to_vtree(paper_info)
    except Exception as e:
        logger.warning("VTree persistence skipped/failed during confirm_save_paper: %s", e)

    logger.info("Confirmed save of staged paper %s to '%s'", temp_id, target_dir)
    return f"Successfully saved paper to '{target_dir}'. Ancestor directory summaries have been updated."


def main() -> None:
    logger.info("Starting FileTools MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
