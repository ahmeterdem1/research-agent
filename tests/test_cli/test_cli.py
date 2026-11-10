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


def test_cli_refresh_command_adds_new_files_to_target_dir(monkeypatch, tmp_path, capsys):
    from src.cold_start import refresher

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    cache_dir = tmp_path / "cache"
    vtree_path = tmp_path / "vtree.json"

    monkeypatch.setattr(settings, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(refresher, "summarize_text", lambda txt: "CLI refresh summary")
    monkeypatch.setattr(refresher, "summarize_directory", lambda sums: "Updated dir summary")

    (lib_dir / "DroppedNote.md").write_text("# Fresh Note\nTesting CLI refresh.", encoding="utf-8")

    rc = cli.main(
        [
            "refresh",
            "--library-path",
            str(lib_dir),
            "--vtree-path",
            str(vtree_path),
            "--target-dir",
            "/Inbox",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== VTree Refresh Pre-Flight Plan [GLOBAL] ===" in out
    assert "Scanned: 1 | Already indexed: 0" in out
    assert "Newly added: 1" in out
    assert "/Inbox/DroppedNote" in out


def test_cli_upgrade_checks_version_and_upgrades_via_uv_or_homebrew(monkeypatch, tmp_path, capsys):
    from src import updater

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "ra_home")
    monkeypatch.setattr(updater, "get_local_version", lambda: "0.1.0")

    # 1. Same version and same SHA -> no-op ("Already up to date")
    updater.save_install_receipt({"version": "0.1.0", "sha": "abc123456789"})
    monkeypatch.setattr(
        updater,
        "fetch_remote_release_info",
        lambda repo=updater.DEFAULT_GITHUB_REPO, ref="HEAD": {
            "version": "0.1.0",
            "sha": "abc123456789",
            "source": "mock",
        },
    )

    rc1 = cli.main(["upgrade"])
    assert rc1 == 0
    assert "Already up to date (version 0.1.0)" in capsys.readouterr().out

    # 2. Newer remote version (0.2.0) -> triggers `uv tool install --force` and updates receipt
    monkeypatch.setattr(
        updater,
        "fetch_remote_release_info",
        lambda repo=updater.DEFAULT_GITHUB_REPO, ref="HEAD": {
            "version": "0.2.0",
            "sha": "def987654321",
            "source": "mock",
        },
    )
    uv_calls = []
    monkeypatch.setattr(
        updater,
        "_run_uv_global_install",
        lambda repo, ref: (uv_calls.append((repo, ref)) or True, "Installed via uv"),
    )
    monkeypatch.setattr(updater, "_refresh_opencode_integration", lambda: None)

    rc2 = cli.main(["upgrade", "--method", "uv"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "Successfully upgraded system-wide installation (uv) from 0.1.0 -> 0.2.0" in out2
    assert uv_calls == [("ahmeterdem1/research-agent", "HEAD")]
    receipt = updater.load_install_receipt()
    assert receipt["version"] == "0.2.0"
    assert receipt["sha"] == "def987654321"

    # 3. Homebrew method support (`--method homebrew --force`)
    brew_calls = []
    monkeypatch.setattr(
        updater,
        "_run_homebrew_upgrade",
        lambda formula="research-agent": (brew_calls.append(formula) or True, "Upgraded via brew"),
    )
    rc3 = cli.main(["upgrade", "--method", "homebrew", "--force"])
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "Successfully upgraded system-wide installation (homebrew)" in out3
    assert brew_calls == ["research-agent"]


def test_cli_rm_and_index_dry_run(monkeypatch, tmp_path, capsys):
    from src.cold_start import refresher

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "Note.md").write_text("# Sample Note\nContent", encoding="utf-8")
    vtree_path = tmp_path / "vtree.json"

    # 1. `ra-cli index --dry-run` prints preflight plan without running subprocess stages
    rc_idx = cli.main(
        [
            "index",
            "--library-path",
            str(lib_dir),
            "--output-vtree",
            str(vtree_path),
            "--dry-run",
        ]
    )
    assert rc_idx == 0
    out_idx = capsys.readouterr().out
    assert "=== Cold-Start Index Pre-Flight Plan [GLOBAL] ===" in out_idx
    assert "[Dry Run] No documents were parsed and no LLM calls were made." in out_idx

    # 2. `ra-cli rm` removes an unwanted node and updates ancestor summaries
    vtree_path.write_text(
        json.dumps(
            {
                "version": 1,
                "library_root": str(lib_dir),
                "root": {
                    "type": "directory",
                    "name": "Root",
                    "virtual_path": "/",
                    "summary": "Root",
                    "children": [
                        {
                            "type": "directory",
                            "name": "papers",
                            "virtual_path": "/papers",
                            "summary": "User created directory.",
                            "children": [
                                {
                                    "type": "document",
                                    "name": "123",
                                    "virtual_path": "/papers/123",
                                    "real_path": "http://arxiv.org/123",
                                    "summary": "Dummy node",
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(refresher, "summarize_directory", lambda sums: "Refreshed dir summary")

    rc_rm = cli.main(["rm", "/papers/123", "--vtree-path", str(vtree_path)])
    assert rc_rm == 0
    out_rm = capsys.readouterr().out
    assert "Successfully removed [document] '/papers/123'" in out_rm
    saved = json.loads(vtree_path.read_text(encoding="utf-8"))
    assert saved["root"]["children"][0]["children"] == []

    # 3. `ra-cli extract` converts a local PDF to a .md file using PyMuPDF
    import fitz

    test_pdf = tmp_path / "test_extract.pdf"
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 80), "Fast PDF Extraction Test", fontsize=16)
    pg.insert_text((72, 120), "1 Introduction", fontsize=12)
    pg.insert_text((72, 150), "This is extracted in milliseconds with PyMuPDF.", fontsize=10)
    doc.save(str(test_pdf))
    doc.close()

    out_md = tmp_path / "out_extracted.md"
    rc_ext = cli.main(["extract", str(test_pdf), "-o", str(out_md)])
    assert rc_ext == 0
    assert out_md.exists()
    md_content = out_md.read_text(encoding="utf-8")
    assert "# Fast PDF Extraction Test" in md_content
    assert "This is extracted in milliseconds with PyMuPDF." in md_content


def test_cli_login_openreview_lifecycle(monkeypatch, tmp_path, capsys):
    from unittest.mock import MagicMock, patch
    from src import openreview_client

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "ra_home")
    monkeypatch.delenv("OPENREVIEW_TOKEN", raising=False)
    monkeypatch.delenv("OPENREVIEW_USERNAME", raising=False)
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)

    # 1. Status when not configured
    rc_st1 = cli.main(["login-openreview", "--status"])
    assert rc_st1 == 0
    assert "Not configured" in capsys.readouterr().out

    # 2. Login with --username and --password (mocking OpenReview POST /login)
    with patch("src.openreview_client.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "token": "jwt_tok_abc999",
            "user": {"id": "~Alice_Researcher1"},
        }
        mock_post.return_value = mock_resp

        rc_login = cli.main(
            [
                "login-openreview",
                "--username",
                "alice@university.edu",
                "--password",
                "secret-pass-123",
            ]
        )
        assert rc_login == 0
        out_login = capsys.readouterr().out
        assert "Successfully authenticated with OpenReview as '~Alice_Researcher1'!" in out_login

    auth_file = (tmp_path / "ra_home") / "openreview_auth.json"
    assert auth_file.exists()
    assert openreview_client.is_openreview_configured() is True

    # 3. Status after login
    rc_st2 = cli.main(["login-openreview", "--status"])
    assert rc_st2 == 0
    out_st2 = capsys.readouterr().out
    assert "Authenticated as ~Alice_Researcher1" in out_st2

    # 4. Logout removes credentials file
    rc_logout = cli.main(["login-openreview", "--logout"])
    assert rc_logout == 0
    assert not auth_file.exists()
    assert openreview_client.is_openreview_configured() is False






