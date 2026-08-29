"""Structured numeric facts store — SQLite (Lane B — PLAN.md Wave 1).

Owns: this file, jointly with src/store.py (both are Lane B's exclusive
write scope).

Contract
--------
SQLite table of normalized XBRL facts (columns matching
data/reference/xbrl_facts.csv: ticker, fiscal_year, period_end, metric,
us_gaap_tag, value_usd, accn, form, source), loaded by src.ingest from
src.xbrl's output.

Required operation:

- `lookup(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss` — the
  function `src.tools.lookup_financial` calls directly. Must return a typed
  `Miss` (never raise) for: unknown ticker, unsupported fiscal year, or a
  metric that is not XBRL-tagged for this filer in this year (FR4.4) — e.g.
  MSFT `sga` is a real, expected miss (filer reports S&M and G&A
  separately; see data/reference/xbrl_facts.csv), not an error condition.
  `Miss.reason` should be specific enough that an agent reasoning over it
  can decide whether to try a different metric/tool rather than retry the
  same failing call.

Loading:

- `load_facts(rows: list[dict]) -> None` — bulk load/replace from
  src.xbrl's normalized rows. Must be idempotent (FR1.6): re-running
  ingestion replaces rather than duplicates rows for a given
  (ticker, metric, fiscal_year) key.

Test fixtures: tests/fixtures/mini_facts.csv (30 rows, same column set as
data/reference/xbrl_facts.csv, real values for MSFT/AAPL FY2023/FY2024 —
deliberately missing AAPL FY2024 `total_assets` and `operating_cash_flow`
so downstream tool tests have a real miss case to exercise without waiting
for the full ingest).
"""

from __future__ import annotations

from src.schemas import Fact, Miss


def load_facts(rows: list[dict], db_path: str | None = None) -> None:
    """Bulk load normalized XBRL rows into the SQLite facts table.
    Idempotent — re-running replaces rather than duplicates.
    """
    raise NotImplementedError


def lookup(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss:
    """Exact lookup of one metric for one ticker/fiscal_year. Returns a
    typed Miss (never raises) when the fact is absent (FR4.4).
    """
    raise NotImplementedError
