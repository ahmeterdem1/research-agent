# Research Agent — Complete MCP Tools & Subagents Reference

This document catalogues every MCP server, tool function, argument signature, return format, and behavioral rule in the Research Agent extension.

---

## Overview of MCP Servers

| OpenCode Key | Binary Entrypoint | Source Module | Purpose |
|---|---|---|---|
| `ra_docs` | `ra-mcp-docs` | [src/mcp_servers/document_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/document_tools.py) | Navigate, search, and read documents from the Global and Project-Local Virtual Trees (`vtree.json`). |
| `ra_search` | `ra-mcp-search` | [src/mcp_servers/search_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/search_tools.py) | Search DuckDuckGo and Semantic Scholar, fetch paper metadata/abstracts, read online/arXiv documents, and traverse citation edges. |
| `ra_ingest` | `ra-mcp-ingest` | [src/mcp_servers/file_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/file_tools.py) | Stage downloaded papers and save them into the Global or Project VTree behind a strict user confirmation gate. |
| `ra_kg` | `ra-mcp-kg` | [src/mcp_servers/kg_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/kg_tools.py) | Read and write facts in the dual-scope (`global` + `project`) SQLite Temporal Knowledge Graph. |

> [!NOTE]
> * **Telemetry**: Every tool across all 4 servers is wrapped with `@track_tool("<server_name>")` from [src/telemetry/store.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/telemetry/store.py), which automatically logs execution latency (`duration_ms`), target `scope`, active `project_id`, and whether the returned text was a success (`ok`) or soft error (`error`) to `~/.research-agent/research_agent.db`.
> * **Live Progress & Timing**: Long-running tools (`read_online_document`, `stage_paper`, `confirm_save_paper`, and `refresh_library`) stream live MCP progress notifications (`ctx.report_progress` / `ctx.info` via [src/mcp_servers/progress.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/progress.py)) and append an `Execution Progress & Timing` summary table so OpenCode users see step-by-step progress in real time.

---

## 1. `ra_docs` — Virtual Tree Document Tools (`DocumentTools`)

Source: [src/mcp_servers/document_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/document_tools.py)  
Backed by: [src/vtree/store.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/vtree/store.py) & [src/vtree/operations.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/vtree/operations.py)

### 1.1 `search_library`
```python
def search_library(
    query: str,
    scope: Literal["all", "global", "project"] = "all",
) -> str
```
* **Purpose**: Primary anti-duplication guard and fast in-memory Lucene / BM25-style ranked search engine across every node (`document`, `book`, `directory`) in the Virtual Tree.
* **Matching & Ranking Logic**:
  * **Tokenized & Stemmed Ranking**: Splits multi-word queries into tokens, strips stop-words, applies morphological suffix stemming (`representations` $\leftrightarrow$ `representation`), and scores nodes across `name`/`title` (weight `3.0`), `virtual_path` (`2.0`), `doi` (`4.0`), and `summary` (`1.0`) with an exact-phrase bonus (`+5.0`).
  * **Partial-Token Recall**: Queries with extra descriptive keywords (e.g. `"platonic representation hypothesis convergence"`) still return `"Position The Platonic Representation Hypothesis"` ranked by match ratio (`matched 3/4 terms`) instead of requiring an exact contiguous substring.
  * **Supported Lucene Syntax**:
    * Quoted exact phrases: `"platonic representation"`
    * Required terms: `+convergence`
    * Excluded terms: `-survey` or `NOT survey`
    * Boolean `OR` / `AND`
    * Field-scoped filters: `title:platonic`, `path:vision`, `summary:alignment`, `doi:10.48550`
* **Parameters**:
  * `query`: Keyword(s), Lucene query, paper title fragment, author/topic term, or DOI.
  * `scope`:
    * `"all"` (default): Searches the Global VTree (`~/.research-agent/vtrees/global.json`) AND, if present, the active workspace's Project VTree (`.opencode/vtree.json`).
    * `"global"`: Searches only the Global VTree.
    * `"project"`: Searches only the Project VTree.
* **Returns**: Ranked blocks tagged with `[GLOBAL]` or `[PROJECT:<id>]`, `[document|book|directory]`, match ratio (`matched K/N terms`), virtual path, and summary, or `"No local documents found matching query: '<query>'"`.
* **Agent Rule**: **MUST** be called before calling `read_online_document` or `stage_paper` to avoid re-downloading papers already in the local library under arbitrary filenames.

