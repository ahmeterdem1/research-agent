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


def _print_cold_start_preflight(
    library_path: Path,
    target_vtree: Path,
    scope: str,
    book_threshold: int,
    overwrite: bool,
) -> None:
    """Scan `library_path` and print a verbose pre-flight workload and page-count cost plan."""
    from src.cold_start.refresher import _norm_path, collect_indexed_real_paths
    from src.cold_start.scanner import scan_directory

    scanned = scan_directory(library_path, book_threshold=book_threshold)
    existing_paths: set[str] = set()
    if target_vtree.exists() and not overwrite:
        try:
            import json

            tree = json.loads(target_vtree.read_text(encoding="utf-8"))
            existing_paths = collect_indexed_real_paths(tree.get("root", {}))
        except Exception:
            pass

    to_parse = []
    skipped = 0
    for f in scanned:
        if not overwrite and (f.real_path in existing_paths or _norm_path(f.real_path) in existing_paths):
            skipped += 1
        else:
            to_parse.append(f)

    total_pages = sum((f.page_count or 1) for f in to_parse)
    books_count = sum(1 for f in to_parse if f.is_book)
    docs_count = len(to_parse) - books_count

    extractor_mode = getattr(settings, "EXTRACTOR_MODE", "docling").lower()
    vision_model = settings.normalize_litellm_model(
        getattr(settings, "VISION_MODEL", settings.DEFAULT_SUMMARIZATION_MODEL),
        default="openrouter/google/gemini-2.5-flash",
    )
    sum_model = settings.normalize_litellm_model(
        getattr(settings, "DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat"),
        default="openrouter/deepseek/deepseek-chat",
    )
    parser_desc = f"LLM Vision OCR ({vision_model})" if extractor_mode == "llm" else "Local Docling (free)"

    lines = [
        f"=== Cold-Start Index Pre-Flight Plan [{scope.upper()}] ===",
        f"Source Library             : {library_path}",
        f"Target VTree               : {target_vtree}",
        f"PDF Parser Mode            : {parser_desc}",
        f"Summarization Model        : {sum_model}",
        f"Already Indexed (Skipped)  : {skipped} file(s)",
        f"Files to Parse             : {len(to_parse)} total ({docs_count} document(s), {books_count} book(s) >{book_threshold}p)",
        f"Total PDF Pages to Parse   : {total_pages} page(s)",
    ]
    for f in to_parse:
        kind = "BOOK" if f.is_book else "DOC"
        pg = f"{f.page_count} pages" if f.page_count is not None else f.file_type
        lines.append(f"  + [{kind}] {f.name} ({pg})")
    lines.append("-" * 54)
    report = "\n".join(lines)
    print(report)
    for l in lines:
        logger.info(l)


def run_cold_start(
    library_path: str,
    scope: str = "global",
    output_vtree: str | Path | None = None,
    book_threshold: int = settings.BOOK_PAGE_THRESHOLD,
    overwrite: bool = False,
    skip_organize: bool = False,
    dry_run: bool = False,
) -> Path:
    if not library_path or not Path(library_path).exists():
        raise ValueError(f"Invalid or missing library path: {library_path}")

    resolved_lib = Path(library_path).expanduser().resolve()
    if output_vtree:
        target_vtree = Path(output_vtree).expanduser().resolve()
    elif scope == "project":
        target_vtree = settings.get_project_vtree_path(must_exist=False)
    else:
        target_vtree = settings.get_global_vtree_path()

    _print_cold_start_preflight(
        library_path=resolved_lib,
        target_vtree=target_vtree,
        scope=scope,
        book_threshold=book_threshold,
        overwrite=overwrite,
    )
    if dry_run:
        print("\n[Dry Run] No documents were parsed and no LLM calls were made.")
        return target_vtree

    logger.info("Starting cold-start pipeline for '%s' -> %s (scope=%s)", resolved_lib, target_vtree, scope)

    # 1. Scan
    scan_args = ["--library-path", str(resolved_lib), "--book-threshold", str(book_threshold)]
    if overwrite:
        scan_args.append("--overwrite")
    run_stage("cs_01_scan.py", scan_args)

    # 2. Extract
    extract_args = ["--book-threshold", str(book_threshold), "--vtree-path", str(target_vtree)]
    if overwrite:
        extract_args.append("--overwrite")
    run_stage("cs_02_extract.py", extract_args)

    # 3. Summarize
    summarize_args = ["--overwrite"] if overwrite else []
    run_stage("cs_03_summarize.py", summarize_args)

    # 4. Build VTree
    vtree_args = ["--library-path", str(resolved_lib), "--output-vtree", str(target_vtree)]
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
    parser.add_argument("--dry-run", action="store_true", help="Only print the pre-flight workload and page count plan")

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
            dry_run=args.dry_run,
        )
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
