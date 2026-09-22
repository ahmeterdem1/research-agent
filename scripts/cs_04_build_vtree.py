import json
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from src.cold_start.vtree_builder import build_initial_vtree

def run(library_path: str, overwrite: bool = False):

    if overwrite and settings.VTREE_PATH.exists():
        print(f"Overwriting existing vtree at {settings.VTREE_PATH}")
        settings.VTREE_PATH.unlink()
    elif settings.VTREE_PATH.exists():
        print(f"VTree already exists at {settings.VTREE_PATH}. Use --overwrite to replace.")
        return

    in_path = settings.CACHE_DIR / "cs_03_summarize.json"
    if not in_path.exists():
        print(f"Error: Missing input {in_path}")
        sys.exit(1)
        
    print(f"--- Stage 4: Building Initial VTree ---")
    data = json.loads(in_path.read_text(encoding="utf-8"))
    
    vtree = build_initial_vtree(data, Path(library_path))
    
    settings.VTREE_PATH.write_text(json.dumps(vtree, indent=2), encoding="utf-8")
    print(f"Initial VTree built successfully. Wrote to {settings.VTREE_PATH}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-path", type=str, default=settings.LIBRARY_ROOT_PATH)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing vtree file")
    args = parser.parse_args()
    if not args.library_path:
        print("Missing library path.")
        sys.exit(1)
    run(args.library_path, args.overwrite)

