import contextlib
import json
import os
import threading
import uuid
from pathlib import Path
from src.vtree.models import VTree

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def atomic_write_text(path: Path | str, content: str, encoding: str = "utf-8") -> Path:
    """Atomically write text to `path` via a PID/UUID-scoped temp file in the same directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        temp_path.write_text(content, encoding=encoding)
        temp_path.replace(target)
    finally:
        temp_path.unlink(missing_ok=True)
    return target


class VTreeStore:
    """Thread- and cross-process-safe storage layer for the VTree JSON file."""

    def __init__(self, filepath: Path | str):
        self.filepath = Path(filepath)
        self._lock_filepath = self.filepath.with_suffix(self.filepath.suffix + ".lock")
        self._lock = threading.RLock()
        self._flock_depth = 0
        self._flock_fd = None
        self._cached_tree: VTree | None = None
        self._cached_stat: tuple[int, int] | None = None

    @staticmethod
    def _file_stat(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    @contextlib.contextmanager
    def file_lock(self):
        """Acquire both the in-process RLock and a system-wide advisory OS flock (re-entrant)."""
        with self._lock:
            acquired_flock = False
            if fcntl is not None and self._flock_depth == 0:
                self._lock_filepath.parent.mkdir(parents=True, exist_ok=True)
                fd = open(self._lock_filepath, "a+", encoding="utf-8")
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
                except Exception:
                    fd.close()
                    raise
                self._flock_fd = fd
                acquired_flock = True
            self._flock_depth += 1
            try:
                yield
            finally:
                self._flock_depth -= 1
                if acquired_flock and self._flock_fd is not None:
                    fd = self._flock_fd
                    self._flock_fd = None
                    try:
                        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                    finally:
                        fd.close()

    def load(self, force_reload: bool = False) -> VTree:
        """Loads and parses the vtree from disk, invalidating the cache if modified externally."""
        with self._lock:
            if not self.filepath.exists():
                self._cached_tree = None
                self._cached_stat = None
                raise FileNotFoundError(f"VTree file not found at {self.filepath}")

            current_stat = self._file_stat(self.filepath)
            if (
                self._cached_tree is not None
                and not force_reload
                and current_stat is not None
                and current_stat == self._cached_stat
            ):
                return self._cached_tree

            data = json.loads(self.filepath.read_text(encoding="utf-8"))
            self._cached_tree = VTree.model_validate(data)
            self._cached_stat = self._file_stat(self.filepath)
            return self._cached_tree

    def save(self, vtree: VTree | dict):
        """Atomically saves the vtree to disk under a cross-process file lock."""
        with self.file_lock():
            if isinstance(vtree, dict):
                vtree = VTree.model_validate(vtree)

            self._cached_tree = vtree
            atomic_write_text(self.filepath, vtree.model_dump_json(indent=2), encoding="utf-8")
            self._cached_stat = self._file_stat(self.filepath)

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

