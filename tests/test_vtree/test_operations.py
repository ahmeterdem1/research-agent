import pytest
from src.vtree.operations import VTreeManager

@pytest.fixture
def sample_vtree():
    return {
        "root": {
            "type": "directory",
            "name": "root",
            "virtual_path": "/",
            "children": [
                {
                    "type": "directory",
                    "name": "math",
                    "virtual_path": "/math",
                    "summary": "Math papers",
                    "children": [
                        {
                            "type": "document",
                            "name": "attention.pdf",
                            "virtual_path": "/math/attention.pdf",
                            "summary": "Attention is all you need. Transformers.",
                            "doi": "10.1234/attention"
                        }
                    ]
                }
            ]
        }
    }

def test_ls_root(sample_vtree):
    manager = VTreeManager(sample_vtree)
    res = manager.ls("/")
    assert "[directory] math" in res
    assert "Math papers" in res

def test_ls_subdir(sample_vtree):
    manager = VTreeManager(sample_vtree)
    res = manager.ls("/math")
    assert "[document] attention.pdf" in res

def test_mkdir(sample_vtree):
    manager = VTreeManager(sample_vtree)
    res = manager.mkdir("/physics")
    assert "Successfully" in res
    
    # Verify it exists
    ls_res = manager.ls("/")
    assert "[directory] physics" in ls_res

def test_mv(sample_vtree):
    manager = VTreeManager(sample_vtree)
    # Move attention.pdf to root
    res = manager.mv("/math/attention.pdf", "/attention.pdf")
    assert "Successfully" in res
    
    # Verify it's in root
    ls_res = manager.ls("/")
    assert "[document] attention.pdf" in ls_res
    
    # Verify it's removed from /math
    math_ls = manager.ls("/math")
    assert "Empty directory" in math_ls or "attention.pdf" not in math_ls

def test_search_library_by_name(sample_vtree):
    manager = VTreeManager(sample_vtree)
    results = manager.search_library("attention.pdf")
    assert len(results) == 1
    assert results[0]["name"] == "attention.pdf"

def test_search_library_by_summary(sample_vtree):
    manager = VTreeManager(sample_vtree)
    results = manager.search_library("transformers")
    assert len(results) == 1
    assert results[0]["name"] == "attention.pdf"

def test_search_library_by_doi(sample_vtree):
    manager = VTreeManager(sample_vtree)
    results = manager.search_library("10.1234/attention")
    assert len(results) == 1
    assert results[0]["name"] == "attention.pdf"


def test_add_document_creates_dirs_and_bubbles_ancestors(sample_vtree):
    manager = VTreeManager(sample_vtree)
    added = manager.add_document(
        "/dl/transformers",
        {
            "name": "flash_attention.pdf",
            "real_path": "/tmp/flash.pdf",
            "summary": "IO-aware exact attention.",
        },
    )
    assert added["virtual_path"] == "/dl/transformers/flash_attention.pdf"
    node = manager._get_node("/dl/transformers/flash_attention.pdf")
    assert node is not None and node["summary"] == "IO-aware exact attention."
    assert manager._get_node("/dl")["_needs_summary_update"] is True
    assert manager._get_node("/dl/transformers")["_needs_summary_update"] is True

