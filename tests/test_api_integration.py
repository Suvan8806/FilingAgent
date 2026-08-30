"""End-to-end integration tests for POST /query (Agent J — PLAN.md Wave 2.2).

How these differ from tests/test_api.py
---------------------------------------
`tests/test_api.py` injects a stub *dispatch dict* and therefore never
touches an arm at all — it tests the HTTP surface in isolation. These tests
run the **real** dispatch that `src.api._default_dispatch()` builds:

    src.api  ->  src.baseline / src.agent / src.agent_langgraph
             ->  src.tools  ->  src.store / src.facts
             ->  src.llm

Only the two genuine external boundaries are faked — the LLM provider and
the corpus. Everything between them is the shipped code.

No live API call, guaranteed three ways
---------------------------------------
`.env` is loaded at `import src` time (src/__init__.py) and holds a real,
working `GROQ_API_KEY`. A mis-scoped stub in this file would therefore not
fail loudly — it would spend real quota. So:

1. `src.llm.default_client` is replaced with a factory returning a fake
   client; `src.llm.PROVIDER` and `src.llm.MODEL` are pinned explicitly (see
   "Provider agnosticism" below) so the fake matches the wire format the
   arms will drive.
2. **Every** API-key env var `src.llm` could build a live client from is
   deleted from the environment for the duration of each test — the set is
   derived from `src.llm`'s own provider table, not hardcoded, so adding a
   provider cannot silently open a hole. With the keys gone,
   `src.llm.default_client(...)` raises instead of returning a client — a
   loud failure, never a silent charge.
   `test_a_live_client_cannot_be_constructed_under_this_fixture` asserts
   that for every provider in the table, so the guard is verified rather
   than assumed.
3. Every request-level test asserts the fake actually recorded calls, so a
   stub that silently failed to take effect shows up as a failing
   assertion rather than as a green test that quietly hit the network.

Provider agnosticism
--------------------
`src.llm` resolves `PROVIDER` / `MODEL` from the environment at import time,
and `src/__init__.py` auto-loads the developer's `.env`. Nothing in this file
asserts a hardcoded provider string: the provider under test is resolved from
`src.llm`'s own OpenAI-compatible table and then **pinned explicitly** by the
`stub_llm` fixture. So the suite behaves identically whether the local
`.env` says groq, gemini, or nothing at all. (This bit once already: the
project switched providers mid-Wave-2.)

The sys.modules / package-attribute hazard
------------------------------------------
`from src import store` (inside `src.tools`, and inside `src.api`'s
`/healthz`) caches the module as an **attribute on the `src` package
object** and short-circuits `sys.modules` on every later import. So
`monkeypatch.setitem(sys.modules, "src.store", fake)` alone is silently
ignored once anything earlier in the session imported the real module —
this is the Wave 1 test-isolation trap documented in tests/test_store.py
and tests/test_facts.py. `_install_fake_module` below therefore sets
**both** the `sys.modules` entry and the `src` package attribute, and lets
monkeypatch restore both.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import src as src_pkg
from src import llm
from src.api import (
    _DEFAULT_DAILY_CAP,
    _DEFAULT_RATE_LIMIT_PER_MIN,
    CANNED_CAPACITY_MESSAGE,
    create_app,
)
from src.schemas import Chunk, Citation, Fact, Miss, Mode, QueryResponse, ToolCall

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

MODES: list[Mode] = ["baseline_rag", "baseline_tools", "agent_custom", "agent_langgraph"]

STUB_ANSWER = "STUBBED-LLM-ANSWER: revenue rose year over year."

QUERY_RESPONSE_FIELDS = {
    "answer",
    "citations",
    "trace",
    "latency_ms",
    "mode",
    "incomplete",
    "refused",
}


# --- Provider resolution (no ambient .env dependence) -----------------------
#
# `src.llm` resolves PROVIDER/MODEL from the environment *at import time*, and
# `src/__init__.py` auto-loads the developer's `.env`. So anything in this file
# keyed to a hardcoded provider string would silently depend on local config
# and break the moment `LLM_PROVIDER` changes (it already went groq -> gemini
# mid-Wave-2). Instead these tests read `src.llm`'s own provider table and pin
# what they need explicitly. Same family of bug as the sys.modules trap above:
# a suite that quietly inherits ambient state passes in CI and fails on a
# contributor's machine.

STUB_MODEL = "stub-model-for-tests"


def _openai_compatible_table() -> dict[str, tuple[str, str]]:
    """`src.llm`'s provider -> (api-key env var, base URL) table.

    Reaching for a private name is deliberate: the alternative is hardcoding
    a provider string here, which is exactly the brittleness being removed.
    Falls back to a groq-shaped entry if the table is ever renamed, so this
    file degrades to the old behavior rather than erroring at collection.
    """
    return getattr(llm, "_OPENAI_COMPATIBLE", {"groq": ("GROQ_API_KEY", "")})


def _fully_supported_openai_compatible_providers() -> list[str]:
    """Providers wired through *every* provider-dispatch point in `src.llm`,
    not merely listed in its table.

    Probed rather than assumed, because `src.llm` is currently only
    half-migrated to the table: `default_client()` and `BASE_URL` are
    table-driven, but `tool_specs()` and `LLMSession.__init__` still branch
    on the literal string "groq" and raise `ValueError` for anything else.
    Both probes are pure constructor/translation calls — no network.
    """
    supported: list[str] = []
    for provider in sorted(_openai_compatible_table()):
        try:
            llm.tool_specs([], provider=provider)
            llm.LLMSession(object(), system="probe", question="probe", provider=provider)
        except ValueError:
            continue
        supported.append(provider)
    return supported


def _stub_provider() -> str:
    """An OpenAI-compatible provider that `src.llm` supports end to end.

    `FakeLLMClient` speaks the OpenAI-compatible `chat.completions` wire
    format, so the pinned provider must be one `src.llm` routes down that
    path. Prefers the process's configured provider when it qualifies — so
    the stub exercises the wire format the deployment will actually use —
    and otherwise falls back to a provider that does, which keeps this suite
    hermetic instead of inheriting whatever `LLM_PROVIDER` the developer's
    `.env` happens to set.
    """
    supported = _fully_supported_openai_compatible_providers()
    assert supported, "src.llm supports no OpenAI-compatible provider end to end"
    if llm.PROVIDER in supported:
        return llm.PROVIDER
    return supported[0]


def _all_provider_key_env_vars() -> set[str]:
    """Every API-key env var `src.llm` could build a live client from."""
    return {key_env for key_env, _base in _openai_compatible_table().values()} | {"ANTHROPIC_API_KEY"}


# --- Fake LLM client (the src.llm boundary) ---------------------------------


class _FakeFunction:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = json.dumps(arguments)


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content: str | None, tool_calls: list[_FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeCompletion:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, outer: "FakeLLMClient") -> None:
        self._outer = outer

    def create(self, **kwargs: Any) -> _FakeCompletion:
        return self._outer._create(**kwargs)


class _FakeChat:
    def __init__(self, outer: "FakeLLMClient") -> None:
        self.completions = _FakeCompletions(outer)


class FakeLLMClient:
    """Adaptive stand-in for `openai.OpenAI` against whichever
    OpenAI-compatible provider `src.llm` is pinned to. No network, ever.

    Deliberately **not** a fixed response script. The four arms differ in how
    many turns they take (baseline_rag: one tools-free call; baseline_tools:
    one tool call + one synthesis call; agent_custom: up to five turns;
    agent_langgraph: whatever LangGraph emits), and a fixed script would
    couple this integration test to each arm's private turn structure and
    break the moment one changes. Instead the rule is behavioral:

    - a request that offers tool schemas and whose conversation does not yet
      contain a tool result gets exactly one `search_filings` tool call back;
    - every other request gets the final text answer.

    The rule is keyed off the submitted `messages` rather than off counters
    on the client, so it is stateless: one `FakeLLMClient` can serve many
    independent `/query` requests in the same test and each conversation
    still gets its own tool call. (An earlier counter-based version silently
    starved the second and third requests of tool calls, which surfaced as
    spurious refusals — worth keeping stateless.)

    That drives baseline_tools' one-call cap and agent_custom's loop to a
    grounded, evidence-backed answer without either arm knowing it is
    stubbed. `.calls` records every request's kwargs so tests can assert the
    stub was genuinely in force.
    """

    def __init__(self, answer: str = STUB_ANSWER, provider: str = "") -> None:
        self.answer = answer
        self.provider = provider
        self.calls: list[dict[str, Any]] = []
        self.client_handouts = 0
        self.chat = _FakeChat(self)

    def _create(self, **kwargs: Any) -> _FakeCompletion:
        self.calls.append(kwargs)
        messages = kwargs.get("messages") or []
        already_used_a_tool = any(message.get("role") == "tool" for message in messages)
        if kwargs.get("tools") and not already_used_a_tool:
            return _FakeCompletion(
                _FakeMessage(
                    content=None,
                    tool_calls=[
                        _FakeToolCall(
                            "call_stub_1",
                            "search_filings",
                            {"query": "revenue"},
                        )
                    ],
                )
            )
        return _FakeCompletion(_FakeMessage(content=self.answer))


# --- Fixture-backed fake corpus (the src.store / src.facts boundary) --------


def _load_mini_corpus() -> list[Chunk]:
    raw = json.loads((FIXTURES_DIR / "mini_corpus.json").read_text(encoding="utf-8"))
    return [Chunk.model_validate(row) for row in raw]


def _load_mini_facts() -> list[dict[str, str]]:
    with open(FIXTURES_DIR / "mini_facts.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _fake_store_search(query: str, k: int = 5, filters: dict | None = None) -> list[Chunk]:
    """Same signature as `src.store.search`. Returns fixture chunks so the
    arms have real `Chunk` objects to cite — the point of the fake is to
    remove Chroma and the embedding model from the loop, not to change the
    shape of what retrieval returns.
    """
    chunks = _load_mini_corpus()
    for key, value in (filters or {}).items():
        chunks = [c for c in chunks if getattr(c, key, None) == value]
    return chunks[:k]


def _fake_facts_lookup(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss:
    """Same signature and `Fact | Miss` return contract as `src.facts.lookup`."""
    for row in _load_mini_facts():
        if row["ticker"] == ticker and row["metric"] == metric and int(row["fiscal_year"]) == fiscal_year:
            return Fact(
                ticker=row["ticker"],
                metric=row["metric"],
                fiscal_year=int(row["fiscal_year"]),
                fiscal_period_end=row["period_end"],
                value=float(row["value_usd"]),
                unit="USD",
            )
    return Miss(ticker=ticker, metric=metric, fiscal_year=fiscal_year, reason="not in mini_facts fixture")


def _install_fake_module(monkeypatch: pytest.MonkeyPatch, dotted_name: str, **attrs: Any) -> types.ModuleType:
    """Install a fake submodule so BOTH resolution paths see it.

    `sys.modules[dotted_name]` alone is not enough: `from src import store`
    reads the attribute already cached on the `src` package object and never
    consults sys.modules again (see this module's docstring). Setting both,
    via monkeypatch, is what makes the fake actually take effect and get
    cleanly torn down.
    """
    fake = types.ModuleType(dotted_name)
    for name, value in attrs.items():
        setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, dotted_name, fake)
    monkeypatch.setattr(src_pkg, dotted_name.rsplit(".", 1)[-1], fake, raising=False)
    return fake


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def trace_db(tmp_path) -> str:
    return str(tmp_path / "traces.sqlite")


@pytest.fixture
def no_live_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a live provider call structurally impossible for the duration of
    the test (see this module's docstring, guarantee #2).

    Derived from `src.llm`'s provider table rather than a hardcoded pair, so
    adding a provider there cannot silently open a hole in this guard.
    """
    for key_env in _all_provider_key_env_vars():
        monkeypatch.delenv(key_env, raising=False)


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch, no_live_api_keys: None) -> FakeLLMClient:
    """Replace the LLM at the `src.llm` boundary — the single place every arm
    reaches the provider through — and pin provider and model explicitly so
    the fake's wire format matches what `LLMSession` will drive, regardless
    of what the developer's `.env` happens to configure.
    """
    provider = _stub_provider()
    fake = FakeLLMClient(provider=provider)

    def _default_client(requested: str | None = None) -> FakeLLMClient:
        fake.client_handouts += 1
        return fake

    monkeypatch.setattr(llm, "PROVIDER", provider)
    monkeypatch.setattr(llm, "MODEL", STUB_MODEL)
    monkeypatch.setattr(llm, "default_client", _default_client)
    return fake


