"""
Live MCP Progress & Step-Trace Helper (``src/mcp_servers/progress.py``).

Provides:
1. ``report_mcp_progress(progress, total, message)``:
   - Always logs ``message`` via the standard ``stderr`` / file logger.
   - When called inside an active FastMCP tool execution (which runs synchronous
     tool functions inside ``anyio.to_thread.run_sync``), automatically bridges back
     to the FastMCP event loop via ``anyio.from_thread.run`` to emit:
     * ``notifications/progress`` (``ctx.report_progress(progress, total, message)``)
       — updates MCP progress listeners and triggers OpenCode's ``resetTimeoutOnProgress``.
     * ``notifications/message`` (``ctx.info(message)``) — streams live status messages
       to MCP clients that display server log notifications.
2. ``StepTimer``:
   - Accumulates a human-readable, timestamped step-by-step execution log (with per-step
     elapsed seconds) that tools like ``stage_paper``, ``confirm_save_paper``,
     ``refresh_library``, and ``read_online_document`` include in their output so the
     user sees exact workload and timing details in OpenCode.
"""

from __future__ import annotations

import time
from typing import Optional

import anyio

from config.settings import get_logger

logger = get_logger(__name__)


def report_mcp_progress(
    progress: float,
    total: Optional[float] = None,
    message: Optional[str] = None,
) -> None:
    """
    Emit a live MCP progress notification (and log message) if inside an active
    FastMCP tool context. Safe to call from any thread or unit test (no-op outside MCP).
    """
    if message:
        logger.info("%s", message)

    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return

    try:
        anyio.from_thread.run(ctx.report_progress, float(progress), float(total) if total is not None else None, message)
    except Exception:
        pass

    if message:
        try:
            anyio.from_thread.run(ctx.info, message)
        except Exception:
            pass


class StepTracker:
    """
    Records a sequential list of execution steps with elapsed durations (in seconds)
    while simultaneously emitting live MCP progress notifications.
    """

    def __init__(
        self,
        tool_label: str = "mcp_tool",
        total_steps: int = 4,
        prefix: Optional[str] = None,
    ):
        self.tool_label = prefix or tool_label
        self.total_steps = max(1, total_steps)
        self.current_step = 0
        self.t_start = time.perf_counter()
        self.t_last = self.t_start
        self.entries: list[str] = []

    def step(self, message: str, step_num: Optional[int] = None) -> None:
        now = time.perf_counter()
        if step_num is not None:
            self.current_step = step_num
        else:
            self.current_step = min(self.total_steps, self.current_step + 1)

        elapsed_total = now - self.t_start
        self.t_last = now
        formatted = f"[{self.current_step}/{self.total_steps} +{elapsed_total:.1f}s] {message}"
        self.entries.append(formatted)
        report_mcp_progress(
            progress=self.current_step,
            total=self.total_steps,
            message=f"[{self.tool_label}] {formatted}",
        )

    def subprogress(self, current: int, total: int, detail: str) -> None:
        """
        Emit fine-grained sub-step progress (e.g. page 4/20 during PDF OCR) without
        appending every single page tick to the final summary table.
        """
        frac = (current / max(1, total)) if total else 0.0
        effective_progress = max(0.0, (self.current_step - 1) + frac)
        report_mcp_progress(
            progress=round(effective_progress, 2),
            total=self.total_steps,
            message=f"[{self.tool_label}] {detail} ({current}/{total})",
        )

    def finish(self, final_message: str) -> None:
        now = time.perf_counter()
        elapsed_total = now - self.t_start
        formatted = f"[Done in {elapsed_total:.1f}s] {final_message}"
        self.entries.append(formatted)
        report_mcp_progress(
            progress=self.total_steps,
            total=self.total_steps,
            message=f"[{self.tool_label}] {formatted}",
        )

    def format_block(self) -> str:
        if not self.entries:
            return ""
        lines = ["Execution Progress & Timing:"]
        for entry in self.entries:
            lines.append(f"  - {entry}")
        return "\n".join(lines)
