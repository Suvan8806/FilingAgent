"""Structured numeric facts store — SQLite (Lane B — PLAN.md Wave 1).

Owns: this file, jointly with src/store.py (both are Lane B's exclusive
write scope).

Contract
--------
SQLite table of normalized XBRL facts, shaped after `src.schemas.Fact`
(ticker, metric, fiscal_year, fiscal_period_end, value, unit) rather than
mirroring the raw CSV columns verbatim — `load_facts` is the normalization
boundary between the raw `data/reference/xbrl_facts.csv` shape (ticker,
fiscal_year, period_end, metric, us_gaap_tag, value_usd, accn, form,
source) and the table `lookup` reads from. `us_gaap_tag`/`accn`/`form`/
`source` are provenance for humans reading the CSV, not needed by the
lookup path, so they are not persisted.

Required operations:

- `lookup(ticker, metric, fiscal_year) -> Fact | Miss` — the function
  `src.tools.lookup_financial` calls directly. Returns a typed `Miss`
  (never raises) for: unknown ticker, unsupported fiscal year, unknown
  metric, or a metric that is XBRL-tagged for other filers/years but not
  this one (FR4.4) — e.g. MSFT `sga` is a real, expected miss (filer
  reports S&M and G&A separately; see data/reference/xbrl_facts.csv), not
  an error condition. `Miss.reason` distinguishes these cases so an agent
  reasoning over it can decide whether to try a different metric/tool
  rather than retry the same failing call.
- `load_facts(rows: list[dict]) -> None` — bulk load/replace from raw
  CSV-shaped dict rows (as read by `csv.DictReader` over a file shaped like
  data/reference/xbrl_facts.csv). Idempotent (FR1.6): re-running ingestion
  replaces rather than duplicates rows for a given (ticker, metric,
  fiscal_year) key, via `INSERT ... ON CONFLICT ... DO UPDATE`.
- `load_facts_from_csv(csv_path: str) -> None` — convenience wrapper that
  reads a CSV file and calls `load_facts`.

A row with an empty `value_usd` (real corpus data, not a data-loading bug —
see MSFT `sga` above) is still loaded, with `value` stored as SQL NULL. This
is what lets `lookup` distinguish "this metric is genuinely not tagged for
this filer" (a row exists, value is NULL) from "we never loaded anything
for this ticker/year/metric at all" (no row) — both are a `Miss`, but with
a different, specific `reason`.

Test fixtures: tests/fixtures/mini_facts.csv (real values for MSFT/AAPL
FY2023/FY2024, MSFT `sga` rows present with an empty `value_usd`,
deliberately missing AAPL FY2024 `total_assets` and `operating_cash_flow`
entirely) is the frozen input for unit-testing this module without a full
ingest run.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from datetime import date
from pathlib import Path

from src.schemas import Fact, Miss

DEFAULT_DB_PATH = "./facts.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS facts (
    ticker TEXT NOT NULL,
    metric TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period_end TEXT,
    value REAL,
    unit TEXT,
    PRIMARY KEY (ticker, metric, fiscal_year)
)
"""

_UPSERT_SQL = """
INSERT INTO facts (ticker, metric, fiscal_year, fiscal_period_end, value, unit)
VALUES (:ticker, :metric, :fiscal_year, :fiscal_period_end, :value, :unit)
ON CONFLICT (ticker, metric, fiscal_year) DO UPDATE SET
    fiscal_period_end = excluded.fiscal_period_end,
    value = excluded.value,
    unit = excluded.unit
"""


def _db_path(db_path: str | None) -> str:
    if db_path is not None:
        return db_path
    return os.environ.get("FACTS_DB", DEFAULT_DB_PATH)


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = _db_path(db_path)
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    return conn


def _parse_value(raw: str | float | None) -> float | None:
    """Empty/absent value_usd means "not XBRL-tagged for this filer" (real
    corpus data — MSFT `sga`), and must become None, never 0.0.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return None
    return float(raw)


def _row_to_record(row: dict) -> dict:
    """Normalize one raw CSV-shaped row (or an already-normalized dict) into
    the table's column shape.
    """
    value = _parse_value(row["value_usd"] if "value_usd" in row else row.get("value"))
    period_end_raw = (row.get("period_end") or row.get("fiscal_period_end") or "").strip()
    return {
        "ticker": row["ticker"].strip(),
        "metric": row["metric"].strip(),
        "fiscal_year": int(row["fiscal_year"]),
        "fiscal_period_end": period_end_raw or None,
        "value": value,
        # Every metric in the corpus is a dollar figure (the raw column is
        # literally named value_usd); an untagged metric has no unit.
        "unit": row.get("unit") or ("USD" if value is not None else None),
    }


def load_facts(rows: list[dict], db_path: str | None = None) -> None:
    """Bulk load normalized XBRL rows into the SQLite facts table.
    Idempotent — re-running replaces rather than duplicates rows for a
    given (ticker, metric, fiscal_year).
    """
    records = [_row_to_record(row) for row in rows]
    conn = _connect(db_path)
    try:
        with conn:
            conn.executemany(_UPSERT_SQL, records)
    finally:
        conn.close()


def load_facts_from_csv(csv_path: str, db_path: str | None = None) -> None:
    """Convenience wrapper: read a CSV shaped like
    data/reference/xbrl_facts.csv and load it via `load_facts`.
    """
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    load_facts(rows, db_path=db_path)


def _miss_reason(conn: sqlite3.Connection, ticker: str, metric: str, fiscal_year: int) -> str:
    """Distinguish *why* a fact is absent so the agent can decide whether a
    different tool call could still succeed (FR4.4).
    """
    has_ticker = conn.execute("SELECT 1 FROM facts WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()
    if not has_ticker:
        return f"unknown ticker {ticker!r}"

    has_year = conn.execute(
        "SELECT 1 FROM facts WHERE ticker = ? AND fiscal_year = ? LIMIT 1",
        (ticker, fiscal_year),
    ).fetchone()
    if not has_year:
        return f"no data loaded for {ticker} fiscal year {fiscal_year}"

    has_metric = conn.execute("SELECT 1 FROM facts WHERE metric = ? LIMIT 1", (metric,)).fetchone()
    if not has_metric:
        return f"unknown metric {metric!r}"

    return f"{metric!r} not recorded for {ticker} fiscal year {fiscal_year}"


def lookup(ticker: str, metric: str, fiscal_year: int, db_path: str | None = None) -> Fact | Miss:
    """Exact lookup of one metric for one ticker/fiscal_year. Returns a
    typed Miss (never raises) when the fact is absent, or present but
    untagged (FR4.4).
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT fiscal_period_end, value, unit FROM facts "
            "WHERE ticker = ? AND metric = ? AND fiscal_year = ?",
            (ticker, metric, fiscal_year),
        ).fetchone()

        if row is None:
            return Miss(
                ticker=ticker,
                metric=metric,
                fiscal_year=fiscal_year,
                reason=_miss_reason(conn, ticker, metric, fiscal_year),
            )

        fiscal_period_end, value, unit = row
        if value is None:
            return Miss(
                ticker=ticker,
                metric=metric,
                fiscal_year=fiscal_year,
                reason=(
                    f"{metric!r} is not XBRL-tagged for {ticker} in fiscal year {fiscal_year} "
                    "(this filer reports the underlying figures under different tags, if at all)"
                ),
            )

        return Fact(
            ticker=ticker,
            metric=metric,
            fiscal_year=fiscal_year,
            fiscal_period_end=date.fromisoformat(fiscal_period_end),
            value=value,
            unit=unit or "USD",
        )
    finally:
        conn.close()
