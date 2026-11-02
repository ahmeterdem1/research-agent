# Research Agent — CLI, Telemetry & Configuration Reference

This guide documents all `ra-cli` commands, environment variables, `.env` resolution rules, cold-start pipeline stages, and telemetry diagnostics.

---

## 1. Global Data Home Layout (`~/.research-agent/`)

By default, all persistent data lives in `~/.research-agent/` (overridable via `$RESEARCH_AGENT_HOME`):

```text
~/.research-agent/
├── .env                        # Global API keys & default model settings
├── research_agent.db           # SQLite DB: Dual-scope Knowledge Graph + Tool Telemetry
├── vtrees/
│   └── global.json             # Master PC-wide Virtual Document Tree
├── cache/
│   ├── texts/                  # Content-addressed extracted Markdown (<sha256>.md)
│   ├── staged/                 # Pending paper manifests (<temp_id>.json)
│   ├── cs_01_scan.json         # Cold-start Stage 1 scan intermediary
│   ├── cs_02_extract.json      # Cold-start Stage 2 extraction intermediary
│   └── cs_03_summarize.json    # Cold-start Stage 3 summary intermediary
└── logs/
    └── research_agent.log      # Rotating/append log from CLI and MCP servers
```

---

## 2. Environment Variables & `.env` Precedence

Implemented in [config/settings.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/config/settings.py). When any CLI command or MCP server starts, environment variables are resolved in the following priority order (highest to lowest):

1. **Explicit Process Environment Variables** (including per-project `mcp.<server>.environment` in `<project>/opencode.json`)
2. **Project-Local `.env`** (`<current_working_directory>/.env`)
3. **Global `.env`** (`~/.research-agent/.env`)
4. **Repository `.env`** (`<repo_root>/.env`)

### Supported Variables

