"""XBRL company-facts fetch + hardcoded tag map (Lane C — PLAN.md Wave 1).

Owns: this file, exclusively.

Contract
--------
**The tag map is hardcoded for exactly the ~8 metrics the golden set asks
about** (PRD FR1.7): revenue, rnd_expense, gross_profit, operating_income,
net_income, total_assets, operating_cash_flow, sga. Do not build a general
US-GAAP tag resolver — tag selection (e.g. `Revenues` vs
`RevenueFromContractWithCustomerExcludingAssessedTax` vs
`RevenueFromContractWithCustomerIncludingAssessedTax`) is a known
multi-hour sink (PRD §11 Risks). The map below was derived by reading
data/reference/xbrl_facts.csv and data/reference/XBRL_FACTS.md and is
committed verbatim here — it is not re-derived from the raw companyfacts
payload at runtime.

Known asymmetry to preserve, not "fix": MSFT does not tag a combined
`SellingGeneralAndAdministrativeExpense` — it reports
`SellingAndMarketingExpense` and `GeneralAndAdministrativeExpense`
separately, so MSFT's `sga` metric is a genuine miss, not a bug (see
xbrl_facts.csv rows `MSFT,...,sga,(not reported)`, and golden item q025,
tier=unanswerable, kind="never_tagged"). AAPL does tag a combined `sga`.
Do not paper over this by summing MSFT's two components into a synthetic
`sga` value — that would silently invalidate q025.

Behavior:
1. Fetch (or, for the frozen build, read the already-fetched payload —
   data/reference/companyfacts_msft.json / companyfacts_aapl.json) the SEC
   `companyfacts` JSON for each of the two tickers.
2. For each of the 8 hardcoded metrics x 2 fiscal years x 2 tickers,
   extract the tagged value using the fixed tag map, or record its absence
   as a typed `Miss`.
3. `extract_facts` emits a normalized row per (metric, fiscal_year) with
   columns matching data/reference/xbrl_facts.csv: ticker, fiscal_year,
   period_end, metric, us_gaap_tag, value_usd, accn, form, source.

This module's output feeds src.facts (which loads it into the SQLite facts
table queried by `lookup_financial`). It does not query the facts table
itself.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Final

from src.schemas import Fact, Miss

# --- Paths ------------------------------------------------------------------

_REFERENCE_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "data" / "reference"

# --- Ticker -> CIK (needed only for the live-fetch refresh path) ------------

_CIK_BY_TICKER: Final[dict[str, str]] = {
    "MSFT": "0000789019",
    "AAPL": "0000320193",
}

# --- The 8 tracked metrics ---------------------------------------------------

TRACKED_METRICS: Final[tuple[str, ...]] = (
    "revenue",
    "rnd_expense",
    "operating_income",
    "net_income",
    "gross_profit",
    "total_assets",
    "operating_cash_flow",
    "sga",
)

# --- Hardcoded tag map, sourced from data/reference/xbrl_facts.csv ----------
# ticker -> metric -> us-gaap tag name, or None if the filer never tags this
# metric (the MSFT `sga` asymmetry — see module docstring).

TAG_MAP: Final[dict[str, dict[str, str | None]]] = {
    "MSFT": {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "rnd_expense": "ResearchAndDevelopmentExpense",
        "operating_income": "OperatingIncomeLoss",
        "net_income": "NetIncomeLoss",
        "gross_profit": "GrossProfit",
        "total_assets": "Assets",
        "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
        "sga": None,
    },
    "AAPL": {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "rnd_expense": "ResearchAndDevelopmentExpense",
        "operating_income": "OperatingIncomeLoss",
        "net_income": "NetIncomeLoss",
        "gross_profit": "GrossProfit",
        "total_assets": "Assets",
        "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
        "sga": "SellingGeneralAndAdministrativeExpense",
    },
}

# --- Fiscal year -> (period end, 10-K accession number), from xbrl_facts.csv

_FISCAL_PERIOD_END: Final[dict[str, dict[int, date]]] = {
    "MSFT": {2023: date(2023, 6, 30), 2024: date(2024, 6, 30)},
    "AAPL": {2023: date(2023, 9, 30), 2024: date(2024, 9, 28)},
}

_ACCESSION: Final[dict[str, dict[int, str]]] = {
    "MSFT": {2023: "0000950170-23-035122", 2024: "0000950170-24-087843"},
    "AAPL": {2023: "0000320193-23-000106", 2024: "0000320193-24-000123"},
}

_FORM: Final[str] = "10-K"
_SOURCE: Final[str] = "XBRL companyfacts"

# SEC rate limit: at most 10 requests/sec (PRD FR1.5 / PLAN.md Lane C).
_MIN_REQUEST_INTERVAL_SECONDS: Final[float] = 0.11


def fetch_company_facts(ticker: str) -> dict:
    """Fetch (or load a committed snapshot of) the raw SEC `companyfacts`
    JSON payload for one ticker.

    Prefers the already-fetched snapshot committed at
    data/reference/companyfacts_{ticker}.json (lowercase) — this is the path
    used by the frozen build and by tests. Falls back to a live EDGAR fetch
    (refresh path) using a compliant User-Agent built from the
    EDGAR_USER_AGENT env var; SEC returns 403 without a real contact email
    in that header.
    """
    ticker = ticker.upper()
    cached_path = _REFERENCE_DIR / f"companyfacts_{ticker.lower()}.json"
    if cached_path.exists():
        with cached_path.open(encoding="utf-8") as f:
            return json.load(f)

    return _fetch_company_facts_live(ticker)


def _fetch_company_facts_live(ticker: str) -> dict:
    """Live EDGAR fetch — refresh path only, not exercised during the frozen
    build (data/reference/companyfacts_*.json is always preferred first).
    """
    import requests  # local import: only needed on the live-fetch path

    cik = _CIK_BY_TICKER.get(ticker)
    if cik is None:
        raise ValueError(f"no known CIK for ticker {ticker!r}")

    user_agent = os.environ.get("EDGAR_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "EDGAR_USER_AGENT env var is required for a live SEC fetch "
            "(must contain a real contact email or SEC returns 403)."
        )

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    time.sleep(_MIN_REQUEST_INTERVAL_SECONDS)  # stay within 10 req/sec
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    response.raise_for_status()
    return response.json()


def _find_annual_value(companyfacts: dict, tag: str, accn: str, period_end: date) -> float | None:
    """Find the USD value tagged `tag` in `companyfacts` for the given 10-K
    accession number and period end. Returns None if the tag or the
    matching fact isn't present in the payload.
    """
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    tag_data = us_gaap.get(tag)
    if tag_data is None:
        return None

    entries = tag_data.get("units", {}).get("USD", [])
    period_end_str = period_end.isoformat()
    matches = [e for e in entries if e.get("accn") == accn and e.get("end") == period_end_str]
    if not matches:
        return None
    return float(matches[0]["val"])


def extract_facts(ticker: str, companyfacts: dict) -> list[dict]:
    """Apply the hardcoded 8-metric tag map to a raw companyfacts payload
    and return normalized rows (one per metric x fiscal_year tracked),
    matching the column set of data/reference/xbrl_facts.csv:
    ticker, fiscal_year, period_end, metric, us_gaap_tag, value_usd, accn,
    form, source.

    A metric never tagged for this filer (MSFT `sga`) or a fact absent from
    the payload produces a row with `us_gaap_tag`/`value_usd` set to None
    rather than being omitted — the absence is itself the reportable
    result. See module docstring for the MSFT `sga` asymmetry that must be
    preserved.
    """
    ticker = ticker.upper()
    tag_map = TAG_MAP.get(ticker, {})
    fiscal_years = _FISCAL_PERIOD_END.get(ticker, {})

    rows: list[dict] = []
    for fiscal_year, period_end in fiscal_years.items():
        accn = _ACCESSION[ticker][fiscal_year]
        for metric in TRACKED_METRICS:
            tag = tag_map.get(metric)
            value = (
                _find_annual_value(companyfacts, tag, accn, period_end) if tag is not None else None
            )
            rows.append(
                {
                    "ticker": ticker,
                    "fiscal_year": fiscal_year,
                    "period_end": period_end.isoformat(),
                    "metric": metric,
                    "us_gaap_tag": tag,
                    "value_usd": value,
                    "accn": accn if value is not None else None,
                    "form": _FORM if value is not None else None,
                    "source": _SOURCE if value is not None else None,
                }
            )
    return rows


def get_fact(ticker: str, metric: str, fiscal_year: int, companyfacts: dict | None = None) -> Fact | Miss:
    """Normalize a single (ticker, metric, fiscal_year) lookup to the
    `Fact` contract, or a typed `Miss` if nothing is tagged.

    Covers three distinct miss reasons: unknown ticker, unsupported fiscal
    year, and a metric that exists on one filer but is never XBRL-tagged on
    another (the MSFT `sga` case — golden item q025).
    """
    ticker = ticker.upper()

    if ticker not in TAG_MAP:
        return Miss(ticker=ticker, metric=metric, fiscal_year=fiscal_year, reason=f"unknown ticker {ticker!r}")

    if fiscal_year not in _FISCAL_PERIOD_END[ticker]:
        return Miss(
            ticker=ticker,
            metric=metric,
            fiscal_year=fiscal_year,
            reason=f"unsupported fiscal year {fiscal_year} for {ticker}",
        )

    tag = TAG_MAP[ticker].get(metric)
    if tag is None:
        return Miss(
            ticker=ticker,
            metric=metric,
            fiscal_year=fiscal_year,
            reason=f"metric {metric!r} is not tagged for {ticker} (never_tagged)",
        )

    if companyfacts is None:
        companyfacts = fetch_company_facts(ticker)

    period_end = _FISCAL_PERIOD_END[ticker][fiscal_year]
    accn = _ACCESSION[ticker][fiscal_year]
    value = _find_annual_value(companyfacts, tag, accn, period_end)
    if value is None:
        return Miss(
            ticker=ticker,
            metric=metric,
            fiscal_year=fiscal_year,
            reason=f"tag {tag!r} not found in companyfacts payload for accn {accn}",
        )

    return Fact(
        ticker=ticker,
        metric=metric,
        fiscal_year=fiscal_year,
        fiscal_period_end=period_end,
        value=value,
        unit="USD",
    )
