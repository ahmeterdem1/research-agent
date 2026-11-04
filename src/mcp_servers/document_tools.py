"""
Document Tools MCP Server (``ra-mcp-docs``).

Provides read and search access to both the system-wide **Global VTree**
(``~/.research-agent/vtrees/global.json``) and an optional **Project-Local VTree**
(``<project>/.opencode/vtree.json`` or ``$RA_VTREE_PATH``).
"""

from pathlib import Path
import sys
from typing import List, Literal, Optional

from fastmcp import FastMCP

sys.path.append(str(Path(__file__).parent.parent.parent))

from config import settings
from config.settings import get_logger
from src.telemetry.store import track_tool
from src.vtree.operations import VTreeManager
from src.vtree.store import VTreeStore

logger = get_logger(__name__)

mcp = FastMCP("DocumentTools")


def get_manager(scope: str = "global") -> VTreeManager:
    """Load the VTreeManager for either 'global' or 'project' scope."""
    if scope == "project":
        vtree_path = settings.get_project_vtree_path(must_exist=True)
        if not vtree_path:
            raise FileNotFoundError(
                "No project-local VTree found at '.opencode/vtree.json'. "
                "Use scope='global' or run `ra-cli index --scope project`."
            )
    else:
        vtree_path = settings.get_global_vtree_path()

    store = VTreeStore(vtree_path)
    vtree_dict = store.load_as_dict()
    return VTreeManager(vtree_dict)


def _iter_managers(scope: str) -> list[tuple[str, VTreeManager]]:
    """
    Yield (scope_label, manager) pairs for the requested scope ('global', 'project', or 'all').
    Keeping `get_manager()` as a single-arg call for 'global' preserves unit test mocks.
    """
    s = (scope or "all").strip().lower()
    managers: list[tuple[str, VTreeManager]] = []

    if s in ("global", "all"):
        try:
            # Call get_manager() with no args when global so legacy mock_get_manager works seamlessly
            managers.append(("GLOBAL", get_manager()))
        except FileNotFoundError:
            if s == "global":
                raise

    if s in ("project", "all"):
        proj_path = settings.get_project_vtree_path(must_exist=True)
        if proj_path is not None:
            try:
                managers.append((f"PROJECT:{settings.get_active_project_id()}", get_manager("project")))
            except FileNotFoundError:
                if s == "project":
                    raise
        elif s == "project":
            raise FileNotFoundError(
                f"No project-local VTree configured for project '{settings.get_active_project_id()}'."
            )

    return managers