| Variable | Default | Description |
|---|---|---|
| `RESEARCH_AGENT_HOME` | `~/.research-agent` | Root directory for global DB, VTree, cache, and logs. |
| `OPENROUTER_API_KEY` | `""` | API key used by LiteLLM for summarization, organization, and LLM OCR. |
| `PATH_TO_TREE` | `""` | Default physical directory path of your PDF/document library for `ra-cli index`. |
| `RA_PROJECT_ID` | `Path.cwd().name` | Canonical project identifier used by `ra_kg`, `ra_docs`, and telemetry when `scope="project"`. |
| `RA_GLOBAL_VTREE_PATH` | `~/.research-agent/vtrees/global.json` | Override path for the Global VTree JSON file. |
| `RA_VTREE_PATH` | `<cwd>/.opencode/vtree.json` | Override path for the Project-Local VTree JSON file. |
| `DEFAULT_AGENT_MODEL` | `openrouter/deepseek/deepseek-chat` | Default model for agentic loops. |
| `DEFAULT_SUMMARIZATION_MODEL` | `openrouter/deepseek/deepseek-chat` | Model used for document/directory summarization and VTree organization. |
| `EXTRACTOR_MODE` | `docling` | PDF extraction backend: `docling` (local IBM Docling) or `llm` (Vision LLM OCR). |
| `VISION_MODEL` | `openrouter/google/gemini-2.5-flash` | Vision model used when `EXTRACTOR_MODE=llm`. |
| `DOCLING_DEVICE` | `auto` | Hardware accelerator for Docling (`auto`, `mps`, `cuda`, `cpu`). |
| `MAX_CONCURRENT_REQUESTS` | `5` | Thread-pool concurrency for cold-start extraction and summarization. |
| `RA_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `OPENCODE_CONFIG_DIR` | `~/.config/opencode` | Target directory for `ra-cli install-opencode`. |

---

## 3. `ra-cli` Command Reference

Source: [src/cli.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/cli.py)

### 3.1 `ra-cli install-opencode`
Registers the Research Agent extension globally with OpenCode:
* Merges `ra_docs`, `ra_search`, `ra_ingest`, and `ra_kg` into `~/.config/opencode/opencode.json`.
* Configures `"ra_ingest_confirm_save_paper": "ask"` in the global `"permission"` block.
* Copies `researcher.md` and `citation-walker.md` into `~/.config/opencode/agents/`.
* Idempotently injects the Research Agent instructions block (`<!-- RESEARCH_AGENT_START --> ... <!-- RESEARCH_AGENT_END -->`) into `~/.config/opencode/AGENTS.md`.
* Migrates any legacy `<repo>/data/` files (`vtree.json`, `research_agent.db`, `cache/texts/*.md`, `.env`) into `~/.research-agent/`.

```bash
# Install with MCP servers disabled by default (recommended; enable per project via `ra-cli project`)
ra-cli install-opencode

# Install with all 4 MCP servers enabled globally in every project by default
ra-cli install-opencode --enable-by-default

# Custom OpenCode config directory and skip data migration
ra-cli install-opencode --opencode-dir ~/.config/opencode --no-migrate
```

### 3.2 `ra-cli project`
Enables or disables Research Agent MCP servers inside a specific project's `opencode.json` (which OpenCode deep-merges over `~/.config/opencode/opencode.json`):

```bash
# Enable all 4 MCP servers in the current working directory
ra-cli project --enable-all

# Disable all 4 MCP servers in the current working directory
ra-cli project --disable-all

# Granularly enable/disable specific servers and set a custom project_id
ra-cli project --docs on --kg on --search off --ingest off --project-id "my-thesis"

# Target a different project directory
ra-cli project --project-dir ~/code/another-repo --enable-all
```

### 3.3 `ra-cli index`
Runs the 5-stage Cold-Start pipeline ([scripts/cold_start.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/scripts/cold_start.py)):
1. **Stage 1 (`cs_01_scan.py`)**: Recursively scans `--library-path`, counts PDF pages via PyMuPDF, and classifies items into documents ($\le 30$ pages) vs. books ($> 30$ pages).
2. **Stage 2 (`cs_02_extract.py`)**: Splits splittable books by TOC outline (`splitter.py`) and extracts Markdown text in parallel (`extractor.py`) into `~/.research-agent/cache/texts/<sha256>.md`. Automatically skips files already present in the existing VTree.
3. **Stage 3 (`cs_03_summarize.py`)**: Generates 2–4 sentence summaries for leaf documents and book sections in parallel via `DEFAULT_SUMMARIZATION_MODEL`, then synthesizes book-level summaries.
4. **Stage 4 (`cs_04_build_vtree.py`)**: Builds the initial `vtree.json` mirroring the folder hierarchy and computes bottom-up directory summaries.
5. **Stage 5 (`organize_vtree.py`)**: Runs an LLM librarian pass to group related documents into clean virtual folders without moving files on disk.

```bash
# Index a directory into the PC-wide Global VTree (~/.research-agent/vtrees/global.json)
ra-cli index --library-path ~/Documents/Papers --scope global

# Index a directory into the current project's .opencode/vtree.json
ra-cli index --library-path ./project_papers --scope project

# Force full rebuild and skip the LLM folder-reorganization pass
ra-cli index --library-path ~/Documents/Papers --scope global --overwrite --skip-organize
```

### 3.4 `ra-cli organize`
Runs Stage 5 (the LLM semantic reorganization pass) standalone on an existing Global or Project VTree:

```bash
ra-cli organize --scope global --max-turns 15
ra-cli organize --scope project
```

### 3.5 `ra-cli status`
Displays a snapshot of your global data home, active `project_id`, Global and Project VTree document counts, active KG triplet counts (`global` vs. `project`), and current project MCP toggles:

```bash
ra-cli status
ra-cli status --project-dir ~/code/my-project
```

### 3.6 `ra-cli stats`
Displays the Research Agent's SQLite tool & subagent telemetry report (`tool_invocations` table in `~/.research-agent/research_agent.db`):
* **Total calls, soft error count & error rate (%)**
* **Average and maximum execution latency (`ms`)** per server and per tool
* **Scope breakdown** (`global`, `project`, `all`, `n/a`)
* **Behavioral Diagnostics**:
  * *Local-First Compliance*: Compares `ra_docs.search_library` calls against external fetch calls (`read_online_document` + `stage_paper`) and warns if the agent is fetching papers online without checking the local VTree first.
  * *Citation Walk Activity*: Total calls to `get_paper_references`, `get_paper_citations`, and `walk_citation_graph`.
  * *Paper Ingestion Gate*: Ratio of `stage_paper` calls to user-approved `confirm_save_paper` calls.
* **Recent Invocations**: Timestamped trace of recent calls (or errors only).

```bash
# View telemetry for the current project
ra-cli stats

# View telemetry across all projects over the last 7 days, showing last 25 calls
ra-cli stats --all-projects --days 7 --recent 25

# Inspect only failed/error tool calls
ra-cli stats --all-projects --errors-only

# Reset telemetry for the current project (or all projects)
ra-cli stats --reset
ra-cli stats --reset --all-projects
```

> [!TIP]
> **Combining `ra-cli stats` with `opencode stats`**:
> Use `opencode stats --models --tools` to inspect LLM token consumption and dollar costs across models, and use `ra-cli stats` to inspect MCP tool latencies, soft error messages, `global` vs. `project` scope usage, and behavioral compliance.

