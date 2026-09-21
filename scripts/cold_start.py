import os
import sys
import json
import argparse
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from config import settings
from src.cold_start.scanner import scan_directory
from src.cold_start.extractor import extract_content
from src.cold_start.splitter import split_book
from src.cold_start.summarizer import summarize_text
from src.cold_start.vtree_builder import build_initial_vtree
from src.cold_start.vtree_organizer import organize_vtree


def extract_files_from_vtree(node) -> dict:
    """Recursively extracts processed documents and books from the vtree into a dict keyed by real_path."""
    result = {}
    
    if node.get("type") in ("document", "book"):
        item = {
            "name": node["name"],
            "real_path": node["real_path"],
            "type": node["type"],
            "summary": node.get("summary", ""),
            "page_count": node.get("page_count")
        }
        if node["type"] == "book" and "children" in node:
            children = []
            for c in node["children"]:
                children.append({
                    "name": c["name"],
                    "real_path": c["real_path"],
                    "type": c["type"],
                    "summary": c.get("summary", ""),
                    "page_count": c.get("page_count")
                })
            item["children"] = children
        
        result[node["real_path"]] = item

    if "children" in node and node.get("type") != "book":
        for child in node["children"]:
            result.update(extract_files_from_vtree(child))
            
    return result

def run_cold_start(args):
    lib_path = args.library_path or settings.LIBRARY_ROOT_PATH
    if not lib_path or not Path(lib_path).exists():
        print(f"Error: Invalid or missing LIBRARY_ROOT_PATH: {lib_path}")
        print("Please set PATH_TO_TREE in your .env file.")
        print(f"Error: Invalid or missing library path: {lib_path}")
        print("Please set PATH_TO_TREE in your .env file or use --library-path.")
        sys.exit(1)
        
    print(f"Starting cold start pipeline for library: {lib_path}")
    
    existing_data = {}
    if settings.VTREE_PATH.exists() and not args.overwrite:
        print(f"Found existing vtree at {settings.VTREE_PATH}. Loading processed files to resume...")
        try:
            old_tree = json.loads(settings.VTREE_PATH.read_text())
            if "root" in old_tree:
                existing_data = extract_files_from_vtree(old_tree["root"])
                print(f"Loaded {len(existing_data)} already processed files.")
        except Exception as e:
            print(f"Warning: Failed to load existing vtree: {e}. Starting fresh.")
    elif args.overwrite:
        print("Overwrite flag set. Starting fresh.")
    
    # 1. Scan
    print("Scanning directory...")
    files_info = scan_directory(lib_path, settings.BOOK_PAGE_THRESHOLD)
    files_info = scan_directory(lib_path, args.book_threshold)
    file_count = len(files_info)
    print(f"Found {len(files_info)} supported files.")
    paper_count = sum(1 for f in files_info if not f.is_book)
    book_count = file_count - paper_count
    print(f"  Papers: {paper_count}, Books: {book_count}")
    
    processed_files = []
    
    # 2 & 3 & 4. Extract and Summarize
    for idx, f in enumerate(files_info):
        print(f"[{idx+1}/{len(files_info)}] Processing {f.name} ({f.file_type})...")
        
        if f.real_path in existing_data:
            print(f"  Skipping extraction of {f.name}. Found in existing vtree.")
            processed_files.append(existing_data[f.real_path])
            continue
        
        if f.is_book:
            if not f.is_splittable:
                print(f"  Warning: Book {f.name} is not splittable (no TOC). Skipping entirely.")
                continue
            
            # Split the book first
            split_res = split_book(f, limit=settings.BOOK_PAGE_THRESHOLD)
            split_res = split_book(f, limit=args.book_threshold)
            if not split_res.is_split:
                print(f"  Warning: Failed to split book {f.name}: {split_res.warning}. Skipping.")
                continue

            print(f"Splitted {f.name} into {len(split_res.sections)} sections with page counts [{', '.join([str(sec.page_count) for sec in split_res.sections])}] ")
                
            book_children = []
            for sec_idx, sec in enumerate(split_res.sections):
                sec_content = extract_content(sec.real_path)
                print(f"  Extracted content from Section {sec_idx+1}/{len(split_res.sections)}: {sec.name} ({sec.page_count} pages)")
                if not sec_content or not sec_content.text.strip():
                    continue
                sec_summary = summarize_text(sec_content.text)
                print(f"  Summarized Section {sec_idx+1}/{len(split_res.sections)}: {sec.name} -> {sec_summary[:60]}...")
                book_children.append({
                    "name": sec.name,
                    "real_path": sec.real_path,
                    "type": "document",
                    "page_count": sec.page_count,
                    "summary": sec_summary
                })
                
            if not book_children:
                continue
                
            processed_files.append({
                "name": f.name,
                "real_path": f.real_path,
                "type": "book",
                "page_count": f.page_count,
                "summary": "A book containing multiple sections.",
                "children": book_children
            })
            continue

        # Regular document
        content = extract_content(f.real_path)
        
        if not content or not content.text.strip():
            print(f"  Warning: No content extracted from {f.name}. Skipping.")
            continue
            
        # Summarize
        summary = summarize_text(content.text)
        
        processed_files.append({
            "name": f.name,
            "real_path": f.real_path,
            "type": "book" if f.is_book else "document",
            "page_count": f.page_count,
            "summary": summary
        })

    # 5. Build Initial VTree
    print("Building virtual tree...")
    vtree = build_initial_vtree(processed_files, Path(lib_path))
    
    # 6. Organize VTree
    print("Organizing virtual tree...")
    final_vtree = vtree
    if not args.skip_organize:
        final_vtree = organize_vtree(vtree)
    else:
        print("Skipping VTree organization (--skip-organize).")
    
    # Save
    print(f"Saving to {settings.VTREE_PATH}...")
    settings.VTREE_PATH.write_text(json.dumps(final_vtree, indent=2))
    
    print("Cold start complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the cold start pipeline to build the virtual file tree.", add_help=True)
    parser.add_argument("--overwrite", action="store_true", help="Start fresh and overwrite existing vtree data")
    parser.add_argument("--library-path", type=str, help="Path to the research library root (overrides PATH_TO_TREE in .env)")
    parser.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD, help="Page limit threshold for identifying and splitting books")
    parser.add_argument("--skip-organize", action="store_true", help="Skip the LLM-based reorganization pass")
    
    args = parser.parse_args()
    run_cold_start(args)

