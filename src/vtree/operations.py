from config.settings import get_logger

logger = get_logger(__name__)


class VTreeManager:
    def __init__(self, vtree: dict):
        self.vtree = vtree

    def _get_node(self, vpath: str) -> dict | None:
        if vpath == "/" or vpath == "":
            return self.vtree.get("root")

        parts = [p for p in vpath.split("/") if p]
        curr = self.vtree.get("root")

        for part in parts:
            if not curr or curr.get("type") not in ("directory", "book"):
                return None
            found = False
            for child in curr.get("children", []):
                if child.get("name") == part:
                    curr = child
                    found = True
                    break
            if not found:
                return None
        return curr

    def ensure_dir(self, vpath: str) -> dict:
        """Ensure that a virtual directory path exists, creating intermediate directories as needed."""
        if vpath in ("/", ""):
            return self.vtree["root"]

        parts = [p for p in vpath.split("/") if p]
        curr = self.vtree["root"]
        built_parts: list[str] = []

        for part in parts:
            built_parts.append(part)
            curr_vpath = "/" + "/".join(built_parts)
            if "children" not in curr:
                curr["children"] = []
            existing = None
            for child in curr["children"]:
                if child.get("name") == part and child.get("type") == "directory":
                    existing = child
                    break
            if existing is None:
                existing = {
                    "type": "directory",
                    "name": part,
                    "virtual_path": curr_vpath,
                    "summary": "User created directory.",
                    "children": [],
                }
                curr["children"].append(existing)
                logger.info("Created virtual directory '%s'", curr_vpath)
            curr = existing
        return curr

    def add_document(self, target_dir_vpath: str, doc_node: dict) -> dict:
        """
        Insert or update a document node inside ``target_dir_vpath`` and mark
        ancestor directories for summary refresh.
        """
        dir_node = self.ensure_dir(target_dir_vpath)
        name = doc_node["name"]
        prefix = "" if target_dir_vpath.rstrip("/") == "" else target_dir_vpath.rstrip("/")
        full_vpath = f"{prefix}/{name}"
        doc_node = {**doc_node, "type": doc_node.get("type", "document"), "virtual_path": full_vpath}
        if "children" in doc_node and isinstance(doc_node["children"], list):
            doc_node["children"] = [
                {**c, "virtual_path": f"{full_vpath}/{c['name']}"} for c in doc_node["children"]
            ]

        if "children" not in dir_node:
            dir_node["children"] = []

        replaced = False
        for idx, child in enumerate(dir_node["children"]):
            if child.get("name") == name:
                dir_node["children"][idx] = doc_node
                replaced = True
                break
        if not replaced:
            dir_node["children"].append(doc_node)

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
        parts = [p for p in vpath.split("/") if p]
        if not parts:
            return "Error: Invalid path."

        parent_vpath = "/" + "/".join(parts[:-1])
        new_dir_name = parts[-1]

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
            "virtual_path": "/" + "/".join(parts),
            "summary": "User created directory.",
            "children": [],
        }

        if "children" not in parent_node:
            parent_node["children"] = []
        parent_node["children"].append(new_dir)
        logger.info("mkdir created '%s'", vpath)
        return f"Successfully created directory '{vpath}'."

    def mv(self, src: str, dest: str) -> str:
        """Move a file or directory from src to dest."""
        if src == "/" or dest == "/":
            return "Error: Cannot move root directory."

        src_parts = [p for p in src.split("/") if p]
        dest_parts = [p for p in dest.split("/") if p]

        if not src_parts or not dest_parts:
            return "Error: Invalid paths."

        norm_src = "/" + "/".join(src_parts)
        norm_dest = "/" + "/".join(dest_parts)
        if norm_dest == norm_src or norm_dest.startswith(norm_src + "/"):
            return f"Error: Cannot move '{src}' into itself or its own subdirectory '{dest}'."

        src_parent_vpath = "/" + "/".join(src_parts[:-1])
        src_name = src_parts[-1]

        dest_parent_vpath = "/" + "/".join(dest_parts[:-1])
        dest_name = dest_parts[-1]

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

        def update_paths(node, new_parent_path, new_name):
            node["name"] = new_name
            node["virtual_path"] = f"{new_parent_path.rstrip('/')}/{new_name}"
            if "children" in node:
                for c in node["children"]:
                    update_paths(c, node["virtual_path"], c["name"])

        update_paths(src_node, dest_parent_vpath, dest_name)

        if "children" not in dest_parent:
            dest_parent["children"] = []
        dest_parent["children"].append(src_node)

        # Mark both the source parent chain and destination parent chain for summary refresh
        self.update_ancestor_summaries(norm_src, "")
        self.update_ancestor_summaries(norm_dest, src_node.get("summary", ""))
        logger.info("Moved VTree node '%s' -> '%s'", src, dest)
        return f"Successfully moved '{src}' to '{dest}'."

    def rm(self, vpath: str) -> str:
        """Remove a document, book, or directory at ``vpath`` and mark ancestors for summary refresh."""
        parts = [p for p in vpath.split("/") if p]
        if not parts:
            return "Error: Cannot remove root directory '/'."

        norm_vpath = "/" + "/".join(parts)
        parent_vpath = "/" + "/".join(parts[:-1])
        target_name = parts[-1]

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
        Search the entire library for nodes matching the query in their name, doi, or summary.
        Returns a list of matching node dicts.
        """
        results = []
        q = query.lower()

        def search(node):
            if not isinstance(node, dict):
                return

            ntype = node.get("type")
            if ntype in ("document", "book", "directory"):
                match = False
                if q in node.get("name", "").lower():
                    match = True
                elif "doi" in node and node.get("doi") and q in node["doi"].lower():
                    match = True
                elif q in node.get("summary", "").lower():
                    match = True

                if match:
                    results.append(node)

            if "children" in node:
                for child in node["children"]:
                    search(child)

        search(self.vtree.get("root"))
        return results

    def update_ancestor_summaries(self, vpath: str, new_summary: str):
        """
        Recursively bubble up summary updates when new documents are added or changed.
        Tags ancestor directories with `_needs_summary_update = True`.
        """
        parts = [p for p in vpath.split("/") if p]

        ancestors = []
        curr = self.vtree.get("root")
        if curr:
            ancestors.append(curr)

        for part in parts[:-1]:
            found = False
            for child in curr.get("children", []):
                if child.get("name") == part:
                    curr = child
                    ancestors.append(curr)
                    found = True
                    break
            if not found:
                break

        for node in ancestors:
            node["_needs_summary_update"] = True
