import json
import multiprocessing
from pathlib import Path
import time

import pytest

from src.vtree.store import VTreeStore


def _worker_append_node(vtree_path_str: str, worker_id: int) -> None:
    store = VTreeStore(vtree_path_str)
    with store.file_lock():
        tree = store.load_as_dict()
        # Sleep briefly inside the lock so multiple processes overlap and contend
        time.sleep(0.02)
        tree["root"]["children"].append(
            {
                "type": "document",
                "name": f"paper_{worker_id}",
                "virtual_path": f"/paper_{worker_id}",
                "real_path": f"/tmp/paper_{worker_id}.pdf",
            }
        )
        store.save_dict(tree)


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


def test_store_invalidates_cache_on_external_disk_modification(tmp_path):
    vtree_path = tmp_path / "vtree.json"
    initial_tree = {
        "version": 1,
        "library_root": "/tmp/test",
        "root": {"type": "directory", "name": "root", "virtual_path": "/", "children": []},
    }
    vtree_path.write_text(json.dumps(initial_tree), encoding="utf-8")

    store = VTreeStore(vtree_path)
    loaded1 = store.load()
    assert len(loaded1.root.children) == 0

    # Simulate another process replacing vtree.json on disk
    updated_tree = {
        "version": 1,
        "library_root": "/tmp/test",
        "root": {
            "type": "directory",
            "name": "root",
            "virtual_path": "/",
            "children": [
                {
                    "type": "document",
                    "name": "external_doc",
                    "virtual_path": "/external_doc",
                    "real_path": "/tmp/external_doc.pdf",
                }
            ],
        },
    }
    vtree_path.write_text(json.dumps(updated_tree), encoding="utf-8")

    loaded2 = store.load()
    assert len(loaded2.root.children) == 1
    assert loaded2.root.children[0].name == "external_doc"


def test_store_cross_process_concurrent_updates_preserve_all_writes(tmp_path):
    vtree_path = tmp_path / "vtree.json"
    initial_tree = {
        "version": 1,
        "library_root": "/tmp/test",
        "root": {"type": "directory", "name": "root", "virtual_path": "/", "children": []},
    }
    vtree_path.write_text(json.dumps(initial_tree), encoding="utf-8")

    num_workers = 6
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker_append_node, args=(str(vtree_path), i)) for i in range(num_workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=20)
        assert p.exitcode == 0

    final_tree = VTreeStore(vtree_path).load_as_dict()
    names = sorted(c["name"] for c in final_tree["root"]["children"])
    assert names == [f"paper_{i}" for i in range(num_workers)]
    leftover_tmps = list(tmp_path.glob("*.tmp"))
    assert leftover_tmps == []
