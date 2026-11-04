"""
Tool & Subagent Usage Telemetry Store and Instrumentation Decorator.

Records every MCP tool call across ``ra_docs``, ``ra_search``, ``ra_ingest``,
and ``ra_kg`` into the ``tool_invocations`` table in ``research_agent.db``.

Why this exists alongside ``opencode stats``:
1. Detects soft errors returned as strings (``"Error: ..."``, ``"Failed to ..."``)
   that OpenCode treats as normal tool completions.
2. Tracks ``scope`` usage (``global`` vs ``project`` vs ``all``) and ``project_id``.
3. Computes behavioral health diagnostics (e.g., whether the agent checks
   ``search_library`` before calling ``read_online_document`` or ``stage_paper``,
   and how often ``@citation-walker`` traversal tools are invoked).
4. Measures per-tool execution latency in milliseconds (``duration_ms``).
"""

from datetime import datetime, timedelta, timezone
import functools
import inspect
import json
from pathlib import Path
import time
from typing import Any, Callable, Optional

from config import settings
from config.settings import get_active_project_id, get_logger
from src.db.connection import connect
from src.db.migrations import init_db
from src.kg.store import TIME_FMT, _utcnow

logger = get_logger(__name__)

_ERROR_PREFIXES = (
    "Error:",
    "Search failed:",
    "Scholar search failed:",
    "Failed to ",
)


def _is_error_response(res: Any) -> bool:
    if not isinstance(res, str):
        return False
    stripped = res.lstrip()
    return any(stripped.startswith(prefix) for prefix in _ERROR_PREFIXES)


def _sanitize_args(bound_args: dict[str, Any], max_str_len: int = 160) -> str:
    cleaned: dict[str, Any] = {}
    for k, v in bound_args.items():
        if isinstance(v, str) and len(v) > max_str_len:
            cleaned[k] = v[:max_str_len] + "...<truncated>"
        else:
            cleaned[k] = v
    try:
        return json.dumps(cleaned, default=str)
    except Exception:
        return "{}"


