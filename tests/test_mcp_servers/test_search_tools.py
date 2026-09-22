import pytest
from unittest.mock import patch, MagicMock
from src.mcp_servers.search_tools import duckduckgo_search, scholar_search, get_paper_details, read_online_document

@patch("src.mcp_servers.search_tools.DDGS")
def test_duckduckgo_search(mock_ddgs):
    mock_instance = mock_ddgs.return_value
    mock_instance.text.return_value = [
        {"title": "Test Title", "href": "http://test.com", "body": "Test Snippet"}
    ]
    
    result = duckduckgo_search("test query", 1)
    assert "Title: Test Title" in result
    assert "URL: http://test.com" in result
    assert "Snippet: Test Snippet" in result

@patch("src.mcp_servers.search_tools.requests.get")
def test_scholar_search(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "title": "Attention Paper",
                "paperId": "123",
                "authors": [{"name": "Ashish Vaswani"}],
                "year": 2017,
                "citationCount": 10000,
                "url": "http://s2.org/123",
                "externalIds": {"DOI": "10.1234"}
            }
        ]
    }
    mock_get.return_value = mock_response
    
    result = scholar_search("Attention", 1)
    assert "Title: Attention Paper" in result
    assert "Authors: Ashish Vaswani" in result
    assert "DOI: 10.1234" in result

@patch("src.mcp_servers.search_tools.requests.get")
def test_scholar_search_429(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_get.return_value = mock_response
    
    result = scholar_search("Attention", 1)
    assert "Rate Limit Exceeded" in result

@patch("src.mcp_servers.search_tools.requests.get")
def test_get_paper_details(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "title": "Attention Paper",
        "abstract": "This is an abstract.",
        "authors": [{"name": "Ashish Vaswani"}],
        "year": 2017,
        "citationCount": 10000,
        "url": "http://s2.org/123",
        "venue": "NIPS",
        "tldr": {"text": "Transformers are good."}
    }
    mock_get.return_value = mock_response
    
    result = get_paper_details("123")
    assert "Venue: NIPS" in result
    assert "TLDR: Transformers are good." in result
    assert "This is an abstract." in result

@patch("src.mcp_servers.search_tools.requests.get")
def test_read_online_document_http(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html><body><p>Test Content HTML</p></body></html>"
    mock_get.return_value = mock_response
    
    result = read_online_document("http://test.com")
    assert "Test Content HTML" in result
