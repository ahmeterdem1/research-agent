import json
from pathlib import Path

from config import settings
from src import cli
from src.kg.store import KGStore


def test_install_opencode_merges_config_and_is_idempotent(tmp_path):
    oc_dir = tmp_path / "opencode_cfg"
    oc_dir.mkdir()

    # Pre-populate existing opencode.json and AGENTS.md to ensure nothing is lost
    (oc_dir / "opencode.json").write_text(
        json.dumps(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "mcp": {"github": {"type": "local", "command": ["npx", "gh"], "enabled": True}},
                "permission": {"bash": "ask"},
            }
        ),
        encoding="utf-8",
    )
    (oc_dir / "AGENTS.md").write_text("# User Custom Rules\nAlways write tests.\n", encoding="utf-8")

    res1 = cli.install_opencode(opencode_config_dir=oc_dir, enable_by_default=False, migrate_repo_data=False)
    assert set(res1["agents_installed"]) == {"citation-walker.md", "researcher.md"}
    assert (oc_dir / "agents" / "researcher.md").exists()
    assert (oc_dir / "agents" / "citation-walker.md").exists()

    cfg = json.loads((oc_dir / "opencode.json").read_text(encoding="utf-8"))
    # Preserved existing entries
    assert cfg["model"] == "anthropic/claude-sonnet-4-5"
    assert cfg["mcp"]["github"]["enabled"] is True
    assert cfg["permission"]["bash"] == "ask"
    # Added Research Agent MCP entries & permission gates
    for srv in ("ra_docs", "ra_search", "ra_ingest", "ra_kg"):
        assert srv in cfg["mcp"]
        assert cfg["mcp"][srv]["enabled"] is False
    assert cfg["permission"]["ra_ingest_confirm_save_paper"] == "ask"

    # Run a second time to verify idempotency of AGENTS.md marker replacement
    cli.install_opencode(opencode_config_dir=oc_dir, enable_by_default=True, migrate_repo_data=False)
    agents_md = (oc_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_md.count(cli.AGENTS_START_MARKER) == 1
    assert agents_md.count(cli.AGENTS_END_MARKER) == 1
    assert "# User Custom Rules" in agents_md


def test_configure_project_granular_and_all(tmp_path):
    proj_dir = tmp_path / "my_project"

    # 1. Enable only docs and kg with a custom project_id
    rc = cli.main(
        [
            "project",
            "--project-dir",
            str(proj_dir),
            "--docs",
            "on",
            "--kg",
            "on",
            "--search",
            "off",
            "--project-id",
            "neuro-sim",
        ]
    )
    assert rc == 0

    cfg_path = proj_dir / "opencode.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["mcp"]["ra_docs"]["enabled"] is True
    assert data["mcp"]["ra_kg"]["enabled"] is True
    assert data["mcp"]["ra_search"]["enabled"] is False
    assert data["mcp"]["ra_kg"]["environment"]["RA_PROJECT_ID"] == "neuro-sim"

    # 2. --disable-all turns all 4 off while keeping environment
    cli.main(["project", "--project-dir", str(proj_dir), "--disable-all"])
    data2 = json.loads(cfg_path.read_text(encoding="utf-8"))
    for k in ("ra_docs", "ra_search", "ra_ingest", "ra_kg"):
        assert data2["mcp"][k]["enabled"] is False
    assert data2["mcp"]["ra_kg"]["environment"]["RA_PROJECT_ID"] == "neuro-sim"

    # 3. --enable-all turns all 4 on
    cli.main(["project", "--project-dir", str(proj_dir), "--enable-all"])
    data3 = json.loads(cfg_path.read_text(encoding="utf-8"))
    for k in ("ra_docs", "ra_search", "ra_ingest", "ra_kg"):
        assert data3["mcp"][k]["enabled"] is True


def test_status_reports_vtree_and_dual_scope_kg(monkeypatch, tmp_path, capsys):
    proj_dir = tmp_path / "active-workspace"
    proj_dir.mkdir()

    db_path = tmp_path / "status_test.db"
    global_vtree = tmp_path / "global_vtree.json"
    proj_vtree = proj_dir / ".opencode" / "vtree.json"
    proj_vtree.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "DB_PATH", db_path)
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(global_vtree))
    monkeypatch.delenv("RA_VTREE_PATH", raising=False)
    monkeypatch.delenv("RA_PROJECT_ID", raising=False)

    # Populate global VTree (2 docs) and project VTree (1 doc)
    global_vtree.write_text(
        json.dumps(
            {
                "root": {
                    "type": "directory",
                    "children": [
                        {"type": "document", "name": "d1"},
                        {"type": "book", "name": "b1", "children": [{"type": "document", "name": "ch1"}]},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    proj_vtree.write_text(
        json.dumps({"root": {"type": "directory", "children": [{"type": "document", "name": "p1"}]}}),
        encoding="utf-8",
    )

    # Populate 2 global triplets, 1 active-workspace triplet, and 1 unrelated project triplet
    store = KGStore(db_path, default_project_id="active-workspace")
    store.add_triplet("User", "User", "interested_in", "RL", "Topic", scope="global")
    store.add_triplet("User", "User", "has_read", "Paper A", "Paper", scope="global")
    store.add_triplet("User", "User", "argued_that", "Local claim", "Argument", scope="project")
    store.add_triplet(
        "User", "User", "argued_that", "Other project claim", "Argument",
        scope="project", project_id="other-workspace",
    )

    cli.configure_project(project_dir=proj_dir, docs=True, kg=True)

    rc = cli.main(["status", "--project-dir", str(proj_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Global facts : 2" in out
    assert "Project facts: 1 (project: active-workspace)" in out
    assert "3 docs/books" in out
    assert "1 docs/books" in out
    assert "ra_docs   : enabled" in out
    assert "ra_kg     : enabled" in out

