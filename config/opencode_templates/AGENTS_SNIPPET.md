<!-- RESEARCH_AGENT_START -->
## Research Agent Extension (`ra_docs`, `ra_search`, `ra_ingest`, `ra_kg`)

When any of the Research Agent MCP servers are enabled in the current OpenCode project:

- **Dual-Scope Knowledge Graph (`ra_kg_*`)**:
  - The user's Knowledge Graph stores facts in two distinct scopes:
    1. `scope="global"` (`[GLOBAL]`): Permanent, cross-project information about the user (background via `has_fact`, papers read via `has_read`, long-term `interested_in` topics, general `prefers`, and foundational `made_mistake` items) as well as objective literature facts (`Paper --covers--> Concept`, `Topic --related_to--> Topic`).
    2. `scope="project"` (`[PROJECT: <name>]`): Information specific to the active project/workspace (local hypotheses or claims via `argued_that`, repo-specific tool/method `prefers`, and narrow project topics).
  - Call `ra_kg_get_user_profile_summary(scope="all")` or `ra_kg_query_kg(scope="all")` to view both global and current-project facts in clearly separated sections.
  - When recording new facts with `ra_kg_add_triplet`, always choose `scope="global"` vs. `scope="project"` deliberately. Use `ra_kg_update_triplet(triplet_id, scope="global" | "project")` to promote or restrict a fact's scope when requested.
- **Virtual Document Tree (`ra_docs_*`)**:
  - Always call `ra_docs_search_library(query, scope="all")` to check both the Global VTree and Project VTree before fetching academic papers from the web.
- **User-Gated Paper Ingestion & Library Refresh (`ra_ingest_*`)**:
  - Never call `ra_ingest_confirm_save_paper` without first calling `ra_ingest_stage_paper`, presenting the summary to the user, and receiving explicit user confirmation.
  - When the user adds new PDFs/documents to their source library directory on disk, call `ra_ingest_refresh_library` to incrementally index only the unindexed files into `vtree.json` without rebuilding the existing tree.
<!-- RESEARCH_AGENT_END -->

