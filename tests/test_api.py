"""Tests for src/api.py + src/traces.py (Lane G — PLAN.md Wave 1).

The four arms (src.baseline, src.agent, src.agent_langgraph) are still
Wave-0 stubs elsewhere in the tree (Lane E, in progress concurrently per
PLAN.md Wave 1) — these tests never call them. Instead every test builds
its own FastAPI app via `create_app(dispatch=...)` with a **stubbed LLM**:
plain Python callables matching the `(question: str) -> QueryResponse`
contract, one per mode. That is enough to exercise the real dispatch dict,
rate limiter, daily cap, trace persistence, and error handling end to end
without depending on another lane's implementation or a live API key.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api import CANNED_CAPACITY_MESSAGE, create_app
from src.schemas import Citation, Mode, QueryResponse, ToolCall

MODES: list[Mode] = ["baseline_rag", "baseline_tools", "agent_custom", "agent_langgraph"]


def _stub_response(mode: Mode, question: str) -> QueryResponse:
    return QueryResponse(
        answer=f"[{mode}] stubbed answer to: {question}",
        citations=[
            Citation(
                chunk_id="MSFT_2024_item7_0",
                ticker="MSFT",
                fiscal_year=2024,
                section="item7",
                source_url="https://example.com/msft-10k",
            )
        ],
        trace=[
            ToolCall(
                name="search_filings",
                arguments={"query": question},
                result_summary="1 chunk returned",
                latency_ms=12.5,
            )
        ],
        latency_ms=0.0,  # left unset on purpose — api.py should fill this in
        mode=mode,
        incomplete=False,
        refused=False,
    )


def _make_stub_dispatch(call_log: list[str] | None = None) -> dict[str, callable]:
    """One stub callable per mode. If `call_log` is provided, each
    invocation appends the mode it was called with — used to assert the
    daily cap actually short-circuits the handler rather than merely
    editing the response afterward.
    """

    def _handler(mode: Mode):
        def _inner(question: str) -> QueryResponse:
            if call_log is not None:
                call_log.append(mode)
            return _stub_response(mode, question)

        return _inner

    return {mode: _handler(mode) for mode in MODES}


def _client(**overrides) -> TestClient:
    app = create_app(**overrides)
    # raise_server_exceptions=False so tests can exercise the app-wide
    # exception handler (FR5.3) the way a real deployed server would,
    # instead of TestClient re-raising the original exception for
    # debugging convenience.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def trace_db(tmp_path):
    return str(tmp_path / "traces.sqlite")


# --- POST /query — all four modes -----------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_query_reaches_stubbed_handler_for_every_mode(mode, trace_db):
    client = _client(dispatch=_make_stub_dispatch(), trace_db=trace_db)

    resp = client.post("/query", json={"question": "What was FY2024 revenue?", "mode": mode})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == mode
    assert body["answer"].startswith(f"[{mode}]")
    assert body["refused"] is False
    assert body["incomplete"] is False
    assert len(body["citations"]) == 1
    assert len(body["trace"]) == 1
    # latency_ms was 0.0 from the stub; api.py must fill in a real value.
    assert body["latency_ms"] > 0


def test_query_rejects_unknown_mode_with_typed_error_not_traceback(trace_db):
    client = _client(dispatch=_make_stub_dispatch(), trace_db=trace_db)

    resp = client.post("/query", json={"question": "hi", "mode": "not_a_real_mode"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert "Traceback" not in body["detail"]
    assert "detail" in body


# --- GET /healthz -----------------------------------------------------------


def test_healthz_never_raises_and_reports_a_status(trace_db, tmp_path, monkeypatch):
    # /healthz always probes the real src.store (regardless of the
    # injected dispatch, since store reachability isn't part of the
    # stubbed-LLM contract) — point CHROMA_DIR at an isolated tmp dir so
    # this test doesn't create/pollute ./chroma_db in the repo root.
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma_db"))
    monkeypatch.setenv("FACTS_DB", str(tmp_path / "facts.db"))
    client = _client(dispatch=_make_stub_dispatch(), trace_db=trace_db)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["vector_store"], str) and body["vector_store"]


# --- GET /stats ---------------------------------------------------------


def test_stats_populates_after_requests(trace_db):
    client = _client(dispatch=_make_stub_dispatch(), trace_db=trace_db)

    # empty before any traffic
    empty = client.get("/stats").json()
    assert empty["operational"]["total_requests"] == 0

    client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    client.post("/query", json={"question": "q2", "mode": "baseline_rag"})
    client.post("/query", json={"question": "q3", "mode": "agent_custom"})

    resp = client.get("/stats")
    assert resp.status_code == 200
    operational = resp.json()["operational"]

    assert operational["total_requests"] == 3
    assert operational["requests_by_mode"] == {"baseline_rag": 2, "agent_custom": 1}
    assert operational["latency_ms"]["p50"] is not None
    assert operational["latency_ms"]["p95"] is not None
    assert operational["refusal_rate"] == 0.0
    assert operational["tool_call_distribution"] == {"search_filings": 3}
    assert "corpus" in resp.json()


def test_stats_reports_refusal_rate(trace_db):
    call_log: list[str] = []
    dispatch = _make_stub_dispatch(call_log)

    def refusing_handler(question: str) -> QueryResponse:
        response = _stub_response("baseline_rag", question)
        return response.model_copy(update={"refused": True})

    dispatch["baseline_rag"] = refusing_handler
    client = _client(dispatch=dispatch, trace_db=trace_db)

    client.post("/query", json={"question": "unanswerable", "mode": "baseline_rag"})
    client.post("/query", json={"question": "answerable", "mode": "agent_custom"})

    operational = client.get("/stats").json()["operational"]
    assert operational["refusal_rate"] == 0.5


# --- Rate limiting (FR6.3) -------------------------------------------------


def test_rate_limit_engages_after_per_ip_threshold(trace_db):
    client = _client(dispatch=_make_stub_dispatch(), rate_limit_per_min=2, trace_db=trace_db)

    r1 = client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    r2 = client.post("/query", json={"question": "q2", "mode": "baseline_rag"})
    r3 = client.post("/query", json={"question": "q3", "mode": "baseline_rag"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    body = r3.json()
    assert body["error"] == "http_error"
    assert "rate limit" in body["detail"].lower()
    assert "Traceback" not in body["detail"]


# --- Daily cap degradation (FR6.3) -----------------------------------------


def test_daily_cap_degrades_to_canned_response_without_calling_handler(trace_db):
    call_log: list[str] = []
    client = _client(
        dispatch=_make_stub_dispatch(call_log),
        daily_cap=1,
        rate_limit_per_min=1000,
        trace_db=trace_db,
    )

    r1 = client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    r2 = client.post("/query", json={"question": "q2", "mode": "baseline_rag"})

    assert r1.status_code == 200
    assert r1.json()["refused"] is False

    # Second request is over the daily cap: still 200 (never a 500), but a
    # canned, honest refusal — and the underlying handler must never have
    # been invoked a second time, since the whole point of the cap is to
    # stop spending the real API key.
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["refused"] is True
    assert body2["answer"] == CANNED_CAPACITY_MESSAGE
    assert body2["citations"] == []
    assert body2["trace"] == []

    assert call_log == ["baseline_rag"]  # only the first call reached the handler


def test_daily_cap_canned_response_is_still_traced(trace_db):
    client = _client(dispatch=_make_stub_dispatch(), daily_cap=1, rate_limit_per_min=1000, trace_db=trace_db)

    client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    client.post("/query", json={"question": "q2", "mode": "baseline_rag"})

    operational = client.get("/stats").json()["operational"]
    assert operational["total_requests"] == 2
    assert operational["refusal_rate"] == 0.5


# --- Errors return typed responses, never raw tracebacks (FR5.3) -----------


def test_handler_exception_returns_typed_500_not_traceback(trace_db):
    def _broken_handler(question: str) -> QueryResponse:
        raise ValueError("boom: something went wrong deep inside the arm")

    dispatch = _make_stub_dispatch()
    dispatch["baseline_rag"] = _broken_handler
    client = _client(dispatch=dispatch, trace_db=trace_db)

    resp = client.post("/query", json={"question": "q1", "mode": "baseline_rag"})

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_error"
    assert "Traceback" not in body["detail"]
    assert "boom" not in body["detail"]  # internal exception text not leaked to the client


def test_malformed_request_body_returns_typed_422(trace_db):
    client = _client(dispatch=_make_stub_dispatch(), trace_db=trace_db)

    resp = client.post("/query", json={"question": "", "mode": "baseline_rag"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert "Traceback" not in body["detail"]


# --- src.traces direct-unit coverage ---------------------------------------


def test_record_trace_writes_a_row(trace_db):
    from src.schemas import QueryRequest
    from src.traces import record_trace

    request = QueryRequest(question="hello", mode="baseline_rag")
    response = _stub_response("baseline_rag", "hello").model_copy(update={"latency_ms": 42.0})

    record_trace("trace-abc", request, response, db_path=trace_db)

    conn = sqlite3.connect(trace_db)
    row = conn.execute("SELECT trace_id, mode, refused, tool_call_count FROM traces").fetchone()
    conn.close()

    assert row == ("trace-abc", "baseline_rag", 0, 1)


def test_record_trace_never_raises_on_persistence_failure(tmp_path):
    from src.schemas import QueryRequest
    from src.traces import record_trace

    request = QueryRequest(question="hello", mode="baseline_rag")
    response = _stub_response("baseline_rag", "hello")

    # Point at a path that cannot possibly be opened as a SQLite DB (a
    # directory) to force a persistence failure — record_trace must
    # swallow it, never propagate.
    bad_path = tmp_path  # a directory, not a file
    record_trace("trace-xyz", request, response, db_path=str(bad_path))  # must not raise


def test_get_stats_with_no_traces_returns_zeroed_shape(trace_db):
    from src.traces import get_stats

    stats = get_stats(db_path=trace_db)
    assert stats["total_requests"] == 0
    assert stats["requests_by_mode"] == {}
    assert stats["refusal_rate"] is None
