"""Unit tests for src/chunking.py (Lane A — PLAN.md Wave 1, PRD FR9.2).

Covers, per the Lane A brief:
- header-split path (section fits the chunk budget in one piece)
- character-chunk fallback path, against the real fixture excerpt
- the table-of-contents / running-header regression this module exists to
  avoid (MSFT uppercase headers, AAPL title-case headers, "last match wins")
- deterministic chunk_id / idempotency
- FR1.3 metadata completeness
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.chunking import CHUNK_CHAR_BUDGET, CHUNK_OVERLAP, _char_chunk, chunk_filing
from src.schemas import Chunk

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_EXCERPT = (FIXTURES_DIR / "sample_10k_excerpt.txt").read_text(encoding="utf-8")

COMMON_KWARGS = {
    "fiscal_period_end": "2024-06-30",
    "filing_date": "2024-07-30",
    "source_url": "https://www.sec.gov/Archives/edgar/data/789019/example/msft-20240630.htm",
}


# --- Regression fixtures: TOC + running-header pollution ------------------
# Modeled directly on what data/raw/msft_fy2024_10k.htm and
# data/raw/aapl_fy2024_10k.htm actually contain: each real heading is
# preceded by a table-of-contents entry that satisfies the same
# "full heading, tightly adjacent" test the real heading does, plus (MSFT
# only) repeated per-page running headers in between. A first-match
# strategy locks onto the TOC line; this module must not.


def _msft_style_text() -> str:
    """Uppercase headers, TOC duplicate + running-header duplicate before
    each real section, matching data/raw/msft_fy2024_10k.htm.
    """
    return "\n".join(
        [
            "PART I",
            "Item 1.",
            "Item 1A.",
            "Item 7.",
            "",
            "ITEM 1. BUSINESS",
            "TOC_STUB item1 -- this line must never appear in an extracted chunk.",
            "",
            "ITEM 1A. RISK FACTORS",
            "TOC_STUB item1a -- this line must never appear in an extracted chunk.",
            "",
            "PART I",
            "Item 1A",
            "",
            "ITEM 1A. RISK FACTORS",
            "REAL_BODY item1a -- our operations and financial results are subject to risk.",
            "",
            "PART I",
            "Item 1B, 1C",
            "",
            "ITEM 2. PROPERTIES",
            "SKIPPED_BODY item2 -- this describes corporate facilities and must never appear.",
            "",
            "ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION",
            "TOC_STUB item7 -- this line must never appear in an extracted chunk.",
            "",
            "PART II",
            "Item 7",
            "",
            "ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION",
            "REAL_BODY item7 -- revenue increased driven by growth across each of our segments.",
            "",
            "PART II",
            "Item 7A",
        ]
    )


def _aapl_style_text() -> str:
    """Title-case headers separated from the item number by non-breaking
    spaces, TOC duplicate before each real section, matching
    data/raw/aapl_fy2024_10k.htm.
    """
    nbsp = "\xa0\xa0\xa0\xa0"
    return "\n".join(
        [
            f"Item 1.{nbsp}Business",
            "TOC_STUB item1 -- this line must never appear in an extracted chunk.",
            "",
            f"Item 1A.{nbsp}Risk Factors",
            "TOC_STUB item1a -- this line must never appear in an extracted chunk.",
            "",
            f"Item 1.{nbsp}Business",
            "REAL_BODY item1 -- the Company designs, manufactures, and markets products.",
            "",
            f"Item 1A.{nbsp}Risk Factors",
            "REAL_BODY item1a -- the Company's business is subject to macroeconomic risk.",
            "",
            f"Item 7.{nbsp}Management’s Discussion and Analysis of Financial Condition",
            "REAL_BODY item7 -- net sales increased year over year across all segments.",
            "",
            "Apple Inc. | 2024 Form 10-K | 26",
        ]
    )


class TestHeaderAnchoringRegression:
    """The exact bug PLAN.md calls out: 'anchor on the full heading, take
    the LAST match, not the first.'
    """

    def test_msft_uppercase_headers_skip_toc_and_running_header(self):
        chunks = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)

        item1a_text = "".join(c.text for c in chunks if c.section == "item1a")
        item7_text = "".join(c.text for c in chunks if c.section == "item7")

        assert "REAL_BODY item1a" in item1a_text
        assert "TOC_STUB item1a" not in item1a_text
        assert "REAL_BODY item7" in item7_text
        assert "TOC_STUB item7" not in item7_text

    def test_msft_item2_boilerplate_between_1a_and_7_is_skipped(self):
        chunks = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        all_text = "".join(c.text for c in chunks)
        assert "SKIPPED_BODY item2" not in all_text

    def test_aapl_title_case_headers_skip_toc(self):
        chunks = chunk_filing(_aapl_style_text(), ticker="AAPL", fiscal_year=2024, **COMMON_KWARGS)

        item1_text = "".join(c.text for c in chunks if c.section == "item1")
        item1a_text = "".join(c.text for c in chunks if c.section == "item1a")

        assert "REAL_BODY item1" in item1_text
        assert "TOC_STUB item1" not in item1_text
        assert "REAL_BODY item1a" in item1a_text
        assert "TOC_STUB item1a" not in item1a_text

    def test_aapl_nbsp_separated_header_is_matched(self):
        """AAPL separates the item number from the title with non-breaking
        spaces (&#160;), not a plain space, once HTML entities are decoded.
        """
        chunks = chunk_filing(_aapl_style_text(), ticker="AAPL", fiscal_year=2024, **COMMON_KWARGS)
        item7_chunks = [c for c in chunks if c.section == "item7"]
        assert item7_chunks, "Item 7 header (nbsp-separated) was not matched at all"
        assert "REAL_BODY item7" in item7_chunks[0].text


class TestStrategySelection:
    """FR1.2: header-split when a section fits the budget in one piece,
    character-chunk-with-overlap fallback when it doesn't.
    """

    def test_header_split_path_short_section_is_one_chunk(self):
        chunks = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        item1_chunks = [c for c in chunks if c.section == "item1"]
        assert len(item1_chunks) == 1

    def test_char_fallback_path_against_real_fixture_excerpt(self):
        """tests/fixtures/sample_10k_excerpt.txt is real AAPL Item 1A prose
        with no header of its own (7.5KB, well over CHUNK_CHAR_BUDGET) --
        wrap it with a real-style header so it becomes a single oversized
        section, and confirm the character-fallback path fires.
        """
        nbsp = "\xa0\xa0\xa0\xa0"
        text = f"Item 1A.{nbsp}Risk Factors\n{SAMPLE_EXCERPT}\n\nItem 7.{nbsp}Management's Discussion\nshort md&a body"

        chunks = chunk_filing(text, ticker="AAPL", fiscal_year=2024, **COMMON_KWARGS)
        item1a_chunks = [c for c in chunks if c.section == "item1a"]

        assert len(item1a_chunks) > 1, "expected the character-fallback path to split this section"
        for c in item1a_chunks:
            assert len(c.text) <= CHUNK_CHAR_BUDGET

    def test_char_fallback_chunks_overlap_by_configured_amount(self):
        nbsp = "\xa0\xa0\xa0\xa0"
        text = f"Item 1A.{nbsp}Risk Factors\n{SAMPLE_EXCERPT}"
        chunks = chunk_filing(text, ticker="AAPL", fiscal_year=2024, **COMMON_KWARGS)
        item1a_chunks = [c for c in chunks if c.section == "item1a"]
        assert len(item1a_chunks) >= 2

        first, second = item1a_chunks[0].text, item1a_chunks[1].text
        tail_of_first = first[-CHUNK_OVERLAP:]
        assert tail_of_first in second, "second chunk should start with the overlap region from the first"


class TestCharChunkHelper:
    """Direct unit tests of the fallback splitter itself."""

    def test_returns_single_piece_when_within_budget(self):
        assert _char_chunk("short text", budget=1500, overlap=200) == ["short text"]

    def test_splits_with_overlap_and_covers_full_text(self):
        text = "x" * 4000
        pieces = _char_chunk(text, budget=1500, overlap=200)
        assert len(pieces) > 1
        assert all(len(p) <= 1500 for p in pieces)
        # Reconstructing by dropping the overlap from each piece after the
        # first should reproduce the original text exactly.
        rebuilt = pieces[0]
        for p in pieces[1:]:
            rebuilt += p[200:]
        assert rebuilt == text

    def test_rejects_overlap_not_smaller_than_budget(self):
        with pytest.raises(ValueError):
            _char_chunk("x" * 100, budget=50, overlap=50)

    def test_rejects_non_positive_budget(self):
        with pytest.raises(ValueError):
            _char_chunk("x" * 100, budget=0, overlap=0)


class TestIdempotencyAndChunkId:
    def test_chunk_ids_are_deterministic_across_reingest(self):
        first_run = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        second_run = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)

        assert [c.chunk_id for c in first_run] == [c.chunk_id for c in second_run]
        assert [c.model_dump() for c in first_run] == [c.model_dump() for c in second_run]

    def test_chunk_id_format(self):
        chunks = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        for c in chunks:
            assert c.chunk_id.startswith(f"MSFT_2024_{c.section}_")
            index_part = c.chunk_id.rsplit("_", 1)[-1]
            assert index_part.isdigit() and len(index_part) == 3

    def test_chunk_ids_unique_within_a_filing(self):
        chunks = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))

    def test_different_tickers_never_collide(self):
        msft_chunks = chunk_filing(_msft_style_text(), ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        aapl_chunks = chunk_filing(_aapl_style_text(), ticker="AAPL", fiscal_year=2024, **COMMON_KWARGS)
        msft_ids = {c.chunk_id for c in msft_chunks}
        aapl_ids = {c.chunk_id for c in aapl_chunks}
        assert msft_ids.isdisjoint(aapl_ids)


class TestMetadataCompleteness:
    """FR1.3: every chunk carries the full metadata set."""

    def test_every_chunk_has_full_fr1_3_metadata(self):
        chunks = chunk_filing(
            _aapl_style_text(),
            ticker="AAPL",
            fiscal_year=2023,
            fiscal_period_end="2023-09-30",
            filing_date="2023-11-03",
            source_url="https://www.sec.gov/Archives/edgar/data/320193/example/aapl-20230930.htm",
        )
        assert chunks, "fixture text produced no chunks -- nothing to assert metadata on"

        for c in chunks:
            assert isinstance(c, Chunk)
            assert c.ticker == "AAPL"
            assert c.fiscal_year == 2023
            assert c.fiscal_period_end == date(2023, 9, 30)
            assert c.filing_date == date(2023, 11, 3)
            assert c.section in ("item1", "item1a", "item7")
            assert c.chunk_id
            assert c.source_url == "https://www.sec.gov/Archives/edgar/data/320193/example/aapl-20230930.htm"
            assert c.text.strip()


class TestMissingSection:
    def test_missing_section_is_skipped_not_erroring(self):
        text = "ITEM 1. BUSINESS\nsome business text\n\nITEM 1A. RISK FACTORS\nsome risk text"
        chunks = chunk_filing(text, ticker="MSFT", fiscal_year=2024, **COMMON_KWARGS)
        sections_found = {c.section for c in chunks}
        assert sections_found == {"item1", "item1a"}

    def test_no_headers_at_all_returns_empty_list(self):
        chunks = chunk_filing(SAMPLE_EXCERPT, ticker="AAPL", fiscal_year=2024, **COMMON_KWARGS)
        assert chunks == []


class TestRealFilingSmokeTest:
    """Light end-to-end sanity check against one real committed filing
    (the smallest, at ~1.5MB) via src.ingest.html_to_text + chunk_filing --
    not the full ingest_all pipeline (src.store/src.facts are Lane B/C's
    responsibility), just this module's own output on real prose.
    """

    def test_real_aapl_fy2024_filing_produces_all_three_sections(self):
        from src.ingest import (
            html_to_text,  # local import: avoids a module-level cross-file dependency
        )

        raw_path = FIXTURES_DIR.parent.parent / "data" / "raw" / "aapl_fy2024_10k.htm"
        if not raw_path.exists():
            pytest.skip("data/raw/aapl_fy2024_10k.htm not present in this checkout")

        html = raw_path.read_text(encoding="utf-8")
        text = html_to_text(html)
        chunks = chunk_filing(
            text,
            ticker="AAPL",
            fiscal_year=2024,
            fiscal_period_end="2024-09-28",
            filing_date="2024-11-01",
            source_url=(
                "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
            ),
        )

        sections_found = {c.section for c in chunks}
        assert sections_found == {"item1", "item1a", "item7"}
        # Every real section here is tens of KB of prose -- well over the
        # chunk budget -- so char-fallback must have fired for all three.
        assert len(chunks) > 3
