"""Unit tests for src/facts.py (Lane B — PLAN.md Wave 1).

Runs against tests/fixtures/mini_facts.csv rather than the real
data/reference/xbrl_facts.csv. That fixture deliberately contains two kinds
of "no number here" rows, and both must come back as a typed Miss, never an
exception and never a fabricated zero:

- MSFT `sga` FY2023/FY2024: a row exists, but `value_usd` is empty — the
  filer genuinely does not tag a combined SG&A figure (real corpus data;
  q025 in the golden set depends on this).
- AAPL FY2024 `total_assets` / `operating_cash_flow`: no row at all.

Each test gets an isolated SQLite file via the `FACTS_DB` env var +
pytest's `tmp_path`, so tests never share state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.schemas import Fact, Miss

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MINI_FACTS_CSV = FIXTURES_DIR / "mini_facts.csv"


@pytest.fixture(autouse=True)
def isolated_facts_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTS_DB", str(tmp_path / "facts.db"))
    yield


@pytest.fixture
def facts():
    """Import src.facts fresh for this test, then remove it from
    sys.modules / the `src` package's attribute cache afterwards.

    Without this teardown, the first real `import src.facts` anywhere in a
    pytest session permanently sets `src.facts` as an attribute on the
    `src` package object. Other lanes' tests (tests/test_tools.py) rely on
    `monkeypatch.setitem(sys.modules, "src.facts", fake)` plus a lazy
    `from src import facts` inside src.tools to swap in a fixture-backed
    fake — but `from src import facts` short-circuits to an *already-set*
    package attribute before it ever consults sys.modules again. Cleaning
    up here keeps this file's real import from leaking into and breaking
    test collection order for sibling lanes' test suites.
    """
    import importlib
    import sys

    import src.facts as facts_module

    importlib.reload(facts_module)
    yield facts_module

    sys.modules.pop("src.facts", None)
    import src as src_pkg

    if hasattr(src_pkg, "facts"):
        delattr(src_pkg, "facts")


@pytest.fixture
def loaded_facts(facts):
    facts.load_facts_from_csv(str(MINI_FACTS_CSV))
    return facts


# --- Fact hits ---------------------------------------------------------


def test_lookup_known_fact_returns_fact_with_expected_values(loaded_facts):
    result = loaded_facts.lookup("MSFT", "revenue", 2023)

    assert isinstance(result, Fact)
    assert result.ticker == "MSFT"
    assert result.metric == "revenue"
    assert result.fiscal_year == 2023
    assert result.fiscal_period_end.isoformat() == "2023-06-30"
    assert result.value == 211915000000
    assert result.unit == "USD"


def test_lookup_known_fact_for_other_ticker_and_year(loaded_facts):
    result = loaded_facts.lookup("AAPL", "net_income", 2024)

    assert isinstance(result, Fact)
    assert result.value == 93736000000
    assert result.fiscal_period_end.isoformat() == "2024-09-28"


# --- Typed misses --------------------------------------------------------


def test_lookup_unknown_ticker_returns_miss(loaded_facts):
    result = loaded_facts.lookup("TSLA", "revenue", 2023)

    assert isinstance(result, Miss)
    assert result.ticker == "TSLA"
    assert result.metric == "revenue"
    assert result.fiscal_year == 2023
    assert "ticker" in result.reason.lower()


def test_lookup_unknown_metric_returns_miss(loaded_facts):
    result = loaded_facts.lookup("MSFT", "ebitda", 2023)

    assert isinstance(result, Miss)
    assert "metric" in result.reason.lower()


def test_lookup_unsupported_fiscal_year_returns_miss(loaded_facts):
    result = loaded_facts.lookup("MSFT", "revenue", 2025)

    assert isinstance(result, Miss)
    assert result.fiscal_year == 2025
    assert "2025" in result.reason


@pytest.mark.parametrize("fiscal_year", [2023, 2024])
def test_msft_sga_is_a_clean_miss_not_a_zero(loaded_facts, fiscal_year):
    """The MSFT sga row exists (empty value_usd) — this must never surface
    as Fact(value=0.0); it is real corpus data (q025 depends on this).
    """
    result = loaded_facts.lookup("MSFT", "sga", fiscal_year)

    assert isinstance(result, Miss)
    assert result.reason  # non-empty, typed-miss contract (schemas.Miss)
    assert not isinstance(result, Fact)


def test_aapl_fy2024_total_assets_is_a_miss(loaded_facts):
    """mini_facts.csv deliberately omits this row entirely."""
    result = loaded_facts.lookup("AAPL", "total_assets", 2024)

    assert isinstance(result, Miss)
    assert result.ticker == "AAPL"
    assert result.metric == "total_assets"
    assert result.fiscal_year == 2024


def test_aapl_fy2024_operating_cash_flow_is_a_miss(loaded_facts):
    """mini_facts.csv deliberately omits this row entirely."""
    result = loaded_facts.lookup("AAPL", "operating_cash_flow", 2024)

    assert isinstance(result, Miss)
    assert result.ticker == "AAPL"
    assert result.metric == "operating_cash_flow"


def test_missing_row_and_untagged_row_have_distinct_reasons(loaded_facts):
    """A metric that was never loaded for this ticker/year (AAPL
    total_assets FY2024) and a metric that is loaded-but-untagged (MSFT sga)
    are both misses, but the agent needs different reasons to act on them.
    """
    never_loaded = loaded_facts.lookup("AAPL", "total_assets", 2024)
    untagged = loaded_facts.lookup("MSFT", "sga", 2023)

    assert isinstance(never_loaded, Miss)
    assert isinstance(untagged, Miss)
    assert never_loaded.reason != untagged.reason
    assert "tagged" in untagged.reason.lower()


def test_lookup_never_raises_for_absent_fact(loaded_facts):
    """FR4.4: absence must be a typed return, never an exception."""
    try:
        result = loaded_facts.lookup("NONEXISTENT", "nonexistent_metric", 1999)
    except Exception as exc:  # noqa: BLE001 - this is exactly what must not happen
        pytest.fail(f"lookup() raised instead of returning a Miss: {exc!r}")
    assert isinstance(result, Miss)


# --- Loading / idempotency -----------------------------------------------


def test_reload_replaces_rather_than_duplicates_rows(facts, tmp_path):
    db_path = str(tmp_path / "idempotency.db")
    facts.load_facts_from_csv(str(MINI_FACTS_CSV), db_path=db_path)
    facts.load_facts_from_csv(str(MINI_FACTS_CSV), db_path=db_path)  # re-ingest

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE ticker = ? AND metric = ? AND fiscal_year = ?",
            ("MSFT", "revenue", 2023),
        ).fetchone()
    finally:
        conn.close()

    assert count == 1


def test_reload_with_updated_value_overwrites_in_place(facts, tmp_path):
    db_path = str(tmp_path / "overwrite.db")
    row = {
        "ticker": "MSFT",
        "fiscal_year": "2023",
        "period_end": "2023-06-30",
        "metric": "revenue",
        "us_gaap_tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "value_usd": "211915000000",
        "accn": "0000950170-23-035122",
        "form": "10-K",
        "source": "XBRL companyfacts",
    }
    facts.load_facts([row], db_path=db_path)
    assert facts.lookup("MSFT", "revenue", 2023, db_path=db_path).value == 211915000000

    corrected = dict(row, value_usd="999000000000")
    facts.load_facts([corrected], db_path=db_path)

    result = facts.lookup("MSFT", "revenue", 2023, db_path=db_path)
    assert isinstance(result, Fact)
    assert result.value == 999000000000


def test_load_facts_accepts_row_with_empty_value_usd_without_crashing(facts, tmp_path):
    db_path = str(tmp_path / "empty_value.db")
    row = {
        "ticker": "MSFT",
        "fiscal_year": "2023",
        "period_end": "2023-06-30",
        "metric": "sga",
        "us_gaap_tag": "(not reported)",
        "value_usd": "",
        "accn": "",
        "form": "",
        "source": "NOT TAGGED - filer reports S&M and G&A separately",
    }

    facts.load_facts([row], db_path=db_path)  # must not raise
    result = facts.lookup("MSFT", "sga", 2023, db_path=db_path)

    assert isinstance(result, Miss)