@pytest.fixture
def fake_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "src.store", search=_fake_store_search)
    _install_fake_module(monkeypatch, "src.facts", lookup=_fake_facts_lookup)


def _client(**overrides: Any) -> TestClient:
    # raise_server_exceptions=False so the app-wide exception handler (FR5.3)
    # is exercised the way a deployed server would exercise it.
    return TestClient(create_app(**overrides), raise_server_exceptions=False)


# --- The no-live-call guard, verified rather than assumed --------------------


def test_a_live_client_cannot_be_constructed_under_this_fixture(no_live_api_keys):
    """If this ever stops raising, every other test in this file could be
    making real, billed provider calls while still passing. That is the
    failure mode this assertion exists to make impossible.
    """
    import src.llm as real_llm

    # The exception *type* is src.llm's business and already changed once
    # mid-Wave-2 (KeyError from os.environ[...] -> a RuntimeError with an
    # actionable message). What this test pins is the property that matters:
    # with no key in the environment, no client object can be built, and the
    # failure names the missing variable so the operator knows what to set.
    keyless = set(real_llm.OLLAMA_NEEDS_NO_KEY)

    for provider, (key_env, _base) in sorted(_openai_compatible_table().items()):
        if provider in keyless:
            continue
        with pytest.raises((KeyError, RuntimeError)) as excinfo:
            real_llm.default_client(provider)
        assert key_env in str(excinfo.value)

    # Keyless providers are exempt by construction, but the exemption is
    # pinned rather than assumed. Two reasons to assert it instead of just
    # skipping:
    #
    #  1. A keyless provider CAN be constructed here, so the blanket
    #     protection above no longer covers everything. Ollama is loopback,
    #     and this file's socket guard deliberately permits loopback so
    #     TestClient works — so a mis-scoped stub could reach a real local
    #     Ollama and silently pass instead of failing. Not billed, but not
    #     hermetic either.
    #  2. If someone adds another keyless provider later, this fails until
    #     they have thought about (1).
    assert keyless == {"ollama"}, (
        f"New keyless provider(s) {keyless - {'ollama'}}: the no-live-client guard "
        "above cannot protect them. Confirm tests cannot reach them before "
        "widening this."
    )


