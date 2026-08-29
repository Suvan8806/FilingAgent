"""Trace persistence — monitoring (Lane G — PLAN.md Wave 1).

Owns: this file, jointly with src/api.py (Lane G's exclusive write scope).

Contract
--------
Implements FR7 (Monitoring): every `/query` invocation persists its trace
to SQLite (path from `TRACE_DB` env var, .env.example), and `/stats`
aggregates recent traffic from it.

Persisted per request (FR7.1): question, mode, latency, tool calls
(serialized `list[ToolCall]`), citation count, refusal flag, trace ID.

Required operations:

- `record_trace(trace_id: str, request: QueryRequest, response: QueryResponse) -> None`
  — write one row. Must not raise on a well-formed `QueryResponse` — a
  broken monitoring write must never take down a successful `/query`
  response to the caller (log and continue, don't propagate).
- `get_stats(window: str | None = None) -> dict` — aggregate over recent
  traces for `GET /stats` (FR7.2): request count by mode, rolling p50/p95
  latency, tool-call distribution, refusal rate.

Both operations accept an optional `db_path` override (defaulting to the
`TRACE_DB` env var) so tests and multiple `create_app()` instances (see
src/api.py) can point at an isolated database without mutating process
environment state.

FR7.3 note for context (not this module's job to implement, just to keep in
mind when shaping the schema): persisted traces are meant to double as a
golden-set feeder — low-confidence or refused live queries are candidates
for future eval cases. Keep enough raw detail in each row that a human
could later triage a trace for that purpose without re-deriving it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from src.schemas import QueryRequest, QueryResponse

logger = logging.getLogger("filingagent.traces")

_DEFAULT_DB_PATH = "./traces.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    question TEXT NOT NULL,
    mode TEXT NOT NULL,
    answer TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    tool_calls_json TEXT NOT NULL,
    tool_call_count INTEGER NOT NULL,
    citation_count INTEGER NOT NULL,
    incomplete INTEGER NOT NULL,
    refused INTEGER NOT NULL
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_traces_created_at ON traces (created_at);"


def _resolve_db_path(db_path: str | None) -> str:
    return db_path or os.environ.get("TRACE_DB", _DEFAULT_DB_PATH)


def _connect(db_path: str | None) -> sqlite3.Connection:
    resolved = _resolve_db_path(db_path)
    conn = sqlite3.connect(resolved)
    conn.execute(_SCHEMA)
    conn.execute(_INDEX)
    return conn


def record_trace(
    trace_id: str,
    request: QueryRequest,
    response: QueryResponse,
    db_path: str | None = None,
) -> None:
    """Persist one /query invocation's trace to SQLite (FR7.1). Must not
    raise on a well-formed response — monitoring failures are logged and
    swallowed so they never take down a successful caller response.
    """
    try:
        tool_calls = [call.model_dump(mode="json") for call in response.trace]
        with closing(_connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO traces (
                    trace_id, created_at, question, mode, answer, latency_ms,
                    tool_calls_json, tool_call_count, citation_count,
                    incomplete, refused
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    datetime.now(timezone.utc).isoformat(),
                    request.question,
                    response.mode,
                    response.answer,
                    response.latency_ms,
                    json.dumps(tool_calls),
                    len(response.trace),
                    len(response.citations),
                    int(response.incomplete),
                    int(response.refused),
                ),
            )
    except Exception:
        # A broken monitoring write must never take down a successful
        # /query response — log and continue (module contract above).
        logger.exception("failed to persist trace", extra={"trace_id": trace_id})


def _parse_window(window: str | None) -> timedelta | None:
    """Parse a simple suffix duration like '30m', '24h', '7d'. Returns
    None (no filter — all recorded traces) for an unset or unparsable
    window rather than raising, since /stats must always return something
    useful.
    """
    if not window:
        return None
    try:
        unit = window[-1]
        amount = float(window[:-1])
    except (ValueError, IndexError):
        return None

    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return None


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(0, min(len(sorted_values) - 1, round(pct / 100 * (len(sorted_values) - 1))))
    return sorted_values[rank]


def get_stats(window: str | None = None, db_path: str | None = None) -> dict[str, Any]:
    """Aggregate recent traces for GET /stats (FR7.2): request count by
    mode, rolling p50/p95 latency, tool-call distribution, refusal rate.

    `window` is an optional suffix duration ('30m', '24h', '7d'); omitted
    or unparsable means "all recorded traces".
    """
    delta = _parse_window(window)
    query = "SELECT mode, latency_ms, tool_calls_json, refused FROM traces"
    params: tuple[Any, ...] = ()
    if delta is not None:
        cutoff = (datetime.now(timezone.utc) - delta).isoformat()
        query += " WHERE created_at >= ?"
        params = (cutoff,)

    try:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(query, params).fetchall()
    except Exception:
        logger.exception("failed to read trace stats")
        rows = []

    total_requests = len(rows)
    requests_by_mode: dict[str, int] = {}
    latencies: list[float] = []
    tool_call_counts: dict[str, int] = {}
    refused_count = 0

    for mode, latency_ms, tool_calls_json, refused in rows:
        requests_by_mode[mode] = requests_by_mode.get(mode, 0) + 1
        latencies.append(latency_ms)
        if refused:
            refused_count += 1
        try:
            for call in json.loads(tool_calls_json):
                name = call.get("name", "unknown")
                tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
        except (json.JSONDecodeError, AttributeError):
            continue

    latencies.sort()

    return {
        "window": window,
        "total_requests": total_requests,
        "requests_by_mode": requests_by_mode,
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
        },
        "tool_call_distribution": tool_call_counts,
        "refusal_rate": (refused_count / total_requests) if total_requests else None,
    }
