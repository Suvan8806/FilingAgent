"""Tests for src/xbrl.py (Lane C — PLAN.md Wave 1).

Owns: this file, exclusively.

Every one of the 8 tracked metrics x 2 tickers x 2 fiscal years is checked
against data/reference/xbrl_facts.csv: it must resolve to the exact
`value_usd` in the CSV (a typed `Fact`, in raw whole dollars — never
rounded), or to a typed `Miss` where the CSV's `value_usd` is blank.

The one asymmetry the golden set depends on (q025): AAPL tags a combined
`sga`; MSFT does not and must return a `Miss`, not zero, not a crash, and
not a silently-summed derivation of its two components.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from src.schemas import Fact, Miss
from src.xbrl import TAG_MAP, TRACKED_METRICS, extract_facts, fetch_company_facts, get_fact

_REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
_CSV_PATH = _REFERENCE_DIR / "xbrl_facts.csv"


def _load_expected_rows() -> list[dict]:
    """Read data/reference/xbrl_facts.csv, restricted to the 8 tracked
    metrics (the CSV also carries two informational sga breakdown rows per
    ticker/year that are out of scope here).
    """
    with _CSV_PATH.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [row for row in rows if row["metric"] in TRACKED_METRICS]


EXPECTED_ROWS = _load_expected_rows()


@pytest.fixture(scope="module")
def companyfacts() -> dict[str, dict]:
    return {
        "MSFT": fetch_company_facts("MSFT"),
        "AAPL": fetch_company_facts("AAPL"),
    }


def _expected_row_ids(rows: list[dict]) -> list[str]:
    return [f"{r['ticker']}-{r['fiscal_year']}-{r['metric']}" for r in rows]


class TestGetFact:
    """Every (ticker, metric, fiscal_year) combination in the CSV, for the
    8 tracked metrics, resolves to the exact value_usd or to a Miss.
    """

    @pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_expected_row_ids(EXPECTED_ROWS))
    def test_resolves_to_csv_value_or_miss(self, companyfacts: dict[str, dict], row: dict) -> None:
        ticker = row["ticker"]
        metric = row["metric"]
        fiscal_year = int(row["fiscal_year"])

        result = get_fact(ticker, metric, fiscal_year, companyfacts=companyfacts[ticker])

        if row["value_usd"] == "":
            assert isinstance(result, Miss), f"expected Miss for {ticker}/{metric}/{fiscal_year}, got {result!r}"
            assert result.ticker == ticker
            assert result.metric == metric
            assert result.fiscal_year == fiscal_year
            assert result.reason
        else:
            assert isinstance(result, Fact), f"expected Fact for {ticker}/{metric}/{fiscal_year}, got {result!r}"
            expected_value = float(row["value_usd"])
            assert result.value == expected_value
            assert result.ticker == ticker
            assert result.metric == metric
            assert result.fiscal_year == fiscal_year
            assert result.unit == "USD"
            assert result.fiscal_period_end == date.fromisoformat(row["period_end"])
            # Never rounded to billions.
            assert result.value == pytest.approx(expected_value, abs=0)


class TestSgaAsymmetry:
    """Golden item q025 depends on exactly this behavior."""

    def test_msft_sga_is_a_typed_miss(self, companyfacts: dict[str, dict]) -> None:
        for fiscal_year in (2023, 2024):
            result = get_fact("MSFT", "sga", fiscal_year, companyfacts=companyfacts["MSFT"])
            assert isinstance(result, Miss)
            assert result.ticker == "MSFT"
            assert result.metric == "sga"
            assert result.fiscal_year == fiscal_year
            assert not hasattr(result, "value")  # Miss carries no numeric value at all

    def test_aapl_sga_is_a_fact(self, companyfacts: dict[str, dict]) -> None:
        for fiscal_year, expected_value in ((2023, 24932000000.0), (2024, 26097000000.0)):
            result = get_fact("AAPL", "sga", fiscal_year, companyfacts=companyfacts["AAPL"])
            assert isinstance(result, Fact)
            assert result.value == expected_value

    def test_msft_sga_miss_does_not_sum_components(self, companyfacts: dict[str, dict]) -> None:
        """MSFT tags SellingAndMarketingExpense and GeneralAndAdministrativeExpense
        separately, which together would equal a plausible-looking sga figure.
        get_fact must not derive/sum them — it must return a typed Miss.
        """
        result = get_fact("MSFT", "sga", 2024, companyfacts=companyfacts["MSFT"])
        # 24,456,000,000 (S&M) + 7,609,000,000 (G&A) = 32,065,000,000 — must not appear as a value.
        assert isinstance(result, Miss)
        assert not hasattr(result, "value")


class TestUnknownInputs:
    def test_unknown_ticker_is_a_miss(self) -> None:
        result = get_fact("GOOG", "revenue", 2024, companyfacts={})
        assert isinstance(result, Miss)
        assert result.ticker == "GOOG"
        assert "unknown ticker" in result.reason

    def test_unsupported_fiscal_year_is_a_miss(self, companyfacts: dict[str, dict]) -> None:
        result = get_fact("AAPL", "revenue", 2020, companyfacts=companyfacts["AAPL"])
        assert isinstance(result, Miss)
        assert result.fiscal_year == 2020


class TestTagMap:
    """The tag map is hardcoded, not derived at runtime (PLAN.md Lane C)."""

    def test_tag_map_covers_both_tickers_and_all_tracked_metrics(self) -> None:
        assert set(TAG_MAP.keys()) == {"MSFT", "AAPL"}
        for ticker, metrics in TAG_MAP.items():
            assert set(metrics.keys()) == set(TRACKED_METRICS), ticker

    def test_msft_sga_tag_is_none(self) -> None:
        assert TAG_MAP["MSFT"]["sga"] is None

    def test_aapl_sga_tag_is_selling_general_and_administrative(self) -> None:
        assert TAG_MAP["AAPL"]["sga"] == "SellingGeneralAndAdministrativeExpense"

    def test_shared_tags_match_between_filers(self) -> None:
        for metric in TRACKED_METRICS:
            if metric == "sga":
                continue
            assert TAG_MAP["MSFT"][metric] == TAG_MAP["AAPL"][metric]


class TestExtractFacts:
    def test_extract_facts_emits_one_row_per_metric_per_year(self, companyfacts: dict[str, dict]) -> None:
        rows = extract_facts("MSFT", companyfacts["MSFT"])
        assert len(rows) == len(TRACKED_METRICS) * 2  # 2 fiscal years

        row_keys = {(r["fiscal_year"], r["metric"]) for r in rows}
        for fiscal_year in (2023, 2024):
            for metric in TRACKED_METRICS:
                assert (fiscal_year, metric) in row_keys

    def test_extract_facts_msft_sga_row_has_no_tag_or_value(self, companyfacts: dict[str, dict]) -> None:
        rows = extract_facts("MSFT", companyfacts["MSFT"])
        sga_rows = [r for r in rows if r["metric"] == "sga"]
        assert len(sga_rows) == 2
        for row in sga_rows:
            assert row["us_gaap_tag"] is None
            assert row["value_usd"] is None

    def test_extract_facts_aapl_sga_row_has_tag_and_value(self, companyfacts: dict[str, dict]) -> None:
        rows = extract_facts("AAPL", companyfacts["AAPL"])
        sga_2024 = next(r for r in rows if r["metric"] == "sga" and r["fiscal_year"] == 2024)
        assert sga_2024["us_gaap_tag"] == "SellingGeneralAndAdministrativeExpense"
        assert sga_2024["value_usd"] == 26097000000.0

    def test_extract_facts_values_match_csv(self, companyfacts: dict[str, dict]) -> None:
        rows_by_key = {(r["fiscal_year"], r["metric"]): r for r in extract_facts("AAPL", companyfacts["AAPL"])}
        for row in EXPECTED_ROWS:
            if row["ticker"] != "AAPL":
                continue
            key = (int(row["fiscal_year"]), row["metric"])
            extracted = rows_by_key[key]
            if row["value_usd"] == "":
                assert extracted["value_usd"] is None
            else:
                assert extracted["value_usd"] == float(row["value_usd"])


class TestFetchCompanyFacts:
    def test_prefers_committed_snapshot_over_live_fetch(self) -> None:
        # Should not raise / not require EDGAR_USER_AGENT, since the
        # committed snapshots at data/reference/companyfacts_*.json exist.
        payload = fetch_company_facts("MSFT")
        assert payload["cik"] == 789019
        assert "us-gaap" in payload["facts"]

    def test_ticker_is_case_insensitive(self) -> None:
        payload = fetch_company_facts("msft")
        assert payload["entityName"] == "MICROSOFT CORPORATION"