def test_stub_llm_fixture_actually_replaces_the_boundary(stub_llm):
    assert llm.default_client() is stub_llm
    # The fixture pins these explicitly; they are never inherited from the
    # developer's .env, so this holds under any LLM_PROVIDER setting.
    assert llm.PROVIDER == stub_llm.provider
    assert llm.PROVIDER in _openai_compatible_table()
    assert llm.MODEL == STUB_MODEL


def test_stubbed_provider_and_model_are_what_the_arms_actually_send(stub_llm, fake_corpus, trace_db):
    """Proves the pinned model reaches the wire layer — i.e. the arms really
    went through the stubbed `src.llm`, not some other path.
    """
    client = _client(trace_db=trace_db, rate_limit_per_min=1000)

    client.post("/query", json={"question": "MSFT FY2024 revenue?", "mode": "agent_custom"})

    assert stub_llm.calls
    assert {call["model"] for call in stub_llm.calls} == {STUB_MODEL}


# --- All four modes, end to end through the real dispatch -------------------


def test_default_dispatch_wires_exactly_the_four_frozen_modes(trace_db):
    from src.agent import run_agent_custom
    from src.baseline import run_baseline_rag, run_baseline_tools

    dispatch = create_app(trace_db=trace_db).state.dispatch

    assert set(dispatch) == set(MODES)
    assert dispatch["baseline_rag"] is run_baseline_rag
    assert dispatch["baseline_tools"] is run_baseline_tools
    assert dispatch["agent_custom"] is run_agent_custom
    assert callable(dispatch["agent_langgraph"])