### 1.2 `list_tree`
```python
def list_tree(
    virtual_path: str = "/",
    include_summaries: bool = False,
    max_depth: int = 3,
    scope: Literal["all", "global", "project"] = "all",
) -> str
```
* **Purpose**: Renders a hierarchical indentation tree of virtual directories, books, and documents starting from `virtual_path` up to `max_depth`.
* **Parameters**:
  * `virtual_path`: Starting virtual directory path (default `"/"`).
  * `include_summaries`: When `True`, appends each node's 2–4 sentence LLM summary inline.
  * `max_depth`: Maximum recursion depth (default `3`).
  * `scope`: `"all"` (renders both Global and Project trees if both exist), `"global"`, or `"project"`.

### 1.3 `get_dir_summary`
```python
def get_dir_summary(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str
```
* **Purpose**: Returns the bottom-up synthesized overview summary of a specific virtual directory (e.g., `"/Transformer Architectures"`).

### 1.4 `list_doc_summaries`
```python
def list_doc_summaries(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str
```
* **Purpose**: Lists the names and summaries of all immediate children inside a virtual directory or book node (`- **<name>**: <summary>`). Works on both `"directory"` nodes and multi-section `"book"` nodes.

### 1.5 `get_document`
```python
def get_document(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str
```
* **Purpose**: Reads and returns the full extracted Markdown text (headings, paragraphs, LaTeX math, HTML tables) of a document or book section from its cached `text_path` (`~/.research-agent/cache/texts/<sha256>.md`).

### 1.6 `get_documents`
```python
def get_documents(
    virtual_paths: List[str],
    scope: Literal["global", "project"] = "global",
) -> str
```
* **Purpose**: Batch-retrieves the full Markdown text of multiple virtual paths in a single tool call, separated by `--- Document: <vpath> (<scope>) ---` headers.

### 1.7 `get_document_real_path`
```python
def get_document_real_path(
    virtual_path: str,
    scope: Literal["global", "project"] = "global",
) -> str
```
* **Purpose**: Resolves a virtual path in the VTree back to its underlying physical PDF/DOCX/MD file path on the user's filesystem (`real_path`) so the user can open the original file.

---

## 2. `ra_search` — Web, Academic & Citation Graph Tools (`SearchTools`)

Source: [src/mcp_servers/search_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/search_tools.py) & [src/subagents/citation_walker.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/subagents/citation_walker.py)

> [!TIP]
> All Semantic Scholar endpoints use direct `requests.get(..., timeout=10)` calls with explicit `HTTP 429` rate-limit handling rather than the `semanticscholar` Python wrapper's async loop, preventing the agent from hanging on rate limits.

### 2.1 `duckduckgo_search`
```python
def duckduckgo_search(query: str, num_results: int = 5) -> str
```
* **Purpose**: Free general web search using DuckDuckGo (`DDGS().text()`).
* **Returns**: Formatted blocks with `Title`, `URL`, and `Snippet`.

### 2.2 `scholar_search`
```python
def scholar_search(query: str, num_results: int = 5) -> str
```
* **Purpose**: Searches Semantic Scholar (`https://api.semanticscholar.org/graph/v1/paper/search`) for academic papers matching `query`. Automatically sanitizes boolean operators (`AND`, `OR`, `NOT`), field prefixes (`author:`, `year:`, `venue:`), and quotes so the Semantic Scholar relevance endpoint never returns zero results due to Lucene syntax.
* **Returns**: Formatted blocks containing `Title`, `Paper ID`, `DOI`, `Authors`, `Year | Citations`, and `URL`.

### 2.3 `get_paper_details`
```python
def get_paper_details(paper_id_or_doi: str) -> str
```
* **Purpose**: Retrieves comprehensive metadata for a single paper from Semantic Scholar.
* **Parameters**:
  * `paper_id_or_doi`: Either a 40-char Semantic Scholar `paperId` or a DOI (e.g., `"10.48550/arXiv.1706.03762"` — automatically prefixed with `DOI:` when `/` is present).
* **Returns**: `Title`, `Authors`, `Venue (Year)`, `Citations`, `URL`, Semantic Scholar `TLDR`, and full `Abstract`.

### 2.4 `get_paper_references`
```python
def get_paper_references(paper_id_or_doi: str, limit: int = 10) -> str
```
* **Purpose**: **Backward citation traversal** — fetches papers cited *by* `paper_id_or_doi` (foundational/prior work).
* **Returns**: Bulleted list with `Paper ID`, `Title`, `Year`, and `Citations` for up to `limit` references.