class TelemetryStore:
    def __init__(
        self,
        db_path: str | Path,
        now_fn: Callable[[], datetime] = _utcnow,
        default_project_id: Optional[str] = None,
    ):
        self.db_path = Path(db_path)
        self._now_fn = now_fn
        self.default_project_id = default_project_id
        init_db(self.db_path)

    def now(self) -> str:
        return self._now_fn().astimezone(timezone.utc).strftime(TIME_FMT)

    def resolve_project_id(self, project_id: Optional[str] = None) -> str:
        return (project_id or self.default_project_id or get_active_project_id()).strip() or "default-project"

    def record_invocation(
        self,
        server_name: str,
        tool_name: str,
        status: str = "ok",
        duration_ms: float = 0.0,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
        args: Optional[dict[str, Any] | str] = None,
        error_msg: Optional[str] = None,
    ) -> int:
        pid = self.resolve_project_id(project_id)
        args_json = args if isinstance(args, str) else _sanitize_args(args or {})
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO tool_invocations "
                "(server_name, tool_name, project_id, scope, status, duration_ms, args_json, error_msg, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server_name,
                    tool_name,
                    pid,
                    scope,
                    status,
                    round(float(duration_ms), 2),
                    args_json,
                    error_msg,
                    self.now(),
                ),
            )
            return cur.lastrowid

    def reset(self, project_id: Optional[str] = None) -> int:
        """Delete recorded tool invocations (all, or filtered to ``project_id``)."""
        with connect(self.db_path) as conn:
            if project_id:
                cur = conn.execute("DELETE FROM tool_invocations WHERE project_id = ?", (project_id,))
            else:
                cur = conn.execute("DELETE FROM tool_invocations")
            return cur.rowcount

    def get_summary(
        self,
        project_id: Optional[str] = None,
        all_projects: bool = False,
        days: Optional[int] = None,
        recent_limit: int = 10,
        errors_only: bool = False,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []

        target_pid = None if all_projects else self.resolve_project_id(project_id)
        if target_pid:
            where.append("project_id = ?")
            params.append(target_pid)

        if days is not None and days > 0:
            cutoff = (self._now_fn().astimezone(timezone.utc) - timedelta(days=days)).strftime(TIME_FMT)
            where.append("created_at >= ?")
            params.append(cutoff)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        with connect(self.db_path) as conn:
            totals = conn.execute(
                f"SELECT COUNT(*) AS calls, "
                f"SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
                f" COALESCE(AVG(duration_ms), 0.0) AS avg_ms, "
                f" COALESCE(MAX(duration_ms), 0.0) AS max_ms "
                f"FROM tool_invocations{where_sql}",
                params,
            ).fetchone()

            by_server_rows = conn.execute(
                f"SELECT server_name, COUNT(*) AS calls, "
                f"SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
                f"ROUND(AVG(duration_ms), 2) AS avg_ms, ROUND(MAX(duration_ms), 2) AS max_ms "
                f"FROM tool_invocations{where_sql} "
                f"GROUP BY server_name ORDER BY calls DESC, server_name ASC",
                params,
            ).fetchall()

            by_tool_rows = conn.execute(
                f"SELECT server_name, tool_name, COUNT(*) AS calls, "
                f"SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
                f"ROUND(AVG(duration_ms), 2) AS avg_ms, ROUND(MAX(duration_ms), 2) AS max_ms "
                f"FROM tool_invocations{where_sql} "
                f"GROUP BY server_name, tool_name ORDER BY calls DESC, tool_name ASC",
                params,
            ).fetchall()

            by_scope_rows = conn.execute(
                f"SELECT COALESCE(scope, 'n/a') AS scope_key, COUNT(*) AS calls "
                f"FROM tool_invocations{where_sql} GROUP BY scope_key ORDER BY calls DESC",
                params,
            ).fetchall()

            recent_where = list(where)
            recent_params = list(params)
            if errors_only:
                recent_where.append("status = 'error'")
            recent_where_sql = (" WHERE " + " AND ".join(recent_where)) if recent_where else ""
            recent_rows = conn.execute(
                f"SELECT * FROM tool_invocations{recent_where_sql} ORDER BY id DESC LIMIT ?",
                [*recent_params, recent_limit],
            ).fetchall()

        total_calls = int(totals["calls"] or 0)
        total_errors = int(totals["errors"] or 0)
        tool_counts = {r["tool_name"]: int(r["calls"]) for r in by_tool_rows}

        local_searches = tool_counts.get("search_library", 0)
        online_reads = tool_counts.get("read_online_document", 0)
        stage_calls = tool_counts.get("stage_paper", 0)
        confirm_calls = tool_counts.get("confirm_save_paper", 0)
        citation_walk_calls = (
            tool_counts.get("get_paper_references", 0)
            + tool_counts.get("get_paper_citations", 0)
            + tool_counts.get("walk_citation_graph", 0)
        )
        kg_read_calls = (
            tool_counts.get("get_user_profile_summary", 0)
            + tool_counts.get("query_kg", 0)
            + tool_counts.get("search_entities", 0)
            + tool_counts.get("get_related_facts", 0)
            + tool_counts.get("get_ontology", 0)
        )
        kg_write_calls = (
            tool_counts.get("remember_user_fact", 0)
            + tool_counts.get("add_triplet", 0)
            + tool_counts.get("update_triplet", 0)
            + tool_counts.get("retract_triplet", 0)
            + tool_counts.get("extend_ontology", 0)
        )
        external_fetches = online_reads + stage_calls

        return {
            "project_filter": "ALL PROJECTS" if all_projects else target_pid,
            "days_filter": days,
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate": round((total_errors / total_calls) * 100.0, 1) if total_calls else 0.0,
            "avg_duration_ms": round(float(totals["avg_ms"] or 0.0), 2),
            "max_duration_ms": round(float(totals["max_ms"] or 0.0), 2),
            "by_server": [dict(r) for r in by_server_rows],
            "by_tool": [dict(r) for r in by_tool_rows],
            "by_scope": {r["scope_key"]: int(r["calls"]) for r in by_scope_rows},
            "behavioral_diagnostics": {
                "local_search_calls": local_searches,
                "external_fetch_calls": external_fetches,
                "citation_walk_calls": citation_walk_calls,
                "paper_stage_calls": stage_calls,
                "paper_confirm_calls": confirm_calls,
                "kg_read_calls": kg_read_calls,
                "kg_write_calls": kg_write_calls,
                "local_first_compliant": (external_fetches == 0) or (local_searches >= external_fetches),
            },
            "recent": [dict(r) for r in recent_rows],
        }


def format_telemetry_summary(summary: dict[str, Any]) -> str:
    lines = [
        "=== Research Agent Tool & Subagent Telemetry ===",
        f"Scope Filter     : {summary['project_filter']}"
        + (f" (last {summary['days_filter']} day(s))" if summary.get("days_filter") else " (all time)"),
        f"Total Tool Calls : {summary['total_calls']} "
        f"(Errors: {summary['total_errors']} / {summary['error_rate']:.1f}%)",
        f"Latency          : avg {summary['avg_duration_ms']:.1f} ms | max {summary['max_duration_ms']:.1f} ms",
    ]

    if summary["by_server"]:
        lines.append("\nBy MCP Server:")
        for s in summary["by_server"]:
            lines.append(
                f"  - {s['server_name']:<12}: {s['calls']} call(s), "
                f"{s['errors']} error(s), avg {s['avg_ms']:.1f} ms"
            )

    if summary["by_tool"]:
        lines.append("\nBy Tool:")
        for t in summary["by_tool"]:
            lines.append(
                f"  - {t['server_name']}.{t['tool_name']:<26}: {t['calls']} call(s), "
                f"{t['errors']} error(s), avg {t['avg_ms']:.1f} ms (max {t['max_ms']:.1f} ms)"
            )

    if summary["by_scope"]:
        scope_parts = [f"{k}={v}" for k, v in summary["by_scope"].items()]
        lines.append(f"\nScope Breakdown  : {', '.join(scope_parts)}")

    diag = summary["behavioral_diagnostics"]
    compliance_str = "OK (local search precedes web fetches)" if diag["local_first_compliant"] else (
        "WARNING (more online fetches than `search_library` calls!)"
    )
    lines.extend(
        [
            "\nBehavioral Diagnostics:",
            f"  - Local-First Check (`search_library` vs web fetch): "
            f"{diag['local_search_calls']} vs {diag['external_fetch_calls']} -> {compliance_str}",
            f"  - Citation Walk Activity (ref/cit/walk calls)      : {diag['citation_walk_calls']}",
            f"  - Paper Ingestion Gate (`stage` vs `confirm`)      : "
            f"{diag['paper_stage_calls']} staged / {diag['paper_confirm_calls']} confirmed",
            f"  - Knowledge Graph Memory (`read` vs `write` calls) : "
            f"{diag.get('kg_read_calls', 0)} read(s) / {diag.get('kg_write_calls', 0)} write(s)",
        ]
    )

    if summary["recent"]:
        lines.append("\nRecent Invocations:")
        for r in summary["recent"]:
            badge = "ERROR" if r["status"] == "error" else "OK"
            scope_str = f" [{r['scope']}]" if r.get("scope") else ""
            line = (
                f"  - [#{r['id']}] {r['created_at']} | {badge:<5} | "
                f"{r['server_name']}.{r['tool_name']}{scope_str} ({r['duration_ms']:.1f} ms)"
            )
            if r.get("error_msg"):
                line += f" -> {r['error_msg']}"
            lines.append(line)

    return "\n".join(lines)


_telemetry_store: Optional[TelemetryStore] = None


def get_telemetry_store() -> TelemetryStore:
    """Return the active TelemetryStore singleton (uses a temp DB under pytest unless overridden)."""
    global _telemetry_store
    if _telemetry_store is None:
        import os
        import tempfile

        if "PYTEST_CURRENT_TEST" in os.environ:
            tmp_db = Path(tempfile.gettempdir()) / "ra_pytest_telemetry.db"
            _telemetry_store = TelemetryStore(tmp_db)
        else:
            _telemetry_store = TelemetryStore(settings.DB_PATH)
    return _telemetry_store


def track_tool(server_name: str):
    """
    Decorator that wraps an MCP tool function to automatically record execution
    telemetry (status, latency, scope, arguments, soft error messages) in SQLite.
    """

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            bound_dict: dict[str, Any] = {}
            scope_val: Optional[str] = None
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                bound_dict = dict(bound.arguments)
                scope_val = bound_dict.get("scope") or bound_dict.get("target_scope")
            except Exception:
                pass

            try:
                res = fn(*args, **kwargs)
            except Exception as exc:
                duration_ms = (time.perf_counter() - t0) * 1000.0
                try:
                    get_telemetry_store().record_invocation(
                        server_name=server_name,
                        tool_name=fn.__name__,
                        status="error",
                        duration_ms=duration_ms,
                        scope=scope_val,
                        args=bound_dict,
                        error_msg=f"{type(exc).__name__}: {exc}"[:200],
                    )
                except Exception:
                    pass
                raise

            duration_ms = (time.perf_counter() - t0) * 1000.0
            is_err = _is_error_response(res)
            err_msg = res.splitlines()[0][:200] if is_err and isinstance(res, str) else None
            try:
                get_telemetry_store().record_invocation(
                    server_name=server_name,
                    tool_name=fn.__name__,
                    status="error" if is_err else "ok",
                    duration_ms=duration_ms,
                    scope=scope_val,
                    args=bound_dict,
                    error_msg=err_msg,
                )
            except Exception as log_exc:
                logger.debug("Telemetry recording skipped: %s", log_exc)

            return res

        return wrapper

    return decorator
