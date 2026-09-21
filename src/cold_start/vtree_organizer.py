import json
from litellm import completion
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from config import settings

def extract_all_files(node) -> set:
    files = set()
    def _extract(n):
        if not isinstance(n, dict):
            return
            
        ntype = n.get("type")
        if ntype in ("document", "book"):
            files.add((
                n.get("name"),
                n.get("real_path"),
                n.get("type"),
                n.get("summary")
            ))
            if ntype == "book" and "children" in n:
                for child in n["children"]:
                    _extract(child)
        elif ntype == "directory" and "children" in n:
            for child in n["children"]:
                _extract(child)
                
    _extract(node)
    return files

def organize_vtree(vtree: dict, max_turns: int = 15) -> dict:
    """
    Agentic pass to reorganize the virtual tree using MCP tools (ls, mkdir, mv).
    It performs groupings safely using deterministic Python functions.
    """
    import copy
    from src.vtree.operations import VTreeManager
    from src.mcp_servers.vtree_organizer_tools import set_active_manager, ls, mkdir, mv, finish, get_tool_schemas
    
    # Create a deep copy so we don't mutate the original dict in case of failure
    organized_vtree = copy.deepcopy(vtree)
    manager = VTreeManager(organized_vtree)
    set_active_manager(manager)
    
    tools = get_tool_schemas()

    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert librarian agent organizing a research directory.\n"
                "You have access to tools to inspect and modify the virtual file tree.\n"
                "Your task is to gently reorganize the files by grouping documents with similar themes into subdirectories.\n"
                "- Do NOT over-complicate. Group items only where it makes logical sense.\n"
                "- Use `ls` to explore the current structure.\n"
                "- Use `mkdir` to create category folders.\n"
                "- Use `mv` to move files into the new folders.\n"
                "- Once you are completely satisfied with the organization, call `finish`."
            )
        },
        {
            "role": "user",
            "content": "Please organize the virtual tree starting from the root ('/')."
        }
    ]

    # Agent Loop

    for turn in range(max_turns):
        print(f"\n[Agent Loop] Turn {turn + 1}/{max_turns}")
        try:
            response = completion(
                model=settings.DEFAULT_SUMMARIZATION_MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto"
            )
            
            message = response.choices[0].message
            messages.append(message)
            
            if not message.tool_calls:
                # LLM responded without a tool call. Remind it to use tools or finish.
                print("  [Agent] No tool call detected. Reminding agent to use tools or finish.")
                messages.append({
                    "role": "user",
                    "content": "Please use the tools provided to organize the tree, or call `finish` if you are done."
                })
                continue
                
            is_finished = False
            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                
                print(f"  [Agent] Calling {func_name}({args})")
                
                if func_name == "ls":
                    res = ls(args.get("vpath", "/"))
                elif func_name == "mkdir":
                    res = mkdir(args.get("vpath", ""))
                elif func_name == "mv":
                    res = mv(args.get("src", ""), args.get("dest", ""))
                elif func_name == "finish":
                    res = finish()
                    is_finished = True
                else:
                    res = f"Error: Unknown function {func_name}"
                    
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": func_name,
                    "content": str(res)
                })
                
            if is_finished:
                break
                
        except Exception as e:
            print(f"Warning: Agent loop encountered an error: {e}")
            break

    # Strict validation
    if "root" not in organized_vtree:
        print("Warning: VTree format corrupted (missing 'root'). Keeping original.")
        return vtree
        
    original_files = extract_all_files(vtree["root"])
    organized_files = extract_all_files(organized_vtree["root"])
    
    if original_files != organized_files:
        print("Warning: Essential file attributes altered or files lost during organization.")
        print(f"Original file count: {len(original_files)}, Organized file count: {len(organized_files)}")
        diff = original_files.symmetric_difference(organized_files)
        print(f"Differences detected: {len(diff)} items.")
        print("Keeping original tree to ensure safety.")
        return vtree
        
    return organized_vtree

