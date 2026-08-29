"""FastAPI app (Lane G — PLAN.md Wave 1).

Owns: this file, jointly with src/traces.py (Lane G's exclusive write
scope). Also owns Dockerfile, docker-compose.yml, and deploy config
(non-Python deliverables of the same lane).

Contract
--------
Implements FR5 (API) and FR6 (Public deployment).

Endpoints:

- `POST /query` — body: `QueryRequest` (question, mode). Dispatches on
  `mode` through a **single dispatch dict** mapping each of the four
  Literal values to its handler (src.baseline.run_baseline_rag,
  run_baseline_tools; src.agent.run_agent_custom;
  src.agent_langgraph.run_agent_langgraph) — not four divergent `if/elif`
  branches. Every invocation is persisted via src.traces.record_trace
  (FR7.1) and gets a per-request trace ID in structured JSON logs (FR5.2).
  Response body: `QueryResponse`.
- `GET /healthz` — liveness + vector store reachability.
- `GET /stats` — corpus stats and rolling operational metrics, via
  src.traces.get_stats (FR7.2).
- `GET /docs` — free, from FastAPI's OpenAPI generation. No custom work
  needed here beyond not disabling it.

Required before the deploy link goes public (FR6.3, non-negotiable):
- Per-IP rate limit — `RATE_LIMIT_PER_MIN` (.env.example).
- Hard global daily request cap that degrades `/query` to a canned
  response once exceeded, rather than continuing to spend the API key —
  `DAILY_REQUEST_CAP` (.env.example).
- A provider-side spend limit on the API key itself (set in the provider
  console, not code — but this module's cap is the in-app backstop).

Errors: typed responses, never raw stack traces to the client (FR5.3).

**FR6.2 — do not ingest at boot.** The index must be baked into the Docker
image at build time; a multi-minute ingest on container start will time
out free-tier hosting. This module should assume a pre-built store/facts
DB exists at startup and fail fast (via /healthz) if it doesn't, not try to
build it.
"""

from __future__ import annotations


def create_app():
    """Construct and return the FastAPI application: routes, dispatch
    dict, rate limiting, daily cap, structured logging. See module
    docstring for the full contract.
    """
    raise NotImplementedError
