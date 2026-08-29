"""Unit tests for src/tools.py (Lane D — PLAN.md Wave 1).

Lane B (`src/store.py`, `src/facts.py`) and Lane C (`src/xbrl.py`) are being
written concurrently in other lanes and are not implemented yet — and even
once they are, `src/store.py` imports `chromadb`, a dependency this test
environment does not install. So these tests never import the real
`src.store` / `src.facts` modules. Instead, before each test, a fixture
-backed fake module is injected directly into `sys.modules["src.store"]`
and `sys.modules["src.facts"]`, built from the frozen fixtures
(`tests/fixtures/mini_corpus.json`, `tests/fixtures/mini_facts.csv`).

`src.tools` imports `store` / `facts` lazily, inside each tool function
body (see its module docstring), so `from src import store` at call time
resolves to whatever is currently in `sys.modules["src.store"]` — real or
fake — without ever executing the real module's top-level code.
"""

from __future__ import annotations

import csv
import json
import sys
import types
from datetime import date
from pathlib import Path

import pytest

from src.schemas import Chunk, Fact, Miss
from src.tools import CalculatorError, calculate, lookup_financial, search_filings

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


# --- fixture loading ---------------------------------------------------------


def _load_mini_corpus() -> list[Chunk]:
    raw = json.loads((FIXTURES_DIR / "mini_corpus.json").read_text(encoding="utf-8"))
    return [Chunk.model_validate(row) for row in raw]


