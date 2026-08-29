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
mandatory or EDGAR returns 403.

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


def ingest_all(manifest_path: str = "data/raw/manifest.json") -> None:
    """Run the full ingestion pipeline end to end: read -> chunk -> embed ->
    persist chunks, and fetch/parse -> normalize -> persist XBRL facts.
    See module docstring for the full contract.
    """
    raise NotImplementedError
