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
Every exception path — validation errors, HTTP errors, and anything
unhandled that bubbles up from a handler — is caught by an app-wide
exception handler and rendered as `ErrorResponse` JSON.

**FR6.2 — do not ingest at boot.** The index must be baked into the Docker
image at build time; a multi-minute ingest on container start will time
out free-tier hosting. This module assumes a pre-built store/facts DB
exists at startup and reports it via `/healthz` if it doesn't, rather than
trying to build it.

Testability
-----------
`create_app()` accepts optional overrides (`dispatch`, `rate_limit_per_min`,
`daily_cap`, `trace_db`) so tests can inject a stubbed LLM/tool loop for
each of the four modes and exercise the rate limiter / daily cap with tiny
thresholds, without monkeypatching module internals or spending a real API
key (PLAN.md: "test with a stubbed LLM").
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from src import traces as traces_module
from src.schemas import Mode, QueryRequest, QueryResponse

logger = logging.getLogger("filingagent.api")

_DEFAULT_RATE_LIMIT_PER_MIN = 10
_DEFAULT_DAILY_CAP = 200
_RATE_LIMIT_WINDOW_SECONDS = 60.0

CANNED_CAPACITY_MESSAGE = (
    "This public demo has reached its daily request capacity. Please try "
    "again after 00:00 UTC, or run the service locally against the "
    "committed corpus (see README) to query without a cap."
)


# --- Local response models (not part of the frozen src/schemas.py contract;
# /healthz and /stats have no models there, so they are defined here,
# owned entirely by this file). ------------------------------------------


class ErrorResponse(BaseModel):
    """Typed error envelope for every failure path (FR5.3) — never a raw
    stack trace.
    """

    error: str
    detail: str
    trace_id: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    vector_store: str


class StatsResponse(BaseModel):
    corpus: dict[str, Any]
    operational: dict[str, Any]


# --- Rate limiting / daily cap (FR6.3) ------------------------------------


class RateLimiter:
    """Per-IP sliding-window rate limit, in-memory. This service runs as a
    single process (see Dockerfile/docker-compose.yml — one API
    container), so an in-memory limiter is sufficient at this scale;
    reaching for Redis here would be speculative generality (YAGNI).
    """

    def __init__(self, limit_per_min: int) -> None:
        self.limit_per_min = limit_per_min
        self._hits: dict[str, deque[float]] = {}

    def check(self, client_id: str) -> bool:
        """Returns True and records a hit if the client is under the
        limit; returns False (and records nothing) if the client should
        be rejected.
        """
        now = time.monotonic()
        window_start = now - _RATE_LIMIT_WINDOW_SECONDS
        hits = self._hits.setdefault(client_id, deque())
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.limit_per_min:
            return False
        hits.append(now)
        return True


