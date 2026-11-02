import argparse
import json
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config import settings
from config.settings import get_logger
from src.cold_start.vtree_builder import build_initial_vtree

logger = get_logger(__name__)


def run(library_path: str, overwrite: bool = False, output_vtree: str | Path | None = None):
    target_path = Path(output_vtree) if output_vtree else settings.get_global_vtree_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if overwrite and target_path.exists():
        logger.info("Overwriting existing VTree at %s", target_path)
        target_path.unlink()
    elif target_path.exists():
        logger.info("VTree already exists at %s. Use --overwrite to replace.", target_path)
        return

    in_path = settings.CACHE_DIR / "cs_03_summarize.json"
    if not in_path.exists():
        logger.error("Missing input %s", in_path)
        sys.exit(1)

    logger.info("--- Stage 4: Building Initial VTree (%s) ---", target_path)
    data = json.loads(in_path.read_text(encoding="utf-8"))

    vtree = build_initial_vtree(data, Path(library_path))

    target_path.write_text(json.dumps(vtree, indent=2), encoding="utf-8")
    logger.info("Initial VTree built successfully. Wrote to %s", target_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-path", type=str, default=settings.LIBRARY_ROOT_PATH)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing vtree file")
    parser.add_argument("--output-vtree", type=str, default=None, help="Target vtree.json path")
    args = parser.parse_args()
    if not args.library_path:
        logger.error("Missing library path.")
        sys.exit(1)
    run(args.library_path, args.overwrite, args.output_vtree)
