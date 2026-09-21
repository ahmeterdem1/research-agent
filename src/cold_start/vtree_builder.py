from pathlib import Path
from src.cold_start.scanner import FileInfo
from src.cold_start.summarizer import summarize_directory

def build_initial_vtree(files: list[dict], library_root: Path) -> dict:
    """
    Builds the initial vtree.json mirroring the real filesystem structure.
    `files` is a list of dicts with: name, real_path, type, page_count, summary, etc.
    """
    vtree = {
        "version": 1,
        "library_root": str(library_root),
        "root": {
            "type": "directory",
            "name": "Root",
            "virtual_path": "/",
            "summary": "",
            "children": []
        }
    }
    
    # Simple nested dictionary for building the tree
    dirs = {"/": vtree["root"]}
    
    for f in files:
        real_path = Path(f["real_path"])
        try:
            rel_path = real_path.relative_to(library_root)
        except ValueError:
            # If not relative to root, just put in root
            rel_path = Path(f["name"])
            
        parent_dir = rel_path.parent
        
        # Ensure directories exist
        curr_path = Path(".")
        curr_vpath = "/"
        
        if str(parent_dir) != ".":
            for part in parent_dir.parts:
                curr_path = curr_path / part
                parent_vpath = curr_vpath
                curr_vpath = f"{curr_vpath}{part}/" if curr_vpath == "/" else f"{curr_vpath}/{part}/"
                
                if curr_vpath not in dirs:
                    new_dir = {
                        "type": "directory",
                        "name": part,
                        "virtual_path": curr_vpath.rstrip('/'),
                        "summary": "",
                        "children": []
                    }
                    dirs[curr_vpath] = new_dir
                    dirs[parent_vpath]["children"].append(new_dir)

        # Add file
        parent_vpath = "/" if str(parent_dir) == "." else f"/{str(parent_dir)}".replace("\\", "/")
        parent_vpath = parent_vpath.replace("//", "/")
        # Just ensure we get the right key
        # Fixing the path mapping logic properly:
        vpath_key = "/"
        if str(parent_dir) != ".":
            vpath_key = f"/{'/'.join(parent_dir.parts)}/"
            
        node = {
            "type": f["type"],
            "name": f["name"],
            "virtual_path": f"{vpath_key.rstrip('/')}/{f['name']}",
            "real_path": f["real_path"],
            "summary": f["summary"]
        }
        if "page_count" in f:
            node["page_count"] = f["page_count"]
        if "children" in f:
            # Need to update virtual_path of children to be relative to the book
            for c in f["children"]:
                c["virtual_path"] = f"{node['virtual_path']}/{c['name']}"
            node["children"] = f["children"]
            
        if vpath_key in dirs:
            dirs[vpath_key]["children"].append(node)
        else:
            dirs["/"]["children"].append(node)
            
    # Compute directory summaries bottom-up
    _summarize_dir_node(vtree["root"])
    
    return vtree

def _summarize_dir_node(node: dict) -> str:
    if node["type"] not in ("directory", "book"):
        return node["summary"]
        
    child_summaries = []
    # If the node has children, process them
    if "children" in node:
        for child in node["children"]:
            child_sum = _summarize_dir_node(child)
            if child_sum:
                child_summaries.append(f"{child['name']}: {child_sum}")
            
    # For books, we already have a summary generated from the extractor/summarizer or it's a generic one
    # For directories, we generate a new summary.
    if node["type"] == "directory":
        node["summary"] = summarize_directory(child_summaries)
    return node["summary"]