@pytest.mark.parametrize("mode", MODES)
def test_query_answers_end_to_end_in_every_mode(mode, stub_llm, fake_corpus, trace_db):
    client = _client(trace_db=trace_db, rate_limit_per_min=1000, daily_cap=1000)

    resp = client.post("/query", json={"question": "How did revenue change in FY2024?", "mode": mode})

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Full QueryResponse shape — every frozen field, nothing extra.
    assert set(body) == QUERY_RESPONSE_FIELDS
    assert body["mode"] == mode
    assert isinstance(body["answer"], str) and body["answer"].strip()
    assert body["latency_ms"] > 0
    assert body["incomplete"] is False
    assert body["refused"] is False

    assert body["citations"], "an evidence-backed answer must carry citations"
    for citation in body["citations"]:
        Citation.model_validate(citation)

    assert body["trace"], "every arm must record at least the retrieval call (FR4.2)"
    for call in body["trace"]:
        ToolCall.model_validate(call)
    assert "search_filings" in {call["name"] for call in body["trace"]}

    # The stub was genuinely in force: the arm reached the provider only
    # through src.llm, and the answer came from the fake, not the network.
    assert stub_llm.client_handouts >= 1, "src.llm.default_client stub was never consulted"
    assert stub_llm.calls, "the fake LLM client recorded no requests — the stub did not take effect"
    assert STUB_ANSWER in body["answer"]


