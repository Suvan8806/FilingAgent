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
multi-hour sink (PRD §11 Risks). The map is already derivable by reading
data/reference/xbrl_facts.csv and data/reference/XBRL_FACTS.md — read them,
don't re-derive from the raw companyfacts payload. Timebox: 30 minutes
(PLAN.md Lane C).

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
   extract the tagged value using the fixed tag map, or record its absence.
3. Emit a normalized row per (ticker, metric, fiscal_year) with columns
   matching data/reference/xbrl_facts.csv: ticker, fiscal_year, period_end,
   metric, us_gaap_tag, value_usd, accn, form, source.

This module's output feeds src.facts (which loads it into the SQLite facts
table queried by `lookup_financial`). It does not query the facts table
itself.
"""

from __future__ import annotations


def fetch_company_facts(ticker: str) -> dict:
    """Fetch (or load a committed snapshot of) the raw SEC `companyfacts`
    JSON payload for one ticker. Requires a compliant EDGAR User-Agent
    (.env.example EDGAR_USER_AGENT) when hitting the live API.
    """
    raise NotImplementedError


def extract_facts(ticker: str, companyfacts: dict) -> list[dict]:
    """Apply the hardcoded 8-metric tag map to a raw companyfacts payload
    and return normalized rows (one per metric x fiscal_year present),
    matching the column set of data/reference/xbrl_facts.csv. See module
    docstring for the metric list and the MSFT `sga` asymmetry that must be
    preserved.
    """
    raise NotImplementedError
