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
            
        # Check if exists
        for child in parent_node.get("children", []):
            if child.get("name") == new_dir_name:
                return f"Error: '{new_dir_name}' already exists in '{parent_vpath}'."
                
        # Create
        new_dir = {
            "type": "directory",
            "name": new_dir_name,
            "virtual_path": "/" + "/".join(parts),
            "summary": "User created directory.",
            "children": []
        }
        
        if "children" not in parent_node:
            parent_node["children"] = []
        parent_node["children"].append(new_dir)
        
        return f"Successfully created directory '{vpath}'."

    def mv(self, src: str, dest: str) -> str:
        """Move a file or directory from src to dest."""
        if src == "/" or dest == "/":
            return "Error: Cannot move root directory."
            
        src_parts = [p for p in src.split("/") if p]
        dest_parts = [p for p in dest.split("/") if p]
        
        if not src_parts or not dest_parts:
            return "Error: Invalid paths."
            
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
            
        # Find src node
        src_node = None
        src_idx = -1
        for i, child in enumerate(src_parent.get("children", [])):
            if child.get("name") == src_name:
                src_node = child
                src_idx = i
                break
                
        if not src_node:
            return f"Error: Source '{src}' not found."
            
        # Check if dest already has a child with the new name
        for child in dest_parent.get("children", []):
            if child.get("name") == dest_name:
                return f"Error: Destination '{dest}' already exists."
                
        # Move
        src_parent["children"].pop(src_idx)
        
        # Update name and virtual_path recursively
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
        
        return f"Successfully moved '{src}' to '{dest}'."

    def check_local(self, title_or_doi: str) -> dict | None:
        """
        Check if a paper is already stored locally by title or DOI.
        Returns the node dict if found, otherwise None.
        """
        def search(node):
            if not isinstance(node, dict):
                return None
                
            ntype = node.get("type")
            if ntype in ("document", "book"):
                if title_or_doi.lower() in node.get("name", "").lower():
                    return node
                if "doi" in node and node["doi"] and title_or_doi.lower() == node["doi"].lower():
                    return node
                    
            if "children" in node:
                for child in node["children"]:
                    found = search(child)
                    if found:
                        return found
            return None
            
        return search(self.vtree.get("root"))

    def update_ancestor_summaries(self, vpath: str, new_summary: str):
        """
        Recursively bubble up summary updates when new documents are added or changed.
        Currently a placeholder for where the LLM summarization logic would be invoked.
        """
        # In a full implementation, this would walk up the tree from vpath to root,
        # gathering child summaries and calling the LLM to update each directory's summary.
        # For now, we just touch the nodes to indicate they need updating or do a simple append.
        parts = [p for p in vpath.split("/") if p]
        
        # Traverse and collect all ancestors
        ancestors = []
        curr = self.vtree.get("root")
        if curr:
            ancestors.append(curr)
            
        for part in parts[:-1]: # exclude the file itself
            found = False
            for child in curr.get("children", []):
                if child.get("name") == part:
                    curr = child
                    ancestors.append(curr)
                    found = True
                    break
            if not found:
                break
                
        # We can tag them as "needs_re-summary" or directly call the summarizer if we import it.
        # To avoid circular imports and keep operations deterministic, we'll just tag them here.
        # The agent or background task can actually run the LLM.
        for node in ancestors:
            node["_needs_summary_update"] = True


