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
> Every tool across all 4 servers is wrapped with `@track_tool("<server_name>")` from [src/telemetry/store.py](file:///Users/ahmeterdem/Desktop/code/research%20agent/src/telemetry/store.py), which automatically logs execution latency (`duration_ms`), target `scope`, active `project_id`, and whether the returned text was a success (`ok`) or soft error (`error`) to `~/.research-agent/research_agent.db`.

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
* **Purpose**: Primary anti-duplication guard and fast in-memory keyword scanner across every node (`document`, `book`, `directory`) in the Virtual Tree.
* **Matching Logic**: Case-insensitive substring match against `name`, `doi`, and LLM-generated `summary`.
* **Parameters**:
  * `query`: Keyword, paper title fragment, author/topic term, or DOI (e.g., `"attention is all you need"` or `"10.48550/arXiv.1706.03762"`).
  * `scope`:
    * `"all"` (default): Searches the Global VTree (`~/.research-agent/vtrees/global.json`) AND, if present, the active workspace's Project VTree (`.opencode/vtree.json`).
    * `"global"`: Searches only the Global VTree.
    * `"project"`: Searches only the Project VTree.
* **Returns**: Formatted blocks tagged with `[GLOBAL]` or `[PROJECT:<id>]`, `[document|book|directory]`, virtual path, and summary, or `"No local documents found matching query: '<query>'"`.
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
* **Purpose**: Searches Semantic Scholar (`https://api.semanticscholar.org/graph/v1/paper/search`) for academic papers matching `query`.
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
def read_online_document(url_or_arxiv_id: str) -> str
```
* **Purpose**: Fetches and extracts the full text of an online paper or web article:
  * **If given an arXiv ID or arXiv URL** (e.g. `"1706.03762"` or `"arxiv:1706.03762"`): Downloads `https://arxiv.org/pdf/<id>.pdf` to a temporary file, extracts structured Markdown (via Docling or Vision LLM OCR per `EXTRACTOR_MODE`), cleans up the temp PDF, and returns the Markdown.
  * **If given a standard HTTP/HTTPS URL**: Fetches the HTML and extracts clean text via `BeautifulSoup`.
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
    preextracted_text: Optional[str] = None,
    precomputed_summary: Optional[str] = None,
) -> str
```
* **Purpose**: Phase 1 of saving a paper. Generates a UUID `temp_id`, caches any extracted text in `~/.research-agent/cache/texts/<sha256>.md`, and writes a persistent staging manifest to `~/.research-agent/cache/staged/<temp_id>.json`.
* **Parameters**:
  * `url_or_arxiv_id`: Source URL or arXiv ID.
  * `target_virtual_dir`: Destination directory in the VTree (e.g. `"/Reinforcement Learning/Offline RL"`). Intermediate directories are created automatically on confirmation.
  * `target_scope`: `"global"` (default — targets `~/.research-agent/vtrees/global.json`) or `"project"` (targets `<project>/.opencode/vtree.json`).
  * `title`: Clean display title for the VTree node.
  * `preextracted_text` / `precomputed_summary`: Optional text and summary if the agent already read the paper in the current turn.
* **Returns**: Staging confirmation with `ID: <temp_id>`, target scope/path, summary, and the instruction to **STOP EXECUTION NOW** and ask the user for approval.

### 3.2 `confirm_save_paper`
```python
def confirm_save_paper(temp_id: str) -> str
```
* **Purpose**: Phase 2 of saving a paper. Protected by `"ra_ingest_confirm_save_paper": "ask"` in OpenCode's permission engine.
* **Behavior**:
  1. Pops the staged manifest from `~/.research-agent/cache/staged/<temp_id>.json`.
  2. Loads (or initializes) the target `vtree.json` (`global` or `project`).
  3. Inserts the `VDocument` node into `target_virtual_dir` via `VTreeManager.add_document()` and marks all ancestor directories with `_needs_summary_update = True`.
  4. Atomically writes the updated `vtree.json` to disk.

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

### 4.1 `add_triplet`
```python
def add_triplet(
    subject: str,
    relation: str,
    object: str,
    object_class: str,
    subject_class: str = "User",
    scope: Literal["global", "project"] = "global",
    confidence: float = 1.0,
    ttl_seconds: Optional[int] = None,
    context: Optional[str] = None,
    object_properties: Optional[dict] = None,
) -> str
```
* **Purpose**: Asserts a fact `(subject, subject_class) --relation--> (object, object_class)` in either `global` or `project` scope.
* **Deduplication & Refresh**: Entity and ontology names are case-insensitive (`COLLATE NOCASE`). Re-asserting an existing live triplet within the same `(scope, project_id)` refreshes its `confidence`, `context`, `ttl_seconds`, and `updated_at` in place rather than creating a duplicate.
* **Validation**: Enforces that `subject_class`, `object_class`, and `relation` exist in the ontology and satisfy the relation's `domain` and `range`. Returns actionable `"Error: ..."` messages listing valid alternatives when violated.

### 4.2 `query_kg`
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
* **Purpose**: Pattern-matches facts across the Knowledge Graph.
* **Scope Isolation**:
  * `scope="all"` (default): Returns all matching `[GLOBAL]` facts PLUS matching `[PROJECT: <active_project_id>]` facts, while automatically excluding facts local to *other* projects.
  * `scope="global"`: Returns only global facts.
  * `scope="project"`: Returns only facts for the active project.
* **Temporal Liveness**: By default (`include_inactive=False`), only returns facts where `is_active = 1` and `expires_at` has not passed. Setting `include_inactive=True` includes expired and retracted facts with their `INACTIVE (<reason>)` badge.

### 4.3 `update_triplet`
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

### 4.4 `retract_triplet`
```python
def retract_triplet(triplet_id: int, reason: str) -> str
```
* **Purpose**: Soft-deletes a triplet (`is_active = 0`, `invalidated_reason = reason`), removing it from active queries and the user profile while preserving it in historical queries (`include_inactive=True`).

### 4.5 `get_user_profile_summary`
```python
def get_user_profile_summary(
    scope: Literal["all", "global", "project"] = "all",
) -> str
```
* **Purpose**: Reaps any expired triplets and returns a structured Markdown summary of all live `User` facts, separated into:
  1. `# Global User Profile (Applies Across All Projects)`
  2. `# Current Project Profile (project: <active_project_id>)`

### 4.6 `search_entities`
```python
def search_entities(
    query: str,
    entity_class: Optional[str] = None,
    limit: int = 20,
) -> str
```
* **Purpose**: Case-insensitive substring search (`LIKE %query%`) over entity names so the agent can discover exact canonical names before calling `query_kg` or `add_triplet`.

### 4.7 `get_related_facts`
```python
def get_related_facts(
    entity: str,
    depth: int = 2,
    scope: Literal["all", "global", "project"] = "all",
) -> str
```
* **Purpose**: Breadth-first graph neighbourhood traversal (up to 3 hops, walking edges in both directions) starting from `entity`, respecting liveness and `scope` isolation.

### 4.8 `get_ontology`
```python
def get_ontology() -> str
```
* **Purpose**: Lists all registered ontology classes and relations (with their `domain -> range` constraints).

### 4.9 `extend_ontology`
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

