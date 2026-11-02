import argparse
from pathlib import Path
import subprocess
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from config.settings import get_logger

logger = get_logger(__name__)


def run_stage(script_name: str, args: list[str] | None = None):
    cmd = [sys.executable, str(Path(__file__).parent / script_name)]
    if args:
        cmd.extend(args)
    logger.info("Running stage: %s", " ".join(cmd))

    result = subprocess.run(cmd)
    if result.returncode != 0:
        logger.error("%s failed with return code %d", script_name, result.returncode)
        sys.exit(result.returncode)


def run_cold_start(
    library_path: str,
    scope: str = "global",
    output_vtree: str | Path | None = None,
    book_threshold: int = settings.BOOK_PAGE_THRESHOLD,
    overwrite: bool = False,
    skip_organize: bool = False,
) -> Path:
    if not library_path or not Path(library_path).exists():
        raise ValueError(f"Invalid or missing library path: {library_path}")

    if output_vtree:
        target_vtree = Path(output_vtree).expanduser().resolve()
    elif scope == "project":
        target_vtree = settings.get_project_vtree_path(must_exist=False)
    else:
        target_vtree = settings.get_global_vtree_path()

    logger.info("Starting cold-start pipeline for '%s' -> %s (scope=%s)", library_path, target_vtree, scope)

    # 1. Scan
    scan_args = ["--library-path", str(library_path), "--book-threshold", str(book_threshold)]
    if overwrite:
        scan_args.append("--overwrite")
    run_stage("cs_01_scan.py", scan_args)

    # 2. Extract
    extract_args = ["--book-threshold", str(book_threshold)]
    if overwrite:
        extract_args.append("--overwrite")
    run_stage("cs_02_extract.py", extract_args)

    # 3. Summarize
    summarize_args = ["--overwrite"] if overwrite else []
    run_stage("cs_03_summarize.py", summarize_args)

    # 4. Build VTree
    vtree_args = ["--library-path", str(library_path), "--output-vtree", str(target_vtree)]
    if overwrite:
        vtree_args.append("--overwrite")
    run_stage("cs_04_build_vtree.py", vtree_args)

    # 5. Organize VTree (Optional)
    if not skip_organize:
        run_stage("organize_vtree.py", ["--vtree-path", str(target_vtree)])
    else:
        logger.info("Skipping VTree organization.")

    logger.info("Cold-start pipeline completed successfully: %s", target_vtree)
    return target_vtree


def main():
    parser = argparse.ArgumentParser(description="Run the full cold start pipeline.")
    parser.add_argument("--overwrite", action="store_true", help="Start fresh and overwrite existing vtree data")
    parser.add_argument("--library-path", type=str, help="Path to the research library root")
    parser.add_argument(
        "--scope",
        choices=["global", "project"],
        default="global",
        help="Target VTree scope: 'global' (~/.research-agent/vtrees/global.json) or 'project' (.opencode/vtree.json)",
    )
    parser.add_argument("--output-vtree", type=str, default=None, help="Explicit output path for vtree.json")
    parser.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    parser.add_argument("--skip-organize", action="store_true", help="Skip the LLM-based reorganization pass")

    args = parser.parse_args()
    lib_path = args.library_path or settings.LIBRARY_ROOT_PATH
    try:
        run_cold_start(
            library_path=lib_path,
            scope=args.scope,
            output_vtree=args.output_vtree,
            book_threshold=args.book_threshold,
            overwrite=args.overwrite,
            skip_organize=args.skip_organize,
        )
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
