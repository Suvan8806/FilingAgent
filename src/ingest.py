"""Ingestion pipeline (Lane A — PLAN.md Wave 1).

Owns: this file and src/chunking.py, exclusively.

Contract
--------
Orchestrates the full FR1 pipeline: read filings, chunk them, embed and
persist chunks, load XBRL facts. Wired to `make ingest`.

**Read from data/raw/, do not re-scrape during the build** (PLAN.md Lane A
note). The four 10-K filings are already committed with provenance in
data/raw/manifest.json — parse those files from disk. A live EDGAR fetch
path (FR1.1) must still exist for reproducibility / refresh, but it is not
exercised by the default `make ingest` target against this repo's frozen
corpus. When it is exercised, EDGAR requires a compliant `User-Agent` (see
.env.example EDGAR_USER_AGENT) and a 10 req/sec rate limit — both are
mandatory or EDGAR returns 403. (That path lives in src/xbrl.py for the
numeric side; this module has no live-fetch path of its own for filing
*text* — the four filings are committed HTML, not an API response, and
FR1.1's EDGAR document-fetch refresh path is intentionally out of scope
for a from-disk build. See "Report" notes for why.)

Steps:
1. For each entry in data/raw/manifest.json: read the local .htm file, strip
   markup, and pass the plain text to src.chunking.chunk_filing along with
   the manifest's ticker/fiscal_year/period_of_report/filing_date/
   source_url.
2. Persist the resulting Chunks via src.store (embeds + upserts into
   Chroma).
3. Call src.xbrl to fetch/parse XBRL company facts and load them via
   src.facts into the SQLite facts table.
4. Idempotency (FR1.6): re-running this pipeline must not duplicate
   records. This falls out of deterministic chunk_ids (src.chunking) plus
   an upsert-not-insert write path in src.store and src.facts — this module
   should not itself need a "have I already ingested this" check if those
   two hold their contracts.

Non-goal: general-purpose re-ingestion scheduling. This is a one-shot batch
job (`make ingest`), not a service.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

from src import facts, store, xbrl
from src.chunking import chunk_filing
from src.schemas import Chunk

logger = logging.getLogger(__name__)

_DEFAULT_MANIFEST_PATH: Final[str] = "data/raw/manifest.json"

# --- HTML -> plain text -------------------------------------------------
# No HTML parsing library is pinned in requirements.txt (Wave 0 froze that
# file; Lane A does not own it), and the four filings (1.5-10MB each) don't
# justify adding one. `html.parser.HTMLParser` ships with the stdlib and,
# with `convert_charrefs=True` (the default), already decodes entities
# (`&#8217;` -> "'", `&#160;` -> a real non-breaking space) for us. The only
# other job here is inserting a separator at block-level tag boundaries so
# text from adjacent <span>/<div>/<td> elements doesn't glue together into
# one run-on word -- SEC filings routinely split a single heading like
# "ITEM 1. BUSINESS" across two adjacent <span> tags for kerning, and that
# case must NOT get an inserted separator, which is why only the tags in
# `_BLOCK_TAGS` (not inline ones like <span>) trigger one.

_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {"p", "div", "tr", "td", "th", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr"}
)
_SKIP_TAGS: Final[frozenset[str]] = frozenset({"script", "style"})
_COLLAPSE_SPACES: Final[re.Pattern[str]] = re.compile(r"[ \t]+")
_COLLAPSE_NEWLINES: Final[re.Pattern[str]] = re.compile(r"\n{2,}")


class _HTMLTextExtractor(HTMLParser):
    """Minimal, dependency-free HTML -> plain text extractor. See module
    docstring for why this exists instead of bs4/lxml.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = _COLLAPSE_SPACES.sub(" ", text)
        text = _COLLAPSE_NEWLINES.sub("\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    """Strip an SEC filing's HTML down to plain text, preserving enough
    whitespace at block boundaries that src.chunking's header regexes (and
    ordinary word boundaries) still work correctly on the result.
    """
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


# --- Pipeline -------------------------------------------------------------


def _load_manifest(manifest_path: str | Path) -> list[dict[str, Any]]:
    path = Path(manifest_path)
    with path.open(encoding="utf-8") as f:
        entries: list[dict[str, Any]] = json.load(f)
    return entries


def _ingest_one_filing(entry: dict[str, Any]) -> list[Chunk]:
    """Read one manifest entry's committed .htm file from disk, strip it to
    plain text, and chunk it. Raises (does not swallow) on a missing or
    unreadable file -- a silent skip here would produce a quietly
    incomplete index, which is worse than a loud failure during `make
    ingest`.
    """
    local_path = Path(entry["local_path"])
    try:
        html = local_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"ingest: could not read {local_path} for {entry.get('ticker')} "
            f"FY{entry.get('fiscal_year')} (see data/raw/manifest.json)"
        ) from exc

    text = html_to_text(html)
    return chunk_filing(
        text,
        ticker=entry["ticker"],
        fiscal_year=entry["fiscal_year"],
        fiscal_period_end=entry["period_of_report"],
        filing_date=entry["filing_date"],
        source_url=entry["source_url"],
    )


def ingest_all(manifest_path: str = _DEFAULT_MANIFEST_PATH) -> None:
    """Run the full ingestion pipeline end to end: read -> chunk -> embed ->
    persist chunks, and fetch/parse -> normalize -> persist XBRL facts.
    See module docstring for the full contract.
    """
    manifest = _load_manifest(manifest_path)

    all_chunks: list[Chunk] = []
    for entry in manifest:
        chunks = _ingest_one_filing(entry)
        logger.info(
            "ingest: %s FY%s (%s) -> %d chunks",
            entry["ticker"], entry["fiscal_year"], entry.get("local_path"), len(chunks),
        )
        all_chunks.extend(chunks)

    store.add_chunks(all_chunks)
    logger.info("ingest: persisted %d chunks total across %d filings", len(all_chunks), len(manifest))

    tickers = sorted({entry["ticker"] for entry in manifest})
    for ticker in tickers:
        companyfacts = xbrl.fetch_company_facts(ticker)
        rows = xbrl.extract_facts(ticker, companyfacts)
        facts.load_facts(rows)
        logger.info("ingest: %s -> %d XBRL fact rows", ticker, len(rows))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ingest_all()
