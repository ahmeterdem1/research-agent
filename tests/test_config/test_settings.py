import logging
from pathlib import Path

from config import settings


def test_get_logger_writes_to_stderr_not_stdout(capsys):
    logger = settings.get_logger("test_module")
    assert isinstance(logger, logging.Logger)
    logger.info("hello stderr")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello stderr" in captured.err


def test_get_active_project_id_env_and_cwd(monkeypatch, tmp_path):
    proj_dir = tmp_path / "my-awesome-repo"
    proj_dir.mkdir()

    monkeypatch.delenv("RA_PROJECT_ID", raising=False)
    assert settings.get_active_project_id(cwd=proj_dir) == "my-awesome-repo"

    monkeypatch.setenv("RA_PROJECT_ID", "custom-proj-id")
    assert settings.get_active_project_id(cwd=proj_dir) == "custom-proj-id"


def test_get_project_vtree_path(monkeypatch, tmp_path):
    monkeypatch.delenv("RA_VTREE_PATH", raising=False)
    proj_dir = tmp_path / "workspace"
    proj_dir.mkdir()

    # Does not exist yet
    assert settings.get_project_vtree_path(cwd=proj_dir, must_exist=True) is None
    target = settings.get_project_vtree_path(cwd=proj_dir, must_exist=False)
    assert target == (proj_dir / ".opencode" / "vtree.json").resolve()

    # Create it
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    assert settings.get_project_vtree_path(cwd=proj_dir, must_exist=True) == target

    # Explicit env override
    override = tmp_path / "custom_vtree.json"
    override.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RA_VTREE_PATH", str(override))
    assert settings.get_project_vtree_path(cwd=proj_dir, must_exist=True) == override.resolve()


def test_get_global_vtree_path_env_override(monkeypatch, tmp_path):
    custom_global = tmp_path / "global_vtree.json"
    monkeypatch.setenv("RA_GLOBAL_VTREE_PATH", str(custom_global))
    assert settings.get_global_vtree_path() == custom_global.resolve()


def test_normalize_litellm_model_adds_openrouter_prefix():
    assert settings.normalize_litellm_model("google/gemini-2.5-flash") == "openrouter/google/gemini-2.5-flash"
    assert settings.normalize_litellm_model("qwen/qwen-2.5-72b-instruct") == "openrouter/qwen/qwen-2.5-72b-instruct"
    assert settings.normalize_litellm_model("deepseek/deepseek-chat") == "openrouter/deepseek/deepseek-chat"
    assert settings.normalize_litellm_model("openrouter/google/gemini-2.5-flash") == "openrouter/google/gemini-2.5-flash"
    assert settings.normalize_litellm_model("ollama/llama3") == "ollama/llama3"


def test_global_env_overrides_repo_env_and_live_reloads(monkeypatch, tmp_path):
    fake_repo = tmp_path / "repo"
    fake_global = tmp_path / "global_home"
    fake_repo.mkdir()
    fake_global.mkdir()

    # Repo .env has legacy EXTRACTOR_MODE=llm, while ~/.research-agent/.env has EXTRACTOR_MODE=pymupdf
    (fake_repo / ".env").write_text("EXTRACTOR_MODE=llm\n", encoding="utf-8")
    (fake_global / ".env").write_text(
        "EXTRACTOR_MODE=pymupdf\nPATH_TO_TREE=/Users/test/literature-dev\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "PROJECT_ROOT", fake_repo)
    monkeypatch.setattr(settings, "DATA_DIR", fake_global)
    # Even when cwd == repo root, global ~/.research-agent/.env must win over repo .env
    monkeypatch.chdir(fake_repo)
    monkeypatch.setattr(settings, "_LAST_ENV_MTIMES", {})

    settings.reload_env_settings()
    assert settings.EXTRACTOR_MODE == "pymupdf"
    assert settings.LIBRARY_ROOT_PATH == "/Users/test/literature-dev"



