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
