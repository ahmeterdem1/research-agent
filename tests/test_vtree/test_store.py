import pytest
from pathlib import Path
import json
from src.vtree.store import VTreeStore

def test_store_read_write(tmp_path):
    vtree_path = tmp_path / "vtree.json"
    
    initial_tree = {
        "version": 1,
        "library_root": "/tmp/test",
        "root": {"type": "directory", "name": "root", "virtual_path": "/", "children": []}
    }
    vtree_path.write_text(json.dumps(initial_tree))
    store = VTreeStore(vtree_path)
    
    # Read tree
    tree = store.load_as_dict()
    assert "root" in tree
    assert tree["root"]["virtual_path"] == "/"
    
    # Modify tree
    tree["root"]["children"] = [{"type": "directory", "name": "test", "virtual_path": "/test"}]
    store.save_dict(tree)
    
    # Read back directly
    raw = json.loads(vtree_path.read_text())
    assert len(raw["root"]["children"]) == 1
    assert raw["root"]["children"][0]["name"] == "test"
    # Read via store
    tree2 = store.load_as_dict()
    assert tree2["root"]["children"][0]["name"] == "test"