### 2.5 `get_paper_citations`
```python
def get_paper_citations(paper_id_or_doi: str, limit: int = 10) -> str
```
* **Purpose**: **Forward citation traversal** — fetches newer papers that *cite* `paper_id_or_doi` (follow-up/state-of-the-art work).
* **Returns**: Bulleted list with `Paper ID`, `Title`, `Year`, and `Citations` for up to `limit` citing papers.

### 2.6 `read_online_document`
```python
def read_online_document(
    url_or_arxiv_id: str,
    ctx: Optional[Context] = None,
) -> str
```
* **Purpose**: Fetches and extracts the full text of an online paper or web article while streaming live MCP progress updates:
  * **If given an arXiv ID or arXiv URL** (e.g. `"1706.03762"` or `"arxiv:1706.03762"`): Downloads `https://arxiv.org/pdf/<id>.pdf` to a temporary file, extracts structured Markdown via `EXTRACTOR_MODE` (`pymupdf` by default for instant zero-cost local extraction, or `docling` / `llm`), caches the result under `~/.research-agent/cache/online_docs/arxiv_<id>.md`, cleans up the temp PDF, and returns the Markdown.
  * **If given a standard HTTP/HTTPS URL**: Fetches the HTML, extracts clean text via `BeautifulSoup`, and caches it under `~/.research-agent/cache/online_docs/url_<sha256>.md`.
* **Zero-Cost Staging Reuse**: Any document read via `read_online_document` is automatically reused from `~/.research-agent/cache/online_docs/` if the user subsequently asks to save it via `stage_paper`, avoiding any duplicate download or PDF re-extraction.
* **Agent Rule**: Must only be called *after* `ra_docs_search_library` confirms the paper is not already stored locally.

### 2.7 `walk_citation_graph`
```python
def walk_citation_graph(start_doi_or_id: str, depth: int = 2) -> str
```
* **Purpose**: Convenience 1-hop summary tool that fetches both backward references and forward citations for `start_doi_or_id` in one call. For deep multi-hop exploration with abstract filtering, the primary agent delegates to the `@citation-walker` subagent.

---

## 3. `ra_ingest` — User-Gated Paper Ingestion Tools (`FileTools`)

Source: [src/mcp_servers/file_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/file_tools.py)

### 3.1 `stage_paper`
```python
def stage_paper(
    url_or_arxiv_id: str,
    target_virtual_dir: str,
    target_scope: Literal["global", "project"] = "global",
    title: Optional[str] = None,
    doi: Optional[str] = None,
    preextracted_text: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str
```
* **Purpose**: Phase 1 of saving a paper. Runs a **4-Tier Content & PDF Resolution Cascade** (0. Instant reuse from `preextracted_text` or `~/.research-agent/cache/online_docs/`, 1. Direct arXiv PDF, 2. Academic API lookup via arXiv Search with Lucene special-character escaping / OpenAlex / Semantic Scholar by `title` or `doi`, 3. Direct HTTP PDF / `<meta name="citation_pdf_url">`, 4. Structured Metadata + Abstract + TLDR fallback when behind a captcha/paywall), caches the extracted Markdown in `~/.research-agent/cache/texts/<sha256>.md`, stages any downloaded PDF in `~/.research-agent/cache/staged/<temp_id>.pdf`, and summarizes the document using `summarize_text()` (`DEFAULT_SUMMARIZATION_MODEL` on OpenRouter).
* **Live Progress & Sequential Batching**: Emits step-by-step MCP progress notifications (`[1/4] -> [4/4]`) and appends an `Execution Progress & Timing` table. When staging multiple papers, agents are instructed to call `stage_paper` sequentially one paper at a time and output a `[k/N]` status line between calls.
* **Parameters**:
  * `url_or_arxiv_id`: Source URL, arXiv ID (e.g. `"2601.18533"`), or DOI.
  * `target_virtual_dir`: Destination directory in the VTree (e.g. `"/nlp/RLHF/RLVRR"`). Intermediate directories are created automatically on confirmation.
  * `target_scope`: `"global"` (default — targets `~/.research-agent/vtrees/global.json`) or `"project"` (targets `<project>/.opencode/vtree.json`).
  * `title`: Clean paper title (strongly recommended when `url_or_arxiv_id` is a conference landing page like `iclr.cc` or `openreview.net` so Tier 2 can locate the open-access PDF).
  * `doi`: Optional DOI of the paper.
  * `preextracted_text`: Optional Markdown text already extracted earlier in the session to bypass re-downloading.
