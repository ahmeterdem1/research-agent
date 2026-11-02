import pytest

from config import settings
from src import cli
from src.kg.store import KGStore
from src.mcp_servers import file_tools, kg_tools
from src.telemetry import store as telemetry_mod
from src.telemetry.store import TelemetryStore, format_telemetry_summary, track_tool


@pytest.fixture
def tstore(tmp_path, monkeypatch) -> TelemetryStore:
    db_file = tmp_path / "telemetry_test.db"
    ts = TelemetryStore(db_file, default_project_id="proj-alpha")
    monkeypatch.setattr(telemetry_mod, "_telemetry_store", ts)
    monkeypatch.setattr(settings, "DB_PATH", db_file)
    return ts


def test_track_tool_records_ok_and_soft_errors(tstore):
    @track_tool("ra_custom")
    def sample_tool(query: str, scope: str = "global", fail: bool = False) -> str:
        if fail:
            return "Error: Invalid query parameter"
        return f"Found result for {query}"

    assert sample_tool("transformers", scope="project") == "Found result for transformers"
    assert sample_tool("bad", scope="global", fail=True).startswith("Error:")

    summary = tstore.get_summary()
    assert summary["total_calls"] == 2
    assert summary["total_errors"] == 1
    assert summary["error_rate"] == 50.0
    assert summary["by_scope"] == {"project": 1, "global": 1}
    assert summary["recent"][0]["status"] == "error"
    assert "Invalid query parameter" in summary["recent"][0]["error_msg"]


def test_mcp_tools_automatically_log_telemetry_and_behavioral_diagnostics(tstore, tmp_path, monkeypatch):
    kg_store = KGStore(tstore.db_path, default_project_id="proj-alpha")
    monkeypatch.setattr(kg_tools, "_store", kg_store)

    # 1. Valid KG call + invalid KG call
    kg_tools.add_triplet(subject="User", relation="interested_in", object="RL", object_class="Topic", scope="global")
    kg_tools.add_triplet(subject="User", relation="nonexistent_rel", object="RL", object_class="Topic")

    # 2. Stage a paper WITHOUT calling search_library first -> triggers local_first_compliant = False!
    file_tools._STAGED_PAPERS.clear()
    monkeypatch.setattr(settings, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(file_tools, "summarize_text", lambda txt: "OpenRouter summary")
    file_tools.stage_paper(
        "https://arxiv.org/abs/1706.03762",
        "/Transformers",
        target_scope="project",
        preextracted_text="# Attention Is All You Need",
    )

    summary = tstore.get_summary()
    assert summary["total_calls"] == 3
    assert summary["total_errors"] == 1
    diag = summary["behavioral_diagnostics"]
    assert diag["local_search_calls"] == 0
    assert diag["external_fetch_calls"] == 1
    assert diag["local_first_compliant"] is False

    text = format_telemetry_summary(summary)
    assert "WARNING (more online fetches than `search_library` calls!)" in text
    assert "ra_kg.add_triplet" in text
    assert "ra_ingest.stage_paper" in text


def test_project_filtering_and_cli_stats_and_reset(tstore, capsys):
    tstore.record_invocation("ra_docs", "search_library", status="ok", scope="all", project_id="proj-alpha")
    tstore.record_invocation("ra_search", "get_paper_references", status="ok", project_id="proj-beta")

    # Default filter is proj-alpha
    s_alpha = tstore.get_summary()
    assert s_alpha["total_calls"] == 1

    # all_projects=True sees both
    s_all = tstore.get_summary(all_projects=True)
    assert s_all["total_calls"] == 2
    assert s_all["behavioral_diagnostics"]["citation_walk_calls"] == 1

    # CLI `ra-cli stats --all-projects`
    rc = cli.main(["stats", "--all-projects"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total Tool Calls : 2" in out
    assert "Citation Walk Activity (ref/cit/walk calls)      : 1" in out

    # CLI `ra-cli stats --reset --project proj-alpha`
    rc_reset = cli.main(["stats", "--reset", "--project", "proj-alpha"])
    assert rc_reset == 0
    assert tstore.get_summary(project_id="proj-alpha")["total_calls"] == 0
    assert tstore.get_summary(project_id="proj-beta")["total_calls"] == 1

