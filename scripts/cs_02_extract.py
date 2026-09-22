import json
import argparse
import sys
from pathlib import Path
import hashlib

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from src.cold_start.scanner import FileInfo
from src.cold_start.splitter import split_book
from src.cold_start.extractor import extract_content

def get_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def extract_files_from_vtree(node) -> dict:
    """Recursively extracts processed documents and books from the vtree into a dict keyed by real_path."""
    result = {}
    if node.get("type") in ("document", "book"):
        result[node.get("real_path")] = node
    if "children" in node:
        for child in node["children"]:
            result.update(extract_files_from_vtree(child))
    return result

def run(book_threshold: int, overwrite: bool = False):
    in_path = settings.CACHE_DIR / "cs_01_scan.json"
    if not in_path.exists():
        print(f"Error: Missing input {in_path}")
        sys.exit(1)
        
    print(f"--- Stage 2: Extracting Text ---")
    data = json.loads(in_path.read_text())
    
    existing_data = {}
    if settings.VTREE_PATH.exists() and not overwrite:
        try:
            old_tree = json.loads(settings.VTREE_PATH.read_text())
            if "root" in old_tree:
                existing_data = extract_files_from_vtree(old_tree["root"])
                print(f"Loaded {len(existing_data)} already processed files from vtree.")
        except Exception as e:
            print(f"Warning: Failed to load existing vtree: {e}")

    TEXT_CACHE_DIR = settings.CACHE_DIR / "texts"
    TEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    extracted_docs = []
    
    for idx, item in enumerate(data):
        print(f"[{idx+1}/{len(data)}] Extracting {item['name']}...")
        
        # Idempotency check
        if item["real_path"] in existing_data:
            existing = existing_data[item["real_path"]]
            # Check if text_path actually exists
            if existing.get("type") == "document" and existing.get("text_path") and Path(existing["text_path"]).exists():
                print(f"  Skipping extraction. Found in existing vtree.")
                extracted_docs.append(existing)
                continue
            elif existing.get("type") == "book" and all(c.get("text_path") and Path(c["text_path"]).exists() for c in existing.get("children", [])):
                print(f"  Skipping extraction. Book found in existing vtree.")
                extracted_docs.append(existing)
                continue

        
        finfo = FileInfo(
            real_path=item["real_path"],
            name=item["name"],
            file_type=item["file_type"],
            page_count=item.get("page_count"),
            is_book=item["is_book"],
            is_splittable=item.get("is_splittable", False)
        )
        
        if finfo.is_book:
            if not finfo.is_splittable:
                print(f"  Warning: Book {finfo.name} is not splittable. Skipping entirely.")
                continue
                
            split_res = split_book(finfo, limit=book_threshold)
            if not split_res.is_split:
                print(f"  Warning: Failed to split book {finfo.name}: {split_res.warning}. Skipping.")
                continue
                
            book_children = []
            for sec_idx, sec in enumerate(split_res.sections):
                content = extract_content(sec.real_path)
                if not content or not content.text.strip():
                    continue
                    
                text_hash = get_text_hash(content.text)
                text_path = TEXT_CACHE_DIR / f"{text_hash}.md"
                if not text_path.exists():
                    text_path.write_text(content.text)
                    
                book_children.append({
                    "name": sec.name,
                    "real_path": sec.real_path,
                    "type": "document",
                    "page_count": sec.page_count,
                    "text_hash": text_hash,
                    "text_path": str(text_path)
                })
                
            if book_children:
                extracted_docs.append({
                    "name": finfo.name,
                    "real_path": finfo.real_path,
                    "type": "book",
                    "page_count": finfo.page_count,
                    "children": book_children
                })
        else:
            # Regular document
            content = extract_content(finfo.real_path)
            if not content or not content.text.strip():
                print(f"  Warning: No content extracted from {finfo.name}. Skipping.")
                continue
                
            text_hash = get_text_hash(content.text)
            text_path = TEXT_CACHE_DIR / f"{text_hash}.md"
            if not text_path.exists():
                text_path.write_text(content.text)
                
            extracted_docs.append({
                "name": finfo.name,
                "real_path": finfo.real_path,
                "type": "document",
                "page_count": finfo.page_count,
                "text_hash": text_hash,
                "text_path": str(text_path)
            })
            
    out_path = settings.CACHE_DIR / "cs_02_extract.json"
    out_path.write_text(json.dumps(extracted_docs, indent=2))
    print(f"Extraction complete. Wrote {len(extracted_docs)} nodes to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    run(args.book_threshold, args.overwrite)
