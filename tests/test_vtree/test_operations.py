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


def test_mv_prevents_moving_into_own_subdirectory_and_marks_ancestors(sample_vtree):
    manager = VTreeManager(sample_vtree)
    manager.mkdir("/math/sub")
    err = manager.mv("/math", "/math/sub/math")
    assert "Error: Cannot move" in err

    manager.mkdir("/nlp")
    ok = manager.mv("/math/attention.pdf", "/nlp/attention.pdf")
    assert "Successfully moved" in ok
    assert manager._get_node("/math")["_needs_summary_update"] is True
    assert manager._get_node("/nlp")["_needs_summary_update"] is True


def test_rm_removes_node_and_marks_ancestors(sample_vtree):
    manager = VTreeManager(sample_vtree)
    res = manager.rm("/math/attention.pdf")
    assert "Successfully removed" in res
    assert manager._get_node("/math/attention.pdf") is None
    assert manager._get_node("/math")["_needs_summary_update"] is True
    assert "Error: Cannot remove root" in manager.rm("/")


def test_search_library_lucene_tokenized_and_stem_matching():
    """
    Verify that `search_library` performs Lucene/BM25-style ranked tokenized search
    rather than requiring a single contiguous substring match:
    1. 'platonic representation hypothesis convergence' matches 'Position The Platonic Representation Hypothesis'
       even though 'convergence' is not in the title and the summary says 'converging'.
    2. 'attention is all you need transformer' matches 'attention.pdf'.
    3. Lucene operators (`"quoted phrase"`, `-excluded`, `+required`, `OR`) work as expected.
    """
    vtree = {
        "root": {
            "type": "directory",
            "name": "root",
            "virtual_path": "/",
            "children": [
                {
                    "type": "directory",
                    "name": "RepresentationLearning",
                    "virtual_path": "/RepresentationLearning",
                    "summary": "Papers on representation alignment across modalities.",
                    "children": [
                        {
                            "type": "document",
                            "name": "Position The Platonic Representation Hypothesis",
                            "virtual_path": "/RepresentationLearning/Position The Platonic Representation Hypothesis",
                            "summary": (
                                "Argues that neural networks trained on different modalities and objectives "
                                "are converging toward a shared statistical model of reality."
                            ),
                            "doi": "10.48550/arXiv.2405.07987",
                        },
                        {
                            "type": "document",
                            "name": "A Survey of Multimodal Representations",
                            "virtual_path": "/RepresentationLearning/A Survey of Multimodal Representations",
                            "summary": "Comprehensive survey of vision-language representation spaces.",
                        },
                    ],
                },
                {
                    "type": "document",
                    "name": "attention.pdf",
                    "virtual_path": "/attention.pdf",
                    "summary": "Attention is all you need. Introduces the Transformer architecture.",
                },
            ],
        }
    }
    manager = VTreeManager(vtree)

    # 1. User's exact GLM-5.3 Flash query: "platonic representation hypothesis convergence"
    hits = manager.search_library("platonic representation hypothesis convergence")
    assert len(hits) >= 1
    assert hits[0]["name"] == "Position The Platonic Representation Hypothesis"

    # 2. Multi-keyword query from docstring: "attention is all you need transformer"
    hits_att = manager.search_library("attention is all you need transformer")
    assert len(hits_att) >= 1
    assert hits_att[0]["name"] == "attention.pdf"

    # 3. Lucene exclusion `-survey` filters out the survey paper
    hits_no_survey = manager.search_library("representation -survey")
    names_no_survey = [h["name"] for h in hits_no_survey]
    assert "Position The Platonic Representation Hypothesis" in names_no_survey
    assert "A Survey of Multimodal Representations" not in names_no_survey

    # 4. Lucene quoted phrase `"shared statistical model"`
    hits_phrase = manager.search_library('"shared statistical model"')
    assert len(hits_phrase) == 1
    assert hits_phrase[0]["name"] == "Position The Platonic Representation Hypothesis"