* **Returns**: Staging confirmation with `ID: <temp_id>`, resolution tier, target scope/path, the OpenRouter-generated summary, `Execution Progress & Timing` breakdown, and the instruction to **STOP EXECUTION NOW** and ask the user for approval.

### 3.2 `confirm_save_paper`
```python
def confirm_save_paper(
    temp_id: str,
    ctx: Optional[Context] = None,
) -> str
```
* **Purpose**: Phase 2 of saving a paper. Protected by `"ra_ingest_confirm_save_paper": "ask"` in OpenCode's permission engine.
* **Behavior**:
  1. Pops the staged manifest from `~/.research-agent/cache/staged/<temp_id>.json`.
  2. Moves any staged PDF (`~/.research-agent/cache/staged/<temp_id>.pdf`) into a single flat folder at `<library_root>/Downloaded/<title>.pdf` and points `real_path` to that file.
  3. Loads (or initializes) the target `vtree.json` (`global` or `project`), inserts the `VDocument` node into `target_virtual_dir`, and refreshes all newly created or modified ancestor directory summaries via OpenRouter (`summarize_directory()`), reporting step progress per directory.
  4. Atomically writes the updated `vtree.json` to disk.

### 3.3 `refresh_library`
```python
def refresh_library(
    scope: Literal["global", "project"] = "global",
    library_path: Optional[str] = None,
    target_virtual_dir: Optional[str] = None,
    update_dir_summaries: bool = True,
    ctx: Optional[Context] = None,
) -> str
```
* **Purpose**: Incrementally scans the source library folder on disk and indexes **only newly dropped** PDF, TXT, MD, or DOCX files that are not yet present in the target VTree (`global` or `project`).
* **Preserves Existing Organization**: Checks all existing `document` and `book` nodes by `real_path` across the entire `vtree.json`—even if those nodes were previously moved into semantic virtual folders by `organize_vtree` or `mv`. Already-indexed files are skipped; existing nodes and folders are untouched.
* **Parameters**:
  * `scope`: `"global"` (default — updates `~/.research-agent/vtrees/global.json`) or `"project"` (updates `<project>/.opencode/vtree.json`).
  * `library_path`: Optional directory on disk to scan. Defaults to the `library_root` stored inside `vtree.json` (or `LIBRARY_ROOT_PATH`).
  * `target_virtual_dir`: Optional destination folder in the VTree for newly discovered files (e.g. `"/Inbox"`). If omitted, mirrors their relative subfolder path under `library_root`.
  * `update_dir_summaries`: Recomputes summaries for ancestor directories modified by newly added nodes (default `True`).
* **Returns**: A summary report with total scanned, already-indexed (skipped), and newly added documents along with their virtual paths and summaries.

---

## 4. `ra_kg` — Dual-Scope Temporal Knowledge Graph Tools (`KnowledgeGraphTools`)

Source: [src/mcp_servers/kg_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/kg_tools.py)  
Backed by: [src/kg/schema.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/kg/schema.py), [src/kg/store.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/kg/store.py), [src/kg/reasoning.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/kg/reasoning.py)

### Base Ontology (Seeded Automatically)

**Classes (`ontology_classes`)**:
| Class | Description |
|---|---|
| `User` | The human user of the system (canonical entity name: `"User"`). |
| `Topic` | A research area or specific scientific discipline. |
| `Argument` | A specific claim or hypothesis proposed or defended by the user. |
| `Mistake` | A factual misconception or invalid premise held by the user. |
| `Preference` | A stated preference (tools, methods, frameworks, writing style). |
| `PersonalFact` | Personal background, research role, institution, or goals. |
| `Paper` | A specific research paper, preprint, book, or publication. |
| `Concept` | A technical term, equation, algorithm, or methodology. |

