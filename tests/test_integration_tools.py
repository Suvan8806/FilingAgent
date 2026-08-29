"""Integration tests: src/tools.py against the REAL src/store.py and
src/facts.py (Agent I — PLAN.md Wave 2.1, "Wire real store/facts into
tools").

How this differs from tests/test_tools.py
-----------------------------------------
`tests/test_tools.py` is Lane D's unit suite: it injects fixture-backed
fake modules into `sys.modules["src.store"]` / `sys.modules["src.facts"]`
so the three tools can be tested in isolation while Lane B was still being
written. Those fakes are hand-written stand-ins, which means they can agree
with `src/tools.py` while the *real* Lane B modules disagree with it — the
exact interface-drift failure mode PLAN.md's "Risks and guards" table calls
out. A green unit suite is therefore not evidence that the wiring works.

This file closes that gap. It imports the real `src.store` (ChromaDB) and
`src.facts` (SQLite) modules — no `sys.modules` substitution anywhere — and
drives them through the public tool functions. It stays CI-safe and fast by
pointing `CHROMA_DIR` / `FACTS_DB` at pytest's `tmp_path` and seeding them
from the frozen Wave 0 fixtures (`tests/fixtures/mini_corpus.json`,
`tests/fixtures/mini_facts.csv`), so it never depends on `make ingest`
having been run, never touches a developer's real `./chroma_db` or
`./facts.db`, and makes no network calls.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from src.schemas import Chunk, Fact, Miss

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MINI_CORPUS_PATH = FIXTURES_DIR / "mini_corpus.json"
MINI_FACTS_PATH = FIXTURES_DIR / "mini_facts.csv"


@pytest.fixture(scope="module")
def real_backends(tmp_path_factory):
    """Seed a real Chroma collection and a real SQLite facts table in a
    throwaway directory, and hand back the genuine `src.store` / `src.facts`
    modules. Module-scoped because embedding the 20 fixture chunks with
    Chroma's local ONNX MiniLM is the slow part and the data is read-only
    for every test below.

    The teardown is load-bearing, for the reason Lane B documents in
    tests/test_store.py and tests/test_facts.py: a real `import src.store`
    permanently sets `store` as an attribute on the `src` *package* object,
    and `from src import store` short-circuits to that attribute before it
    ever consults `sys.modules`. Leaving it set would silently defeat
    tests/test_tools.py's `monkeypatch.setitem(sys.modules, "src.store",
    fake)` in any run where this file collects first — turning a fake-backed
    unit test into an accidental integration test. So this module reverses
    its own imports on the way out.
    """
    import importlib
    import sys

    tmp_path = tmp_path_factory.mktemp("tools_integration")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma_db"))
    monkeypatch.setenv("FACTS_DB", str(tmp_path / "facts.db"))

    import src as src_pkg
    import src.facts as facts_module
    import src.store as store_module

    # Reload so each module's env-var-derived caches (src.store's per-path
    # client cache) are rebuilt against the tmp dirs set above.
    importlib.reload(store_module)
    importlib.reload(facts_module)

    chunks = [
        Chunk.model_validate(row)
        for row in json.loads(MINI_CORPUS_PATH.read_text(encoding="utf-8"))
    ]
    store_module.add_chunks(chunks)
    facts_module.load_facts_from_csv(str(MINI_FACTS_PATH))

    yield store_module, facts_module

    for dotted, attr in (("src.store", "store"), ("src.facts", "facts")):
        sys.modules.pop(dotted, None)
        if hasattr(src_pkg, attr):
            delattr(src_pkg, attr)
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def _use_real_backends(real_backends):
    """Every test in this module runs against the real modules; `src.tools`
    resolves them by `from src import store` at call time, so simply having
    them seeded and un-faked is enough.
    """
    return real_backends


# --- interface-drift guards -------------------------------------------------
# These assert the exact call shapes src/tools.py uses actually bind against
# the real Lane B signatures. A signature change in src/store.py or
# src/facts.py fails here with a readable message instead of surfacing as a
# TypeError inside a live agent loop.


def test_store_search_accepts_the_kwargs_tools_passes(real_backends):
    store, _ = real_backends
    signature = inspect.signature(store.search)
    signature.bind("a query", k=5, filters={"ticker": "MSFT"})
    signature.bind("a query", k=5, filters=None)


def test_facts_lookup_accepts_the_positional_args_tools_passes(real_backends):
    _, facts = real_backends
    signature = inspect.signature(facts.lookup)
    signature.bind("MSFT", "revenue", 2024)


# --- search_filings against real ChromaDB -----------------------------------


def test_search_filings_returns_real_chunks_from_chroma():
    from src.tools import search_filings

    results = search_filings("operating segment performance")

    assert results, "real Chroma search returned nothing for a seeded corpus"
    assert len(results) <= 5, "FR2.1 top-5 budget must survive the real store"
    assert all(isinstance(chunk, Chunk) for chunk in results)
    assert all(chunk.chunk_id and chunk.source_url for chunk in results)


def test_search_filings_ticker_filter_is_a_hard_filter_in_the_real_store():
    from src.tools import search_filings

    results = search_filings("risk factors", ticker="AAPL")

    assert results
    assert all(chunk.ticker == "AAPL" for chunk in results), (
        "FR2.2: a ticker filter must never leak another filer's chunk, "
        "however similar the embedding"
    )


def test_search_filings_combined_filters_round_trip_through_chroma():
    from src.tools import search_filings

    results = search_filings("revenue", ticker="MSFT", fiscal_year=2024, section="item7")

    assert results
    assert all(
        chunk.ticker == "MSFT" and chunk.fiscal_year == 2024 and chunk.section == "item7"
        for chunk in results
    )


def test_search_filings_unmatchable_filter_returns_empty_list_not_error():
    from src.tools import search_filings

    assert search_filings("anything at all", ticker="MSFT", fiscal_year=2099) == []


def test_search_filings_preserves_chunk_metadata_types_through_chroma():
    """Chroma metadata is str/int/float/bool only — dates are stored as ISO
    strings by src.store and must come back as real `date` objects, or
    citations downstream silently degrade to strings.
    """
    from datetime import date

    from src.tools import search_filings

    chunk = search_filings("business overview", ticker="MSFT")[0]

    assert isinstance(chunk.fiscal_year, int)
    assert isinstance(chunk.fiscal_period_end, date)
    assert isinstance(chunk.filing_date, date)


# --- lookup_financial against real SQLite -----------------------------------


def test_lookup_financial_returns_a_real_fact_from_sqlite():
    from src.tools import lookup_financial

    result = lookup_financial("MSFT", "revenue", 2024)

    assert isinstance(result, Fact)
    assert result.value == 245122000000.0
    assert result.unit == "USD"
    assert result.fiscal_period_end.isoformat() == "2024-06-30"


def test_lookup_financial_msft_sga_is_a_typed_miss_through_the_real_store():
    """The MSFT `sga` row exists with an empty value_usd (real corpus data —
    the filer reports S&M and G&A separately). The real facts layer must
    turn that into a Miss with a specific reason, not a Fact with value 0.0
    (golden item q025).
    """
    from src.tools import lookup_financial

    result = lookup_financial("MSFT", "sga", 2024)

    assert isinstance(result, Miss)
    assert result.reason


def test_lookup_financial_aapl_sga_is_a_real_fact_through_the_real_store():
    from src.tools import lookup_financial

    result = lookup_financial("AAPL", "sga", 2024)

    assert isinstance(result, Fact)
    assert result.value == 26097000000.0


@pytest.mark.parametrize(
    "ticker,metric,fiscal_year",
    [
        ("TSLA", "revenue", 2024),  # unknown ticker
        ("MSFT", "revenue", 2019),  # unsupported fiscal year
        ("MSFT", "ebitda", 2024),  # unknown metric
    ],
)
def test_lookup_financial_absent_facts_are_misses_never_exceptions(
    ticker, metric, fiscal_year
):
    from src.tools import lookup_financial

    result = lookup_financial(ticker, metric, fiscal_year)

    assert isinstance(result, Miss)
    assert result.reason


# --- idempotency (FR1.6) ----------------------------------------------------


def test_reloading_fixtures_does_not_duplicate_rows_or_chunks(real_backends):
    """Both write paths upsert rather than insert, so `make ingest` is safe
    to re-run. Asserted here because it is the property that lets Wave 3
    re-run measurement without rebuilding the index from scratch.
    """
    store, facts = real_backends

    before = store._get_collection().count()
    chunks = [
        Chunk.model_validate(row)
        for row in json.loads(MINI_CORPUS_PATH.read_text(encoding="utf-8"))
    ]
    store.add_chunks(chunks)
    facts.load_facts_from_csv(str(MINI_FACTS_PATH))

    assert store._get_collection().count() == before
    assert isinstance(lookup_after_reload(), Fact)


def lookup_after_reload() -> Fact | Miss:
    from src.tools import lookup_financial

    return lookup_financial("MSFT", "revenue", 2024)
