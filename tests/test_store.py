"""Unit tests for src/store.py (Lane B — PLAN.md Wave 1).

Runs against tests/fixtures/mini_corpus.json (20 chunks spanning both
tickers, both fiscal years, all three sections) rather than a real ingest
run. Each test gets an isolated on-disk Chroma directory via the
`CHROMA_DIR` env var + pytest's `tmp_path`, so tests never share state and
never touch a developer's real `./chroma_db`.

The metadata-filter tests are the load-bearing ones (PRD FR2.2): a query
filtered to one fiscal_year/ticker must never leak a chunk from the other
fiscal_year/ticker into the results, regardless of embedding similarity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.schemas import Chunk

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MINI_CORPUS_PATH = FIXTURES_DIR / "mini_corpus.json"


def _load_mini_corpus() -> list[Chunk]:
    raw = json.loads(MINI_CORPUS_PATH.read_text(encoding="utf-8"))
    return [Chunk.model_validate(row) for row in raw]


@pytest.fixture(autouse=True)
def isolated_chroma_dir(tmp_path, monkeypatch):
    """Point every test at its own throwaway persistence directory so
    add_chunks/search calls never collide across tests or with a real
    developer store.
    """
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma_db"))
    yield


@pytest.fixture
def store():
    """Import src.store fresh so its module-level client cache starts
    empty for each test, then remove it from sys.modules / the `src`
    package's attribute cache afterwards.

    Without this teardown, the first real `import src.store` anywhere in a
    pytest session permanently sets `src.store` as an attribute on the
    `src` package object. Other lanes' tests (tests/test_tools.py) rely on
    `monkeypatch.setitem(sys.modules, "src.store", fake)` plus a lazy
    `from src import store` inside src.tools to swap in a fixture-backed
    fake — but `from src import store` short-circuits to an *already-set*
    package attribute before it ever consults sys.modules again. Cleaning
    up here keeps this file's real import from leaking into and breaking
    test collection order for sibling lanes' test suites.
    """
    import importlib
    import sys

    import src.store as store_module

    importlib.reload(store_module)
    yield store_module

    sys.modules.pop("src.store", None)
    import src as src_pkg

    if hasattr(src_pkg, "store"):
        delattr(src_pkg, "store")


@pytest.fixture
def mini_corpus() -> list[Chunk]:
    return _load_mini_corpus()


def test_mini_corpus_fixture_has_both_tickers_and_years(mini_corpus):
    tickers = {chunk.ticker for chunk in mini_corpus}
    years = {chunk.fiscal_year for chunk in mini_corpus}
    sections = {chunk.section for chunk in mini_corpus}
    assert tickers == {"MSFT", "AAPL"}
    assert years == {2023, 2024}
    assert sections == {"item1", "item1a", "item7"}


def test_add_chunks_then_search_returns_chunks_with_metadata(store, mini_corpus):
    store.add_chunks(mini_corpus)

    results = store.search("segment revenue growth", k=5)

    assert len(results) == 5
    for chunk in results:
        assert isinstance(chunk, Chunk)
        assert chunk.chunk_id
        assert chunk.ticker in ("MSFT", "AAPL")
        assert chunk.section in ("item1", "item1a", "item7")
        assert chunk.source_url.startswith("https://www.sec.gov/")


def test_search_respects_k(store, mini_corpus):
    store.add_chunks(mini_corpus)

    results = store.search("risk factors", k=3)

    assert len(results) == 3


def test_search_on_empty_store_returns_empty_list(store):
    results = store.search("anything at all", k=5)

    assert results == []


def test_fiscal_year_filter_excludes_other_year(store, mini_corpus):
    """The whole point of FR2.2: filtering to fiscal_year=2024 must never
    return a 2023 chunk, even though 2023 MSFT chunks are semantically very
    close to this query (same company, same "operating segments" topic).
    """
    store.add_chunks(mini_corpus)

    results = store.search(
        "Microsoft operating segments",
        k=20,
        filters={"ticker": "MSFT", "fiscal_year": 2024},
    )

    assert results, "expected at least one MSFT FY2024 chunk to match"
    fiscal_years = {chunk.fiscal_year for chunk in results}
    assert fiscal_years == {2024}, f"a non-2024 chunk leaked through the filter: {fiscal_years}"


def test_ticker_filter_excludes_other_ticker(store, mini_corpus):
    """Filtering to ticker=AAPL must never return an MSFT chunk, even for a
    query phrase ("gross margin") both filers discuss.
    """
    store.add_chunks(mini_corpus)

    results = store.search("gross margin", k=20, filters={"ticker": "AAPL"})

    assert results, "expected at least one AAPL chunk to match"
    tickers = {chunk.ticker for chunk in results}
    assert tickers == {"AAPL"}, f"an MSFT chunk leaked through the ticker filter: {tickers}"


def test_section_filter_excludes_other_sections(store, mini_corpus):
    results_setup = store
    results_setup.add_chunks(mini_corpus)

    results = results_setup.search("company risks and disclosures", k=20, filters={"section": "item1a"})

    assert results, "expected at least one item1a chunk to match"
    sections = {chunk.section for chunk in results}
    assert sections == {"item1a"}, f"a non-item1a chunk leaked through the section filter: {sections}"


def test_combined_filters_narrow_to_exact_slice(store, mini_corpus):
    results = store
    results.add_chunks(mini_corpus)

    matches = results.search(
        "results of operations",
        k=20,
        filters={"ticker": "AAPL", "fiscal_year": 2024, "section": "item7"},
    )

    assert matches
    for chunk in matches:
        assert chunk.ticker == "AAPL"
        assert chunk.fiscal_year == 2024
        assert chunk.section == "item7"
    # Confirm against the fixture directly: exactly 2 AAPL/2024/item7 chunks exist.
    expected_ids = {
        chunk.chunk_id
        for chunk in mini_corpus
        if chunk.ticker == "AAPL" and chunk.fiscal_year == 2024 and chunk.section == "item7"
    }
    assert {chunk.chunk_id for chunk in matches} == expected_ids


def test_add_chunks_is_idempotent_on_reingest(store, mini_corpus):
    """Re-running ingestion must not duplicate records (FR1.6) — upsert is
    keyed on chunk_id.
    """
    store.add_chunks(mini_corpus)
    store.add_chunks(mini_corpus)  # simulate a second ingest run

    results = store.search("Microsoft", k=len(mini_corpus) + 10, filters={"ticker": "MSFT"})

    expected_ids = {chunk.chunk_id for chunk in mini_corpus if chunk.ticker == "MSFT"}
    result_ids = [chunk.chunk_id for chunk in results]

    assert len(result_ids) == len(set(result_ids)), "duplicate chunk_id returned after re-ingest"
    assert set(result_ids) == expected_ids


def test_add_chunks_upsert_updates_text_in_place(store, mini_corpus):
    """Re-adding the same chunk_id with different text should replace the
    stored document, not create a second entry.
    """
    original = mini_corpus[0]
    store.add_chunks([original])

    updated = original.model_copy(update={"text": "REVISED TEXT FOR UPSERT TEST"})
    store.add_chunks([updated])

    results = store.search(
        "REVISED TEXT FOR UPSERT TEST",
        k=5,
        filters={"ticker": original.ticker, "fiscal_year": original.fiscal_year},
    )

    matching = [chunk for chunk in results if chunk.chunk_id == original.chunk_id]
    assert len(matching) == 1
    assert matching[0].text == "REVISED TEXT FOR UPSERT TEST"


def test_add_chunks_with_empty_list_is_a_noop(store, mini_corpus):
    store.add_chunks(mini_corpus)
    before = store.search("segments", k=len(mini_corpus) + 5)

    store.add_chunks([])

    after = store.search("segments", k=len(mini_corpus) + 5)
    assert len(before) == len(after)
