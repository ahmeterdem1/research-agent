"""
Package entrypoints for system-wide installation (`uv tool install -e .`).

Ensures the repository root is on `sys.path` before dispatching to `src.*` and
`config.*` modules so the CLI and MCP servers work from any working directory on the PC.
"""

from pathlib import Path
import sys

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def main() -> None:
    from src.cli import main as _cli_main

    sys.exit(_cli_main())


def mcp_docs() -> None:
    from src.mcp_servers.document_tools import main as _docs_main

    _docs_main()


def mcp_search() -> None:
    from src.mcp_servers.search_tools import main as _search_main

    _search_main()


def mcp_ingest() -> None:
    from src.mcp_servers.file_tools import main as _ingest_main

    _ingest_main()


def mcp_kg() -> None:
    from src.mcp_servers.kg_tools import main as _kg_main

    _kg_main()
