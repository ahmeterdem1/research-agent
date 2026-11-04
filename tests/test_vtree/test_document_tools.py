import pytest
from src.mcp_servers.document_tools import search_library, get_manager
from unittest.mock import patch, MagicMock

@patch("src.mcp_servers.document_tools.get_manager")
def test_search_library_tool_matches(mock_get_manager):
    mock_mgr = MagicMock()
    mock_mgr.search_library.return_value = [
        {"type": "document", "name": "test.pdf", "virtual_path": "/test.pdf", "summary": "A test paper"}
    ]
    mock_get_manager.return_value = mock_mgr
    
    result = search_library("test")
    assert "Match Found" in result
    assert "test.pdf" in result
    assert "/test.pdf" in result

@patch("src.mcp_servers.document_tools.get_manager")
def test_search_library_tool_no_matches(mock_get_manager):
    mock_mgr = MagicMock()
    mock_mgr.search_library.return_value = []
    mock_get_manager.return_value = mock_mgr
    
    result = search_library("notfound")
    assert "No local documents found" in result


def test_dual_scope_document_tools(monkeypatch, tmp_path):
    import json
    from src.mcp_servers import document_tools

    global_vtree_file = tmp_path / "global_vtree.json"
    project_vtree_file = tmp_path / "project_vtree.json"

    global_vtree_file.write_text(
        json.dumps(
            {
                "version": 1,
                "library_root": str(tmp_path),
                "root": {
                    "type": "directory",
                    "name": "Root",
                    "virtual_path": "/",
                    "summary": "Global root",
                    "children": [
                        {
                            "type": "document",
                            "name": "global_paper.pdf",
                            "virtual_path": "/global_paper.pdf",
                            "real_path": "/abs/global_paper.pdf",
                            "summary": "A foundational paper on transformers.",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    project_vtree_file.write_text(
        json.dumps(
            {
                "version": 1,
                "library_root": str(tmp_path),
                "root": {
                    "type": "directory",
                    "name": "Root",
                    "virtual_path": "/",
                    "summary": "Project root",
                    "children": [
                        {
                            "type": "document",
                            "name": "local_notes.md",
                            "virtual_path": "/local_notes.md",
                            "real_path": "/abs/local_notes.md",
                            "summary": "Project notes on transformers.",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(global_vtree_file))
    monkeypatch.setenv("RA_VTREE_PATH", str(project_vtree_file))
    monkeypatch.setenv("RA_PROJECT_ID", "demo-proj")

    # 1. search_library(scope="all") searches both global and project
    both = document_tools.search_library("transformers", scope="all")
    assert "[GLOBAL]" in both and "global_paper.pdf" in both
    assert "[PROJECT:demo-proj]" in both and "local_notes.md" in both

    # 2. search_library(scope="global") vs scope="project"
    only_global = document_tools.search_library("transformers", scope="global")
    assert "global_paper.pdf" in only_global and "local_notes.md" not in only_global

    only_proj = document_tools.search_library("transformers", scope="project")
    assert "local_notes.md" in only_proj and "global_paper.pdf" not in only_proj

    # 3. list_tree(scope="all") renders both trees
    tree_out = document_tools.list_tree("/", include_summaries=True, scope="all")
    assert "[GLOBAL]" in tree_out and "[PROJECT:demo-proj]" in tree_out

    # 4. get_document_real_path
    rpath = document_tools.get_document_real_path("/local_notes.md", scope="project")
    assert "/abs/local_notes.md" in rpath


def test_get_document_on_book_node_returns_chapter_toc(monkeypatch, tmp_path):
    import json
    from src.mcp_servers import document_tools

    global_vtree_file = tmp_path / "global_vtree.json"
    global_vtree_file.write_text(
        json.dumps(
            {
                "version": 1,
                "library_root": str(tmp_path),
                "root": {
                    "type": "directory",
                    "name": "Root",
                    "virtual_path": "/",
                    "summary": "Root",
                    "children": [
                        {
                            "type": "book",
                            "name": "DeepLearningBook",
                            "virtual_path": "/DeepLearningBook",
                            "real_path": "/abs/dl_book.pdf",
                            "page_count": 65,
                            "summary": "A book containing multiple sections on deep learning.",
                            "children": [
                                {
                                    "type": "document",
                                    "name": "Chapter 1 Intro",
                                    "virtual_path": "/DeepLearningBook/Chapter 1 Intro",
                                    "real_path": "/abs/dl_book.pdf",
                                    "page_count": 30,
                                    "summary": "Covers linear algebra and probability basics.",
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(global_vtree_file))

    out = document_tools.get_document("/DeepLearningBook", scope="global")
    assert "# Book: DeepLearningBook" in out
    assert "/DeepLearningBook/Chapter 1 Intro" in out
    assert "Covers linear algebra and probability basics." in out


