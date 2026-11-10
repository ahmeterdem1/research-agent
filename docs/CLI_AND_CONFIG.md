# Research Agent — CLI, Telemetry & Configuration Reference

This guide documents all `ra-cli` commands, environment variables, `.env` resolution rules, cold-start pipeline stages, and telemetry diagnostics.

---

## 1. Global Data Home Layout (`~/.research-agent/`)

By default, all persistent data lives in `~/.research-agent/` (overridable via `$RESEARCH_AGENT_HOME`):

```text
~/.research-agent/
├── .env                        # Global API keys & default model settings
├── openreview_auth.json        # Optional user-authenticated OpenReview token (mode 0600)
├── research_agent.db           # SQLite DB: Dual-scope Knowledge Graph + Tool Telemetry
├── vtrees/
│   └── global.json             # Master PC-wide Virtual Document Tree
├── cache/
│   ├── texts/                  # Content-addressed extracted Markdown (<sha256>.md)
│   ├── online_docs/            # Cached read_online_document texts (reused by stage_paper)
│   ├── staged/                 # Pending paper manifests (<temp_id>.json) and PDFs (<temp_id>.pdf)
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
| `EXTRACTOR_MODE` | `pymupdf` | PDF extraction backend: `pymupdf` (default — instant, zero-cost local structured Markdown extraction via PyMuPDF `fitz`), `docling` (local IBM Docling layout OCR), or `llm` (Vision LLM OCR via OpenRouter using the **Dr. DocBench** extraction prompt in [`config/prompts.toml`](file:///Users/ahmeterdem/Desktop/code/research%20agent/config/prompts.toml#L40-L116)). |
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

### 3.4 `ra-cli refresh`
Incrementally scans your source folder on disk ([src/cold_start/refresher.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/cold_start/refresher.py)), compares discovered files against `real_path` entries already in your `vtree.json`, extracts + summarizes **only** newly dropped files, and inserts them into the existing VTree without resetting custom virtual folders:

```bash
# Preview how many new documents/pages will be parsed and estimated LLM calls without writing changes
ra-cli refresh --scope global --dry-run

# Incrementally index newly dropped PDFs into the Global VTree (uses library_root from vtree.json)
ra-cli refresh --scope global

# Place all newly discovered files into a specific virtual directory (e.g. /Inbox)
ra-cli refresh --scope global --target-dir /Inbox

# Refresh a Project-Local VTree and run the LLM semantic organizer afterward
ra-cli refresh --scope project --library-path ./project_papers --organize
```

### 3.5 `ra-cli organize`
Runs Stage 5 (the LLM semantic reorganization pass) standalone on an existing Global or Project VTree:

```bash
ra-cli organize --scope global --max-turns 15
ra-cli organize --scope project
```

### 3.6 `ra-cli status`
Displays a snapshot of your global data home, active `project_id`, Global and Project VTree document counts, active KG triplet counts (`global` vs. `project`), and current project MCP toggles:

```bash
ra-cli status
ra-cli status --project-dir ~/code/my-project
```

### 3.7 `ra-cli stats`
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

### 3.8 `ra-cli upgrade`
Checks the source GitHub repository (`https://github.com/ahmeterdem1/research-agent`, supporting private repositories via `gh` CLI, `GITHUB_TOKEN`, or SSH/HTTPS `git ls-remote`) and upgrades your **system-wide installation** (`uv tool` or Homebrew) if the remote version or commit differs from your installed version—**without touching any local development checkout**:

```bash
# Check if an upgrade is available on GitHub without installing
ra-cli upgrade --check

# Upgrade the system-wide installation if a newer version/commit is on GitHub
ra-cli upgrade

# Force reinstall from a specific branch or tag (or via Homebrew when published)
ra-cli upgrade --ref main --force
ra-cli upgrade --method homebrew
```

### 3.9 `ra-cli extract`
Manually runs the Research Agent's PDF/document-to-Markdown conversion pipeline ([src/cold_start/extractor.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/cold_start/extractor.py) and [`_resolve_paper_content`](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/file_tools.py#L469-L645)) on any local file (`.pdf`, `.docx`, `.md`, `.txt`), arXiv ID, DOI, or URL without staging or modifying your VTree:

```bash
# Extract a local PDF to ./<stem>.md using the default extractor (pymupdf)
ra-cli extract /path/to/paper.pdf

# Specify a custom output file path
ra-cli extract /path/to/paper.pdf -o ./extracted_paper.md

# Extract directly from an arXiv ID or URL
ra-cli extract 2405.07987 -o ./platonic_representation.md

# Override the extraction backend for comparison (pymupdf, docling, or llm) or print to stdout
ra-cli extract /path/to/paper.pdf --mode pymupdf --stdout
```

### 3.10 `ra-cli login-openreview`
Authenticates with your personal OpenReview account (`POST https://api2.openreview.net/login` and `/mfa/verify` when Multi-Factor Authentication is enabled) and saves a 7-day JWT bearer token in `~/.research-agent/openreview_auth.json` (`chmod 0600`) to enable `ra_search.openreview_search` and `ra_search.get_openreview_paper`. No built-in or default credentials exist—OpenReview access is strictly opt-in per user:

```bash
# Interactive login (prompts for email/username, hidden password, and 6-digit MFA code via emailOtp or TOTP if enabled)
ra-cli login-openreview

# Optional: pass a base32 TOTP secret so MFA codes can be generated automatically on background token refresh
ra-cli login-openreview --totp-secret <YOUR_BASE32_TOTP_SECRET>

# Check current OpenReview authentication status
ra-cli login-openreview --status

# Log out and delete ~/.research-agent/openreview_auth.json
ra-cli login-openreview --logout
```




