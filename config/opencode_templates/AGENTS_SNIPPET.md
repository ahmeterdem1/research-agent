<!-- RESEARCH_AGENT_START -->
## Research Agent Extension (`ra_docs`, `ra_search`, `ra_ingest`, `ra_kg`)

When any of the Research Agent MCP servers are enabled in the current OpenCode project:

- **Dual-Scope Knowledge Graph & Proactive User Memory (`ra_kg_*`)**:
  - **Session Start Rule**: At the beginning of a conversation (or before answering personalized research questions), call `ra_kg_get_user_profile_summary(scope="all")` to load both the Global User Profile (`[GLOBAL]`) and the Current Project Profile (`[PROJECT: <name>]`).
  - **Proactive Every-Turn Memory Rule (DO NOT WAIT TO BE ASKED)**: Whenever the user mentions any personal or research context during normal conversation, AUTOMATICALLY call `ra_kg_remember_user_fact(category, fact, scope=..., context=...)` in the same turn:
    * `category="background"` (`has_fact`, `scope="global"`): User's academic role, thesis, expertise, or background.
    * `category="interest"` (`interested_in`, `scope="global"`): Research topics or domains the user cares about or asks to explore.
    * `category="preference"` (`prefers`, `scope="global"` or `"project"`): Preferred explanation depth, math rigor, tools, or workflows.
    * `category="argument"` (`argued_that`, `scope="project"`): Hypotheses, claims, or opinions proposed by the user.
    * `category="mistake"` (`made_mistake`, `scope="global"`): Conceptual misconceptions clarified during the discussion.
    * `category="read_paper"` (`has_read`, `scope="global"`): Papers the user mentions having read or finishes discussing in depth.
  - For literature-level connections (`Paper --covers--> Concept`, `Paper --references--> Paper`, `Topic --related_to--> Topic`), call `ra_kg_add_triplet` (`object_class` and `subject_class` are auto-inferred from `relation`). Use `ra_kg_update_triplet` or `ra_kg_retract_triplet` when the user revises a claim or corrects a recorded fact.
- **Virtual Document Tree (`ra_docs_*`)**:
  - Always call `ra_docs_search_library(query, scope="all")` to check both the Global VTree and Project VTree before fetching academic papers from the web.
- **User-Gated Paper Ingestion & Library Refresh (`ra_ingest_*`)**:
  - Never call `ra_ingest_confirm_save_paper` without first calling `ra_ingest_stage_paper`, presenting the summary to the user, and receiving explicit user confirmation.
  - **Live Progress for Multi-Paper Staging**: Each `ra_ingest_stage_paper` call downloads a PDF, runs page-by-page OCR/Markdown extraction, and summarizes the paper (~10–20s per paper). When staging multiple papers (e.g., 2 to 10+ papers), NEVER emit all `ra_ingest_stage_paper` calls in a single parallel batch turn (which freezes the OpenCode chat UI with no intermediate feedback). Always stage them **ONE PAPER PER TURN sequentially**, outputting a visible progress line in chat before each call (e.g., `**[1/10] Staging "Paper Title" (2405.07987) -> /target/dir...**`) so the user sees live progress after every paper. Once all papers are staged, present a consolidated summary table and ask for permission to call `ra_ingest_confirm_save_paper`.
  - When the user adds new PDFs/documents to their source library directory on disk, call `ra_ingest_refresh_library` to incrementally index only the unindexed files into `vtree.json` without rebuilding the existing tree.
<!-- RESEARCH_AGENT_END -->

