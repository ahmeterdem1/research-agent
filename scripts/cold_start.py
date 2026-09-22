import argparse
import sys
import subprocess
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))
from config import settings

def run_stage(script_name: str, args: list[str] = None):
    cmd = [sys.executable, str(Path(__file__).parent / script_name)]
    if args:
        cmd.extend(args)
    print(f"\n=========================================")
    print(f"Running {' '.join(cmd)}")
    print(f"=========================================\n")
    
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"Error: {script_name} failed with return code {result.returncode}")
        sys.exit(result.returncode)

def main():
    parser = argparse.ArgumentParser(description="Run the full cold start pipeline.")
    parser.add_argument("--overwrite", action="store_true", help="Start fresh and overwrite existing vtree data")
    parser.add_argument("--library-path", type=str, help="Path to the research library root")
    parser.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    parser.add_argument("--skip-organize", action="store_true", help="Skip the LLM-based reorganization pass")
    
    args = parser.parse_args()
    
    lib_path = args.library_path or settings.LIBRARY_ROOT_PATH
    if not lib_path or not Path(lib_path).exists():
        print(f"Error: Invalid or missing library path: {lib_path}")
        sys.exit(1)
        
    print(f"Starting pipeline for: {lib_path}")
    
    # 1. Scan
    run_stage("cs_01_scan.py", ["--library-path", lib_path, "--book-threshold", str(args.book_threshold)])
    
    # 2. Extract
    extract_args = ["--book-threshold", str(args.book_threshold)]
    if args.overwrite:
        extract_args.append("--overwrite")
    run_stage("cs_02_extract.py", extract_args)
    
    # 3. Summarize
    run_stage("cs_03_summarize.py")
    
    # 4. Build Initial VTree
    run_stage("cs_04_build_vtree.py", ["--library-path", lib_path])
    
    # 5. Organize VTree (Optional)
    if not args.skip_organize:
        run_stage("organize_vtree.py")
    else:
        print("Skipping VTree organization.")
        
    print("\nCold start pipeline completed successfully!")

if __name__ == "__main__":
    main()
