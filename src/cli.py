"""
Unified Management CLI (``ra-cli``) for the Research Agent OpenCode extension.

Subcommands:
- ``ra-cli install-opencode``: Register MCP servers, permissions, agents, and
  global instructions in ``~/.config/opencode/`` and migrate repo data into ``~/.research-agent/``.
- ``ra-cli project``: Enable or disable individual Research Agent MCP servers
  (``ra_docs``, ``ra_search``, ``ra_ingest``, ``ra_kg``) in a project's ``opencode.json``.
- ``ra-cli index``: Run the cold-start document ingestion pipeline targeting either
  the global VTree (``~/.research-agent/vtrees/global.json``) or a project-local VTree (``.opencode/vtree.json``).
- ``ra-cli organize``: Run the LLM-based VTree semantic reorganization pass.
- ``ra-cli status``: Inspect global data home, VTree document counts, dual-scope KG triplet
  counts, and active project OpenCode toggles.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Optional

sys.path.append(str(Path(__file__).parent.parent))

from config import settings
from config.settings import get_logger
from src.kg.store import KGStore

logger = get_logger(__name__)

MCP_SERVERS = {
    "ra_docs": "ra-mcp-docs",
    "ra_search": "ra-mcp-search",
    "ra_ingest": "ra-mcp-ingest",
    "ra_kg": "ra-mcp-kg",
}

DEFAULT_PERMISSIONS = {
    "ra_docs_*": "allow",
    "ra_search_*": "allow",
    "ra_kg_*": "allow",
    "ra_ingest_stage_paper": "allow",
    "ra_ingest_refresh_library": "allow",
    "ra_ingest_confirm_save_paper": "ask",
}

AGENTS_START_MARKER = "<!-- RESEARCH_AGENT_START -->"
AGENTS_END_MARKER = "<!-- RESEARCH_AGENT_END -->"


def _default_opencode_dir() -> Path:
    env_dir = os.getenv("OPENCODE_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return Path.home() / ".config" / "opencode"


def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in %s (%s); starting with empty object", path, e)
        return {}


def _count_vtree_docs(vtree_path: Optional[Path]) -> int:
    if not vtree_path or not vtree_path.exists():
        return 0
    try:
        data = json.loads(vtree_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    def _walk(node: dict) -> int:
        if not isinstance(node, dict):
            return 0
        c = 1 if node.get("type") in ("document", "book") else 0
        for child in node.get("children", []):
            c += _walk(child)
        return c

    return _walk(data.get("root", {}))


def migrate_repo_data_to_global(data_dir: Optional[Path] = None) -> list[str]:
    """
    Copy existing repo-local `data/vtree.json`, `data/research_agent.db`, and
    `data/cache/texts` into the global data directory if not already present.
    """
    target_home = data_dir or settings.DATA_DIR
    repo_data = settings.PROJECT_ROOT / "data"
    migrated: list[str] = []

    if not repo_data.exists() or repo_data.resolve() == target_home.resolve():
        return migrated

    # 1. vtree.json -> vtrees/global.json
    src_vtree = repo_data / "vtree.json"
    dst_vtree = target_home / "vtrees" / "global.json"
    if src_vtree.exists() and not dst_vtree.exists():
        dst_vtree.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_vtree, dst_vtree)
        migrated.append(f"vtree.json -> {dst_vtree}")

    # 2. research_agent.db
    src_db = repo_data / "research_agent.db"
    dst_db = target_home / "research_agent.db"
    if src_db.exists() and not dst_db.exists():
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_db, dst_db)
        migrated.append(f"research_agent.db -> {dst_db}")

    # 3. cache/texts
    src_texts = repo_data / "cache" / "texts"
    dst_texts = target_home / "cache" / "texts"
    if src_texts.exists():
        dst_texts.mkdir(parents=True, exist_ok=True)
        copied_texts = 0
        for md_file in src_texts.glob("*.md"):
            target_md = dst_texts / md_file.name
            if not target_md.exists():
                shutil.copy2(md_file, target_md)
                copied_texts += 1
        if copied_texts:
            migrated.append(f"{copied_texts} cached text file(s) -> {dst_texts}")

    # 4. .env -> ~/.research-agent/.env
    src_env = settings.PROJECT_ROOT / ".env"
    dst_env = target_home / ".env"
    if src_env.exists() and not dst_env.exists():
        shutil.copy2(src_env, dst_env)
        migrated.append(f".env -> {dst_env}")

    for item in migrated:
        logger.info("Migrated %s", item)
    return migrated


def install_opencode(
    opencode_config_dir: Optional[Path] = None,
    enable_by_default: bool = False,
    migrate_repo_data: bool = True,
) -> dict:
    """
    Install or update Research Agent MCP servers, permissions, agent definitions,
    and global instructions inside `~/.config/opencode/`.
    """
    cfg_dir = (opencode_config_dir or _default_opencode_dir()).expanduser().resolve()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # 1. Merge opencode.json
    cfg_path = cfg_dir / "opencode.json"
    cfg = _load_json_file(cfg_path)
    cfg.setdefault("$schema", "https://opencode.ai/config.json")

    mcp_block = cfg.setdefault("mcp", {})
    for server_key, binary_name in MCP_SERVERS.items():
        existing = mcp_block.get(server_key, {})
        enabled_val = True if enable_by_default else existing.get("enabled", False)
        mcp_block[server_key] = {
            **existing,
            "type": "local",
            "command": [binary_name],
            "enabled": enabled_val,
        }

    perm_block = cfg.setdefault("permission", {})
    for pattern, rule in DEFAULT_PERMISSIONS.items():
        perm_block.setdefault(pattern, rule)

    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Updated global OpenCode configuration at %s", cfg_path)

    # 2. Copy agent definitions (researcher.md, citation-walker.md)
    templates_dir = settings.PROJECT_ROOT / "config" / "opencode_templates"
    agents_dst = cfg_dir / "agents"
    agents_dst.mkdir(parents=True, exist_ok=True)
    installed_agents: list[str] = []
    for agent_md in (templates_dir / "agents").glob("*.md"):
        target = agents_dst / agent_md.name
        shutil.copy2(agent_md, target)
        installed_agents.append(target.name)
        logger.info("Installed OpenCode agent profile: %s", target)

    # 3. Inject or update AGENTS.md snippet
    snippet_path = templates_dir / "AGENTS_SNIPPET.md"
    agents_md_path = cfg_dir / "AGENTS.md"
    if snippet_path.exists():
        snippet = snippet_path.read_text(encoding="utf-8").strip()
        existing_md = agents_md_path.read_text(encoding="utf-8") if agents_md_path.exists() else ""
        if AGENTS_START_MARKER in existing_md and AGENTS_END_MARKER in existing_md:
            before = existing_md.split(AGENTS_START_MARKER)[0].rstrip()
            after = existing_md.split(AGENTS_END_MARKER)[-1].lstrip()
            parts = [p for p in (before, snippet, after) if p]
            updated_md = "\n\n".join(parts) + "\n"
        elif existing_md.strip():
            updated_md = existing_md.rstrip() + "\n\n" + snippet + "\n"
        else:
            updated_md = snippet + "\n"
        agents_md_path.write_text(updated_md, encoding="utf-8")
        logger.info("Updated global instructions at %s", agents_md_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []

    return {
        "config_path": str(cfg_path),
        "agents_installed": sorted(installed_agents),
        "agents_md_path": str(agents_md_path),
        "migrated": migrated,
    }


def configure_project(
    project_dir: Optional[Path] = None,
    enable_all: bool = False,
    disable_all: bool = False,
    docs: Optional[bool] = None,
    search: Optional[bool] = None,
    ingest: Optional[bool] = None,
    kg: Optional[bool] = None,
    project_id: Optional[str] = None,
) -> Path:
    """
    Create or update `<project_dir>/opencode.json` to enable or disable individual
    Research Agent MCP modules for a specific OpenCode workspace.
    """
    target_dir = (project_dir or Path.cwd()).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = target_dir / "opencode.json"

    cfg = _load_json_file(cfg_path)
    cfg.setdefault("$schema", "https://opencode.ai/config.json")
    mcp_block = cfg.setdefault("mcp", {})

    if enable_all:
        toggles = {k: True for k in MCP_SERVERS}
    elif disable_all:
        toggles = {k: False for k in MCP_SERVERS}
    else:
        toggles = {}
        if docs is not None:
            toggles["ra_docs"] = docs
        if search is not None:
            toggles["ra_search"] = search
        if ingest is not None:
            toggles["ra_ingest"] = ingest
        if kg is not None:
            toggles["ra_kg"] = kg

    for server_key, enabled_val in toggles.items():
        entry = mcp_block.setdefault(server_key, {})
        entry.setdefault("type", "local")
        entry.setdefault("command", [MCP_SERVERS[server_key]])
        entry["enabled"] = bool(enabled_val)

    if project_id:
        for server_key in ("ra_kg", "ra_docs", "ra_ingest"):
            entry = mcp_block.setdefault(server_key, {})
            env_map = entry.setdefault("environment", {})
            env_map["RA_PROJECT_ID"] = project_id

    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Updated project OpenCode configuration at %s", cfg_path)
    return cfg_path


def get_status(project_dir: Optional[Path] = None) -> dict:
    """Gather system-wide and current-project status metrics."""
    target_dir = (project_dir or Path.cwd()).expanduser().resolve()
    proj_id = settings.get_active_project_id(cwd=target_dir)

    global_vtree = settings.get_global_vtree_path()
    project_vtree = settings.get_project_vtree_path(cwd=target_dir, must_exist=True)

    kg_global_count = 0
    kg_project_count = 0
    if settings.DB_PATH.exists():
        try:
            store = KGStore(settings.DB_PATH, default_project_id=proj_id)
            kg_global_count = len(store.query(scope="global", limit=10000))
            kg_project_count = len(store.query(scope="project", project_id=proj_id, limit=10000))
        except Exception as e:
            logger.warning("Could not query KG counts: %s", e)

    proj_cfg = _load_json_file(target_dir / "opencode.json").get("mcp", {})
    mcp_status = {}
    for key in MCP_SERVERS:
        if key in proj_cfg and "enabled" in proj_cfg[key]:
            mcp_status[key] = "enabled" if proj_cfg[key]["enabled"] else "disabled"
        else:
            mcp_status[key] = "inherited (global)"

    return {
        "data_dir": str(settings.DATA_DIR),
        "db_path": str(settings.DB_PATH),
        "active_project_id": proj_id,
        "global_vtree_path": str(global_vtree),
        "global_vtree_exists": global_vtree.exists(),
        "global_vtree_docs": _count_vtree_docs(global_vtree),
        "project_vtree_path": str(project_vtree) if project_vtree else None,
        "project_vtree_exists": project_vtree is not None and project_vtree.exists(),
        "project_vtree_docs": _count_vtree_docs(project_vtree),
        "kg_global_triplets": kg_global_count,
        "kg_project_triplets": kg_project_count,
        "project_mcp_status": mcp_status,
    }


def format_status(status: dict) -> str:
    lines = [
        "=== Research Agent Status ===",
        f"Global Data Home : {status['data_dir']}",
        f"Knowledge Graph  : {status['db_path']}",
        f"  - Global facts : {status['kg_global_triplets']}",
        f"  - Project facts: {status['kg_project_triplets']} (project: {status['active_project_id']})",
        f"Global VTree     : {status['global_vtree_path']} "
        f"({'present' if status['global_vtree_exists'] else 'missing'}, {status['global_vtree_docs']} docs/books)",
        f"Project VTree    : {status['project_vtree_path'] or '.opencode/vtree.json (not present)'} "
        f"({status['project_vtree_docs']} docs/books)",
        "Project MCP Toggles (opencode.json):",
    ]
    for server, state in status["project_mcp_status"].items():
        lines.append(f"  - {server:<10}: {state}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    from src.updater import DEFAULT_GITHUB_REPO, get_local_version

    parser = argparse.ArgumentParser(
        prog="ra-cli",
        description="System-wide management CLI for the Research Agent OpenCode extension.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"ra-cli {get_local_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. install-opencode
    p_install = sub.add_parser(
        "install-opencode",
        help="Register MCP servers, permissions, and agents in ~/.config/opencode/",
    )
    p_install.add_argument(
        "--opencode-dir",
        type=Path,
        default=None,
        help="Custom OpenCode config directory (defaults to ~/.config/opencode)",
    )
    p_install.add_argument(
        "--enable-by-default",
        "--enable-globally",
        dest="enable_by_default",
        action="store_true",
        help="Enable ra_* MCP servers globally by default (otherwise disabled until enabled per project)",
    )
    p_install.add_argument(
        "--no-migrate",
        action="store_true",
        help="Skip migrating repo-local data/ into ~/.research-agent/",
    )

    # 2. project
    p_proj = sub.add_parser(
        "project",
        help="Enable or disable Research Agent MCP servers in a project's opencode.json",
    )
    p_proj.add_argument("--project-dir", type=Path, default=None, help="Target project directory (defaults to cwd)")
    p_proj.add_argument("--enable-all", action="store_true", help="Enable all 4 Research Agent MCP servers")
    p_proj.add_argument("--disable-all", action="store_true", help="Disable all 4 Research Agent MCP servers")
    p_proj.add_argument("--docs", choices=["on", "off"], help="Toggle ra_docs (Virtual Tree tools)")
    p_proj.add_argument("--search", choices=["on", "off"], help="Toggle ra_search (Academic & web search tools)")
    p_proj.add_argument("--ingest", choices=["on", "off"], help="Toggle ra_ingest (User-gated paper saving)")
    p_proj.add_argument("--kg", choices=["on", "off"], help="Toggle ra_kg (Dual-scope Knowledge Graph)")
    p_proj.add_argument("--project-id", type=str, default=None, help="Explicit project_id override for KG scoping")

    # 3. index
    p_index = sub.add_parser(
        "index",
        help="Run the cold-start document ingestion pipeline for a global or project VTree",
    )
    p_index.add_argument("--library-path", type=str, default=None, help="Directory of PDFs/documents to index")
    p_index.add_argument(
        "--scope",
        choices=["global", "project"],
        default="global",
        help="Target VTree scope ('global' -> ~/.research-agent/vtrees/global.json, 'project' -> .opencode/vtree.json)",
    )
    p_index.add_argument("--output-vtree", type=str, default=None, help="Explicit output vtree.json path")
    p_index.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    p_index.add_argument("--overwrite", action="store_true", help="Rebuild from scratch")
    p_index.add_argument("--skip-organize", action="store_true", help="Skip LLM reorganization pass")

    # 4. refresh
    p_refresh = sub.add_parser(
        "refresh",
        help="Incrementally scan the source folder and index only newly dropped files into an existing VTree",
    )
    p_refresh.add_argument(
        "--library-path",
        type=str,
        default=None,
        help="Source library directory (defaults to library_root recorded in VTree or LIBRARY_ROOT_PATH)",
    )
    p_refresh.add_argument(
        "--scope",
        choices=["global", "project"],
        default="global",
        help="Target VTree scope ('global' or 'project')",
    )
    p_refresh.add_argument("--vtree-path", type=str, default=None, help="Explicit vtree.json path")
    p_refresh.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Optional virtual folder in VTree to place newly discovered files (e.g. '/Inbox')",
    )
    p_refresh.add_argument("--book-threshold", type=int, default=settings.BOOK_PAGE_THRESHOLD)
    p_refresh.add_argument(
        "--no-dir-summaries",
        action="store_true",
        help="Skip refreshing ancestor directory summaries with the LLM",
    )
    p_refresh.add_argument(
        "--organize",
        action="store_true",
        help="Run the LLM semantic reorganization pass after inserting new files",
    )

    # 5. organize
    p_org = sub.add_parser("organize", help="Run the LLM reorganization pass on an existing VTree")
    p_org.add_argument("--scope", choices=["global", "project"], default="global")
    p_org.add_argument("--vtree-path", type=str, default=None)
    p_org.add_argument("--max-turns", type=int, default=15)

    # 5. status
    p_status = sub.add_parser("status", help="Display global and current-project Research Agent status")
    p_status.add_argument("--project-dir", type=Path, default=None)

    # 7. stats
    p_stats = sub.add_parser(
        "stats",
        help="Display tool and subagent usage telemetry, error rates, latency, and behavioral diagnostics",
    )
    p_stats.add_argument("--project", type=str, default=None, help="Filter by specific project_id")
    p_stats.add_argument("--all-projects", action="store_true", help="Aggregate telemetry across all projects")
    p_stats.add_argument("--days", type=int, default=None, help="Restrict telemetry to the last N days")
    p_stats.add_argument("--recent", type=int, default=10, help="Number of recent tool invocations to list")
    p_stats.add_argument("--errors-only", action="store_true", help="Only list errors in the recent invocations block")
    p_stats.add_argument("--reset", action="store_true", help="Clear recorded telemetry for the target project (or all with --all-projects)")

    # 8. upgrade
    p_upgrade = sub.add_parser(
        "upgrade",
        help="Upgrade the system-wide Research Agent installation from GitHub (or Homebrew) without modifying local dev files",
    )
    p_upgrade.add_argument(
        "--repo",
        type=str,
        default=DEFAULT_GITHUB_REPO,
        help=f"GitHub owner/repo slug (defaults to {DEFAULT_GITHUB_REPO})",
    )
    p_upgrade.add_argument(
        "--ref",
        type=str,
        default="HEAD",
        help="Git branch, tag, or commit SHA to install (defaults to HEAD)",
    )
    p_upgrade.add_argument(
        "--method",
        choices=["uv", "homebrew"],
        default=None,
        help="Override detected package manager ('uv' or 'homebrew')",
    )
    p_upgrade.add_argument(
        "--check",
        action="store_true",
        help="Only check if a newer version/commit is available on GitHub without installing",
    )
    p_upgrade.add_argument(
        "--force",
        action="store_true",
        help="Reinstall from GitHub even if local and remote versions match",
    )

    args = parser.parse_args(argv)

    if args.command == "install-opencode":
        res = install_opencode(
            opencode_config_dir=args.opencode_dir,
            enable_by_default=args.enable_by_default,
            migrate_repo_data=not args.no_migrate,
        )
        print(f"Installed OpenCode config: {res['config_path']}")
        print(f"Installed agents: {', '.join(res['agents_installed'])}")
        print(f"Updated global instructions: {res['agents_md_path']}")
        if res["migrated"]:
            print("Migrated data to global home:")
            for m in res["migrated"]:
                print(f"  - {m}")
        return 0

    if args.command == "project":
        to_bool = lambda v: (v == "on") if v in ("on", "off") else None
        cfg_file = configure_project(
            project_dir=args.project_dir,
            enable_all=args.enable_all,
            disable_all=args.disable_all,
            docs=to_bool(args.docs),
            search=to_bool(args.search),
            ingest=to_bool(args.ingest),
            kg=to_bool(args.kg),
            project_id=args.project_id,
        )
        print(f"Updated project configuration: {cfg_file}")
        return 0

    if args.command == "index":
        from scripts.cold_start import run_cold_start

        lib_path = args.library_path or settings.LIBRARY_ROOT_PATH
        try:
            out = run_cold_start(
                library_path=lib_path,
                scope=args.scope,
                output_vtree=args.output_vtree,
                book_threshold=args.book_threshold,
                overwrite=args.overwrite,
                skip_organize=args.skip_organize,
            )
            print(f"Indexed library into VTree: {out}")
            return 0
        except ValueError as e:
            logger.error("%s", e)
            return 1

    if args.command == "refresh":
        from src.cold_start.refresher import refresh_vtree

        try:
            res = refresh_vtree(
                scope=args.scope,
                library_path=args.library_path,
                vtree_path=args.vtree_path,
                target_virtual_dir=args.target_dir,
                book_threshold=args.book_threshold,
                update_dir_summaries=not args.no_dir_summaries,
                reorganize=args.organize,
            )
            print(
                f"Refreshed VTree [{res.scope}] ({res.vtree_path}):\n"
                f"  Scanned: {res.scanned_count} | Already indexed: {res.already_indexed_count} | "
                f"Newly added: {len(res.added_nodes)} | Repaired: {len(res.repaired_nodes)}"
            )
            for node in res.added_nodes:
                print(f"  + [{node.get('type', 'document')}] {node.get('virtual_path')}")
            for node in res.repaired_nodes:
                print(f"  ~ [repaired] {node.get('virtual_path')}")
            if res.failed_files:
                print(f"  ! Failed/skipped ({len(res.failed_files)}): {', '.join(res.failed_files)}")
            return 0
        except ValueError as e:
            logger.error("%s", e)
            return 1

    if args.command == "organize":
        from src.cold_start.vtree_organizer import organize_vtree

        if args.vtree_path:
            vpath = Path(args.vtree_path).expanduser().resolve()
        elif args.scope == "project":
            vpath = settings.get_project_vtree_path(must_exist=False)
        else:
            vpath = settings.get_global_vtree_path()

        if not vpath or not vpath.exists():
            logger.error("No VTree found at %s", vpath)
            return 1
        vtree = json.loads(vpath.read_text(encoding="utf-8"))
        organized = organize_vtree(vtree, args.max_turns)
        if organized is not vtree:
            vpath.write_text(json.dumps(organized, indent=2), encoding="utf-8")
            print(f"Organized VTree saved to {vpath}")
        return 0

    if args.command == "status":
        st = get_status(project_dir=args.project_dir)
        print(format_status(st))
        return 0

    if args.command == "stats":
        from src.telemetry.store import TelemetryStore, format_telemetry_summary

        tstore = TelemetryStore(settings.DB_PATH)
        if args.reset:
            target_pid = None if args.all_projects else tstore.resolve_project_id(args.project)
            deleted = tstore.reset(project_id=target_pid)
            print(f"Cleared {deleted} telemetry record(s).")
            return 0

        summary = tstore.get_summary(
            project_id=args.project,
            all_projects=args.all_projects,
            days=args.days,
            recent_limit=args.recent,
            errors_only=args.errors_only,
        )
        print(format_telemetry_summary(summary))
        return 0

    if args.command == "upgrade":
        from src.updater import run_upgrade

        res = run_upgrade(
            repo=args.repo,
            ref=args.ref,
            method=args.method,
            check_only=args.check,
            force=args.force,
        )
        print(res.message)
        if not res.upgraded and res.message.startswith("Upgrade failed:"):
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