def test_response_validates_against_the_frozen_query_response_contract(stub_llm, fake_corpus, trace_db):
    client = _client(trace_db=trace_db, rate_limit_per_min=1000)

    body = client.post("/query", json={"question": "MSFT FY2024 revenue?", "mode": "agent_custom"}).json()

    # Round-trips through the Wave 0 contract with extra="forbid".
    assert QueryResponse.model_validate(body).mode == "agent_custom"


def test_baseline_tools_makes_at_most_one_tool_call_through_the_api(stub_llm, fake_corpus, trace_db):
    """The confound control (FR3.2) must still hold when driven over HTTP,
    not only in Lane E's direct unit tests.
    """
    client = _client(trace_db=trace_db, rate_limit_per_min=1000)

    body = client.post("/query", json={"question": "MSFT FY2024 revenue?", "mode": "baseline_tools"}).json()

    assert len(body["trace"]) == 1
    assert body["incomplete"] is False


# --- Trace persistence (FR7.1) and /stats (FR7.2) ---------------------------


def test_traces_persist_to_sqlite_across_requests(stub_llm, fake_corpus, trace_db):
    client = _client(trace_db=trace_db, rate_limit_per_min=1000)

    for mode in ("baseline_rag", "baseline_tools", "agent_custom"):
        assert client.post("/query", json={"question": f"q for {mode}", "mode": mode}).status_code == 200

    conn = sqlite3.connect(trace_db)
    rows = conn.execute(
        "SELECT trace_id, question, mode, answer, latency_ms, tool_call_count, citation_count, refused FROM traces"
    ).fetchall()
    conn.close()

    assert len(rows) == 3
    assert len({row[0] for row in rows}) == 3, "each request must get its own trace id"
    assert {row[2] for row in rows} == {"baseline_rag", "baseline_tools", "agent_custom"}
    for trace_id, question, _mode, answer, latency_ms, tool_calls, citations, refused in rows:
        assert trace_id and question and answer
        assert latency_ms > 0
        assert tool_calls >= 1
        assert citations >= 1
        assert refused == 0


def test_stats_populates_from_real_dispatch_traffic(stub_llm, fake_corpus, trace_db):
    client = _client(trace_db=trace_db, rate_limit_per_min=1000)

    assert client.get("/stats").json()["operational"]["total_requests"] == 0

    client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    client.post("/query", json={"question": "q2", "mode": "agent_custom"})
    client.post("/query", json={"question": "q3", "mode": "agent_custom"})

    stats = client.get("/stats").json()
    operational = stats["operational"]

    assert operational["total_requests"] == 3
    assert operational["requests_by_mode"] == {"baseline_rag": 1, "agent_custom": 2}
    assert operational["latency_ms"]["p50"] > 0
    assert operational["latency_ms"]["p95"] > 0
    assert operational["refusal_rate"] == 0.0
    assert operational["tool_call_distribution"]["search_filings"] == 3
    assert "corpus" in stats


