"""The three agent tools (Lane D — PLAN.md Wave 1).

Owns: this file, exclusively.

Contract
--------
Implements the three tools whose JSON schemas (including the highest
-leverage prose in the repo — the `description` fields the LLM actually
sees at decision time) are frozen in src/tool_schemas.py. Parameter names
and types here must match that file exactly; do not drift them apart.

- `search_filings(query, ticker, fiscal_year, section) -> list[Chunk]` —
  thin pass-through to `src.store.search` with the same filter semantics.
  Returns an empty list (not an error) when nothing clears the similarity
  threshold under the given filters.
- `lookup_financial(ticker, metric, fiscal_year) -> Fact | Miss` — thin
  pass-through to `src.facts.lookup`. Never raises for an absent fact
  (FR4.4) — that is what `Miss` is for.
- `calculate(expression: str) -> float` — arithmetic ONLY over a restricted
  AST node whitelist (FR4.3). **Must not use Python's `eval`.** Parse with
  `ast.parse(expression, mode="eval")` and walk the tree, allowing only:
  `ast.Expression`, `ast.BinOp`, `ast.UnaryOp`, `ast.Constant` (numeric),
  and the operators `+ - * / ( )`. Reject anything else (names, calls,
  attribute access, subscripts, comparisons, ...) by raising a clear,
  typed error — this is a security boundary (arbitrary code execution via
  tool-calling), not a style preference. Division by zero must be handled
  explicitly, not left to propagate a raw ZeroDivisionError to the agent
  loop (FR9.1 lists it as a required failure-path test).

Required unit-test failure paths (FR9.1): unknown ticker, missing/
unsupported fiscal year, malformed `calculate` expression, division by
zero. Test against tests/fixtures/mini_corpus.json and
tests/fixtures/mini_facts.csv, not the real store/facts DB.
"""

from __future__ import annotations

from src.schemas import Chunk, Fact, Miss


def search_filings(
    query: str,
    ticker: str | None = None,
    fiscal_year: int | None = None,
    section: str | None = None,
) -> list[Chunk]:
    """Semantic search over indexed filing prose. See src/tool_schemas.py
    SEARCH_FILINGS_SCHEMA for the description surfaced to the LLM.
    """
    raise NotImplementedError


def lookup_financial(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss:
    """Exact XBRL fact lookup. See src/tool_schemas.py
    LOOKUP_FINANCIAL_SCHEMA for the description surfaced to the LLM.
    """
    raise NotImplementedError


def calculate(expression: str) -> float:
    """Arithmetic over a restricted AST node whitelist — never `eval`.
    See src/tool_schemas.py CALCULATE_SCHEMA for the description surfaced
    to the LLM, and the module docstring above for the exact node
    whitelist.
    """
    raise NotImplementedError
