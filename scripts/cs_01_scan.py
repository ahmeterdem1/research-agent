import json
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from src.cold_start.scanner import scan_directory

def run(library_path: str, book_threshold: int, overwrite: bool = False):
    out_path = settings.CACHE_DIR / "cs_01_scan.json"

    if Path(out_path).exists() and not overwrite:
        print(f"Scan file exists: {out_path} and overwrite is not set. Skipping scan.")
        return
    elif Path(out_path).exists():
        print(f"Scan file already exists: {out_path}. Overwriting the scan file.")
        return

    print(f"--- Stage 1: Scanning {library_path} ---")
    files_info = scan_directory(library_path, book_threshold)
    
    output = []
    for f in files_info:
        output.append({
            "real_path": f.real_path,
            "name": f.name,
            "file_type": f.file_type,
            "page_count": f.page_count,
            "is_book": f.is_book,
            "is_splittable": f.is_splittable
        })
        
    
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Scanned {len(output)} files. Wrote intermediary to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-path", type=str, default=settings.LIBRARY_ROOT_PATH)
    parser.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing scan file")
    args = parser.parse_args()
    if not args.library_path:
        print("Missing library path.")
        sys.exit(1)
    run(args.library_path, args.book_threshold, args.overwrite)

