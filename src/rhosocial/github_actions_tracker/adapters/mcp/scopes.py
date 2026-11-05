# src/rhosocial/github_actions_tracker/adapters/mcp/scopes.py
"""MCP scope definitions for GitHub Actions Tracker.

Scopes map one-to-one onto the read/write split the tools already honour, so an
OAuth grant can express the same limit the tools enforce. Keeping the write
scope small matters more here than elsewhere: ``ghat:write`` is the
permission to spend CI minutes and possibly deploy.
"""

from __future__ import annotations

#: Scope vocabulary.
SCOPES: list[str] = [
    # Reading runs, jobs, steps, logs and repository metadata.
    "ghat:read",
    # Triggering and re-running workflows -- spends CI minutes.
    "ghat:write",
    # Operator-only surface (credentials, repository subscriptions).
    "ghat:admin",
]

#: Tool to scope mapping. Every registered tool must appear here; a tool missing
#: from this table would be reachable without a scope check.
TOOL_SCOPES: dict[str, str] = {
    # Read-only surface.
    "ci_status": "ghat:read",
    "list_repos": "ghat:read",
    "find_runs": "ghat:read",
    "recent_runs": "ghat:read",
    "get_run": "ghat:read",
    "get_job": "ghat:read",
    "get_job_log": "ghat:read",
    "search_job_log": "ghat:read",
    "get_run_failed_logs": "ghat:read",
    "wait_for_run": "ghat:read",
    # Write surface.
    "trigger_workflow": "ghat:write",
    "cancel_run": "ghat:write",
    "rerun_run": "ghat:write",
}

#: Credential and subscription management stay out of MCP entirely; they live in
#: the CLI and the REST API. This tuple documents that decision for readers who
#: expect to find them here.
OPERATOR_ONLY: tuple[str, ...] = (
    "credentials",
    "repository_subscriptions",
)
