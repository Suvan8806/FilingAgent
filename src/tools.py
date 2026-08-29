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
  (FR4.4) — that is what `Miss` is for. Also never raises for an
  *unexpected* internal error from the underlying store: this is a tool
  boundary the LLM calls directly, so any failure below it is converted
  into a typed `Miss` rather than propagating an exception into the agent
  loop.
- `calculate(expression: str) -> float` — arithmetic ONLY over a restricted
  AST node whitelist (FR4.3). **Must not use Python's `eval`.** Parsed with
  `ast.parse(expression, mode="eval")` and walked, allowing only:
  `ast.BinOp` (`+ - * / **`), `ast.UnaryOp` (unary `+`/`-`), and
  `ast.Constant` holding an `int` or `float` (never `bool`, `str`, `bytes`,
  `None`, or complex). Parentheses need no explicit node — they only affect
  parse precedence. Anything else (`ast.Name`, `ast.Call`, `ast.Attribute`,
  `ast.Subscript`, comprehensions, comparisons, ...) is rejected by falling
  through to a typed `CalculatorError` — this is a security boundary
  (arbitrary code execution via tool-calling), not a style preference.
  Division by zero, and float overflow from huge exponents, are caught and
  re-raised as the same typed `CalculatorError` rather than a raw
  `ZeroDivisionError`/`OverflowError` traceback (FR9.1).

Both `src.store` and `src.facts` are imported lazily, inside each function
body, rather than at module import time. Lane B owns those two files and is
writing them concurrently; deferring the import means `import src.tools`
never fails because of unrelated work-in-progress in a sibling module (a
missing dependency, a transient syntax error, etc.), and it is exactly what
makes `store.search` / `facts.lookup` patchable via `sys.modules` in tests
without ever touching the real ChromaDB/SQLite-backed implementations.

Required unit-test failure paths (FR9.1): unknown ticker, missing/
unsupported fiscal year, malformed `calculate` expression, division by
zero, and an injection attempt through `calculate`. Test against
tests/fixtures/mini_corpus.json and tests/fixtures/mini_facts.csv, not the
real store/facts DB.
"""

from __future__ import annotations

import ast
import operator
from typing import Callable

from src.schemas import Chunk, Fact, Miss


class CalculatorError(ValueError):
    """Raised by `calculate` for a malformed or disallowed expression, or
    for arithmetic that cannot be evaluated (division by zero, overflow).
    A typed, catchable error — never a raw traceback (FR4.3, FR9.1).
    """


def search_filings(
    query: str,
    ticker: str | None = None,
    fiscal_year: int | None = None,
    section: str | None = None,
) -> list[Chunk]:
    """Semantic search over indexed filing prose. See src/tool_schemas.py
    SEARCH_FILINGS_SCHEMA for the description surfaced to the LLM.

    Thin pass-through to `src.store.search`: builds a hard-filter dict from
    whichever of `ticker` / `fiscal_year` / `section` are given (omitting
    the ones left as `None`, so an unfiltered call searches the whole
    corpus) and forwards the query with the fixed top-5 budget (FR2.1).
    An empty result list means nothing cleared the similarity threshold
    under the given filters — not an error, and not raised as one.
    """
    from src import store

    filters = {
        key: value
        for key, value in {"ticker": ticker, "fiscal_year": fiscal_year, "section": section}.items()
        if value is not None
    }
    return store.search(query, k=5, filters=filters or None)


def lookup_financial(ticker: str, metric: str, fiscal_year: int) -> Fact | Miss:
    """Exact XBRL fact lookup. See src/tool_schemas.py
    LOOKUP_FINANCIAL_SCHEMA for the description surfaced to the LLM.

    Thin pass-through to `src.facts.lookup`, which is itself responsible
    for returning a typed `Miss` (never raising) on an unknown ticker,
    unsupported fiscal year, or an untagged metric (FR4.4). This wrapper
    adds one more layer of defense: if the underlying lookup raises
    anything unexpected anyway, it is caught here and converted into a
    `Miss` too, so a bug two layers down can never surface as a raw
    exception to the tool-calling agent loop.
    """
    from src import facts

    try:
        return facts.lookup(ticker, metric, fiscal_year)
    except Exception as exc:  # noqa: BLE001 - tool boundary must never raise (FR4.4)
        return Miss(
            ticker=ticker,
            metric=metric,
            fiscal_year=fiscal_year,
            reason=f"internal lookup error: {exc}",
        )


# --- calculate ---------------------------------------------------------

_ALLOWED_BINOPS: dict[type, Callable[[int | float, int | float], int | float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARYOPS: dict[type, Callable[[int | float], int | float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def calculate(expression: str) -> float:
    """Arithmetic over a restricted AST node whitelist — never `eval`.
    See src/tool_schemas.py CALCULATE_SCHEMA for the description surfaced
    to the LLM, and the module docstring above for the exact node
    whitelist.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise CalculatorError(f"malformed expression {expression!r}: {exc}") from exc

    try:
        result = _eval_node(tree.body, expression)
    except ZeroDivisionError as exc:
        raise CalculatorError(f"division by zero in expression {expression!r}") from exc
    except OverflowError as exc:
        raise CalculatorError(f"numeric overflow in expression {expression!r}") from exc

    return float(result)


def _eval_node(node: ast.AST, expression: str) -> int | float:
    """Recursively evaluate a whitelisted subset of the AST. Any node type
    not explicitly handled below (ast.Name, ast.Call, ast.Attribute,
    ast.Subscript, comprehensions, comparisons, ...) falls through to the
    final `raise` — it is never evaluated, executed, or looked up.
    """
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _eval_node(node.left, expression)
        right = _eval_node(node.right, expression)
        return _ALLOWED_BINOPS[type(node.op)](left, right)

    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand, expression))

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value

    raise CalculatorError(
        f"disallowed element {type(node).__name__!r} in expression {expression!r} — "
        "calculate() only accepts numeric literals, + - * / **, unary minus, and "
        "parentheses; names, calls, attribute access, subscripts, and comprehensions "
        "are rejected"
    )
