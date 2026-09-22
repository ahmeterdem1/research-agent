import pytest
from unittest.mock import patch, MagicMock
from src.subagents.citation_walker import get_references, get_citations, walk_citation_graph

@patch("src.subagents.citation_walker.requests.get")
def test_get_references(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "references": [
            {"paperId": "ref1", "title": "Reference 1"},
            {"paperId": "ref2", "title": "Reference 2"}
        ]
    }
    mock_get.return_value = mock_response
    
    results = get_references("base_paper")
    assert len(results) == 2
    assert results[0]["id"] == "ref1"
    assert results[1]["title"] == "Reference 2"

@patch("src.subagents.citation_walker.requests.get")
def test_get_citations(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "citations": [
            {"paperId": "cit1", "title": "Citation 1"}
        ]
    }
    mock_get.return_value = mock_response
    
    results = get_citations("base_paper")
    assert len(results) == 1
    assert results[0]["id"] == "cit1"

@patch("src.subagents.citation_walker.requests.get")
def test_rate_limit(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_get.return_value = mock_response
    
    assert get_references("base") == []
    assert get_citations("base") == []

def test_walk_citation_graph():
    # Stub test
    res = walk_citation_graph("123", depth=2)
    assert "Citation Walk Summary for 123:" in res
