import json
from litellm import completion
from tenacity import retry, stop_after_attempt, wait_exponential
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from config import settings
from config.settings import get_logger

logger = get_logger(__name__)

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

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def _call_llm_with_retry(messages, tools):
    model = settings.normalize_litellm_model(
        getattr(settings, "DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat")
    )
    req_timeout = int(getattr(settings, "LLM_REQUEST_TIMEOUT", 90))
    return completion(
        model=model,
        timeout=req_timeout,
        messages=messages,
        tools=tools,
        tool_choice="auto"
    )

def organize_vtree(vtree: dict, max_turns: int = 15) -> dict:
    """
    Agentic pass to reorganize the virtual tree using MCP tools (ls, mkdir, mv).
    It performs groupings safely using deterministic Python functions.
    """
    import copy
    from src.vtree.operations import VTreeManager
    from src.mcp_servers.vtree_organizer_tools import set_active_manager, ls, mkdir, mv, finish, get_tool_schemas
    from config.prompts import get_prompts
    
    # Create a deep copy so we don't mutate the original dict in case of failure
    organized_vtree = copy.deepcopy(vtree)
    manager = VTreeManager(organized_vtree)
    set_active_manager(manager)
    
    tools = get_tool_schemas()
    prompts = get_prompts()["vtree_organizer"]

    messages = [
        {
            "role": "system",
            "content": prompts["system_prompt"]
        },
        {
            "role": "user",
            "content": prompts["user_prompt_start"]
        }
    ]

    # Agent Loop

    for turn in range(max_turns):
        logger.info("[Organizer Agent Loop] Turn %d/%d", turn + 1, max_turns)
        try:
            response = _call_llm_with_retry(
                messages=messages,
                tools=tools
            )
            
            message = response.choices[0].message
            messages.append(message)
            
            if not message.tool_calls:
                # LLM responded without a tool call. Remind it to use tools or finish.
                logger.info("[Organizer Agent] No tool call detected. Reminding agent to use tools or finish.")
                messages.append({
                    "role": "user",
                    "content": prompts["user_prompt_remind"]
                })
                continue
                
            is_finished = False
            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                
                logger.info("[Organizer Agent] Calling %s(%s)", func_name, args)
                
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
            logger.warning("Organizer agent loop encountered an error: %s", e)
            break

    # Strict validation
    if "root" not in organized_vtree:
        logger.warning("VTree format corrupted (missing 'root'). Keeping original.")
        return vtree
        
    original_files = extract_all_files(vtree["root"])
    organized_files = extract_all_files(organized_vtree["root"])
    
    if original_files != organized_files:
        diff = original_files.symmetric_difference(organized_files)
        logger.warning(
            "Essential file attributes altered or files lost during organization "
            "(original=%d, organized=%d, diff=%d). Keeping original tree.",
            len(original_files),
            len(organized_files),
            len(diff),
        )
        return vtree

    # Recompute directory summaries for any newly created ("User created directory.") or modified folders
    try:
        from src.cold_start.refresher import _refresh_dirty_directory_summaries

        _refresh_dirty_directory_summaries(organized_vtree["root"])
    except Exception as e:
        logger.warning("Could not refresh directory summaries after organization: %s", e)

    return organized_vtree

