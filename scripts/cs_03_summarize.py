import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from src.cold_start.summarizer import summarize_text

def run():
    in_path = settings.CACHE_DIR / "cs_02_extract.json"
    if not in_path.exists():
        print(f"Error: Missing input {in_path}")
        sys.exit(1)
        
    print(f"--- Stage 3: Summarizing Documents ---")
    data = json.loads(in_path.read_text(encoding="utf-8"))
    
    for item in data:
        if item.get("type") == "book":
            item["summary"] = "A book containing multiple sections."
            for child in item.get("children", []):
                text_path = Path(child["text_path"])
                print(f"Summarizing {child['name']}...")
                text = text_path.read_text(encoding="utf-8")
                child["summary"] = summarize_text(text)
        else:
            text_path = Path(item["text_path"])
            print(f"Summarizing {item['name']}...")
            text = text_path.read_text(encoding="utf-8")
            item["summary"] = summarize_text(text)
            
    out_path = settings.CACHE_DIR / "cs_03_summarize.json"
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Summarization complete. Wrote intermediary to {out_path}")

if __name__ == "__main__":
    run()

