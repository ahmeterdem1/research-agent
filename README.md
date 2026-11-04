# Research Agent — System-Wide OpenCode Extension

A local-first academic and technical research extension for [**OpenCode**](https://github.com/anomalyco/opencode) (`opencode` TUI, `opencode web`, and OpenCode Desktop). 

## Research Agent Proposes You:

- Personalized context that is both project specific and global through a knowledge graph
  - Agent manages a knowledge graph on the user
  - Saves users interests
  - Saves users arguments
  - Saves users mistakes
  - Agent can address your arguments across chats
- A paper management pipeline completely offloaded to the agent
  - Agent finds the papers
  - Agent installs the pdfs 
  - No captchas can stop the agent
- A *virtual* filesystem
  - Actual papers are saved in a flat directory
  - Agent puts the papers on directories in a *virtual filesystem*
  - Agent can call unix-like tools on this virtual filesystem
  - Agent can organize the papers however it likes, without touching your actual pdfs

### Already have some papers?

- Research Agent has a cold start pipeline
  - Provide the root of the directory
  - Cold start generates markdowns and summaries from pdfs
  - Builds the virtual filesystem
  - Organizes the virtual filesystem

Installed once system-wide on your PC, it equips OpenCode with:

1. **Dual-Scope Virtual Document Tree (`ra_docs`)**: A decoupled semantic hierarchy (`vtree.json`) over your PDF/Markdown/DOCX textbooks and papers—supporting both a **PC-wide Global Library** (`~/.research-agent/vtrees/global.json`) and optional **Project-Local Libraries** (`<project>/.opencode/vtree.json`).
2. **Academic & Web Literature Search (`ra_search`)**: Free DuckDuckGo web search, Semantic Scholar paper search & metadata, arXiv PDF extraction, and forward/backward citation graph edges.
3. **Autonomous Citation Walker Subagent (`@citation-walker`)**: An OpenCode subagent running in an isolated context window that traverses backward references and forward citations over multiple hops and returns a curated bibliography.
4. **User-Gated Paper Ingestion & Incremental Library Refresh (`ra_ingest`)**: Two-phase staging (`stage_paper` -> `confirm_save_paper`) protected by OpenCode's native `"ask"` permission gate so no paper is ever saved to your library without your explicit confirmation, plus `refresh_library` (`ra-cli refresh`) to incrementally index newly dropped local PDFs/documents into your existing `vtree.json` without losing semantic folder organization.
5. **Dual-Scope Temporal Knowledge Graph (`ra_kg`)**: An embedded SQLite knowledge graph (`~/.research-agent/research_agent.db`) with TTL expiry, soft-retraction history, extendable ontology, and strict separation between **`[GLOBAL]` user facts** (background, `has_read` reading history, enduring interests, foundational mistakes) and **`[PROJECT: <id>]` workspace facts** (project-specific hypotheses, arguments, and tool preferences).
6. **Tool & Subagent Telemetry (`ra-cli stats`)**: Automatic SQLite instrumentation recording call volume, soft error rates, latency (`ms`), scope distribution, and behavioral compliance checks across all MCP tools.

---

## Quick Start

### 1. Install Globally with `uv`
Install the package system-wide so all CLI and MCP binaries (`ra-cli`, `ra-mcp-docs`, `ra-mcp-search`, `ra-mcp-ingest`, `ra-mcp-kg`) are available on your `$PATH`:

```bash
# Standalone system-wide installation from GitHub (recommended for general use)
uv tool install git+https://github.com/ahmeterdem1/research-agent.git

# Or editable installation from a local development checkout
uv tool install -e .

# Upgrade your system-wide installation anytime from GitHub (leaves local dev checkouts untouched)
ra-cli upgrade
```

### 2. Configure Your Global API Key
Place your OpenRouter API key (and optional default library path) in `~/.research-agent/.env`:

```bash
mkdir -p ~/.research-agent
cat << 'EOF' > ~/.research-agent/.env
OPENROUTER_API_KEY=sk-or-v1-...
PATH_TO_TREE=/Users/yourname/Documents/Papers
DEFAULT_SUMMARIZATION_MODEL=openrouter/deepseek/deepseek-chat
EXTRACTOR_MODE=pymupdf
EOF
```

### 3. Register with OpenCode
Run the installer once to merge the MCP servers, permission gates, agent profiles (`researcher`, `@citation-walker`), and global `AGENTS.md` instructions into `~/.config/opencode/`:

```bash
ra-cli install-opencode
```
*(By default, the 4 MCP servers are registered with `"enabled": false` globally so unrelated coding projects have zero tool-schema overhead until you enable them in a project).*

### 4. Index or Incrementally Refresh Your Research Library
Build your Global Virtual Tree (`~/.research-agent/vtrees/global.json`) or a Project-Local Virtual Tree (`.opencode/vtree.json`), or incrementally index newly dropped PDFs into an existing tree:

```bash
# Build the PC-wide Global VTree (initial cold start)
ra-cli index --library-path ~/Documents/Papers --scope global

# Or build a Project-Local VTree inside the current project
ra-cli index --library-path ./papers --scope project

# Preview how many documents/PDF pages will be parsed (and estimated LLM calls) before spending API credits
ra-cli refresh --scope global --dry-run

# Incrementally index ONLY newly dropped files and heal stub summaries in an existing VTree
ra-cli refresh --scope global
ra-cli refresh --scope global --target-dir /Inbox
```

### 5. Enable Modules in Any OpenCode Project
Inside any project folder on your PC, toggle all or specific Research Agent MCP servers on or off:

```bash
# Enable all 4 MCP servers (ra_docs, ra_search, ra_ingest, ra_kg) in the current project
ra-cli project --enable-all

# Or toggle individual servers granularly
ra-cli project --docs on --kg on --search off --ingest off
```

Then launch `opencode` (or `opencode web`) in that directory and press `Tab` to switch to the **`researcher`** agent (or stay in `build`/`plan` mode—all enabled `ra_*` tools are available).

---

## Global vs. Project-Local Separation

| Layer | Global Scope (`~/.research-agent/`) | Project Scope (`<project>/`) |
|---|---|---|
| **Knowledge Graph (`ra_kg`)** | `scope="global"`: Enduring user background (`has_fact`), `has_read` paper history, long-term `interested_in` topics, general `prefers`, and objective literature links (`Paper --covers--> Concept`). | `scope="project"`: Workspace-bound hypotheses (`argued_that`), repo-specific tool/method `prefers`, and narrow project topics. Automatically keyed by `project_id` (`Path.cwd().name` or `$RA_PROJECT_ID`). |
| **Virtual Tree (`ra_docs`)** | `scope="global"`: Queries `~/.research-agent/vtrees/global.json`. | `scope="project"`: Queries `<project>/.opencode/vtree.json`. (`scope="all"` searches both simultaneously). |
| **Paper Ingestion (`ra_ingest`)** | `target_scope="global"`: Saves confirmed papers into the Global VTree. | `target_scope="project"`: Saves confirmed papers into the current project's `.opencode/vtree.json`. |
| **Extracted Text Cache** | Shared globally at `~/.research-agent/cache/texts/<sha256>.md` so identical PDFs are never extracted twice across projects. | Automatically reuses the global SHA-256 text cache. |

---

## Monitoring, Testing & Debugging (`ra-cli status`, `ra-cli stats`, `ra-cli extract`)

```bash
# View Global & Project VTree document counts, KG triplet counts, and active MCP toggles
ra-cli status

# View detailed MCP tool & subagent telemetry, soft error rates, latency, and behavioral checks
ra-cli stats
ra-cli stats --all-projects --days 7 --recent 20

# Manually test PDF-to-Markdown extraction on a local PDF, arXiv ID, or URL
ra-cli extract /path/to/paper.pdf -o ./paper.md
ra-cli extract 2405.07987 --mode pymupdf
```

---

## Documentation Index

* **[Complete MCP Tools & Subagents Reference (`docs/TOOLS_REFERENCE.md`)](./docs/TOOLS_REFERENCE.md)** — Detailed signatures, parameters, return formats, and behavioral rules for all 26 tools across `ra_docs`, `ra_search`, `ra_ingest`, `ra_kg`, and the cold-start organizer, plus the `researcher` and `@citation-walker` agent profiles.
* **[CLI, Telemetry & Configuration Reference (`docs/CLI_AND_CONFIG.md`)](./docs/CLI_AND_CONFIG.md)** — Complete guide to `ra-cli` commands, environment variables, `.env` precedence, OpenCode configuration merging, and the cold-start pipeline stages.

---

## Running the Test Suite

```bash
uv run pytest tests/ -v
```

---

## Acknowledgements

* The Vision LLM document-to-Markdown extraction prompt (`[extractor].llm_ocr_prompt` in [`config/prompts.toml`](./config/prompts.toml), used when `EXTRACTOR_MODE=llm`) is adapted from [**Dr. DocBench**](https://arxiv.org/abs/2606.01393).
