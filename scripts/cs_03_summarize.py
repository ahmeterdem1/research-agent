import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from src.cold_start.summarizer import summarize_text, summarize_book

def robust_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252", errors="replace")

def run():
    in_path = settings.CACHE_DIR / "cs_02_extract.json"
    if not in_path.exists():
        print(f"Error: Missing input {in_path}")
        sys.exit(1)
        
    print(f"--- Stage 3: Summarizing Documents ---")
    data = json.loads(in_path.read_text(encoding="utf-8"))
    
    for item in data:
        if item.get("type") == "book":
            child_summaries = []
            for child in item.get("children", []):
                text_path = Path(child["text_path"])
                print(f"Summarizing {child['name']}...")
                text = robust_read(text_path)
                child_summary = summarize_text(text)
                child["summary"] = child_summary
                child_summaries.append(f"{child['name']}: {child_summary}")
                
            print(f"Summarizing book {item['name']} from its sections...")
            book_summary = summarize_book(child_summaries)
            item["summary"] = f"A book containing multiple sections. {book_summary}"
        else:
            text_path = Path(item["text_path"])
            print(f"Summarizing {item['name']}...")
            text = robust_read(text_path)
            item["summary"] = summarize_text(text)
            
    out_path = settings.CACHE_DIR / "cs_03_summarize.json"
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Summarization complete. Wrote intermediary to {out_path}")

if __name__ == "__main__":
    run()

