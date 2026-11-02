import os
from pathlib import Path
from dataclasses import dataclass
import fitz  # PyMuPDF

from config.settings import get_logger

logger = get_logger(__name__)

@dataclass
class FileInfo:
    real_path: str
    name: str
    file_type: str
    page_count: int | None
    is_book: bool
    is_splittable: bool = False

def scan_directory(root_path: str | Path, book_threshold: int = 30) -> list[FileInfo]:
    """
    Recursively scans the directory and classifies files.
    """
    root_path = Path(root_path)
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"Directory {root_path} does not exist.")

    files = []
    supported_extensions = {".pdf", ".txt", ".md", ".docx"}

    for dirpath, _, filenames in os.walk(root_path):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            ext = file_path.suffix.lower()
            
            if ext not in supported_extensions:
                continue

            page_count = None
            is_book = False
            is_splittable = False

            if ext == ".pdf":
                try:
                    with fitz.open(file_path) as doc:
                        page_count = doc.page_count
                        logger.info("Scanned %s: %d pages.", file_path, page_count)
                        if page_count > book_threshold:
                            is_book = True
                            toc = doc.get_toc()
                            if len(toc) > 0:
                                is_splittable = True
                except Exception as e:
                    logger.warning("Could not read %s: %s", file_path, e)
                    continue
            
            files.append(FileInfo(
                real_path=str(file_path),
                name=file_path.stem,
                file_type=ext.lstrip('.'),
                page_count=page_count,
                is_book=is_book,
                is_splittable=is_splittable
            ))
            
    return files

