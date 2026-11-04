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

import concurrent.futures

def extract_single_leaf(job, text_cache_dir):
    try:
        print(f"    Extracting {job['name']}...")
        content = extract_content(job["real_path"])
        if content and content.text.strip():
            text_hash = get_text_hash(content.text)
            text_path = text_cache_dir / f"{text_hash}.md"
            if not text_path.exists():
                text_path.write_text(content.text, encoding="utf-8")
            job["result"] = {
                "text_hash": text_hash,
                "text_path": str(text_path)
            }
        else:
            job["result"] = None
    except Exception as e:
        print(f"    Error extracting {job['name']}: {e}")
        job["result"] = None

def run(book_threshold: int, overwrite: bool = False, vtree_path: str | Path | None = None):
    in_path = settings.CACHE_DIR / "cs_01_scan.json"
    if not in_path.exists():
        print(f"Error: Missing input {in_path}")
        sys.exit(1)
        
    print(f"--- Stage 2: Extracting Text ---")
    data = json.loads(in_path.read_text(encoding="utf-8"))
    
    target_vtree = Path(vtree_path) if vtree_path else settings.VTREE_PATH
    existing_data = {}
    if target_vtree.exists() and not overwrite:
        try:
            old_tree = json.loads(target_vtree.read_text(encoding="utf-8"))
            if "root" in old_tree:
                existing_data = extract_files_from_vtree(old_tree["root"])
                print(f"Loaded {len(existing_data)} already processed files from {target_vtree}.")
        except Exception as e:
            print(f"Warning: Failed to load existing vtree: {e}")

    TEXT_CACHE_DIR = settings.CACHE_DIR / "texts"
    TEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    jobs = []
    extracted_docs_proto = []
    
    for idx, item in enumerate(data):
        print(f"[{idx+1}/{len(data)}] Planning extraction for {item['name']}...")
        
        # Idempotency check
        if item["real_path"] in existing_data:
            existing = existing_data[item["real_path"]]
            if existing.get("type") == "document" and existing.get("text_path") and Path(existing["text_path"]).exists():
                print(f"  Skipping extraction. Found in existing vtree.")
                existing["_skip"] = True
                extracted_docs_proto.append(existing)
                continue
            elif existing.get("type") == "book" and all(c.get("text_path") and Path(c["text_path"]).exists() for c in existing.get("children", [])):
                print(f"  Skipping extraction. Book found in existing vtree.")
                existing["_skip"] = True
                extracted_docs_proto.append(existing)
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
                
            book_proto = {"name": finfo.name, "real_path": finfo.real_path, "type": "book", "page_count": finfo.page_count, "children": []}
            for sec in split_res.sections:
                job = {"real_path": sec.real_path, "name": sec.name, "page_count": sec.page_count}
                jobs.append(job)
                book_proto["children"].append(job)
            extracted_docs_proto.append(book_proto)
        else:
            job = {"real_path": finfo.real_path, "name": finfo.name, "page_count": finfo.page_count}
            jobs.append(job)
            extracted_docs_proto.append(job)
            
    # Process all jobs in parallel
    if jobs:
        print(f"\nExecuting {len(jobs)} extraction tasks in parallel...")
        MAX_WORKERS = getattr(settings, "MAX_CONCURRENT_REQUESTS", 5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(extract_single_leaf, job, TEXT_CACHE_DIR) for job in jobs]
            concurrent.futures.wait(futures)

    # Reconstruct final data
    extracted_docs = []
    for proto in extracted_docs_proto:
        if proto.get("_skip"):
            proto.pop("_skip", None)
            extracted_docs.append(proto)
            continue
            
        if proto.get("type") == "book":
            valid_children = []
            for c_job in proto.get("children", []):
                if c_job.get("result"):
                    valid_children.append({
                        "name": c_job["name"],
                        "real_path": c_job["real_path"],
                        "type": "document",
                        "page_count": c_job["page_count"],
                        "text_hash": c_job["result"]["text_hash"],
                        "text_path": c_job["result"]["text_path"]
                    })
            if valid_children:
                extracted_docs.append({
                    "name": proto["name"],
                    "real_path": proto["real_path"],
                    "type": "book",
                    "page_count": proto.get("page_count"),
                    "children": valid_children
                })
        else:
            if proto.get("result"):
                extracted_docs.append({
                    "name": proto["name"],
                    "real_path": proto["real_path"],
                    "type": "document",
                    "page_count": proto.get("page_count"),
                    "text_hash": proto["result"]["text_hash"],
                    "text_path": proto["result"]["text_path"]
                })
            
    out_path = settings.CACHE_DIR / "cs_02_extract.json"
    out_path.write_text(json.dumps(extracted_docs, indent=2), encoding="utf-8")
    print(f"Extraction complete. Wrote {len(extracted_docs)} nodes to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--vtree-path", type=str, default=None)
    args = parser.parse_args()
    run(args.book_threshold, args.overwrite, args.vtree_path)