def _load_mini_facts() -> dict[tuple[str, str, int], dict[str, str]]:
    rows: dict[tuple[str, str, int], dict[str, str]] = {}
    with (FIXTURES_DIR / "mini_facts.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["ticker"], row["metric"], int(row["fiscal_year"]))
            rows[key] = row
    return rows


MINI_CORPUS = _load_mini_corpus()
MINI_FACTS = _load_mini_facts()
KNOWN_TICKERS = {row["ticker"] for row in MINI_FACTS.values()}
KNOWN_FISCAL_YEARS = {int(row["fiscal_year"]) for row in MINI_FACTS.values()}


# --- fixture-backed fakes for src.store / src.facts -------------------------


def _fake_store_search(query: str, k: int = 5, filters: dict | None = None) -> list[Chunk]:
    filters = filters or {}
    results = [
        chunk
        for chunk in MINI_CORPUS
        if all(getattr(chunk, key) == value for key, value in filters.items())
    ]
    return results[:k]


def _fake_facts_lookup(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss:
    if ticker not in KNOWN_TICKERS:
        return Miss(ticker=ticker, metric=metric, fiscal_year=fiscal_year, reason="unknown ticker")
    if fiscal_year not in KNOWN_FISCAL_YEARS:
        return Miss(
            ticker=ticker, metric=metric, fiscal_year=fiscal_year, reason="unsupported fiscal year"
        )
    row = MINI_FACTS.get((ticker, metric, fiscal_year))
    if row is None or not row["value_usd"]:
        return Miss(
            ticker=ticker,
            metric=metric,
            fiscal_year=fiscal_year,
            reason="metric not tagged for this filer in this fiscal year",
        )
    return Fact(
        ticker=ticker,
        metric=metric,
        fiscal_year=fiscal_year,
        fiscal_period_end=date.fromisoformat(row["period_end"]),
        value=float(row["value_usd"]),
        unit="USD",
    )


def _fake_facts_lookup_raises(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss:
    raise RuntimeError("simulated internal facts-store failure")


def _install_fake_module(monkeypatch: pytest.MonkeyPatch, dotted_name: str, **attrs) -> types.ModuleType:
    fake = types.ModuleType(dotted_name)
    for name, value in attrs.items():
        setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, dotted_name, fake)
    return fake


@pytest.fixture(autouse=True)
def fake_backends(monkeypatch: pytest.MonkeyPatch):
    """Install fixture-backed fakes for src.store and src.facts before every
    test, and let monkeypatch restore sys.modules afterwards. Tests that
    need different behavior (e.g. a raising facts.lookup) re-install their
    own fake mid-test — that's fine, monkeypatch still cleans up.
    """
    _install_fake_module(monkeypatch, "src.store", search=_fake_store_search)
    _install_fake_module(monkeypatch, "src.facts", lookup=_fake_facts_lookup)


# --- search_filings ---------------------------------------------------------


def test_search_filings_unfiltered_returns_chunks_capped_at_five():
    results = search_filings("segment information")
    assert len(results) == 5
    assert all(isinstance(chunk, Chunk) for chunk in results)


def test_search_filings_filters_by_ticker():
    results = search_filings("risk", ticker="AAPL")
    assert results
    assert all(chunk.ticker == "AAPL" for chunk in results)


def test_search_filings_filters_by_fiscal_year():
    results = search_filings("revenue", fiscal_year=2023)
    assert results
    assert all(chunk.fiscal_year == 2023 for chunk in results)


def test_search_filings_filters_by_section():
    results = search_filings("results of operations", section="item7")
    assert results
    assert all(chunk.section == "item7" for chunk in results)


def test_search_filings_combines_all_filters():
    results = search_filings("gross margin", ticker="AAPL", fiscal_year=2024, section="item7")
    assert results
    assert all(
        chunk.ticker == "AAPL" and chunk.fiscal_year == 2024 and chunk.section == "item7"
        for chunk in results
    )


def test_search_filings_no_match_returns_empty_list_not_error():
    results = search_filings("anything", ticker="MSFT", fiscal_year=2099)
    assert results == []


def test_search_filings_forwards_query_and_top5_budget_to_store(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def spy_search(query, k=5, filters=None):
        calls.append({"query": query, "k": k, "filters": filters})
        return []

    _install_fake_module(monkeypatch, "src.store", search=spy_search)

    search_filings("principal supply chain risk", ticker="AAPL", section="item1a")

    assert len(calls) == 1
    assert calls[0]["query"] == "principal supply chain risk"
    assert calls[0]["k"] == 5
    assert calls[0]["filters"] == {"ticker": "AAPL", "section": "item1a"}


def test_search_filings_omits_none_filters_entirely(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def spy_search(query, k=5, filters=None):
        calls.append(filters)
        return []

    _install_fake_module(monkeypatch, "src.store", search=spy_search)

    search_filings("anything")

    assert calls == [None]


# --- lookup_financial ---------------------------------------------------------


def test_lookup_financial_returns_fact_for_known_metric():
    result = lookup_financial("MSFT", "revenue", 2024)
    assert isinstance(result, Fact)
    assert result.value == 245122000000.0
    assert result.unit == "USD"
    assert result.fiscal_year == 2024


def test_lookup_financial_returns_fact_matching_xbrl_facts_csv():
    result = lookup_financial("AAPL", "net_income", 2023)
    assert isinstance(result, Fact)
    assert result.value == 96995000000.0


def test_lookup_financial_unknown_ticker_returns_typed_miss_not_raise():
    result = lookup_financial("TSLA", "revenue", 2024)
    assert isinstance(result, Miss)
    assert result.ticker == "TSLA"
    assert "unknown ticker" in result.reason


def test_lookup_financial_missing_fiscal_year_returns_typed_miss_not_raise():
    result = lookup_financial("MSFT", "revenue", 2019)
    assert isinstance(result, Miss)
    assert "fiscal year" in result.reason


def test_lookup_financial_msft_sga_is_a_clean_typed_miss_not_error():
    """MSFT reports S&M and G&A separately, never a combined `sga` tag
    (see src/xbrl.py module docstring and golden item q025). This must
    surface as a Miss with a specific reason, not a KeyError/None.
    """
    result = lookup_financial("MSFT", "sga", 2024)
    assert isinstance(result, Miss)
    assert result.ticker == "MSFT"
    assert result.metric == "sga"
    assert result.reason  # non-empty, human-readable


def test_lookup_financial_aapl_sga_is_a_real_fact_not_a_miss():
    """The asymmetry cuts both ways: AAPL does tag a combined sga, so this
    must NOT come back as a miss (guards against over-broad "sga is always
    a miss" bugs).
    """
    result = lookup_financial("AAPL", "sga", 2024)
    assert isinstance(result, Fact)
    assert result.value == 26097000000.0


def test_lookup_financial_never_raises_on_unexpected_internal_error(monkeypatch: pytest.MonkeyPatch):
    """FR4.4: the tool boundary must never raise. Even if the layer below
    (src.facts) has a bug and raises unexpectedly, lookup_financial must
    convert it into a typed Miss rather than propagate the exception.
    """
    _install_fake_module(monkeypatch, "src.facts", lookup=_fake_facts_lookup_raises)

    result = lookup_financial("MSFT", "revenue", 2024)

    assert isinstance(result, Miss)
    assert "internal lookup error" in result.reason


# --- calculate ---------------------------------------------------------


def test_calculate_basic_arithmetic():
    assert calculate("2 + 2") == 4.0


def test_calculate_operator_precedence_and_parentheses():
    assert calculate("(245122000000 - 211915000000) / 211915000000") == pytest.approx(
        0.15669962013071279
    )


def test_calculate_unary_minus():
    assert calculate("-5 + 3") == -2.0


def test_calculate_exponent():
    assert calculate("2 ** 10") == 1024.0


def test_calculate_returns_float():
    result = calculate("4 / 2")
    assert isinstance(result, float)
    assert result == 2.0


def test_calculate_large_financial_figures_preserve_precision():
    # Real MSFT FY2024 vs FY2023 revenue delta (golden item q014 territory).
    assert calculate("245122000000 - 211915000000") == 33207000000.0


def test_calculate_malformed_expression_raises_calculator_error():
    with pytest.raises(CalculatorError):
        calculate("2 + ")


def test_calculate_empty_expression_raises_calculator_error():
    with pytest.raises(CalculatorError):
        calculate("")


def test_calculate_division_by_zero_raises_calculator_error_not_zerodivisionerror():
    with pytest.raises(CalculatorError):
        calculate("1 / 0")


def test_calculate_division_by_zero_inside_larger_expression():
    with pytest.raises(CalculatorError):
        calculate("(10 - 10) / (5 - 5)")


@pytest.mark.parametrize(
    "malicious_expression",
    [
        "__import__('os').system('echo pwned')",  # Call + Attribute + Name
        "os.system('echo pwned')",  # Attribute + Name
        "x",  # bare Name
        "x + 1",  # Name inside BinOp
        "(1).bit_length()",  # Call + Attribute on a literal
        "[1, 2, 3][0]",  # List + Subscript
        "[i for i in range(10)]",  # ListComp + Call + Name
        "2 < 3",  # Compare
        "True",  # bool Constant, must not be treated as numeric
        "None",  # NoneType Constant
        "'2 + 2'",  # str Constant
        "print(1)",  # Call + Name
        "lambda: 1",  # Lambda
        "1; 2",  # statement separator - invalid in eval mode
    ],
)
def test_calculate_rejects_injection_attempts(malicious_expression):
    with pytest.raises(CalculatorError):
        calculate(malicious_expression)
