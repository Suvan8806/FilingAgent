.PHONY: ingest serve eval eval-live test lint

# Parse data/raw/ + data/reference/, chunk, embed, and populate the Chroma
# store and SQLite facts table. Idempotent — safe to re-run (FR1.6).
# Reads the four committed 10-Ks from disk via data/raw/manifest.json and the
# committed companyfacts snapshots from data/reference/ — no live EDGAR
# traffic on this path (PLAN.md "Lane A note"). Embeddings are computed
# locally by Chroma's default ONNX MiniLM, so this costs no API tokens.
ingest:
	python -m src.ingest

# Run the FastAPI app locally with the pre-built index.
serve:
	uvicorn src.api:app --reload

# Replay recorded judge fixtures (eval/fixtures/judgments/) — deterministic,
# no live API calls. This is what CI runs (FR8.5, FR9.5).
eval:
	python -m eval.run_eval --replay

# Run the full four-arm eval against the real index with live judge calls.
# Not run in CI — this is the Wave 3 measurement run.
eval-live:
	python -m eval.run_eval --live

# Run the full test suite with coverage.
test:
	pytest tests/ -v

# Lint src/, eval/, and tests/.
lint:
	ruff check src/ eval/ tests/
