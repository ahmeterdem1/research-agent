import logging
import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent


def _resolve_data_dir() -> Path:
    """
    Resolve the global data directory for the Research Agent.
    Defaults to ~/.research-agent (or $RESEARCH_AGENT_HOME if set).
    Falls back to PROJECT_ROOT / 'data' only if the environment blocks writing to ~/.research-agent.
    """
    env_home = os.getenv("RESEARCH_AGENT_HOME")
    if env_home:
        candidate = Path(env_home).expanduser().resolve()
    else:
        candidate = Path.home() / ".research-agent"

    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        fallback = PROJECT_ROOT / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()

_INITIAL_PROCESS_ENV_KEYS: set[str] = set(os.environ.keys())
_LAST_ENV_MTIMES: dict[str, Optional[int]] = {}


def _get_env_files_mtimes() -> dict[str, Optional[int]]:
    paths = {
        "repo": PROJECT_ROOT / ".env",
        "global": DATA_DIR / ".env",
        "cwd": Path.cwd() / ".env",
    }
    mtimes: dict[str, Optional[int]] = {}
    for key, p in paths.items():
        try:
            mtimes[key] = p.stat().st_mtime_ns if p.exists() else None
        except OSError:
            mtimes[key] = None
    return mtimes


def _load_layered_dotenv(force_override: bool = False) -> None:
    """
    Load .env files with strict, deterministic precedence (lowest to highest):
    1. Repository fallback `<repo_root>/.env`
    2. Global user config `~/.research-agent/.env` (ALWAYS overrides `<repo_root>/.env`,
       even when `Path.cwd()` happens to be `<repo_root>`)
    3. Workspace-local `<cwd>/.env` (only when `<cwd>` is a separate project directory
       distinct from `<repo_root>` and `~/.research-agent`)
    4. Explicit process environment variables present before startup (unless `force_override=True`
       when a `.env` file was modified live on disk).
    """
    global _LAST_ENV_MTIMES
    repo_env = PROJECT_ROOT / ".env"
    global_env = DATA_DIR / ".env"
    cwd_env = Path.cwd() / ".env"

    merged: dict[str, str] = {}
    if repo_env.exists():
        for k, v in dotenv_values(repo_env).items():
            if v is not None:
                merged[k] = v

    if global_env.exists():
        for k, v in dotenv_values(global_env).items():
            if v is not None:
                merged[k] = v

    try:
        cwd_resolved = cwd_env.resolve()
        is_distinct_cwd = cwd_resolved not in {repo_env.resolve(), global_env.resolve()}
    except OSError:
        is_distinct_cwd = False

    if is_distinct_cwd and cwd_env.exists():
        for k, v in dotenv_values(cwd_env).items():
            if v is not None:
                merged[k] = v

    for k, v in merged.items():
        if force_override or k not in _INITIAL_PROCESS_ENV_KEYS:
            os.environ[k] = v

    _LAST_ENV_MTIMES = _get_env_files_mtimes()


_load_layered_dotenv(force_override=False)

# Subdirectories inside DATA_DIR
CACHE_DIR = DATA_DIR / "cache"
STAGED_DIR = CACHE_DIR / "staged"
VTREES_DIR = DATA_DIR / "vtrees"
LOGS_DIR = DATA_DIR / "logs"

