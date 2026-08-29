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

The header-anchoring bug this module is built to avoid
-----------------------------------------------------
Every one of the four committed 10-Ks (data/raw/*.htm) repeats each target
item heading at least twice before the real section body: once as a table-
of-contents entry, and — for MSFT specifically — again and again as a
per-page running header. A naive "first regex match wins" strategy locks
onto the table of contents and returns a section made of link text, not
prose. Verified directly against all four raw filings (see
data/reference/OUTLINES.md for the independently-authored character
offsets used to cross-check this): MSFT headers are uppercase
("ITEM 7. MANAGEMENT'S DISCUSSION..."), AAPL headers are title case
("Item 7.    Management's Discussion...", separated from the item number by
runs of non-breaking spaces, not single spaces). Two properties hold across
all four documents and are what this module relies on:

- The real heading always has the section title glued tightly to the item
  number (`ITEM 7.` immediately followed by `MANAGEMENT`, modulo
  whitespace/casing). Prose cross-references like "...Part II, Item 7 of
  this Form 10-K under the heading 'Management's Discussion...'" never have
  that tight adjacency — there is always narrative text in between — so
  anchoring on the *full* heading (item number + title, not just the
  number) already excludes them.
- The table-of-contents entry does satisfy that tight-adjacency test too
  (it is, after all, "Item 7." next to "Management's Discussion..." in the
  rendered text once HTML tags are stripped) — but it always appears
  *before* the real heading in document order. Taking the **last** match
  rather than the first is what selects the real section over the TOC line.
  This is the exact bug that has bitten this project once already.

Test fixtures: tests/fixtures/sample_10k_excerpt.txt (a real excerpt from
data/raw/aapl_fy2024_10k.htm, Item 1A — note it is prose only, with no
header text of its own, so tests/test_chunking.py wraps it with a
synthetic header line to exercise the character-fallback path against real
filing prose) and tests/fixtures/mini_corpus.json (20 pre-built Chunk-shaped
records spanning both tickers, both fiscal years, and all three sections,
for testing downstream consumers without depending on this module's
output).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Final

from src.schemas import Chunk, Section

logger = logging.getLogger(__name__)

# --- Tunables -----------------------------------------------------------
# 1500/200 lands new chunks close to the size already used by the frozen
# Wave-0 fixture (tests/fixtures/mini_corpus.json averages ~1375 chars per
# chunk) — enough headroom under the ~256-token window of Chroma's default
# MiniLM embedding function (src/store.py) without truncating mid-thought
# on every chunk.

CHUNK_CHAR_BUDGET: Final[int] = 1500
CHUNK_OVERLAP: Final[int] = 200

# --- Section header patterns ----------------------------------------------
# Anchored on the FULL heading (item number + title), and every filing is
# searched for the LAST match, not the first — see the module docstring for
# why both of those are load-bearing, not stylistic choices. `\s{1,20}`
# rather than bare `\s*` covers the runs of `&#160;` (non-breaking space)
# AAPL uses between the item number and the title, without being so loose
# that it accidentally spans a paragraph break.

_SECTION_HEADER_PATTERNS: Final[dict[Section, re.Pattern[str]]] = {
    "item1": re.compile(r"ITEM\s*1\.\s{1,20}BUSINESS", re.IGNORECASE),
    "item1a": re.compile(r"ITEM\s*1A\.?\s{1,20}RISK\s*FACTORS", re.IGNORECASE),
    "item7": re.compile(r"ITEM\s*7\.\s{1,20}MANAGEMENT", re.IGNORECASE),
}

# Generic "any Item N[.] <Title>" heading, used only to find where an
# already-located section *ends* -- i.e. where Item 1B/2/3/.../7A picks up
# and this module's "skip everything else" (FR1.2) begins. Deliberately
# broader than the three patterns above: by the time this is used we've
# already anchored past the table of contents (see `_locate_section`), so
# the only remaining risk is stopping too early on a running header --
# and running headers observed in data/raw/ never carry a capitalized title
# glued directly to a period the way a real heading does.
_ANY_ITEM_BOUNDARY: Final[re.Pattern[str]] = re.compile(
    r"\bITEM\s+\d{1,2}[A-Z]?\.\s{1,20}[A-Z]", re.IGNORECASE
)

_SECTION_ORDER: Final[tuple[Section, ...]] = ("item1", "item1a", "item7")


def _locate_section(text: str, section: Section) -> tuple[int, int] | None:
    """Return the (start, end) character span of `section` within `text`,
    or None if its header never matched.

    `start` is the LAST match of the section's full-heading pattern (see
    module docstring). `end` is the first Item-heading of any kind found
    after that, or end-of-document if none follows (e.g. Item 7 in a
    filing where Item 7A/8 fell outside the excerpt passed in).
    """
    pattern = _SECTION_HEADER_PATTERNS[section]
    matches = list(pattern.finditer(text))
    if not matches:
        return None

    last_match = matches[-1]
    boundary = _ANY_ITEM_BOUNDARY.search(text, last_match.end())
    end = boundary.start() if boundary is not None else len(text)
    return last_match.start(), end


def _char_chunk(text: str, budget: int, overlap: int) -> list[str]:
    """Fixed-size character chunking with overlap (FR1.2 fallback).

    Returns `[text]` unchanged (one chunk) when it already fits the
    budget — the caller uses this to decide which strategy "fired".
    """
    if budget <= 0:
        raise ValueError(f"budget must be positive, got {budget}")
    if overlap < 0 or overlap >= budget:
        raise ValueError(f"overlap must be in [0, budget); got overlap={overlap}, budget={budget}")

    if len(text) <= budget:
        return [text]

    step = budget - overlap
    pieces: list[str] = []
    pos = 0
    while pos < len(text):
        piece = text[pos : pos + budget]
        if piece.strip():
            pieces.append(piece)
        if pos + budget >= len(text):
            break
        pos += step
    return pieces


def _make_chunk_id(ticker: str, fiscal_year: int, section: Section, index: int) -> str:
    """Deterministic chunk id: a stable, human-legible function of
    ticker + fiscal_year + section + index. Re-chunking identical input
    always yields identical ids (FR1.6 idempotency) — `src.store.add_chunks`
    upserts on this key, so a stable-but-opaque hash would satisfy the
    contract too, but this form is directly greppable in logs and matches
    the id convention already committed in
    tests/fixtures/mini_corpus.json (e.g. "MSFT_2023_item1_001").
    """
    return f"{ticker.upper()}_{fiscal_year}_{section}_{index:03d}"


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
    period_end = date.fromisoformat(fiscal_period_end)
    filed_on = date.fromisoformat(filing_date)

    chunks: list[Chunk] = []
    for section in _SECTION_ORDER:
        located = _locate_section(text, section)
        if located is None:
            logger.warning(
                "chunk_filing: %s FY%s -- no '%s' header found, section skipped",
                ticker, fiscal_year, section,
            )
            continue

        start, end = located
        section_text = text[start:end].strip()
        if not section_text:
            logger.warning(
                "chunk_filing: %s FY%s -- '%s' header found but section body is empty",
                ticker, fiscal_year, section,
            )
            continue

        pieces = _char_chunk(section_text, CHUNK_CHAR_BUDGET, CHUNK_OVERLAP)
        strategy = "header_split" if len(pieces) == 1 else "char_fallback"
        logger.info(
            "chunk_filing: %s FY%s %s -- strategy=%s chunks=%d chars=%d",
            ticker, fiscal_year, section, strategy, len(pieces), len(section_text),
        )

        for i, piece in enumerate(pieces, start=1):
            chunks.append(
                Chunk(
                    text=piece,
                    ticker=ticker,
                    fiscal_year=fiscal_year,
                    fiscal_period_end=period_end,
                    section=section,
                    chunk_id=_make_chunk_id(ticker, fiscal_year, section, i),
                    source_url=source_url,
                    filing_date=filed_on,
                )
            )

    return chunks
