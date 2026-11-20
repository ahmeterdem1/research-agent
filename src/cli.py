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
import tempfile
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

SUPPORTED_HARNESSES = (
    "opencode",
    "gemini-cli",
    "claude-code",
    "antigravity",
    "claude-desktop",
    "cursor",
)

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


def _resolve_mcp_binary(binary_name: str) -> str:
    """Resolve absolute executable path for an MCP server binary if found on PATH or beside sys.executable."""
    found = shutil.which(binary_name)
    if found:
        return str(Path(found).resolve())
    candidate = Path(sys.executable).parent / binary_name
    if candidate.exists():
        return str(candidate.resolve())
    return binary_name


def _inject_instructions_snippet(target_md_path: Path) -> Optional[Path]:
    """Idempotently inject or refresh the global Research Agent instructions snippet in `target_md_path`."""
    snippet_path = settings.PROJECT_ROOT / "config" / "opencode_templates" / "AGENTS_SNIPPET.md"
    if not snippet_path.exists():
        return None
    target_md_path.parent.mkdir(parents=True, exist_ok=True)
    snippet = snippet_path.read_text(encoding="utf-8").strip()
    existing_md = target_md_path.read_text(encoding="utf-8") if target_md_path.exists() else ""
    if AGENTS_START_MARKER in existing_md and AGENTS_END_MARKER in existing_md:
        before = existing_md.split(AGENTS_START_MARKER)[0].rstrip()
        after = existing_md.split(AGENTS_END_MARKER)[-1].lstrip()
        parts = [p for p in (before, snippet, after) if p]
        updated_md = "\n\n".join(parts) + "\n"
    elif existing_md.strip():
        updated_md = existing_md.rstrip() + "\n\n" + snippet + "\n"
    else:
        updated_md = snippet + "\n"
    target_md_path.write_text(updated_md, encoding="utf-8")
    return target_md_path


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

    # 4. .env -> ~/.research-agent/.env (only seed if ~/.research-agent/.env does not exist yet;
    # never overwrite the user's global ~/.research-agent/.env configuration)
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
    agents_md_path = cfg_dir / "AGENTS.md"
    if _inject_instructions_snippet(agents_md_path):
        logger.info("Updated global instructions at %s", agents_md_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []

    return {
        "harness": "opencode",
        "config_path": str(cfg_path),
        "agents_installed": sorted(installed_agents),
        "agents_md_path": str(agents_md_path),
        "migrated": migrated,
    }


def install_gemini_cli(
    gemini_dir: Optional[Path] = None,
    migrate_repo_data: bool = True,
) -> dict:
    """
    Register Research Agent MCP servers inside `~/.gemini/settings.json` with
    programmatic 2-phase paper-save gating (`confirm_save_paper` isolated on `trust: False`)
    and inject instructions into `~/.gemini/GEMINI.md`.
    """
    env_dir = os.getenv("GEMINI_CONFIG_DIR", "").strip()
    cfg_dir = (gemini_dir or (Path(env_dir) if env_dir else Path.home() / ".gemini")).expanduser().resolve()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = cfg_dir / "settings.json"
    cfg = _load_json_file(cfg_path)
    mcp_servers = cfg.setdefault("mcpServers", {})

    mcp_servers["ra-docs"] = {
        "command": _resolve_mcp_binary("ra-mcp-docs"),
        "args": [],
        "trust": True,
    }
    mcp_servers["ra-search"] = {
        "command": _resolve_mcp_binary("ra-mcp-search"),
        "args": [],
        "trust": True,
    }
    mcp_servers["ra-kg"] = {
        "command": _resolve_mcp_binary("ra-mcp-kg"),
        "args": [],
        "trust": True,
    }
    mcp_servers["ra-ingest"] = {
        "command": _resolve_mcp_binary("ra-mcp-ingest"),
        "args": [],
        "trust": True,
        "excludeTools": ["confirm_save_paper"],
    }
    mcp_servers["ra-ingest-confirm"] = {
        "command": _resolve_mcp_binary("ra-mcp-ingest"),
        "args": [],
        "trust": False,
        "includeTools": ["confirm_save_paper"],
    }

    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Updated Gemini CLI configuration at %s", cfg_path)

    gemini_md_path = cfg_dir / "GEMINI.md"
    if _inject_instructions_snippet(gemini_md_path):
        logger.info("Updated Gemini CLI instructions at %s", gemini_md_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []
    return {
        "harness": "gemini-cli",
        "config_path": str(cfg_path),
        "instructions_path": str(gemini_md_path),
        "migrated": migrated,
    }


def install_claude_code(
    claude_dir: Optional[Path] = None,
    claude_json_path: Optional[Path] = None,
    migrate_repo_data: bool = True,
) -> dict:
    """
    Register Research Agent MCP servers inside `~/.claude.json` with programmatic
    2-phase paper-save gating (`permissions.ask` on `mcp__ra_ingest__confirm_save_paper`)
    in `~/.claude/settings.json` and instructions in `~/.claude/CLAUDE.md`.
    """
    env_dir = os.getenv("CLAUDE_CONFIG_DIR", "").strip()
    cfg_dir = (claude_dir or (Path(env_dir) if env_dir else Path.home() / ".claude")).expanduser().resolve()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    mcp_json_path = (
        claude_json_path
        or (cfg_dir.parent / ".claude.json" if (claude_dir is not None or env_dir) else Path.home() / ".claude.json")
    ).expanduser().resolve()
    mcp_json_path.parent.mkdir(parents=True, exist_ok=True)

    mcp_cfg = _load_json_file(mcp_json_path)
    mcp_servers = mcp_cfg.setdefault("mcpServers", {})
    for server_key, binary_name in MCP_SERVERS.items():
        existing = mcp_servers.get(server_key, {})
        mcp_servers[server_key] = {
            **existing,
            "type": "stdio",
            "command": _resolve_mcp_binary(binary_name),
            "args": [],
        }
    mcp_json_path.write_text(json.dumps(mcp_cfg, indent=2), encoding="utf-8")
    logger.info("Updated Claude Code MCP config at %s", mcp_json_path)

    settings_path = cfg_dir / "settings.json"
    settings_cfg = _load_json_file(settings_path)
    perms = settings_cfg.setdefault("permissions", {})
    allow_list = perms.setdefault("allow", [])
    ask_list = perms.setdefault("ask", [])

    for rule in (
        "mcp__ra_docs__*",
        "mcp__ra_search__*",
        "mcp__ra_kg__*",
        "mcp__ra_ingest__stage_paper",
        "mcp__ra_ingest__refresh_library",
    ):
        if rule not in allow_list:
            allow_list.append(rule)

    if "mcp__ra_ingest__confirm_save_paper" not in ask_list:
        ask_list.append("mcp__ra_ingest__confirm_save_paper")

    # Ensure confirm_save_paper is not mistakenly pre-allowed
    if "mcp__ra_ingest__confirm_save_paper" in allow_list:
        allow_list.remove("mcp__ra_ingest__confirm_save_paper")

    settings_path.write_text(json.dumps(settings_cfg, indent=2), encoding="utf-8")
    logger.info("Updated Claude Code permissions at %s", settings_path)

    claude_md_path = cfg_dir / "CLAUDE.md"
    if _inject_instructions_snippet(claude_md_path):
        logger.info("Updated Claude Code instructions at %s", claude_md_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []
    return {
        "harness": "claude-code",
        "config_path": str(mcp_json_path),
        "settings_path": str(settings_path),
        "instructions_path": str(claude_md_path),
        "migrated": migrated,
    }


def install_antigravity(
    antigravity_dir: Optional[Path] = None,
    migrate_repo_data: bool = True,
) -> dict:
    """
    Register Research Agent MCP servers inside `~/.gemini/config/mcp_config.json`
    and inject global instructions into `~/.gemini/config/GEMINI.md` for Antigravity.
    """
    env_dir = os.getenv("ANTIGRAVITY_CONFIG_DIR", "").strip()
    cfg_dir = (
        antigravity_dir or (Path(env_dir) if env_dir else Path.home() / ".gemini" / "config")
    ).expanduser().resolve()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = cfg_dir / "mcp_config.json"
    cfg = _load_json_file(cfg_path)
    mcp_servers = cfg.setdefault("mcpServers", {})
    for server_key, binary_name in MCP_SERVERS.items():
        existing = mcp_servers.get(server_key, {})
        mcp_servers[server_key] = {
            **existing,
            "command": _resolve_mcp_binary(binary_name),
            "args": [],
        }
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Updated Antigravity MCP config at %s", cfg_path)

    gemini_md_path = cfg_dir / "GEMINI.md"
    if _inject_instructions_snippet(gemini_md_path):
        logger.info("Updated Antigravity instructions at %s", gemini_md_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []
    return {
        "harness": "antigravity",
        "config_path": str(cfg_path),
        "instructions_path": str(gemini_md_path),
        "migrated": migrated,
    }


def install_claude_desktop(
    claude_desktop_dir: Optional[Path] = None,
    migrate_repo_data: bool = True,
) -> dict:
    """
    Register Research Agent MCP servers inside Claude Desktop's
    `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/` or
    Linux `~/.config/Claude/`).
    """
    if claude_desktop_dir is not None:
        cfg_dir = Path(claude_desktop_dir).expanduser().resolve()
    elif (Path.home() / "Library" / "Application Support").exists():
        cfg_dir = (Path.home() / "Library" / "Application Support" / "Claude").resolve()
    else:
        cfg_dir = (Path.home() / ".config" / "Claude").resolve()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = cfg_dir / "claude_desktop_config.json"
    cfg = _load_json_file(cfg_path)
    mcp_servers = cfg.setdefault("mcpServers", {})
    for server_key, binary_name in MCP_SERVERS.items():
        existing = mcp_servers.get(server_key, {})
        mcp_servers[server_key] = {
            **existing,
            "command": _resolve_mcp_binary(binary_name),
            "args": [],
        }
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Updated Claude Desktop MCP config at %s", cfg_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []
    return {
        "harness": "claude-desktop",
        "config_path": str(cfg_path),
        "migrated": migrated,
    }


def install_cursor(
    cursor_dir: Optional[Path] = None,
    migrate_repo_data: bool = True,
) -> dict:
    """
    Register Research Agent MCP servers inside `~/.cursor/mcp.json`
    and inject global Research Agent instructions into `~/.cursor/rules/research-agent.mdc`.
    """
    env_dir = os.getenv("CURSOR_CONFIG_DIR", "").strip()
    cfg_dir = (cursor_dir or (Path(env_dir) if env_dir else Path.home() / ".cursor")).expanduser().resolve()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = cfg_dir / "mcp.json"
    cfg = _load_json_file(cfg_path)
    mcp_servers = cfg.setdefault("mcpServers", {})
    for server_key, binary_name in MCP_SERVERS.items():
        existing = mcp_servers.get(server_key, {})
        mcp_servers[server_key] = {
            **existing,
            "command": _resolve_mcp_binary(binary_name),
            "args": [],
        }
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Updated Cursor MCP config at %s", cfg_path)

    rules_path = cfg_dir / "rules" / "research-agent.mdc"
    if _inject_instructions_snippet(rules_path):
        logger.info("Updated Cursor rules at %s", rules_path)

    migrated = migrate_repo_data_to_global() if migrate_repo_data else []
    return {
        "harness": "cursor",
        "config_path": str(cfg_path),
        "instructions_path": str(rules_path),
        "migrated": migrated,
    }


def _harness_detection_specs() -> list[tuple[str, Path, str, str]]:
    """Return `(harness, config_path, section_key, server_key)` tuples for auto-detection."""
    home = Path.home()
    return [
        ("opencode", _default_opencode_dir() / "opencode.json", "mcp", "ra_docs"),
        ("gemini-cli", home / ".gemini" / "settings.json", "mcpServers", "ra-docs"),
        ("claude-code", home / ".claude.json", "mcpServers", "ra_docs"),
        ("antigravity", home / ".gemini" / "config" / "mcp_config.json", "mcpServers", "ra_docs"),
        ("claude-desktop", home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json", "mcpServers", "ra_docs"),
        ("claude-desktop", home / ".config" / "Claude" / "claude_desktop_config.json", "mcpServers", "ra_docs"),
        ("cursor", home / ".cursor" / "mcp.json", "mcpServers", "ra_docs"),
    ]


def record_installed_harnesses(harness_names: list[str]) -> list[str]:
    """Persist newly installed harness names into `~/.research-agent/install_receipt.json`."""
    from src.updater import load_install_receipt, save_install_receipt

    receipt = load_install_receipt() or {}
    existing = receipt.get("harnesses")
    recorded = set(existing) if isinstance(existing, list) else set()
    recorded.update(h for h in harness_names if h in SUPPORTED_HARNESSES)
    ordered = [h for h in SUPPORTED_HARNESSES if h in recorded]
    receipt["harnesses"] = ordered
    save_install_receipt(receipt)
    return ordered


def get_installed_harnesses() -> list[str]:
    """Return installed harnesses from `install_receipt.json` and on-disk MCP configs."""
    from src.updater import load_install_receipt

    found: set[str] = set()
    receipt = load_install_receipt() or {}
    recorded = receipt.get("harnesses")
    if isinstance(recorded, list):
        found.update(h for h in recorded if h in SUPPORTED_HARNESSES)

    for harness, cfg_path, section_key, server_key in _harness_detection_specs():
        try:
            if not cfg_path.exists():
                continue
            data = _load_json_file(cfg_path)
        except OSError:
            continue
        section = data.get(section_key)
        if isinstance(section, dict) and server_key in section:
            found.add(harness)

    return [h for h in SUPPORTED_HARNESSES if h in found]


def install_harnesses(
    harnesses: list[str],
    opencode_config_dir: Optional[Path] = None,
    gemini_dir: Optional[Path] = None,
    claude_dir: Optional[Path] = None,
    antigravity_dir: Optional[Path] = None,
    claude_desktop_dir: Optional[Path] = None,
    cursor_dir: Optional[Path] = None,
    enable_by_default: bool = False,
    migrate_repo_data: bool = True,
) -> dict:
    """Install Research Agent across one or more supported AI harnesses."""
    requested: list[str] = []
    for raw in harnesses:
        for item in raw.split(","):
            tok = item.strip().lower()
            if not tok:
                continue
            if tok == "all":
                for h in SUPPORTED_HARNESSES:
                    if h not in requested:
                        requested.append(h)
            elif tok not in requested:
                requested.append(tok)

    if not requested:
        requested = ["opencode"]

    installed: list[dict] = []
    skipped: list[tuple[str, str]] = []
    did_migrate = False

    for target in requested:
        should_migrate = migrate_repo_data and not did_migrate
        if target == "opencode":
            res = install_opencode(
                opencode_config_dir=opencode_config_dir,
                enable_by_default=enable_by_default,
                migrate_repo_data=should_migrate,
            )
            installed.append(res)
            did_migrate = True
        elif target == "gemini-cli":
            res = install_gemini_cli(
                gemini_dir=gemini_dir,
                migrate_repo_data=should_migrate,
            )
            installed.append(res)
            did_migrate = True
        elif target == "claude-code":
            res = install_claude_code(
                claude_dir=claude_dir,
                migrate_repo_data=should_migrate,
            )
            installed.append(res)
            did_migrate = True
        elif target == "antigravity":
            res = install_antigravity(
                antigravity_dir=antigravity_dir,
                migrate_repo_data=should_migrate,
            )
            installed.append(res)
            did_migrate = True
        elif target == "claude-desktop":
            res = install_claude_desktop(
                claude_desktop_dir=claude_desktop_dir,
                migrate_repo_data=should_migrate,
            )
            installed.append(res)
            did_migrate = True
        elif target == "cursor":
            res = install_cursor(
                cursor_dir=cursor_dir,
                migrate_repo_data=should_migrate,
            )
            installed.append(res)
            did_migrate = True
        else:
            reason = (
                f"Skipped unknown harness '{target}'. "
                f"Supported harnesses: {', '.join(SUPPORTED_HARNESSES)}."
            )
            logger.warning("%s", reason)
            skipped.append((target, reason))

    if installed:
        record_installed_harnesses([r["harness"] for r in installed if r.get("harness")])

    return {"installed": installed, "skipped": skipped}


def create_unified_http_mcp_server():
    """Mount all 4 Research Agent MCP servers into one unified Streamable HTTP hub."""
    from fastmcp import FastMCP
    from src.mcp_servers.document_tools import mcp as docs_mcp
    from src.mcp_servers.search_tools import mcp as search_mcp
    from src.mcp_servers.file_tools import mcp as ingest_mcp
    from src.mcp_servers.kg_tools import mcp as kg_mcp

    hub = FastMCP("ResearchAgent")
    hub.mount(docs_mcp, namespace="ra_docs")
    hub.mount(search_mcp, namespace="ra_search")
    hub.mount(ingest_mcp, namespace="ra_ingest")
    hub.mount(kg_mcp, namespace="ra_kg")
    return hub


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

    from src.openreview_client import get_openreview_status

    or_status = get_openreview_status()

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
        "openreview": or_status,
    }


def format_status(status: dict) -> str:
    or_info = status.get("openreview") or {}
    if or_info.get("configured"):
        or_line = f"Authenticated as {or_info.get('user_id')} ({or_info.get('auth_file')})"
    else:
        or_line = "Not configured (optional — run 'ra-cli login-openreview')"

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
        f"OpenReview Auth  : {or_line}",
        "Project MCP Toggles (opencode.json):",
    ]
    for server, state in status["project_mcp_status"].items():
        lines.append(f"  - {server:<10}: {state}")
    return "\n".join(lines)


MCP_SERVER_METADATA: dict[str, dict] = {
    "ra_docs": {
        "description": "Search and read local papers and books in the Dual-Scope Virtual Document Tree.",
        "tools": [
            "list_tree",
            "search_library",
            "list_doc_summaries",
            "get_dir_summary",
            "get_document",
            "get_documents",
            "get_document_real_path",
        ],
        "safety_annotations": "All tools mark readOnlyHint=True, destructiveHint=False.",
    },
    "ra_search": {
        "description": "Search Semantic Scholar, OpenReview, DuckDuckGo, fetch online docs, and walk citation graphs.",
        "tools": [
            "duckduckgo_search",
            "scholar_search",
            "get_paper_details",
            "get_paper_references",
            "get_paper_citations",
            "read_online_document",
            "walk_citation_graph",
            "openreview_search",
            "get_openreview_paper",
        ],
        "safety_annotations": "All tools mark readOnlyHint=True, destructiveHint=False.",
    },
    "ra_ingest": {
        "description": "Stage, save (two-phase confirmation), and incrementally refresh papers in the local library.",
        "tools": [
            "stage_paper",
            "confirm_save_paper",
            "refresh_library",
        ],
        "safety_annotations": (
            "confirm_save_paper marks readOnlyHint=False, destructiveHint=True; "
            "stage_paper and refresh_library mark destructiveHint=False."
        ),
    },
    "ra_kg": {
        "description": "Manage the Dual-Scope Temporal Knowledge Graph and user profile memory.",
        "tools": [
            "remember_user_fact",
            "add_triplet",
            "query_kg",
            "update_triplet",
            "retract_triplet",
            "get_user_profile_summary",
            "search_entities",
            "get_related_facts",
            "get_ontology",
            "extend_ontology",
        ],
        "safety_annotations": "All tools mark destructiveHint=False.",
    },
}


def get_mcp_info() -> dict:
    """Gather resolved MCP entry points, HTTP endpoint details, paths, sandbox requirements, and harness status."""
    from src.updater import detect_install_method, get_local_version

    ra_cli_path = _resolve_mcp_binary("ra-cli")
    servers_info = []
    for name, binary in MCP_SERVERS.items():
        meta = MCP_SERVER_METADATA.get(name, {})
        servers_info.append(
            {
                "name": name,
                "binary": binary,
                "command_path": _resolve_mcp_binary(binary),
                "description": meta.get("description", ""),
                "tools": list(meta.get("tools", [])),
                "safety_annotations": meta.get("safety_annotations", ""),
            }
        )

    temp_dir = tempfile.gettempdir()
    fs_paths = [str(settings.DATA_DIR), temp_dir]
    if settings.LIBRARY_ROOT_PATH:
        lib_str = str(settings.LIBRARY_ROOT_PATH)
        if lib_str not in fs_paths:
            fs_paths.append(lib_str)

    installed_set = set(get_installed_harnesses())
    harness_paths: dict[str, list[str]] = {h: [] for h in SUPPORTED_HARNESSES}
    for h_name, cfg_path, _, _ in _harness_detection_specs():
        path_str = str(cfg_path)
        if path_str not in harness_paths[h_name]:
            harness_paths[h_name].append(path_str)

    harnesses_info = {
        h_name: {
            "installed": h_name in installed_set,
            "config_paths": harness_paths[h_name],
        }
        for h_name in SUPPORTED_HARNESSES
    }

    return {
        "installation": {
            "version": get_local_version(),
            "install_method": detect_install_method(),
            "python_executable": sys.executable,
            "ra_cli_path": ra_cli_path,
            "data_dir": str(settings.DATA_DIR),
            "env_file": str(settings.DATA_DIR / ".env"),
            "db_path": str(settings.DB_PATH),
            "global_vtree_path": str(settings.get_global_vtree_path()),
            "library_root_path": str(settings.LIBRARY_ROOT_PATH) if settings.LIBRARY_ROOT_PATH else None,
            "temp_dir": temp_dir,
        },
        "mcp_servers": servers_info,
        "http_server": {
            "command": f"{ra_cli_path} serve-http --host 127.0.0.1 --port 8765",
            "default_url": "http://127.0.0.1:8765/mcp",
            "transport": "streamable-http",
            "safety_annotations": (
                "Exposes all 29 tools with MCP ToolAnnotations "
                "(destructiveHint=True on ra_ingest_confirm_save_paper; destructiveHint=False on all others)."
            ),
        },
        "sandbox_requirements": {
            "filesystem_read_write": fs_paths,
            "network_access": (
                "Required by ra_search (Semantic Scholar, arXiv, OpenReview, DuckDuckGo) "
                "and ra_ingest (PDF downloads + LLM summarization API)."
            ),
        },
        "harnesses": harnesses_info,
    }


def format_mcp_info(info: dict) -> str:
    """Format `get_mcp_info()` into a human-readable summary for configuring MCP clients."""
    inst = info["installation"]
    http = info["http_server"]
    sandbox = info["sandbox_requirements"]

    lines = [
        "=== Research Agent MCP & Installation Info ===",
        f"Version          : {inst['version']} ({inst['install_method']})",
        f"ra-cli Binary    : {inst['ra_cli_path']}",
        f"Python Runtime   : {inst['python_executable']}",
        f"Global Data Home : {inst['data_dir']}",
        f"Env Config File  : {inst['env_file']}",
        f"Knowledge Graph  : {inst['db_path']}",
        f"Global VTree     : {inst['global_vtree_path']}",
        f"Library Root     : {inst['library_root_path'] or '(not set — configure PATH_TO_TREE in .env)'}",
        f"Temp Directory   : {inst['temp_dir']}",
        "",
        "Local Stdio MCP Servers:",
    ]
    for srv in info["mcp_servers"]:
        lines.extend(
            [
                f"  - {srv['name']} ({srv['binary']})",
                f"      Command     : {srv['command_path']}",
                f"      Description : {srv['description']}",
                f"      Tools       : {', '.join(srv['tools'])}",
                f"      Annotations : {srv['safety_annotations']}",
            ]
        )

    lines.extend(
        [
            "",
            "Unified Streamable HTTP MCP Endpoint (single-URL alternative):",
            f"  - Start Cmd   : {http['command']}",
            f"  - Default URL : {http['default_url']} ({http['transport']})",
            f"  - Annotations : {http['safety_annotations']}",
            "",
            "Sandbox & Permission Requirements (for sandboxed GUI clients):",
            "  - Filesystem (R/W):",
        ]
    )
    for p in sandbox["filesystem_read_write"]:
        lines.append(f"      * {p}")
    lines.append(f"  - Network     : {sandbox['network_access']}")

    lines.extend(["", "Harness Config Locations:"])
    for h_name, h_info in info["harnesses"].items():
        state = "installed" if h_info["installed"] else "not configured"
        lines.append(f"  - {h_name:<15} [{state}]: {', '.join(h_info['config_paths'])}")

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

    # 1. install-opencode / install
    p_install = sub.add_parser(
        "install-opencode",
        aliases=["install"],
        help="Register MCP servers, programmatic 2-phase confirmation gates, and instructions in supported AI harnesses",
    )
    p_install.add_argument(
        "--harness",
        "--target",
        dest="harnesses",
        action="append",
        default=None,
        help="Target harness(es): opencode, gemini-cli, claude-code, antigravity, claude-desktop, cursor, or all (defaults to opencode)",
    )
    p_install.add_argument(
        "--opencode-dir",
        type=Path,
        default=None,
        help="Custom OpenCode config directory (defaults to ~/.config/opencode)",
    )
    p_install.add_argument(
        "--gemini-dir",
        type=Path,
        default=None,
        help="Custom Gemini CLI config directory (defaults to ~/.gemini)",
    )
    p_install.add_argument(
        "--claude-dir",
        type=Path,
        default=None,
        help="Custom Claude Code config directory (defaults to ~/.claude)",
    )
    p_install.add_argument(
        "--antigravity-dir",
        type=Path,
        default=None,
        help="Custom Antigravity config directory (defaults to ~/.gemini/config)",
    )
    p_install.add_argument(
        "--claude-desktop-dir",
        type=Path,
        default=None,
        help="Custom Claude Desktop config directory",
    )
    p_install.add_argument(
        "--cursor-dir",
        type=Path,
        default=None,
        help="Custom Cursor config directory (defaults to ~/.cursor)",
    )
    p_install.add_argument(
        "--enable-by-default",
        "--enable-globally",
        dest="enable_by_default",
        action="store_true",
        help="Enable ra_* MCP servers globally by default in OpenCode (otherwise disabled until enabled per project)",
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
    p_index.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the pre-flight workload and page-count cost plan without parsing or calling LLMs",
    )

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
    p_refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the verbose pre-flight workload and page-count cost plan without parsing or calling LLMs",
    )

    # 5. organize
    p_org = sub.add_parser("organize", help="Run the LLM reorganization pass on an existing VTree")
    p_org.add_argument("--scope", choices=["global", "project"], default="global")
    p_org.add_argument("--vtree-path", type=str, default=None)
    p_org.add_argument("--max-turns", type=int, default=15)

    # 6. rm
    p_rm = sub.add_parser(
        "rm",
        help="Remove a document, book, or directory at a virtual path from the VTree and refresh ancestor summaries",
    )
    p_rm.add_argument("virtual_path", type=str, help="Virtual path in the VTree to remove (e.g. '/papers/123')")
    p_rm.add_argument("--scope", choices=["global", "project"], default="global")
    p_rm.add_argument("--vtree-path", type=str, default=None)
    p_rm.add_argument(
        "--no-dir-summaries",
        action="store_true",
        help="Skip refreshing ancestor directory summaries after removal",
    )

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
        aliases=["update"],
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
    docling_group = p_upgrade.add_mutually_exclusive_group()
    docling_group.add_argument(
        "--with-docling",
        action="store_true",
        default=False,
        help="Include optional [docling] neural PDF extraction dependencies (docling, torch, torchvision) during upgrade",
    )
    docling_group.add_argument(
        "--without-docling",
        action="store_true",
        default=False,
        help="Install only core lightweight dependencies during upgrade (omit optional [docling] extra)",
    )

    # 9. extract
    p_extract = sub.add_parser(
        "extract",
        help="Convert a single PDF/DOCX/TXT file (or arXiv ID / URL) to Markdown to inspect extraction quality and speed",
    )
    p_extract.add_argument(
        "source",
        type=str,
        help="Path to a local PDF/DOCX/TXT file, or an arXiv ID / URL",
    )
    p_extract.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Optional output .md file path (if omitted, saves `<stem>.md` in current directory or prints preview)",
    )
    p_extract.add_argument(
        "--mode",
        choices=["pymupdf", "docling", "llm"],
        default=None,
        help="Override EXTRACTOR_MODE ('pymupdf' [default, $0], 'docling', or 'llm')",
    )
    p_extract.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full extracted Markdown directly to stdout instead of writing a .md file",
    )

    # 10. login-openreview
    p_or_login = sub.add_parser(
        "login-openreview",
        aliases=["openreview-login"],
        help="Authenticate with your personal OpenReview account to enable OpenReview search and review tools",
    )
    p_or_login.add_argument(
        "-u",
        "--username",
        type=str,
        default=None,
        help="OpenReview email or username (prompts interactively if omitted)",
    )
    p_or_login.add_argument(
        "-p",
        "--password",
        type=str,
        default=None,
        help="OpenReview password (prompts securely with hidden input if omitted)",
    )
    p_or_login.add_argument(
        "--token",
        type=str,
        default=None,
        help="Directly save a pre-issued OpenReview JWT bearer token instead of logging in with username/password",
    )
    p_or_login.add_argument(
        "--mfa-code",
        type=str,
        default=None,
        help="6-digit OpenReview MFA verification code (prompts interactively if MFA is enabled on your account)",
    )
    p_or_login.add_argument(
        "--mfa-method",
        choices=["totp", "emailOtp"],
        default=None,
        help="Preferred OpenReview MFA method ('totp' authenticator app or 'emailOtp' email code)",
    )
    p_or_login.add_argument(
        "--totp-secret",
        type=str,
        default=None,
        help="Optional base32 TOTP secret so MFA codes can be generated automatically on token refresh",
    )
    p_or_login.add_argument(
        "--api-url",
        type=str,
        default="https://api2.openreview.net",
        help="OpenReview API base URL (default: https://api2.openreview.net)",
    )
    p_or_login.add_argument(
        "--no-save-password",
        action="store_true",
        help="Only store the short-lived JWT token in ~/.research-agent/openreview_auth.json (disable auto-refresh)",
    )
    p_or_login.add_argument(
        "--status",
        action="store_true",
        help="Show current OpenReview authentication status and exit",
    )
    p_or_login.add_argument(
        "--logout",
        action="store_true",
        help="Remove saved OpenReview credentials/token from ~/.research-agent/openreview_auth.json",
    )

    # 11. serve-http (Gemini Consumer App Custom MCP Connector)
    p_serve = sub.add_parser(
        "serve-http",
        help="Run a unified Streamable HTTP MCP server (/mcp) for the Gemini consumer app's Custom MCP Connectors",
    )
    p_serve.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="HTTP port to listen on (default: 8765)",
    )
    p_serve.add_argument(
        "--path",
        type=str,
        default="/mcp",
        help="Streamable HTTP endpoint path (default: /mcp)",
    )

    # 12. mcp-info
    p_info = sub.add_parser(
        "mcp-info",
        aliases=["info", "endpoints"],
        help="Print resolved MCP binary paths, HTTP endpoint command, sandbox directories, and harness config locations",
    )
    p_info.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-readable text",
    )

    args = parser.parse_args(argv)

    if args.command in ("install-opencode", "install"):
        targets = args.harnesses if args.harnesses else ["opencode"]
        outcome = install_harnesses(
            harnesses=targets,
            opencode_config_dir=args.opencode_dir,
            gemini_dir=args.gemini_dir,
            claude_dir=args.claude_dir,
            antigravity_dir=args.antigravity_dir,
            claude_desktop_dir=args.claude_desktop_dir,
            cursor_dir=args.cursor_dir,
            enable_by_default=args.enable_by_default,
            migrate_repo_data=not args.no_migrate,
        )
        for res in outcome["installed"]:
            h = res.get("harness", "opencode")
            if h == "opencode":
                print(f"Installed OpenCode config: {res['config_path']}")
                print(f"Installed agents: {', '.join(res['agents_installed'])}")
                print(f"Updated global instructions: {res['agents_md_path']}")
            elif h == "gemini-cli":
                print(f"Installed Gemini CLI config: {res['config_path']}")
                print(f"Updated Gemini CLI instructions: {res['instructions_path']}")
            elif h == "claude-code":
                print(f"Installed Claude Code MCP config: {res['config_path']}")
                print(f"Installed Claude Code permissions: {res['settings_path']}")
                print(f"Updated Claude Code instructions: {res['instructions_path']}")
            elif h == "antigravity":
                print(f"Installed Antigravity MCP config: {res['config_path']}")
                print(f"Updated Antigravity instructions: {res['instructions_path']}")
            elif h == "claude-desktop":
                print(f"Installed Claude Desktop MCP config: {res['config_path']}")
            elif h == "cursor":
                print(f"Installed Cursor MCP config: {res['config_path']}")
                print(f"Updated Cursor rules: {res['instructions_path']}")
            if res.get("migrated"):
                print("Migrated data to global home:")
                for m in res["migrated"]:
                    print(f"  - {m}")
        for _, skip_msg in outcome["skipped"]:
            print(skip_msg)
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
                dry_run=args.dry_run,
            )
            if not args.dry_run:
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
                dry_run=args.dry_run,
                on_preflight=print,
            )
            if args.dry_run:
                print("\n[Dry Run] No documents were parsed and no LLM calls were made.")
                return 0
            print(
                f"\nRefreshed VTree [{res.scope}] ({res.vtree_path}):\n"
                f"  Scanned: {res.scanned_count} | Already indexed: {res.already_indexed_count} | "
                f"Cache-relinked: {res.relinked_cache_count} | "
                f"Newly added: {len(res.added_nodes)} ({res.new_pages_count} pages) | "
                f"Repaired: {len(res.repaired_nodes)} ({res.repaired_pages_count} pages)"
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
        from src.vtree.store import VTreeStore

        if args.vtree_path:
            vpath = Path(args.vtree_path).expanduser().resolve()
        elif args.scope == "project":
            vpath = settings.get_project_vtree_path(must_exist=False)
        else:
            vpath = settings.get_global_vtree_path()

        if not vpath or not vpath.exists():
            logger.error("No VTree found at %s", vpath)
            return 1
        store = VTreeStore(vpath)
        with store.file_lock():
            vtree = store.load_as_dict()
            organized = organize_vtree(vtree, args.max_turns)
            if organized is not vtree:
                store.save_dict(organized)
                print(f"Organized VTree saved to {vpath}")
        return 0

    if args.command == "rm":
        from src.cold_start.refresher import _refresh_dirty_directory_summaries
        from src.vtree.operations import VTreeManager
        from src.vtree.store import VTreeStore

        if args.vtree_path:
            vpath = Path(args.vtree_path).expanduser().resolve()
        elif args.scope == "project":
            vpath = settings.get_project_vtree_path(must_exist=False)
        else:
            vpath = settings.get_global_vtree_path()

        if not vpath or not vpath.exists():
            logger.error("No VTree found at %s", vpath)
            return 1

        store = VTreeStore(vpath)
        with store.file_lock():
            vtree_dict = store.load_as_dict()
            mgr = VTreeManager(vtree_dict)
            msg = mgr.rm(args.virtual_path)
            print(msg)
            if msg.startswith("Error:"):
                return 1
            if not args.no_dir_summaries:
                _refresh_dirty_directory_summaries(vtree_dict["root"])
            store.save_dict(vtree_dict)
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

    if args.command in ("upgrade", "update"):
        from src.updater import run_upgrade

        with_docling_opt = True if args.with_docling else (False if args.without_docling else None)
        res = run_upgrade(
            repo=args.repo,
            ref=args.ref,
            method=args.method,
            check_only=args.check,
            force=args.force,
            with_docling=with_docling_opt,
        )
        print(res.message)
        if not res.upgraded and res.message.startswith("Upgrade failed:"):
            return 1
        return 0

    if args.command in ("login-openreview", "openreview-login"):
        import getpass
        from src.openreview_client import (
            OpenReviewAuthError,
            clear_openreview_auth,
            get_openreview_status,
            login_openreview,
        )

        if args.logout:
            removed = clear_openreview_auth()
            if removed:
                print("Removed saved OpenReview credentials from ~/.research-agent/openreview_auth.json.")
            else:
                print("No saved OpenReview credentials found.")
            return 0

        if args.status:
            st = get_openreview_status()
            if st["configured"]:
                print(
                    f"OpenReview Auth: Authenticated as {st['user_id']}\n"
                    f"  - API URL  : {st['api_url']}\n"
                    f"  - Auth File: {st['auth_file']}"
                )
            else:
                print("OpenReview Auth: Not configured. Run `ra-cli login-openreview` to log in.")
            return 0

        username = args.username
        password = args.password
        token = args.token

        if not token:
            if not username:
                try:
                    username = input("OpenReview Email or Username: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled.")
                    return 1
            if not password:
                try:
                    password = getpass.getpass("OpenReview Password: ")
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled.")
                    return 1

        def _interactive_mfa_prompt(method: str, available_methods: list[str], trigger_challenge) -> tuple[str, str]:
            active_m = method
            if active_m == "emailOtp":
                print("OpenReview MFA required (emailOtp): A 6-digit verification code has been sent to your email.")
            elif active_m == "totp":
                extra = " (or type 'email' to receive a code via email)" if "emailOtp" in available_methods else ""
                print(f"OpenReview MFA required (totp): Enter the 6-digit code from your authenticator app{extra}.")
            try:
                ans = input(f"OpenReview MFA Code ({active_m}): ").strip()
                if ans.lower() == "email" and "emailOtp" in available_methods and active_m != "emailOtp":
                    trigger_challenge("emailOtp")
                    active_m = "emailOtp"
                    print("A 6-digit verification code has been sent to your email.")
                    ans = input("OpenReview MFA Code (emailOtp): ").strip()
                return active_m, ans
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                return active_m, ""

        try:
            rec = login_openreview(
                username=username,
                password=password,
                token=token,
                api_url=args.api_url,
                save_password_for_refresh=not args.no_save_password,
                mfa_code=args.mfa_code,
                mfa_method=args.mfa_method,
                totp_secret=args.totp_secret,
                mfa_prompt_callback=_interactive_mfa_prompt,
            )
            print(
                f"Successfully authenticated with OpenReview as '{rec['user_id']}'!\n"
                f"  - Saved to : {settings.DATA_DIR / 'openreview_auth.json'} (mode 0600)\n"
                f"  - Tools enabled: ra_search.openreview_search, ra_search.get_openreview_paper"
            )
            return 0
        except OpenReviewAuthError as e:
            logger.error("%s", e)
            return 1
        except Exception as e:
            logger.error("OpenReview login failed: %s", e)
            return 1

    if args.command == "extract":
        import time
        from src.cold_start.extractor import extract_content

        if args.mode:
            settings.EXTRACTOR_MODE = args.mode

        active_mode = getattr(settings, "EXTRACTOR_MODE", "pymupdf").lower()
        t0 = time.perf_counter()
        src_str = args.source.strip()
        local_path = Path(src_str).expanduser()

        if local_path.exists() and local_path.is_file():
            extracted = extract_content(local_path)
            if not extracted or not extracted.text.strip():
                logger.error("No text extracted from %s", local_path)
                return 1
            md_text = extracted.text
            headings = extracted.headings
            default_out = Path.cwd() / f"{local_path.stem}.md"
        else:
            from src.mcp_servers.file_tools import _clean_filename, _resolve_paper_content

            resolved = _resolve_paper_content(url_or_arxiv_id=src_str, temp_id="cli-extract")
            md_text = resolved.get("text", "")
            if not md_text.strip():
                logger.error("Could not resolve or extract '%s'", src_str)
                return 1
            headings = [l.strip("# ").strip() for l in md_text.splitlines() if l.startswith("#")]
            default_out = Path.cwd() / f"{_clean_filename(resolved.get('title') or 'extracted')}.md"

        elapsed = time.perf_counter() - t0
        if args.stdout:
            print(md_text)
            return 0

        out_path = args.output.expanduser().resolve() if args.output else default_out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md_text, encoding="utf-8")
        print(
            f"Extracted Markdown in {elapsed:.2f}s (mode={active_mode}):\n"
            f"  - Source   : {src_str}\n"
            f"  - Output   : {out_path}\n"
            f"  - Size     : {len(md_text):,} chars ({len(md_text.splitlines()):,} lines)\n"
            f"  - Headings : {len(headings)} detected"
        )
        if headings:
            preview_h = ", ".join(headings[:8]) + ("..." if len(headings) > 8 else "")
            print(f"  - Sections : {preview_h}")
        return 0

    if args.command == "serve-http":
        norm_path = "/" + args.path.lstrip("/")
        local_url = f"http://{args.host}:{args.port}{norm_path}"
        print(
            "Starting unified Research Agent Streamable HTTP MCP server...\n"
            f"  - Endpoint             : {local_url}\n"
            "  - 2-Phase Save Policy  : MCP ToolAnnotations (confirm_save_paper marks readOnlyHint=False, destructiveHint=True)\n"
            "  - Gemini Consumer App  : Open Customize -> Connectors -> + Custom and add the URL.\n"
            f"  - HTTPS Tunnel (if req): cloudflared tunnel --url http://{args.host}:{args.port}"
        )
        hub = create_unified_http_mcp_server()
        hub.run(transport="streamable-http", host=args.host, port=args.port, path=norm_path)
        return 0

    if args.command in ("mcp-info", "info", "endpoints"):
        info = get_mcp_info()
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(format_mcp_info(info))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