def test_stats_window_filter_is_honored(stub_llm, fake_corpus, trace_db):
    client = _client(trace_db=trace_db, rate_limit_per_min=1000)
    client.post("/query", json={"question": "q1", "mode": "baseline_rag"})

    assert client.get("/stats", params={"window": "24h"}).json()["operational"]["total_requests"] == 1
    # A window that ends before the request was recorded excludes it.
    assert client.get("/stats", params={"window": "0h"}).json()["operational"]["total_requests"] == 0


# --- FR6.3 safety rails, at their documented defaults ------------------------


def _canned_response(mode: Mode, question: str) -> QueryResponse:
    return QueryResponse(
        answer=f"[{mode}] {question}",
        citations=[],
        trace=[],
        latency_ms=0.0,
        mode=mode,
        incomplete=False,
        refused=False,
    )


def _counting_dispatch(call_log: list[str]) -> dict[str, Any]:
    """A dispatch that costs nothing to call. The rails are provider
    -independent by design — the whole point of the daily cap is that it
    fires *before* the arm runs — so exercising them against the real arms
    would only add LLM stubbing noise to what is a pure gating test.
    """

    def _make(mode: Mode):
        def _handler(question: str) -> QueryResponse:
            call_log.append(mode)
            return _canned_response(mode, question)

        return _handler

    return {mode: _make(mode) for mode in MODES}


def _env_example_int(name: str) -> int:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(rf"^{name}=(\d+)\s*$", text, flags=re.MULTILINE)
    assert match, f"{name} is not documented in .env.example"
    return int(match.group(1))


def test_code_defaults_match_the_values_documented_in_env_example():
    """The rails are only credible if the numbers in the docs are the numbers
    the code enforces when no env var is set (which is exactly the state of a
    fresh deploy that forgot to configure them).
    """
    assert _DEFAULT_RATE_LIMIT_PER_MIN == _env_example_int("RATE_LIMIT_PER_MIN") == 10
    assert _DEFAULT_DAILY_CAP == _env_example_int("DAILY_REQUEST_CAP") == 200


def test_rate_limit_engages_at_the_documented_default_of_ten_per_minute(monkeypatch, trace_db):
    monkeypatch.delenv("RATE_LIMIT_PER_MIN", raising=False)
    call_log: list[str] = []
    client = _client(dispatch=_counting_dispatch(call_log), daily_cap=10_000, trace_db=trace_db)

    statuses = [
        client.post("/query", json={"question": f"q{i}", "mode": "baseline_rag"}).status_code for i in range(12)
    ]

    assert statuses == [200] * 10 + [429, 429]
    assert len(call_log) == 10, "a rate-limited request must never reach an arm"

    rejected = client.post("/query", json={"question": "again", "mode": "baseline_rag"})
    body = rejected.json()
    assert body["error"] == "http_error"
    assert "rate limit" in body["detail"].lower()
    assert "Traceback" not in body["detail"]


def test_rate_limit_reads_the_env_var_when_set(monkeypatch, trace_db):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "3")
    client = _client(dispatch=_counting_dispatch([]), daily_cap=10_000, trace_db=trace_db)

    statuses = [
        client.post("/query", json={"question": f"q{i}", "mode": "baseline_rag"}).status_code for i in range(4)
    ]

    assert statuses == [200, 200, 200, 429]


def test_rate_limit_is_per_client_not_global(monkeypatch, trace_db):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "2")
    app = create_app(dispatch=_counting_dispatch([]), daily_cap=10_000, trace_db=trace_db)
    limiter = app.state.rate_limiter

    assert limiter.check("1.2.3.4") is True
    assert limiter.check("1.2.3.4") is True
    assert limiter.check("1.2.3.4") is False
    # A different IP still has its full budget — the limit is per-IP (FR6.3).
    assert limiter.check("5.6.7.8") is True