class DailyCap:
    """Hard global daily request cap (FR6.3). Resets at UTC midnight.
    Exceeding it does not raise or 500 — src.api degrades `/query` to a
    canned response instead (see `CANNED_CAPACITY_MESSAGE`), preserving
    the caller's real API key spend.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._day: date | None = None
        self._count = 0

    def try_consume(self) -> bool:
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._count = 0
        if self._count >= self.cap:
            return False
        self._count += 1
        return True


# --- Structured JSON logging (FR5.2) --------------------------------------


def _log_event(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    logger.info(json.dumps(payload, default=str))


# --- Dispatch construction --------------------------------------------------


def _default_dispatch() -> dict[str, Callable[[str], QueryResponse]]:
    """Import the four arms lazily (at create_app() time, not module import
    time) and wire them into a single mode -> handler dispatch dict — the
    control-arm design this whole project is built around (PRD FR3).
    """
    from src.agent import run_agent_custom
    from src.agent_langgraph import run_agent_langgraph
    from src.baseline import run_baseline_rag, run_baseline_tools

    return {
        "baseline_rag": run_baseline_rag,
        "baseline_tools": run_baseline_tools,
        "agent_custom": run_agent_custom,
        "agent_langgraph": run_agent_langgraph,
    }


def _canned_capacity_response(mode: Mode) -> QueryResponse:
    """FR6.3: what /query returns once the daily cap is exhausted — a
    typed, honest response, never a 500 and never a silent drop.
    """
    return QueryResponse(
        answer=CANNED_CAPACITY_MESSAGE,
        citations=[],
        trace=[],
        latency_ms=0.0,
        mode=mode,
        incomplete=False,
        refused=True,
    )


def _persist_trace_safely(
    trace_id: str, request: QueryRequest, response: QueryResponse, trace_db: str
) -> None:
    try:
        traces_module.record_trace(trace_id, request, response, db_path=trace_db)
    except Exception:
        # record_trace already swallows its own errors; this is a second
        # backstop in case a future change to that contract forgets to.
        logger.exception("trace persistence raised unexpectedly", extra={"trace_id": trace_id})


def _client_id(request: Request) -> str:
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


# --- App factory --------------------------------------------------------


def create_app(
    *,
    dispatch: dict[str, Callable[[str], QueryResponse]] | None = None,
    rate_limit_per_min: int | None = None,
    daily_cap: int | None = None,
    trace_db: str | None = None,
) -> FastAPI:
    """Construct and return the FastAPI application: routes, dispatch
    dict, rate limiting, daily cap, structured logging. See module
    docstring for the full contract.
    """
    app = FastAPI(
        title="FilingAgent",
        description=(
            "Answers natural-language questions about SEC 10-K filings via "
            "four control arms — baseline_rag, baseline_tools, agent_custom, "
            "agent_langgraph. See PRD.md FR3 for the experimental design."
        ),
        version="0.1.0",
    )

    app.state.dispatch = dispatch if dispatch is not None else _default_dispatch()
    app.state.rate_limiter = RateLimiter(
        rate_limit_per_min
        if rate_limit_per_min is not None
        else int(os.environ.get("RATE_LIMIT_PER_MIN", str(_DEFAULT_RATE_LIMIT_PER_MIN)))
    )
    app.state.daily_cap = DailyCap(
        daily_cap
        if daily_cap is not None
        else int(os.environ.get("DAILY_REQUEST_CAP", str(_DEFAULT_DAILY_CAP)))
    )
    app.state.trace_db = trace_db or os.environ.get("TRACE_DB", "./traces.sqlite")

    _register_exception_handlers(app)
    _register_routes(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="validation_error",
                detail=str(exc),
            ).model_dump(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error="http_error",
                detail=str(exc.detail),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a raw stack trace to the client (FR5.3). Full detail
        # still goes to structured logs for debugging.
        logger.exception("unhandled exception in %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="internal_error",
                detail="An internal error occurred while processing the request.",
            ).model_dump(),
        )


def _register_routes(app: FastAPI) -> None:
    @app.post("/query", response_model=QueryResponse)
    async def query(body: QueryRequest, request: Request) -> QueryResponse:
        trace_id = str(uuid.uuid4())
        client_id = _client_id(request)
        start = time.perf_counter()

        _log_event("query_received", trace_id=trace_id, mode=body.mode, client_id=client_id)

        if not request.app.state.rate_limiter.check(client_id):
            _log_event("rate_limited", trace_id=trace_id, mode=body.mode, client_id=client_id)
            raise StarletteHTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded ({request.app.state.rate_limiter.limit_per_min} "
                    "requests/min per client). Try again shortly."
                ),
            )

        if not request.app.state.daily_cap.try_consume():
            _log_event("daily_cap_reached", trace_id=trace_id, mode=body.mode, client_id=client_id)
            response = _canned_capacity_response(body.mode)
            latency_ms = (time.perf_counter() - start) * 1000
            response = response.model_copy(update={"latency_ms": latency_ms})
            await run_in_threadpool(
                _persist_trace_safely, trace_id, body, response, request.app.state.trace_db
            )
            return response

        handler = request.app.state.dispatch.get(body.mode)
        if handler is None:
            # Unreachable under normal operation — QueryRequest.mode is a
            # frozen Literal validated by Pydantic before we get here — but
            # guarded explicitly rather than trusting that invariant forever.
            raise StarletteHTTPException(status_code=500, detail=f"No handler registered for mode {body.mode!r}.")

        # The four arms are synchronous (LLM SDK calls, tool loop, sqlite
        # lookups) — run them off the event loop so one slow /query does
        # not stall every other concurrent request (FastAPI async rule:
        # never call blocking code directly inside an async route).
        response = await run_in_threadpool(handler, body.question)

        latency_ms = (time.perf_counter() - start) * 1000
        if not response.latency_ms:
            response = response.model_copy(update={"latency_ms": latency_ms})

        await run_in_threadpool(
            _persist_trace_safely, trace_id, body, response, request.app.state.trace_db
        )
        _log_event(
            "query_completed",
            trace_id=trace_id,
            mode=body.mode,
            client_id=client_id,
            latency_ms=response.latency_ms,
            refused=response.refused,
            incomplete=response.incomplete,
            tool_calls=len(response.trace),
            citations=len(response.citations),
        )
        return response

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        try:
            from src import store

            # store.search is a blocking Chroma call — keep it off the
            # event loop like every other synchronous call in this app.
            await run_in_threadpool(store.search, query="__healthcheck__", k=1)
            return HealthResponse(status="ok", vector_store="reachable")
        except NotImplementedError:
            return HealthResponse(status="degraded", vector_store="not yet implemented")
        except Exception as exc:  # noqa: BLE001 — liveness probe must never raise
            return HealthResponse(status="degraded", vector_store=f"unreachable: {exc}")

    @app.get("/stats", response_model=StatsResponse)
    async def stats(request: Request, window: str | None = None) -> StatsResponse:
        # traces_module.get_stats does blocking sqlite3 I/O.
        operational = await run_in_threadpool(
            traces_module.get_stats, window=window, db_path=request.app.state.trace_db
        )
        return StatsResponse(corpus=_corpus_stats(), operational=operational)


def _corpus_stats() -> dict[str, Any]:
    """Best-effort corpus stats. src.store's frozen interface (add_chunks,
    search) does not expose a count/inspect operation, and this module
    must not reach around it into chromadb directly (src/store.py's own
    docstring: "no other module should import chromadb directly") — so
    real corpus stats become available once Lane B's store is populated
    and, if needed, extends that interface. Until then this reports
    honestly rather than fabricating numbers.
    """
    try:
        from src import store  # noqa: F401 — presence check only

        return {
            "status": "unavailable",
            "reason": (
                "src.store does not expose a count operation in the frozen "
                "Wave 0 contract; corpus stats populate once ingestion runs "
                "against a real index (see PLAN.md Wave 2)."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": str(exc)}


app = create_app()
