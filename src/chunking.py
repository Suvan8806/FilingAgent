"""Section-aware chunking (Lane A — PLAN.md Wave 1).

Owns: this file and src/ingest.py, exclusively.

Contract
--------
Input: raw filing text (already HTML-stripped) plus filing-level metadata
(ticker, fiscal_year, fiscal_period_end, filing_date, source_url).

Output: list[src.schemas.Chunk].

Behavior (PRD FR1.2, FR1.3):

1. Split the filing on section headers into exactly three sections: Item 1
   (Business), Item 1A (Risk Factors), Item 7 (MD&A). Section-header
   detection must handle multiple header formats/variants across filers and
   fiscal years — MSFT and AAPL do not format headers identically, and
   neither company formats them identically across its own two filed years
   (see data/reference/OUTLINES.md for the actual heading text and character
   offsets observed in each of the four committed filings).
2. Within a detected section, if the section's plain text exceeds the chunk
   budget, fall back to fixed-size character chunking with overlap. Log
   which strategy fired per document/section (regex-header-split vs.
   character-fallback) — this is a debuggability requirement, not optional
   polish.
3. Every emitted chunk carries the full FR1.3 metadata set: ticker,
   fiscal_year, fiscal_period_end, section, filing_date, chunk_id,
   source_url. `chunk_id` must be **deterministic** (a stable hash of
   filing identity + section + position, not a random UUID) so that
   src.store.add_chunks can upsert idempotently on re-ingestion (FR1.6).

Test fixtures: tests/fixtures/sample_10k_excerpt.txt (a real excerpt from
data/raw/aapl_fy2024_10k.htm, Item 1A, chosen because it contains a clear
section header followed by several risk-factor paragraphs — enough text to
exercise both the header-split path and, if the chunk budget is set below
its length, the character-fallback path) and
tests/fixtures/mini_corpus.json (20 pre-built Chunk-shaped records spanning
both tickers, both fiscal years, and all three sections, for testing
downstream consumers without depending on this module's output).
"""

from __future__ import annotations

from src.schemas import Chunk


def chunk_filing(
    text: str,
    *,
    ticker: str,
    fiscal_year: int,
    fiscal_period_end: str,
    filing_date: str,
    source_url: str,
) -> list[Chunk]:
    """Split one filing's plain text into section-tagged, metadata-complete
    Chunk records. See module docstring for the full contract.
    """
    raise NotImplementedError