def test_daily_cap_engages_at_the_documented_default_of_two_hundred(monkeypatch, trace_db):
    monkeypatch.delenv("DAILY_REQUEST_CAP", raising=False)
    call_log: list[str] = []
    # The rate limiter is deliberately lifted here: the two rails are
    # independent, and the daily cap cannot be reached at 10 req/min.
    client = _client(dispatch=_counting_dispatch(call_log), rate_limit_per_min=10_000, trace_db=trace_db)

    for i in range(_DEFAULT_DAILY_CAP):
        assert client.post("/query", json={"question": f"q{i}", "mode": "baseline_rag"}).status_code == 200

    assert len(call_log) == _DEFAULT_DAILY_CAP

    over = client.post("/query", json={"question": "one too many", "mode": "baseline_rag"})

    # Degradation, not failure: still 200, still a well-formed QueryResponse.
    assert over.status_code == 200
    body = over.json()
    assert set(body) == QUERY_RESPONSE_FIELDS
    assert body["answer"] == CANNED_CAPACITY_MESSAGE
    assert body["refused"] is True
    assert body["citations"] == []
    assert body["trace"] == []
    assert body["mode"] == "baseline_rag"

    # And, crucially, no arm ran — that is what stops the key being spent.
    assert len(call_log) == _DEFAULT_DAILY_CAP


def test_daily_cap_reads_the_env_var_and_short_circuits_the_llm(monkeypatch, stub_llm, fake_corpus, trace_db):
    """Over the cap, `/query` must not reach `src.llm` at all — asserted
    against the real dispatch, since that is the path that would otherwise
    spend the provider key.
    """
    monkeypatch.setenv("DAILY_REQUEST_CAP", "1")
    client = _client(rate_limit_per_min=10_000, trace_db=trace_db)

    first = client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    assert first.status_code == 200
    calls_after_first = len(stub_llm.calls)
    assert calls_after_first >= 1

    second = client.post("/query", json={"question": "q2", "mode": "baseline_rag"})
    assert second.status_code == 200
    assert second.json()["answer"] == CANNED_CAPACITY_MESSAGE
    assert len(stub_llm.calls) == calls_after_first, "the capped request still reached the LLM"


def test_daily_cap_degradation_is_still_traced_and_counted(monkeypatch, trace_db):
    monkeypatch.setenv("DAILY_REQUEST_CAP", "1")
    client = _client(dispatch=_counting_dispatch([]), rate_limit_per_min=10_000, trace_db=trace_db)

    client.post("/query", json={"question": "q1", "mode": "baseline_rag"})
    client.post("/query", json={"question": "q2", "mode": "baseline_rag"})

    operational = client.get("/stats").json()["operational"]
    assert operational["total_requests"] == 2
    assert operational["refusal_rate"] == 0.5


# --- Arm unavailability degrades one mode, not the whole service -------------


def test_missing_langgraph_dependency_degrades_only_that_one_mode(monkeypatch, trace_db):
    """If `langgraph` is not installed in the deployed image, a hard import
    in `_default_dispatch` would make `import src.api` itself fail and take
    down all four modes. Simulated here by poisoning the module entry, which
    makes the import raise ImportError exactly as a missing dependency would.
    """
    from src.agent import run_agent_custom
    from src.baseline import run_baseline_rag, run_baseline_tools

    monkeypatch.setitem(sys.modules, "src.agent_langgraph", None)

    app = create_app(trace_db=trace_db, rate_limit_per_min=1000)

    assert app.state.dispatch["baseline_rag"] is run_baseline_rag
    assert app.state.dispatch["baseline_tools"] is run_baseline_tools
    assert app.state.dispatch["agent_custom"] is run_agent_custom

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/query", json={"question": "anything", "mode": "agent_langgraph"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "http_error"
    assert "agent_langgraph" in body["detail"]
    assert "Traceback" not in body["detail"]


# --- /healthz and /docs over the real app ------------------------------------


def test_healthz_reports_a_status_against_the_real_store(fake_corpus, trace_db):
    client = _client(trace_db=trace_db)

    body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["vector_store"] == "reachable"


def test_openapi_docs_are_served(trace_db):
    client = _client(trace_db=trace_db)

    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/query" in schema["paths"]
    assert "/stats" in schema["paths"]
    assert "/healthz" in schema["paths"]
