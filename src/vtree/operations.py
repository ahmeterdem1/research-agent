from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
import threading

import tantivy

from config.settings import get_logger

logger = get_logger(__name__)


def _norm_vpath(vpath: str) -> PurePosixPath:
    """Normalize any virtual path into a rooted `PurePosixPath` (e.g. `'/a/b'`)."""
    cleaned = (vpath or "/").strip()
    return PurePosixPath("/" + cleaned.lstrip("/"))


def _build_vtree_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_integer_field("doc_id", stored=True)
    builder.add_text_field("doi", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("name", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("vpath", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("summary", stored=False, tokenizer_name="en_stem")
    return builder.build()


_VTREE_SCHEMA = _build_vtree_schema()
_DEFAULT_FIELDS = ["doi", "name", "vpath", "summary"]
_FIELD_BOOSTS = {"doi": 8.0, "name": 5.0, "vpath": 2.5, "summary": 1.5}


def _prepare_tantivy_query(query: str) -> str:
    """
    Normalize common user/LLM field aliases and punctuation before passing to
    `tantivy.Index.parse_query` (e.g. map `title:` -> `name:`, `path:` -> `vpath:`,
    strip non-schema colons and `/` slashes outside quoted phrases).
    """
    parts = re.split(r'("[^"]*")', query)
    out: list[str] = []
    for part in parts:
        if part.startswith('"') and part.endswith('"') and len(part) >= 2:
            out.append(part)
            continue
        cleaned = re.sub(r"\btitle:", "name:", part, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bpath:", "vpath:", cleaned, flags=re.IGNORECASE)
        # Preserve valid schema field prefixes; turn other `word:` (e.g. `Position:`) into space
        cleaned = re.sub(
            r"\b(?!(?:doi|name|vpath|summary)\b)([A-Za-z0-9_]+):",
            r"\1 ",
            cleaned,
        )
        cleaned = cleaned.replace("/", " ")
        out.append(cleaned)
    return "".join(out).strip()


def _collect_searchable_nodes(root: dict | None) -> list[dict]:
    """Walk the VTree depth-first and return all indexable nodes (excluding root `/`)."""
    nodes: list[dict] = []

    def _walk(node: dict | None) -> None:
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        vpath = node.get("virtual_path", "")
        if ntype in ("document", "book", "directory") and not (
            vpath in ("/", "") and (node.get("name") or "").lower() == "root"
        ):
            nodes.append(node)
        for child in node.get("children", []) or []:
            _walk(child)

    _walk(root)
    return nodes


class VTreeManager:
    def __init__(self, vtree: dict):
        self.vtree = vtree
        self._lock = threading.RLock()
        self._cached_state_key: tuple | None = None
        self._cached_searchable_nodes: list[dict] | None = None
        self._cached_tantivy_index: tantivy.Index | None = None

    def _touch(self) -> None:
        """Atomically bump the VTree's revision counter and `last_updated` UTC timestamp."""
        with self._lock:
            self.vtree["revision"] = int(self.vtree.get("revision") or 0) + 1
            self.vtree["last_updated"] = datetime.now(timezone.utc).isoformat()

    def _get_state_key(self) -> tuple:
        return (
            self.vtree.get("version", 1),
            self.vtree.get("revision", 0),
            self.vtree.get("last_updated"),
        )

    def _get_searchable_nodes_and_index(self) -> tuple[list[dict], tantivy.Index | None]:
        """
        Return cached `(searchable_nodes, tantivy_index)` if the VTree's
        `(version, revision, last_updated)` is unchanged; otherwise rebuild and cache them.
        """
        with self._lock:
            current_key = self._get_state_key()
            if (
                self._cached_state_key == current_key
                and self._cached_searchable_nodes is not None
            ):
                return self._cached_searchable_nodes, self._cached_tantivy_index

            nodes = _collect_searchable_nodes(self.vtree.get("root"))
            if not nodes:
                self._cached_state_key = current_key
                self._cached_searchable_nodes = []
                self._cached_tantivy_index = None
                return [], None

            index = tantivy.Index(_VTREE_SCHEMA)
            writer = index.writer(heap_size=15_000_000, num_threads=1)
            for idx, node in enumerate(nodes):
                writer.add_document(
                    tantivy.Document(
                        doc_id=idx,
                        doi=node.get("doi") or "",
                        name=node.get("name") or "",
                        vpath=node.get("virtual_path") or "",
                        summary=node.get("summary") or "",
                    )
                )
            writer.commit()
            index.reload()

            self._cached_state_key = current_key
            self._cached_searchable_nodes = nodes
            self._cached_tantivy_index = index
            return nodes, index

    def _get_node(self, vpath: str) -> dict | None:
        p = _norm_vpath(vpath)
        if p == PurePosixPath("/"):
            return self.vtree.get("root")

        curr = self.vtree.get("root")
        for part in p.parts[1:]:
            if not curr or curr.get("type") not in ("directory", "book"):
                return None
            curr = next((c for c in curr.get("children", []) if c.get("name") == part), None)
            if curr is None:
                return None
        return curr

    def ensure_dir(self, vpath: str) -> dict:
        """Ensure that a virtual directory path exists, creating intermediate directories as needed."""
        p = _norm_vpath(vpath)
        if p == PurePosixPath("/"):
            return self.vtree["root"]

        curr = self.vtree["root"]
        parts = p.parts[1:]
        created_any = False
        for idx, part in enumerate(parts):
            curr_vpath = str(PurePosixPath("/", *parts[: idx + 1]))
            children = curr.setdefault("children", [])
            existing = next(
                (c for c in children if c.get("name") == part and c.get("type") == "directory"),
                None,
            )
            if existing is None:
                existing = {
                    "type": "directory",
                    "name": part,
                    "virtual_path": curr_vpath,
                    "summary": "User created directory.",
                    "children": [],
                }
                children.append(existing)
                created_any = True
                logger.info("Created virtual directory '%s'", curr_vpath)
            curr = existing
        if created_any:
            self._touch()
        return curr

    def add_document(self, target_dir_vpath: str, doc_node: dict) -> dict:
        """
        Insert or update a document node inside ``target_dir_vpath`` and mark
        ancestor directories for summary refresh.
        """
        dir_node = self.ensure_dir(target_dir_vpath)
        name = doc_node["name"]
        full_vpath = str(_norm_vpath(target_dir_vpath) / name)
        doc_node = {**doc_node, "type": doc_node.get("type", "document"), "virtual_path": full_vpath}
        if "children" in doc_node and isinstance(doc_node["children"], list):
            doc_node["children"] = [
                {**c, "virtual_path": str(PurePosixPath(full_vpath) / c["name"])}
                for c in doc_node["children"]
            ]

        children = dir_node.setdefault("children", [])
        replaced = False
        for idx, child in enumerate(children):
            if child.get("name") == name:
                children[idx] = doc_node
                replaced = True
                break
        if not replaced:
            children.append(doc_node)

        self.update_ancestor_summaries(full_vpath, doc_node.get("summary", ""))
        logger.info("%s document '%s' in VTree", "Updated" if replaced else "Added", full_vpath)
        return doc_node

    def ls(self, vpath: str) -> str:
        """List contents of a directory."""
        node = self._get_node(vpath)
        if not node:
            return f"Error: Path '{vpath}' not found."

        if node.get("type") not in ("directory", "book"):
            return f"Error: '{vpath}' is not a directory."

        children = node.get("children", [])
        if not children:
            return "Empty directory."

        result = []
        for c in children:
            t = c.get("type", "unknown")
            name = c.get("name", "unknown")
            summary = c.get("summary", "")
            result.append(f"[{t}] {name} - {summary[:100]}...")

        return "\n".join(result)

    def mkdir(self, vpath: str) -> str:
        """Create a new directory at the specified virtual path."""
        p = _norm_vpath(vpath)
        if p == PurePosixPath("/"):
            return "Error: Invalid path."

        parent_vpath = str(p.parent)
        new_dir_name = p.name

        parent_node = self._get_node(parent_vpath)
        if not parent_node:
            return f"Error: Parent path '{parent_vpath}' does not exist."

        if parent_node.get("type") != "directory":
            return f"Error: Parent '{parent_vpath}' is not a directory."

        for child in parent_node.get("children", []):
            if child.get("name") == new_dir_name:
                return f"Error: '{new_dir_name}' already exists in '{parent_vpath}'."

        new_dir = {
            "type": "directory",
            "name": new_dir_name,
            "virtual_path": str(p),
            "summary": "User created directory.",
            "children": [],
        }

        parent_node.setdefault("children", []).append(new_dir)
        self._touch()
        logger.info("mkdir created '%s'", vpath)
        return f"Successfully created directory '{vpath}'."

    def mv(self, src: str, dest: str) -> str:
        """Move a file or directory from src to dest."""
        if src == "/" or dest == "/":
            return "Error: Cannot move root directory."

        src_p = _norm_vpath(src)
        dest_p = _norm_vpath(dest)
        if src_p == PurePosixPath("/") or dest_p == PurePosixPath("/"):
            return "Error: Invalid paths."

        if dest_p.is_relative_to(src_p):
            return f"Error: Cannot move '{src}' into itself or its own subdirectory '{dest}'."

        src_parent_vpath = str(src_p.parent)
        src_name = src_p.name
        dest_parent_vpath = str(dest_p.parent)
        dest_name = dest_p.name

        src_parent = self._get_node(src_parent_vpath)
        if not src_parent:
            return f"Error: Source parent '{src_parent_vpath}' not found."

        dest_parent = self._get_node(dest_parent_vpath)
        if not dest_parent:
            return f"Error: Destination parent '{dest_parent_vpath}' not found."

        if dest_parent.get("type") != "directory":
            return f"Error: Destination parent '{dest_parent_vpath}' is not a directory."

        src_node = None
        src_idx = -1
        for i, child in enumerate(src_parent.get("children", [])):
            if child.get("name") == src_name:
                src_node = child
                src_idx = i
                break

        if not src_node:
            return f"Error: Source '{src}' not found."

        for child in dest_parent.get("children", []):
            if child.get("name") == dest_name:
                return f"Error: Destination '{dest}' already exists."

        src_parent["children"].pop(src_idx)

        def update_paths(node: dict, new_parent: PurePosixPath, new_name: str) -> None:
            new_path = new_parent / new_name
            node["name"] = new_name
            node["virtual_path"] = str(new_path)
            for c in node.get("children", []):
                update_paths(c, new_path, c["name"])

        update_paths(src_node, dest_p.parent, dest_name)

        dest_parent.setdefault("children", []).append(src_node)

        # Mark both the source parent chain and destination parent chain for summary refresh
        self.update_ancestor_summaries(str(src_p), "")
        self.update_ancestor_summaries(str(dest_p), src_node.get("summary", ""))
        logger.info("Moved VTree node '%s' -> '%s'", src, dest)
        return f"Successfully moved '{src}' to '{dest}'."

    def rm(self, vpath: str) -> str:
        """Remove a document, book, or directory at ``vpath`` and mark ancestors for summary refresh."""
        p = _norm_vpath(vpath)
        if p == PurePosixPath("/"):
            return "Error: Cannot remove root directory '/'."

        norm_vpath = str(p)
        parent_vpath = str(p.parent)
        target_name = p.name

        parent_node = self._get_node(parent_vpath)
        if not parent_node or "children" not in parent_node:
            return f"Error: Path '{vpath}' not found."

        target_idx = -1
        for i, child in enumerate(parent_node["children"]):
            if child.get("name") == target_name:
                target_idx = i
                break

        if target_idx == -1:
            return f"Error: Path '{vpath}' not found."

        removed = parent_node["children"].pop(target_idx)
        self.update_ancestor_summaries(norm_vpath, "")
        logger.info("Removed VTree node '%s' (type=%s)", norm_vpath, removed.get("type"))
        return f"Successfully removed [{removed.get('type', 'node')}] '{norm_vpath}'."

    def search_library(self, query: str) -> list[dict]:
        """
        Full-text Lucene / BM25 ranked search across the VTree (`doi`, `name`, `virtual_path`,
        and `summary`) powered by Rust's ``tantivy`` engine with ``en_stem`` analysis.

        Supports:
        - Multi-keyword natural queries (e.g. `'platonic representation hypothesis convergence'`).
        - Quoted phrases (`"platonic representation"`), required terms (`+platonic`),
          excluded terms (`-survey` or `NOT survey`), boolean `OR`/`AND`, and field prefixes.
        - Field-weighted BM25 scoring (`doi` > `name` > `virtual_path` > `summary`), returning
          matching nodes sorted from highest to lowest relevance score.
        """
        q_raw = (query or "").strip()
        if not q_raw:
            return []

        nodes, index = self._get_searchable_nodes_and_index()
        if not nodes or index is None:
            return []

        prepared_q = _prepare_tantivy_query(q_raw)
        try:
            parsed_query = index.parse_query(
                prepared_q,
                default_field_names=_DEFAULT_FIELDS,
                field_boosts=_FIELD_BOOSTS,
            )
        except ValueError:
            fallback_q = re.sub(r'[^\w\s"]+', " ", q_raw).strip()
            if not fallback_q:
                return []
            try:
                parsed_query = index.parse_query(
                    fallback_q,
                    default_field_names=_DEFAULT_FIELDS,
                    field_boosts=_FIELD_BOOSTS,
                )
            except ValueError:
                return []

        searcher = index.searcher()
        result = searcher.search(parsed_query, limit=len(nodes))
        if not result.hits:
            return []

        # When multiple terms are queried without explicit OR, filter out weak tail hits
        # (< 35% of the top hit's BM25 score) so tangential 1-word mentions don't pollute results.
        top_score = result.hits[0][0]
        has_explicit_or = " OR " in q_raw.upper()
        word_count = len(re.findall(r"[a-zA-Z0-9]+", re.sub(r'"[^"]*"', " ", q_raw)))
        min_score = 0.0 if (has_explicit_or or word_count <= 2) else (top_score * 0.35)

        matched_nodes: list[dict] = []
        for score, doc_address in result.hits:
            if score < min_score:
                continue
            doc = searcher.doc(doc_address)
            doc_id = doc["doc_id"][0]
            matched_nodes.append(nodes[doc_id])
        return matched_nodes

    def update_ancestor_summaries(self, vpath: str, new_summary: str):
        """
        Recursively bubble up summary updates when new documents are added or changed.
        Tags ancestor directories with `_needs_summary_update = True`.
        """
        parts = _norm_vpath(vpath).parts[1:-1]

        ancestors = []
        curr = self.vtree.get("root")
        if curr:
            ancestors.append(curr)

        for part in parts:
            next_node = next((c for c in curr.get("children", []) if c.get("name") == part), None)
            if next_node is None:
                break
            curr = next_node
            ancestors.append(curr)

        for node in ancestors:
            node["_needs_summary_update"] = True

        self._touch()
