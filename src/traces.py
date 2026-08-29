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

FR7.3 note for context (not this module's job to implement, just to keep in
mind when shaping the schema): persisted traces are meant to double as a
golden-set feeder — low-confidence or refused live queries are candidates
for future eval cases. Keep enough raw detail in each row that a human
could later triage a trace for that purpose without re-deriving it.
"""

from __future__ import annotations

from src.schemas import QueryRequest, QueryResponse


def record_trace(trace_id: str, request: QueryRequest, response: QueryResponse) -> None:
    """Persist one /query invocation's trace to SQLite (FR7.1). Must not
    raise on a well-formed response.
    """
    raise NotImplementedError


def get_stats(window: str | None = None) -> dict:
    """Aggregate recent traces for GET /stats (FR7.2): request count by
    mode, rolling p50/p95 latency, tool-call distribution, refusal rate.
    """
    raise NotImplementedError
