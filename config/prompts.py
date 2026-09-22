import tomllib
from pathlib import Path
from config.settings import PROJECT_ROOT

PROMPTS_FILE_PATH = PROJECT_ROOT / "config" / "prompts.toml"

_prompts_cache = None

def get_prompts(force_reload: bool = False) -> dict:
    """
    Loads and parses the prompts.toml file.
    Caches the result in memory unless force_reload is True.
    """
    global _prompts_cache
    if _prompts_cache is not None and not force_reload:
        return _prompts_cache
        
    if not PROMPTS_FILE_PATH.exists():
        raise FileNotFoundError(f"Prompts file not found at {PROMPTS_FILE_PATH}")
        
    with open(PROMPTS_FILE_PATH, "rb") as f:
        _prompts_cache = tomllib.load(f)
        
    return _prompts_cache

