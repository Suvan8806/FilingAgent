"""Unit tests for src/ingest.py (Lane A — PLAN.md Wave 1, PRD FR9.2/FR1.6).

Deliberately avoids depending on src.store / src.xbrl / src.facts actually
working — those belong to Lanes B and C and may still be `NotImplementedError`
stubs at the point these tests run (Wave 1 lanes build in parallel against
frozen contracts, PLAN.md "Wave 1"). Every test that exercises `ingest_all`
monkeypatches those three call sites and asserts against what src.ingest
itself is responsible for: reading the manifest, stripping HTML, chunking,
and calling the downstream contracts with the right arguments, the right
number of times, deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import ingest
from src.schemas import Chunk

# --- Tiny synthetic filings -------------------------------------------------
# Real entity encoding (&#160; non-breaking space, &#8217; right single
# quote) and real block-tag-per-heading-fragment structure (a heading split
# across two adjacent <span> tags), modeled on what data/raw/*.htm actually
# contains -- see tests/test_chunking.py for the plain-text equivalents this
# is meant to survive HTML-stripping into.

_MSFT_HTML = """
<html><body>
<p><span>ITEM 1. B</span><span>USINESS</span></p>
<p>General overview of the company for testing purposes.</p>
<p><span>ITEM 1A. RISK FACTORS</span></p>
<p>Our operations and financial results are subject to a variety of risks.</p>
<p><span>ITEM 7. MANAGEMENT&#8217;S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION</span></p>
<p>Revenue increased driven by growth across each of our segments.</p>
</body></html>
"""

_AAPL_HTML = """
<html><body>
<p><span>Item 1.&#160;&#160;&#160;&#160;Business</span></p>
<p>The Company designs, manufactures, and markets smartphones for testing.</p>
<p><span>Item 1A.&#160;&#160;&#160;&#160;Risk Factors</span></p>
<p>The Company&#8217;s business is subject to macroeconomic risk for testing.</p>
<p><span>Item 7.&#160;&#160;&#160;&#160;Management&#8217;s Discussion and Analysis</span></p>
<p>Net sales increased year over year across all segments for testing.</p>
</body></html>
"""


@pytest.fixture()
def two_filing_manifest(tmp_path: Path) -> Path:
    """A small on-disk manifest + two synthetic .htm files, shaped exactly
    like data/raw/manifest.json entries (PLAN.md Lane A note: parse from
    disk, same contract as the real corpus).
    """
    msft_path = tmp_path / "msft_fy2024_10k.htm"
    aapl_path = tmp_path / "aapl_fy2024_10k.htm"
    msft_path.write_text(_MSFT_HTML, encoding="utf-8")
    aapl_path.write_text(_AAPL_HTML, encoding="utf-8")

    manifest = [
        {
            "ticker": "MSFT",
            "cik": "0000789019",
            "fiscal_year": 2024,
            "period_of_report": "2024-06-30",
            "filing_date": "2024-07-30",
            "accession": "0000950170-24-087843",
            "source_url": "https://www.sec.gov/Archives/edgar/data/789019/example/msft-20240630.htm",
            "local_path": str(msft_path),
        },
        {
            "ticker": "AAPL",
            "cik": "0000320193",
            "fiscal_year": 2024,
            "period_of_report": "2024-09-28",
            "filing_date": "2024-11-01",
            "accession": "0000320193-24-000123",
            "source_url": "https://www.sec.gov/Archives/edgar/data/320193/example/aapl-20240928.htm",
            "local_path": str(aapl_path),
        },
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


class _Recorder:
    """Captures calls to a monkeypatched function without executing the
    real (possibly still-`NotImplementedError`-stubbed) implementation.
    """

    def __init__(self, return_value=None):
        self.calls: list[tuple[tuple, dict]] = []
        self._return_value = return_value

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._return_value


def _patch_downstream(monkeypatch, *, companyfacts=None, extracted_rows=None):
    """Stub out every Lane B/C call src.ingest makes so tests exercise only
    this module's own responsibilities.
    """
    add_chunks = _Recorder()
    fetch_company_facts = _Recorder(return_value=companyfacts if companyfacts is not None else {})
    extract_facts = _Recorder(return_value=extracted_rows if extracted_rows is not None else [])
    load_facts = _Recorder()

    monkeypatch.setattr(ingest.store, "add_chunks", add_chunks)
    monkeypatch.setattr(ingest.xbrl, "fetch_company_facts", fetch_company_facts)
    monkeypatch.setattr(ingest.xbrl, "extract_facts", extract_facts)
    monkeypatch.setattr(ingest.facts, "load_facts", load_facts)

    return {
        "add_chunks": add_chunks,
        "fetch_company_facts": fetch_company_facts,
        "extract_facts": extract_facts,
        "load_facts": load_facts,
    }


# --- html_to_text -----------------------------------------------------------


class TestHtmlToText:
    def test_strips_tags(self):
        text = ingest.html_to_text("<p>hello <b>world</b></p>")
        assert "<" not in text
        assert "hello" in text and "world" in text

    def test_decodes_entities(self):
        text = ingest.html_to_text("<p>Company&#8217;s risk &amp; return</p>")
        assert "’" in text  # right single quotation mark
        assert "&#8217;" not in text
        assert "&amp;" not in text

    def test_drops_script_and_style_content(self):
        html = "<html><head><style>.x{color:red}</style></head><body><script>alert(1)</script><p>real text</p></body></html>"
        text = ingest.html_to_text(html)
        assert "real text" in text
        assert "alert" not in text
        assert "color:red" not in text

    def test_block_tags_insert_a_separator_so_words_dont_glue(self):
        html = "<div>first</div><div>second</div>"
        text = ingest.html_to_text(html)
        assert "firstsecond" not in text

    def test_inline_spans_do_not_insert_a_separator(self):
        """Real filings split a single heading across adjacent <span> tags
        for kerning (see data/raw/msft_fy2024_10k.htm, 'ITEM 1. B' + 'USINESS'
        as two spans) -- these must concatenate into one word, not two.
        """
        html = "<p><span>ITEM 1. B</span><span>USINESS</span></p>"
        text = ingest.html_to_text(html)
        assert "ITEM 1. BUSINESS" in text


# --- ingest_all orchestration ------------------------------------------------


class TestIngestAllOrchestration:
    def test_add_chunks_called_once_with_chunks_from_every_filing(self, tmp_path, monkeypatch, two_filing_manifest):
        recorders = _patch_downstream(monkeypatch)

        ingest.ingest_all(str(two_filing_manifest))

        assert len(recorders["add_chunks"].calls) == 1
        (chunks,), _ = recorders["add_chunks"].calls[0]
        assert all(isinstance(c, Chunk) for c in chunks)

        tickers_seen = {c.ticker for c in chunks}
        assert tickers_seen == {"MSFT", "AAPL"}

        sections_seen = {(c.ticker, c.section) for c in chunks}
        assert sections_seen == {
            ("MSFT", "item1"), ("MSFT", "item1a"), ("MSFT", "item7"),
            ("AAPL", "item1"), ("AAPL", "item1a"), ("AAPL", "item7"),
        }

    def test_chunk_metadata_matches_manifest_entry(self, monkeypatch, two_filing_manifest):
        recorders = _patch_downstream(monkeypatch)
        ingest.ingest_all(str(two_filing_manifest))

        (chunks,), _ = recorders["add_chunks"].calls[0]
        msft_chunks = [c for c in chunks if c.ticker == "MSFT"]
        assert msft_chunks
        for c in msft_chunks:
            assert c.fiscal_year == 2024
            assert c.fiscal_period_end.isoformat() == "2024-06-30"
            assert c.filing_date.isoformat() == "2024-07-30"
            assert c.source_url == "https://www.sec.gov/Archives/edgar/data/789019/example/msft-20240630.htm"

    def test_xbrl_fetched_and_loaded_once_per_distinct_ticker(self, monkeypatch, two_filing_manifest):
        recorders = _patch_downstream(monkeypatch)
        ingest.ingest_all(str(two_filing_manifest))

        fetched_tickers = [call.args[0] for call in _as_call_objects(recorders["fetch_company_facts"].calls)]
        assert sorted(fetched_tickers) == ["AAPL", "MSFT"]

        loaded_calls = recorders["load_facts"].calls
        assert len(loaded_calls) == 2  # once per distinct ticker, not once per filing

    def test_missing_file_raises_instead_of_silently_skipping(self, tmp_path, monkeypatch):
        manifest = [
            {
                "ticker": "MSFT",
                "fiscal_year": 2024,
                "period_of_report": "2024-06-30",
                "filing_date": "2024-07-30",
                "source_url": "https://example.com/msft.htm",
                "local_path": str(tmp_path / "does_not_exist.htm"),
            }
        ]
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _patch_downstream(monkeypatch)

        with pytest.raises(RuntimeError):
            ingest.ingest_all(str(manifest_path))


class TestIdempotency:
    def test_reingesting_produces_identical_chunk_ids(self, monkeypatch, two_filing_manifest):
        recorders_1 = _patch_downstream(monkeypatch)
        ingest.ingest_all(str(two_filing_manifest))
        (chunks_1,), _ = recorders_1["add_chunks"].calls[0]

        recorders_2 = _patch_downstream(monkeypatch)
        ingest.ingest_all(str(two_filing_manifest))
        (chunks_2,), _ = recorders_2["add_chunks"].calls[0]

        ids_1 = [c.chunk_id for c in chunks_1]
        ids_2 = [c.chunk_id for c in chunks_2]
        assert ids_1 == ids_2
        assert [c.model_dump() for c in chunks_1] == [c.model_dump() for c in chunks_2]


def _as_call_objects(calls):
    """Adapts the (args, kwargs) tuples recorded by _Recorder into objects
    with an `.args` attribute, for readability at call sites above.
    """
    class _Call:
        def __init__(self, args, kwargs):
            self.args = args
            self.kwargs = kwargs

    return [_Call(args, kwargs) for args, kwargs in calls]
