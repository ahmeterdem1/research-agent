import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# We expect PATH_TO_TREE to be provided in the environment
LIBRARY_ROOT_PATH = os.getenv("PATH_TO_TREE", "")

# We keep our project data internally
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

# Ensure data directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Important files
VTREE_PATH = DATA_DIR / "vtree.json"
DB_PATH = DATA_DIR / "research_agent.db"

# Model Configuration
DEFAULT_SUMMARIZATION_MODEL = "openrouter/deepseek/deepseek-chat"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Extraction Configuration
EXTRACTOR_MODE = os.getenv("EXTRACTOR_MODE", "docling") # 'docling' or 'llm'
VISION_MODEL = os.getenv("VISION_MODEL", "openrouter/google/gemini-1.5-pro")

# Application Thresholds
BOOK_PAGE_THRESHOLD = 30

