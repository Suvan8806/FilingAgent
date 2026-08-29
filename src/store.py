"""Vector store wrapper — ChromaDB (Lane B — PLAN.md Wave 1).

Owns: this file and src/facts.py, exclusively.

Contract
--------
Thin wrapper over a local, persistent ChromaDB collection. This module owns
all Chroma-specific detail; no other module should import chromadb
directly.

Required operations:

- `add_chunks(chunks: list[Chunk]) -> None` — embed and upsert. Keys on
  `chunk_id` (deterministic, from src.chunking) so re-running ingestion
  (FR1.6) updates in place rather than duplicating. Persists to the
  directory named by the `CHROMA_DIR` env var (.env.example), defaulting to
  `./chroma_db` when unset.
- `search(query: str, k: int, filters: dict | None = None) -> list[Chunk]`
  — top-k by embedding similarity, with optional hard metadata filters on
  `ticker`, `fiscal_year`, and `section` (PRD FR2.2). This is the retrieval
  primitive that src.tools.search_filings sits directly on top of; the
  filter dict keys match that tool's parameter names exactly so it can pass
  its kwargs straight through.

Embeddings: Chroma's default local ONNX MiniLM embedding function (see
PRD §9 Technology choices) — no second API key, no per-call embedding
cost, keeps `docker compose up` one-command. The model is downloaded once
and cached under Chroma's local cache dir on first use.

Test fixtures: tests/fixtures/mini_corpus.json (20 realistic Chunk records)
is the frozen input for unit-testing this module without a real ingest run.
"""

from __future__ import annotations

import os
from datetime import date

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.utils import embedding_functions

from src.schemas import Chunk

COLLECTION_NAME = "filings"

# Recognized hard-filter keys (PRD FR2.2). Anything else in a `filters` dict
# is ignored rather than raising — callers (src.tools.search_filings) pass
# their full kwarg set through, some of which may be None/absent.
_FILTER_KEYS = ("ticker", "fiscal_year", "section")

# One PersistentClient per resolved CHROMA_DIR path, so tests can point at an
# isolated tmp_path via the env var without paying to reopen the on-disk
# store on every call within a test.
_clients: dict[str, chromadb.ClientAPI] = {}


def _chroma_dir() -> str:
    return os.environ.get("CHROMA_DIR", "./chroma_db")


def _get_client() -> chromadb.ClientAPI:
    path = _chroma_dir()
    if path not in _clients:
        _clients[path] = chromadb.PersistentClient(path=path)
    return _clients[path]


def _get_collection() -> Collection:
    client = _get_client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_functions.DefaultEmbeddingFunction(),
    )


def _chunk_to_metadata(chunk: Chunk) -> dict[str, str | int]:
    """Chroma metadata values must be str/int/float/bool — dates are stored
    as ISO strings and rebuilt on the way back out in `_row_to_chunk`.
    """
    return {
        "ticker": chunk.ticker,
        "fiscal_year": chunk.fiscal_year,
        "fiscal_period_end": chunk.fiscal_period_end.isoformat(),
        "section": chunk.section,
        "source_url": chunk.source_url,
        "filing_date": chunk.filing_date.isoformat(),
    }


def _row_to_chunk(chunk_id: str, text: str, metadata: dict) -> Chunk:
    return Chunk(
        text=text,
        chunk_id=chunk_id,
        ticker=metadata["ticker"],
        fiscal_year=metadata["fiscal_year"],
        fiscal_period_end=date.fromisoformat(metadata["fiscal_period_end"]),
        section=metadata["section"],
        source_url=metadata["source_url"],
        filing_date=date.fromisoformat(metadata["filing_date"]),
    )


def _build_where(filters: dict | None) -> dict | None:
    """Translate a filters dict into a Chroma `where` clause, keeping only
    the hard-filter keys the contract promises (PRD FR2.2) and dropping
    keys that are absent or explicitly None.
    """
    if not filters:
        return None

    conditions = [{key: filters[key]} for key in _FILTER_KEYS if filters.get(key) is not None]
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def add_chunks(chunks: list[Chunk]) -> None:
    """Embed and upsert chunks into the persistent Chroma collection,
    keyed on `chunk_id` for idempotent re-ingestion (FR1.6).
    """
    if not chunks:
        return

    collection = _get_collection()
    collection.upsert(
        ids=[chunk.chunk_id for chunk in chunks],
        documents=[chunk.text for chunk in chunks],
        metadatas=[_chunk_to_metadata(chunk) for chunk in chunks],
    )


def search(query: str, k: int = 5, filters: dict | None = None) -> list[Chunk]:
    """Top-k semantic search with optional hard metadata filters on ticker,
    fiscal_year, and section (PRD FR2.1, FR2.2). Returns an empty list when
    the collection is empty or nothing matches the filters — never raises
    for "no results".
    """
    collection = _get_collection()
    result = collection.query(
        query_texts=[query],
        n_results=k,
        where=_build_where(filters),
        include=["documents", "metadatas"],
    )

    ids = result["ids"][0]
    documents = result["documents"][0]
    metadatas = result["metadatas"][0]

    return [
        _row_to_chunk(chunk_id, text, metadata)
        for chunk_id, text, metadata in zip(ids, documents, metadatas, strict=True)
    ]