for _d in (CACHE_DIR, STAGED_DIR, VTREES_DIR, LOGS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

# Important files
GLOBAL_VTREE_PATH = VTREES_DIR / "global.json"
VTREE_PATH = GLOBAL_VTREE_PATH
DB_PATH = DATA_DIR / "research_agent.db"

# We expect PATH_TO_TREE to be provided in the environment
LIBRARY_ROOT_PATH = os.getenv("PATH_TO_TREE", "")

# Model Configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

_DIRECT_LITELLM_PROVIDERS = {
    "openrouter",
    "ollama",
    "ollama_chat",
    "bedrock",
    "vertex_ai",
    "azure",
    "huggingface",
    "groq",
    "together_ai",
    "fireworks_ai",
    "hosted_vllm",
}


def normalize_litellm_model(model_name: str, default: str = "openrouter/deepseek/deepseek-chat") -> str:
    """
    Normalize a model identifier for LiteLLM.
    If the user specifies an OpenRouter-style `<vendor>/<model>` slug (e.g.
    `google/gemini-2.5-flash`, `qwen/qwen-2.5-72b-instruct`, `deepseek/deepseek-chat`)
    without the `openrouter/` prefix, automatically prepend `openrouter/` so LiteLLM
    does not fail with `BadRequestError: LLM Provider NOT provided`.
    """
    cleaned = (model_name or "").strip().strip("'\"")
    if not cleaned:
        return default
    if cleaned.startswith("openrouter/"):
        return cleaned
    parts = cleaned.split("/")
    if len(parts) == 2 and parts[0].lower() not in _DIRECT_LITELLM_PROVIDERS:
        return f"openrouter/{cleaned}"
    return cleaned


DEFAULT_AGENT_MODEL = normalize_litellm_model(
    os.getenv("DEFAULT_AGENT_MODEL", "openrouter/deepseek/deepseek-chat"),
    default="openrouter/deepseek/deepseek-chat",
)
DEFAULT_SUMMARIZATION_MODEL = normalize_litellm_model(
    os.getenv("DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat"),
    default="openrouter/deepseek/deepseek-chat",
)

# Extraction Configuration
# Supported modes: 'pymupdf' (default, fast local native PDF->Markdown, $0 cost),
# 'docling' (local vision neural networks), or 'llm' (OpenRouter Vision LLM OCR).
EXTRACTOR_MODE = os.getenv("EXTRACTOR_MODE", "pymupdf")
VISION_MODEL = normalize_litellm_model(
    os.getenv("VISION_MODEL", "openrouter/google/gemini-2.5-flash"),
    default="openrouter/google/gemini-2.5-flash",
)
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", 5))
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", 90))

# Application Thresholds
BOOK_PAGE_THRESHOLD = 30


def reload_env_settings() -> None:
    """
    Check if `~/.research-agent/.env`, `<cwd>/.env`, or `<repo>/.env` has been modified
    since startup and, if so, reload environment variables and refresh module-level
    configuration attributes live (without requiring an MCP server / OpenCode restart).
    """
    global LIBRARY_ROOT_PATH, OPENROUTER_API_KEY, DEFAULT_AGENT_MODEL
    global DEFAULT_SUMMARIZATION_MODEL, EXTRACTOR_MODE, VISION_MODEL
    global MAX_CONCURRENT_REQUESTS, LLM_REQUEST_TIMEOUT

    current_mtimes = _get_env_files_mtimes()
    if current_mtimes == _LAST_ENV_MTIMES:
        return

    _load_layered_dotenv(force_override=True)
    LIBRARY_ROOT_PATH = os.getenv("PATH_TO_TREE", "")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    DEFAULT_AGENT_MODEL = normalize_litellm_model(
        os.getenv("DEFAULT_AGENT_MODEL", "openrouter/deepseek/deepseek-chat"),
        default="openrouter/deepseek/deepseek-chat",
    )
    DEFAULT_SUMMARIZATION_MODEL = normalize_litellm_model(
        os.getenv("DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat"),
        default="openrouter/deepseek/deepseek-chat",
    )
    EXTRACTOR_MODE = os.getenv("EXTRACTOR_MODE", "pymupdf")
    VISION_MODEL = normalize_litellm_model(
        os.getenv("VISION_MODEL", "openrouter/google/gemini-2.5-flash"),
        default="openrouter/google/gemini-2.5-flash",
    )
    MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", 5))
    LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", 90))


class _StderrStreamHandler(logging.StreamHandler):
    """StreamHandler that always resolves the current ``sys.stderr`` (never ``stdout``)."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger that writes to stderr (never stdout, to keep
    MCP stdio JSON-RPC streams clean) and to DATA_DIR/logs/research_agent.log.
    """
    root_logger = logging.getLogger("research_agent")
    if not root_logger.handlers:
        level_name = os.getenv("RA_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        root_logger.setLevel(level)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        stderr_handler = _StderrStreamHandler()
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)

        try:
            log_file = LOGS_DIR / "research_agent.log"
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except OSError:
            pass

        root_logger.propagate = False

    child_name = name if name.startswith("research_agent") else f"research_agent.{name}"
    return logging.getLogger(child_name)


def get_active_project_id(cwd: Optional[Path] = None) -> str:
    """
    Determine the canonical project identifier for the current OpenCode workspace.
    Checks RA_PROJECT_ID first, otherwise uses the resolved current directory name.
    """
    explicit = os.getenv("RA_PROJECT_ID", "").strip()
    if explicit:
        return explicit
    target = (cwd or Path.cwd()).resolve()
    return target.name or "default-project"


def get_global_vtree_path() -> Path:
    """
    Return the active global VTree path.
    Checks RA_GLOBAL_VTREE_PATH first, then GLOBAL_VTREE_PATH, and falls back to
    legacy vtree.json locations if they exist and GLOBAL_VTREE_PATH does not yet exist.
    """
    explicit = os.getenv("RA_GLOBAL_VTREE_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if GLOBAL_VTREE_PATH.exists():
        return GLOBAL_VTREE_PATH
    for legacy in (DATA_DIR / "vtree.json", PROJECT_ROOT / "data" / "vtree.json"):
        if legacy.exists():
            return legacy
    return GLOBAL_VTREE_PATH


def get_project_vtree_path(cwd: Optional[Path] = None, must_exist: bool = True) -> Optional[Path]:
    """
    Return the project-local VTree path (<project>/.opencode/vtree.json or $RA_VTREE_PATH).
    If must_exist=True, returns None when no project-local VTree exists.
    """
    explicit = os.getenv("RA_VTREE_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
    else:
        base = (cwd or Path.cwd()).resolve()
        path = base / ".opencode" / "vtree.json"

    if must_exist and not path.exists():
        return None
    return path
