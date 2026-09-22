import json
import sys
from pathlib import Path
import concurrent.futures
import argparse

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from src.cold_start.summarizer import summarize_text, summarize_book

def robust_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252", errors="replace")

def run(overwrite: bool = False):
    in_path = settings.CACHE_DIR / "cs_02_extract.json"
    out_path = settings.CACHE_DIR / "cs_03_summarize.json"

    if not in_path.exists():
        print(f"Error: Missing input {in_path}")
        sys.exit(1)

    if out_path.exists() and not overwrite:
        print(f"Summary file exists: {out_path} and overwrite is not set. Skipping summarization.")
        return
    elif out_path.exists():
        print(f"Summary file already exists: {out_path}. Overwriting the summary file.")
        
    print(f"--- Stage 3: Summarizing Documents ---")
    data = json.loads(in_path.read_text(encoding="utf-8"))
    
    # We will use a thread pool semaphore to limit concurrent requests
    MAX_WORKERS = getattr(settings, "MAX_CONCURRENT_REQUESTS", 5)
    
    def process_leaf(node):
        text_path = Path(node["text_path"])
        print(f"Summarizing leaf node: {node['name']}...")
        text = robust_read(text_path)
        try:
            node["summary"] = summarize_text(text)
        except Exception as e:
            print(f"  Failed to summarize {node['name']}: {e}")
            node["summary"] = "Summary generation failed."
        
    def process_book(node):
        print(f"Summarizing book {node['name']} from its sections...")
        child_summaries = [f"{c['name']}: {c.get('summary', 'No summary')}" for c in node.get("children", [])]
        try:
            book_summary = summarize_book(child_summaries)
            node["summary"] = f"A book containing multiple sections. {book_summary}"
        except Exception as e:
            print(f"  Failed to summarize book {node['name']}: {e}")
            node["summary"] = "A book containing multiple sections. Summary generation failed."

    # Pass 1: Summarize all leaves (documents and book children) in parallel
    leaves = []
    for item in data:
        if item.get("type") == "book":
            for child in item.get("children", []):
                leaves.append(child)
        else:
            leaves.append(item)
            
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_leaf, leaf) for leaf in leaves]
        concurrent.futures.wait(futures)

    # Pass 2: Summarize all books in parallel (depends on child summaries)
    books = [item for item in data if item.get("type") == "book"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_book, book) for book in books]
        concurrent.futures.wait(futures)
            
    
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Summarization complete. Wrote intermediary to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing summary file")
    args = parser.parse_args()
    run(args.overwrite)

