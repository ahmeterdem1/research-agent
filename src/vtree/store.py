import json
import threading
from pathlib import Path
from src.vtree.models import VTree

class VTreeStore:
    """Thread-safe storage layer for the VTree JSON file."""
    def __init__(self, filepath: Path | str):
        self.filepath = Path(filepath)
        self._lock = threading.RLock()
        self._cached_tree: VTree | None = None

    def load(self, force_reload: bool = False) -> VTree:
        """Loads and parses the vtree from disk, returning Pydantic models."""
        with self._lock:
            if self._cached_tree and not force_reload:
                return self._cached_tree
                
            if not self.filepath.exists():
                raise FileNotFoundError(f"VTree file not found at {self.filepath}")
                
            data = json.loads(self.filepath.read_text(encoding="utf-8"))
            self._cached_tree = VTree.model_validate(data)
            return self._cached_tree

    def save(self, vtree: VTree | dict):
        """Atomically saves the vtree to disk."""
        with self._lock:
            # If passed a dict (from operations), convert to Pydantic to ensure validity
            if isinstance(vtree, dict):
                vtree = VTree.model_validate(vtree)
                
            self._cached_tree = vtree
            
            # Atomic write pattern: write to .tmp then rename
            temp_path = self.filepath.with_suffix('.json.tmp')
            temp_path.write_text(vtree.model_dump_json(indent=2), encoding="utf-8")
            temp_path.replace(self.filepath)

    def load_as_dict(self) -> dict:
        """Helper to get raw dict, useful for VTreeManager operations."""
        with self._lock:
            if not self.filepath.exists():
                raise FileNotFoundError(f"VTree file not found at {self.filepath}")
            return json.loads(self.filepath.read_text(encoding="utf-8"))
            
    def save_dict(self, vtree_dict: dict):
        """Save directly from dict, bypassing explicit Pydantic instance creation (still validated)."""
        self.save(vtree_dict)

# Global store instance will be created in app factory or dependencies usually,
# but we can provide a default singleton helper if needed.

