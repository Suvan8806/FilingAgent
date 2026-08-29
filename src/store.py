"""Vector store wrapper — ChromaDB (Lane B — PLAN.md Wave 1).

Owns: this file and src/facts.py, exclusively.

Contract
--------
Thin wrapper over a local, persistent ChromaDB collection. This module owns
all Chroma-specific detail; no other module should import chromadb
directly.

Required operations:

- `add_chunks(chunks: list[Chunk]) -> None` — embed and upsert. Must key on
  `chunk_id` (deterministic, from src.chunking) so re-running ingestion
  (FR1.6) updates in place rather than duplicating. Persist to the
  directory named by the `CHROMA_DIR` env var (.env.example).
- `search(query: str, k: int, filters: dict | None = None) -> list[Chunk]`
  — top-k by embedding similarity, with optional hard metadata filters on
  `ticker`, `fiscal_year`, and `section` (PRD FR2.2). This is the retrieval
  primitive that src.tools.search_filings sits directly on top of; keep the
  filter contract identical to that tool's parameter names.

Embeddings: Chroma's default local ONNX MiniLM embedding function (see
PRD §9 Technology choices) — no second API key, no per-call embedding
cost, keeps `docker compose up` one-command.

Test fixtures: tests/fixtures/mini_corpus.json (20 realistic Chunk records)
is the frozen input for unit-testing this module without a real ingest run.
"""

from __future__ import annotations

from src.schemas import Chunk


def add_chunks(chunks: list[Chunk]) -> None:
    """Embed and upsert chunks into the persistent Chroma collection,
    keyed on `chunk_id` for idempotent re-ingestion (FR1.6).
    """
    raise NotImplementedError


def search(query: str, k: int = 5, filters: dict | None = None) -> list[Chunk]:
    """Top-k semantic search with optional metadata filters on ticker,
    fiscal_year, and section (PRD FR2.1, FR2.2).
    """
    raise NotImplementedError