**Relations (`ontology_relations`)**:
| Relation | Domain -> Range | Recommended Scope | Description |
|---|---|---|---|
| `has_read` | `User -> Paper` | `global` | User has read or is familiar with this paper. |
| `has_fact` | `User -> PersonalFact` | `global` | Personal background, role, or expertise. |
| `interested_in` | `User -> Topic` | `global` or `project` | Long-term interest (`global`) or current project topic (`project`). |
| `prefers` | `User -> Preference` | `global` or `project` | General preference (`global`) or repo-specific preference (`project`). |
| `argued_that` | `User -> Argument` | `project` or `global` | Working project hypothesis (`project`) or general position (`global`). |
| `made_mistake` | `User -> Mistake` | `global` or `project` | Factual error or misconception held by the user. |
| `related_to` | `Topic -> Topic` | `global` | Topical relatedness or hierarchy. |
| `references` | `Paper -> Paper` | `global` | Direct citation relation between two papers. |
| `covers` | `Paper -> Concept` | `global` | Paper discusses or introduces this concept. |
| `contradicts` | `Argument -> Argument` | `global` or `project` | Two arguments conflict with each other. |
| `corrected_by` | `Mistake -> Argument` | `global` or `project` | Mistake was resolved by this valid insight. |

---

### 4.1 `remember_user_fact`
```python
def remember_user_fact(
    fact: str,
    category: Literal[
        "background",
        "interest",
        "preference",
        "argument",
        "mistake",
        "read_paper",
    ] = "interest",
    scope: Literal["global", "project"] = "global",
    context: Optional[str] = None,
) -> str
```
* **Purpose**: High-priority, single-call tool for proactively persisting user facts during conversation without needing to look up ontology class or relation names.
* **Automatic Category Mapping**:
  * `"background"` $\rightarrow$ `User --has_fact--> PersonalFact`
  * `"interest"` $\rightarrow$ `User --interested_in--> Topic`
  * `"preference"` $\rightarrow$ `User --prefers--> Preference`
  * `"argument"` $\rightarrow$ `User --argued_that--> Argument`
  * `"mistake"` $\rightarrow$ `User --made_mistake--> Mistake`
  * `"read_paper"` $\rightarrow$ `User --has_read--> Paper`
* **Agent Rule**: Automatically called whenever the user reveals their background, research interests, preferences, hypotheses/arguments, misconceptions, or papers they have read.

### 4.2 `add_triplet`
```python
def add_triplet(
    subject: str,
    relation: str,
    object: str,
    object_class: Optional[str] = None,
    subject_class: Optional[str] = None,
    scope: Literal["global", "project"] = "global",
    confidence: float = 1.0,
    ttl_seconds: Optional[int] = None,
    context: Optional[str] = None,
    object_properties: Optional[dict] = None,
) -> str
```
* **Purpose**: Asserts a fact `(subject, subject_class) --relation--> (object, object_class)` in either `global` or `project` scope.
* **Auto-Inferred Classes**: When `object_class` or `subject_class` is omitted (`None`), `KGStore.add_triplet` automatically infers the expected `domain` (`subject_class`) and `range` (`object_class`) from `relation`'s registered ontology definition (defaulting `subject_class` to `"User"` if `subject.lower() == "user"`).
* **Deduplication & Refresh**: Entity and ontology names are case-insensitive (`COLLATE NOCASE`). Re-asserting an existing live triplet within the same `(scope, project_id)` refreshes its `confidence`, `context`, `ttl_seconds`, and `updated_at` in place rather than creating a duplicate.
* **Validation**: Enforces that `subject_class`, `object_class`, and `relation` exist in the ontology and satisfy the relation's `domain` and `range`. Returns actionable `"Error: ..."` messages listing valid alternatives when violated.

### 4.3 `query_kg`
```python
def query_kg(
    subject: Optional[str] = None,
    relation: Optional[str] = None,
    object: Optional[str] = None,
    subject_class: Optional[str] = None,
    object_class: Optional[str] = None,
    scope: Literal["all", "global", "project"] = "all",
    include_inactive: bool = False,
    limit: int = 50,
) -> str
```
* **Purpose**: Pattern-matches facts across the Knowledge Graph. Automatically falls back to substring and multi-token entity matching on `subject` and `object` when an exact entity name match is not found.
* **Scope Isolation**:
  * `scope="all"` (default): Returns all matching `[GLOBAL]` facts PLUS matching `[PROJECT: <active_project_id>]` facts, while automatically excluding facts local to *other* projects.
  * `scope="global"`: Returns only global facts.
  * `scope="project"`: Returns only facts for the active project.
* **Temporal Liveness**: By default (`include_inactive=False`), only returns facts where `is_active = 1` and `expires_at` has not passed. Setting `include_inactive=True` includes expired and retracted facts with their `INACTIVE (<reason>)` badge.

