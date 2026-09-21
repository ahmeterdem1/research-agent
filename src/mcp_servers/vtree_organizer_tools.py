from fastmcp import FastMCP
import json
from src.vtree.operations import VTreeManager

# We will instantiate the FastMCP server
mcp = FastMCP("VTreeOrganizer")

# We need a global or injected state for the tree during the organization session.
# FastMCP tools are standalone functions, so we use a module-level variable to hold the active manager.
_active_manager: VTreeManager | None = None

def set_active_manager(manager: VTreeManager):
    global _active_manager
    _active_manager = manager

@mcp.tool()
def ls(vpath: str) -> str:
    """
    List contents of a directory in the virtual tree.
    Use this to inspect the current structure before and after making changes.
    Args:
        vpath: The virtual path to list (e.g., '/', '/Machine Learning').
    """
    if not _active_manager:
        return "Error: No active VTreeManager."
    return _active_manager.ls(vpath)

@mcp.tool()
def mkdir(vpath: str) -> str:
    """
    Create a new directory in the virtual tree.
    Args:
        vpath: The full virtual path of the new directory (e.g., '/Machine Learning/Transformers').
    """
    if not _active_manager:
        return "Error: No active VTreeManager."
    return _active_manager.mkdir(vpath)

@mcp.tool()
def mv(src: str, dest: str) -> str:
    """
    Move a file or directory from one virtual path to another.
    Can also be used to rename by changing the destination name.
    Args:
        src: The current full virtual path of the item.
        dest: The new full virtual path (including the item's name).
    """
    if not _active_manager:
        return "Error: No active VTreeManager."
    return _active_manager.mv(src, dest)

@mcp.tool()
def finish() -> str:
    """
    Call this tool when you have finished organizing the virtual tree.
    """
    return "ORGANIZATION_COMPLETE"

def get_tool_schemas() -> list[dict]:
    """Returns the OpenAI-compatible tool schemas for the VTree Organizer."""
    return [
        {
            "type": "function",
            "function": {
                "name": "ls",
                "description": "List contents of a directory in the virtual tree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "vpath": {"type": "string", "description": "The virtual path to list (e.g., '/', '/Machine Learning')."}
                    },
                    "required": ["vpath"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "mkdir",
                "description": "Create a new directory in the virtual tree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "vpath": {"type": "string", "description": "The full virtual path of the new directory (e.g., '/Machine Learning/Transformers')."}
                    },
                    "required": ["vpath"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "mv",
                "description": "Move a file or directory from one virtual path to another.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "src": {"type": "string", "description": "The current full virtual path of the item."},
                        "dest": {"type": "string", "description": "The new full virtual path (including the item's name)."}
                    },
                    "required": ["src", "dest"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Call this tool when you have finished organizing the virtual tree.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }
    ]


