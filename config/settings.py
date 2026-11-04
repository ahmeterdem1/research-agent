import logging
import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

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

# Load .env files in precedence order (highest precedence first, since override=False
# keeps whichever value was loaded first, and real process env vars always win):
# 1. Active workspace / project .env
# 2. Global ~/.research-agent/.env
# 3. Repository root .env
load_dotenv(Path.cwd() / ".env", override=False)
load_dotenv(DATA_DIR / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env", override=False)

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