### 4.4 `update_triplet`
```python
def update_triplet(
    triplet_id: int,
    confidence: Optional[float] = None,
    context: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    make_permanent: bool = False,
    scope: Optional[Literal["global", "project"]] = None,
) -> str
```
* **Purpose**: Updates metadata or scope on an existing live triplet (`[#id]`).
* **Scope Promotion / Demotion**: Passing `scope="global"` promotes a project-local fact to permanent global memory (`project_id = NULL`); passing `scope="project"` restricts a global fact to the active project.

### 4.5 `retract_triplet`
```python
def retract_triplet(triplet_id: int, reason: str) -> str
```
* **Purpose**: Soft-deletes a triplet (`is_active = 0`, `invalidated_reason = reason`), removing it from active queries and the user profile while preserving it in historical queries (`include_inactive=True`).

### 4.6 `get_user_profile_summary`
```python
def get_user_profile_summary(
    scope: Literal["all", "global", "project"] = "all",
) -> str
```
* **Purpose**: Reaps any expired triplets and returns a structured Markdown summary of all live `User` facts, separated into:
  1. `# Global User Profile (Applies Across All Projects)`
  2. `# Current Project Profile (project: <active_project_id>)`

### 4.7 `search_entities`
```python
def search_entities(
    query: str,
    entity_class: Optional[str] = None,
    limit: int = 20,
) -> str
```
* **Purpose**: Case-insensitive search over entity names so the agent can discover canonical names before calling `query_kg` or `add_triplet`. Automatically falls back to multi-token matching (`OR` across query words, ranked by token hit count) when an exact contiguous substring match returns no entities.

### 4.8 `get_related_facts`
```python
def get_related_facts(
    entity: str,
    depth: int = 2,
    scope: Literal["all", "global", "project"] = "all",
) -> str
```
* **Purpose**: Breadth-first graph neighbourhood traversal (up to 3 hops, walking edges in both directions) starting from `entity` (with automatic partial/token entity name resolution), respecting liveness and `scope` isolation.

### 4.9 `get_ontology`
```python
def get_ontology() -> str
```
* **Purpose**: Lists all registered ontology classes and relations (with their `domain -> range` constraints).

### 4.10 `extend_ontology`
```python
def extend_ontology(
    kind: Literal["class", "relation"],
    name: str,
    description: str,
    domain: Optional[str] = None,
    range: Optional[str] = None,
) -> str
```
* **Purpose**: Dynamically adds a new PascalCase `class` (e.g., `"Dataset"`) or snake_case `relation` (e.g., `"uses_dataset"`, `domain="Paper"`, `range="Dataset"`) to the ontology when existing terms do not fit.

---

## 5. Internal Cold-Start VTree Organizer Tools

Source: [src/mcp_servers/vtree_organizer_tools.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/mcp_servers/vtree_organizer_tools.py) & [src/cold_start/vtree_organizer.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/cold_start/vtree_organizer.py)

Used internally during Stage 5 of cold-start indexing (`ra-cli index` / `ra-cli organize`) to let the librarian LLM group related documents into semantic subdirectories without changing their physical `real_path` on disk:
* `ls(virtual_path)`: Lists immediate children and truncated summaries.
* `mkdir(virtual_path)`: Creates a new virtual category directory.
* `mv(src_virtual_path, dest_virtual_path)`: Moves a document, book, or directory to a new virtual path and updates descendant `virtual_path` strings recursively.
* `finish()`: Signals that semantic reorganization is complete, triggering bottom-up re-summarization of modified directories.

---

## 6. OpenCode Agent Profiles

Installed into `~/.config/opencode/agents/` by `ra-cli install-opencode`:

1. **`researcher` ([config/opencode_templates/agents/researcher.md](file:///Users/ahmeterdem/Desktop/code/research%20agent/config/opencode_templates/agents/researcher.md))**:
   * `mode: primary` — Switchable via `Tab` in OpenCode.
   * Locks down raw coding tools (`bash`, `edit`, `write` set to `deny`) so the agent interacts purely as a research assistant over `ra_docs_*`, `ra_search_*`, `ra_kg_*`, and `ra_ingest_*`.
2. **`@citation-walker` ([config/opencode_templates/agents/citation-walker.md](file:///Users/ahmeterdem/Desktop/code/research%20agent/config/opencode_templates/agents/citation-walker.md))**:
   * `mode: subagent` — Invoked automatically by `researcher` (or manually via `@citation-walker` in chat) to perform multi-hop forward and backward citation walks in an isolated context window using only `ra_search_*` and `ra_docs_search_library`.

