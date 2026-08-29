.PHONY: ingest serve eval eval-live test lint

# Parse data/raw/ + data/reference/, chunk, embed, and populate the Chroma
# store and SQLite facts table. Idempotent — safe to re-run (FR1.6).
# TODO(Wave 2): wire to src/ingest.py + src/xbrl.py once implemented.
ingest:
	@echo "TODO: python -m src.ingest"

# Run the FastAPI app locally with the pre-built index.
# TODO(Wave 1/2): wire to src/api.py once implemented.
serve:
	@echo "TODO: uvicorn src.api:app --reload"

# Replay recorded judge fixtures (eval/fixtures/judgments/) — deterministic,
# no live API calls. This is what CI runs (FR8.5, FR9.5).
# TODO(Wave 1): wire to eval/run_eval.py once implemented.
eval:
	@echo "TODO: python -m eval.run_eval --replay"

# Run the full four-arm eval against the real index with live judge calls.
# Not run in CI — this is the Wave 3 measurement run.
# TODO(Wave 1): wire to eval/run_eval.py once implemented.
eval-live:
	@echo "TODO: python -m eval.run_eval --live"

# Run the full test suite with coverage.
test:
	pytest tests/ -v

# Lint src/, eval/, and tests/.
lint:
	ruff check src/ eval/ tests/