@mcp.tool()
@track_tool("ra_docs")
def get_dir_summary(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str:
    """Gets the summary of a specific virtual directory in either the 'global' or 'project' VTree."""
    try:
        manager = get_manager() if scope == "global" else get_manager("project")
    except FileNotFoundError as e:
        return f"Error: {e}"

    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found in {scope} VTree."
    if node.get("type") not in ("directory", "book"):
        return f"Error: '{virtual_path}' is not a directory."

    return node.get("summary", "No summary available.")


@mcp.tool()
@track_tool("ra_docs")
def list_doc_summaries(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str:
    """Lists summaries of all immediate items in a virtual directory ('global' or 'project' VTree)."""
    try:
        manager = get_manager() if scope == "global" else get_manager("project")
    except FileNotFoundError as e:
        return f"Error: {e}"

    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found in {scope} VTree."
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
@track_tool("ra_docs")
def list_tree(
    virtual_path: str = "/",
    include_summaries: bool = False,
    max_depth: int = 3,
    scope: Literal["all", "global", "project"] = "all",
) -> str:
    """
    Lists the virtual directory tree structure starting from ``virtual_path`` up to ``max_depth``.
    By default ``scope="all"`` displays the Global VTree and, if present, the Project-Local VTree.
    """
    try:
        managers = _iter_managers(scope)
    except FileNotFoundError as e:
        return f"Error: {e}"

    if not managers:
        return "No Virtual Tree libraries found. Run `ra-cli index --library-path <path>` to build one."

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

    blocks = []
    for label, mgr in managers:
        start_node = mgr._get_node(virtual_path)
        if not start_node:
            blocks.append(f"[{label}] Error: Path '{virtual_path}' not found.")
        else:
            rendered = _walk(start_node, 1).strip()
            blocks.append(f"[{label}]\n{rendered}" if len(managers) > 1 else rendered)

    return "\n\n".join(blocks)


@mcp.tool()
@track_tool("ra_docs")
def get_document(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str:
    """Gets the full extracted Markdown text of a local document by its virtual path."""
    try:
        manager = get_manager() if scope == "global" else get_manager("project")
    except FileNotFoundError as e:
        return f"Error: {e}"

    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found in {scope} VTree."
    if node.get("type") not in ("document", "book"):
        return f"Error: '{virtual_path}' is not a document."

    if node.get("type") == "book" and not node.get("text_path") and not node.get("text_hash"):
        children = node.get("children", [])
        if children:
            lines = [
                f"# Book: {node.get('name', virtual_path)}",
                f"**Virtual Path**: `{virtual_path}`",
                f"**Overview**: {node.get('summary', 'No summary available.')}",
                "",
                "This book is split into the following chapter/section documents. "
                "Call `get_document` with the specific chapter's virtual path to read its full text:",
            ]
            for c in children:
                c_vpath = c.get("virtual_path") or f"{virtual_path.rstrip('/')}/{c.get('name', '')}"
                c_pages = f" ({c['page_count']} pages)" if c.get("page_count") else ""
                lines.append(f"- `{c_vpath}`{c_pages}: {c.get('summary', 'No summary.')}")
            return "\n".join(lines)

    text_path = node.get("text_path")
    resolved_text_path: Optional[Path] = None
    if text_path:
        candidate = Path(text_path)
        if candidate.exists():
            resolved_text_path = candidate
        else:
            fallback = settings.CACHE_DIR / "texts" / candidate.name
            if fallback.exists():
                resolved_text_path = fallback
    text_hash = node.get("text_hash") or node.get("file_hash")
    if resolved_text_path is None and text_hash:
        hash_candidate = settings.CACHE_DIR / "texts" / f"{text_hash}.md"
        if hash_candidate.exists():
            resolved_text_path = hash_candidate

    if resolved_text_path is None and node.get("real_path"):
        try:
            from src.cold_start.refresher import _load_cold_start_cache_map, _norm_path

            cmap = _load_cold_start_cache_map()
            rp = str(node["real_path"])
            hit = cmap.get(rp) or cmap.get(_norm_path(rp))
            if hit:
                cand_global = settings.CACHE_DIR / "texts" / f"{hit['text_hash']}.md"
                cand_orig = Path(hit["text_path"]) if hit.get("text_path") else None
                if cand_global.exists():
                    resolved_text_path = cand_global
                elif cand_orig and cand_orig.exists():
                    resolved_text_path = cand_orig
        except Exception:
            pass

    if resolved_text_path is None:
        return (
            f"Error: Text content not found for {virtual_path}. "
            "It may not have been extracted properly during cold start."
        )

    try:
        return resolved_text_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed reading document text at %s: %s", text_path, e)
        return f"Error reading document text: {e}"


@mcp.tool()
@track_tool("ra_docs")
def get_documents(
    virtual_paths: List[str],
    scope: Literal["global", "project"] = "global",
) -> str:
    """Batch retrieve full text for multiple documents."""
    results = []
    for vp in virtual_paths:
        res = get_document(vp, scope=scope)
        results.append(f"--- Document: {vp} ({scope}) ---\n{res}\n")
    return "\n".join(results)


@mcp.tool()
@track_tool("ra_docs")
def search_library(
    query: str,
    scope: Literal["all", "global", "project"] = "all",
) -> str:
    """
    Instantly searches the local virtual library (both Global and Project-Local by default
    when `scope="all"`) for documents, books, or directories matching the keyword query.
    The search is case-insensitive and matches against the file name, DOI, and the
    LLM-generated summaries of every item.

    Use this tool FIRST before deciding to fetch a paper from the web. By searching
    for keywords related to the paper (e.g., 'attention is all you need transformer'),
    you can discover if the paper was already ingested under a different file name,
    thus preventing duplicate downloads.

    Returns a formatted list of matches containing their scope, virtual path, type, and summary.
    """
    try:
        managers = _iter_managers(scope)
    except FileNotFoundError as e:
        return f"Error: {e}"

    formatted = []
    for label, manager in managers:
        results = manager.search_library(query)
        for r in results:
            vpath = r.get("virtual_path", "unknown")
            rtype = r.get("type", "unknown")
            name = r.get("name", "unknown")
            summary = r.get("summary", "")
            formatted.append(
                f"Match Found [{label}] [{rtype}]: '{name}'\n"
                f"Path: {vpath}\n"
                f"Summary: {summary}\n---"
            )

    if not formatted:
        return f"No local documents found matching query: '{query}'"

    return "\n".join(formatted)


@mcp.tool()
@track_tool("ra_docs")
def get_document_real_path(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str:
    """
    Returns the underlying filesystem path (`real_path`) of a document or book in the VTree
    so the user can open the original PDF directly in their viewer.
    """
    try:
        manager = get_manager() if scope == "global" else get_manager("project")
    except FileNotFoundError as e:
        return f"Error: {e}"

    node = manager._get_node(virtual_path)
    if not node:
        return f"Error: Path '{virtual_path}' not found in {scope} VTree."
    real_path = node.get("real_path")
    if not real_path:
        return f"Error: '{virtual_path}' does not have an associated filesystem file."
    return f"Real path for '{virtual_path}' [{scope}]: {real_path}"


def main() -> None:
    logger.info("Starting DocumentTools MCP server (active project: %s)", settings.get_active_project_id())
    mcp.run()


if __name__ == "__main__":
    main()
