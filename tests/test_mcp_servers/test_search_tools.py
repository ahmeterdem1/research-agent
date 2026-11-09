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
                "externalIds": {"DOI": "10.1234", "ArXiv": "1706.03762"}
            }
        ]
    }
    mock_get.return_value = mock_response
    
    result = scholar_search("Attention", 1)
    assert "Title: Attention Paper" in result
    assert "Authors: Ashish Vaswani" in result
    assert "DOI: 10.1234" in result
    assert "ArXiv ID: 1706.03762" in result

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


@patch("src.mcp_servers.search_tools.get_references")
@patch("src.mcp_servers.search_tools.get_citations")
def test_get_paper_references_and_citations_tools(mock_cits, mock_refs):
    from src.mcp_servers.search_tools import get_paper_citations, get_paper_references

    mock_refs.return_value = [{"id": "r1", "title": "Foundational Paper", "year": 2015, "citationCount": 500}]
    mock_cits.return_value = [{"id": "c1", "title": "Follow-up Paper", "year": 2020, "citationCount": 120}]

    ref_out = get_paper_references("123", limit=5)
    cit_out = get_paper_citations("123", limit=5)

    assert "Foundational Paper" in ref_out and "r1" in ref_out
    assert "Follow-up Paper" in cit_out and "c1" in cit_out


@patch("src.mcp_servers.search_tools.requests.get")
def test_read_online_document_pagination_outline_and_boilerplate_stripping(mock_get):
    body_paragraphs = "\n".join(f"<p>Paragraph line {i} of the article.</p>" for i in range(1, 510))
    html = (
        "<html><head><script>console.log('noise');</script><style>.x{color:red}</style></head>"
        "<body>"
        "<nav>Top Menu Link 1 | Top Menu Link 2</nav>"
        "<article>"
        "<h1>1. Introduction</h1>"
        f"{body_paragraphs}"
        "<h2>2. Conclusion</h2>"
        "<p>Final summary paragraph.</p>"
        "</article>"
        "<footer>Copyright Footer Noise</footer>"
        "</body></html>"
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = html
    mock_get.return_value = mock_response

    # Page 1: lines 1-400 + outline + continuation footer
    page1 = read_online_document("https://example.org/long-post", start_line=1)
    assert "Showing lines 1-400 of" in page1
    assert "Document Outline (Headings & Line Numbers):" in page1
    assert "# 1. Introduction" in page1
    assert "## 2. Conclusion" in page1
    assert "Top Menu Link 1" not in page1
    assert "Copyright Footer Noise" not in page1
    assert "console.log" not in page1
    assert "start_line=401" in page1

    # Page 2: lines 401-600 reaches end of document
    page2 = read_online_document("https://example.org/long-post", start_line=401, end_line=600)
    assert "Showing lines 401-" in page2
    assert "Final summary paragraph." in page2
    assert "End of document reached" in page2


