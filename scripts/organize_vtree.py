import os
import sys
import json
import argparse
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from config import settings
from src.cold_start.vtree_organizer import organize_vtree

def run_organize(args):
    vtree_path = Path(args.vtree_path)
    
    if not vtree_path.exists():
        print(f"Error: No virtual tree found at {vtree_path}")
        print("Please run cold_start.py first to build the initial tree.")
        sys.exit(1)
        
    print(f"Loading virtual tree from {vtree_path}...")
    try:
        vtree = json.loads(vtree_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Error reading JSON from {vtree_path}: {e}")
        sys.exit(1)
        
    print("Sending tree to LLM for organization...")
    organized_vtree = organize_vtree(vtree, args.max_turns)
    
    # We can use the simple identity check since organize_vtree returns the original tree on failure
    if organized_vtree is vtree:
        print("Organization failed or skipped. The virtual tree was not modified.")
    else:
        print(f"Organization successful! Saving updated tree to {vtree_path}...")
        vtree_path.write_text(json.dumps(organized_vtree, indent=2))
        print("Done.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reorganize an existing virtual file tree using LLM.")
    parser.add_argument("--vtree-path", type=str, default=settings.VTREE_PATH, help=f"Path to the vtree.json file (defaults to {settings.VTREE_PATH})")
    parser.add_argument("--max-turns", type=int, default=15, help="Maximum number of turns for the agent to organize the tree (defaults to 15).")
    
    args = parser.parse_args()
    run_organize(args)

