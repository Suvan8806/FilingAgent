"""FilingAgent — tool JSON schemas (Wave 0, frozen).

These are the three tools exposed to every tool-using arm (`baseline_tools`,
`agent_custom`, `agent_langgraph` — see PRD FR3, FR4). Parameter names and
types here are load-bearing: Lane D (src/tools.py) implements against this
exact shape, Lane E (src/baseline.py, src/agent.py) passes these schemas to
the provider SDK verbatim, and the eval harness (Lane F) asserts
`expected_tools` against the `name` field of recorded `ToolCall`s.

**Tool descriptions are the highest-leverage prose in this repo.** The LLM
never sees this repo's docstrings or the PRD — it sees exactly the
`description` strings below, at decision time, with no other signal about
what each tool is for. A vague description here is why an agent might call
`search_filings` for a number that's sitting in the XBRL table, or call
`lookup_financial` with a metric name that doesn't match the hardcoded tag
map. Every description is written to disambiguate against the *other two*
tools, not just to explain itself in isolation.
"""

from __future__ import annotations

from typing import Any

# --- search_filings ---------------------------------------------------------

SEARCH_FILINGS_SCHEMA: dict[str, Any] = {
    "name": "search_filings",
    "description": (
        "Semantic search over indexed 10-K prose — Item 1 (Business), Item 1A "
        "(Risk Factors), and Item 7 (MD&A) only. Use this for qualitative "
        "questions: what a company says about its risks, strategy, business "
        "segments, competitive position, or management's narrative explanation "
        "of a financial result (e.g. 'why did revenue grow'). Do NOT use this "
        "to obtain a specific reported number — dollar figures, percentages, or "
        "counts that appear in the financial statements are handled far more "
        "reliably by lookup_financial, which returns an exact XBRL-tagged value "
        "instead of a fuzzy text match that may quote a nearby-but-different "
        "figure. This tool has no access to Item 8 (financial statements and "
        "notes) — it is not indexed, so some numeric facts are only reachable "
        "via lookup_financial, never via search. If a query names a specific "
        "fiscal year, prefer setting fiscal_year explicitly rather than typing "
        "the year into free text — the corpus contains filings whose fiscal "
        "years do not align with calendar years (e.g. Microsoft's FY ends in "
        "June, Apple's in late September), and an unfiltered search can return "
        "prose from the wrong fiscal period."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language search text describing the information "
                    "need, e.g. 'principal supply chain risk' or 'drivers of "
                    "R&D expense growth'. Not a keyword list — write it as you "
                    "would ask a person."
                ),
            },
            "ticker": {
                "type": ["string", "null"],
                "description": (
                    "Restrict results to one filer, e.g. 'MSFT' or 'AAPL'. "
                    "Omit (null) to search across all indexed tickers — rarely "
                    "correct when the question names a specific company."
                ),
            },
            "fiscal_year": {
                "type": ["integer", "null"],
                "description": (
                    "Restrict results to one fiscal year, e.g. 2024. This is "
                    "the filer's own fiscal year label, not a calendar year — "
                    "always set this when the question names a fiscal year, "
                    "since fiscal-year windows differ by company and an "
                    "unfiltered search can silently mix periods."
                ),
            },
            "section": {
                "type": ["string", "null"],
                "enum": ["item1", "item1a", "item7", None],
                "description": (
                    "Restrict results to one section: 'item1' (Business — "
                    "segments, products, strategy), 'item1a' (Risk Factors), "
                    "or 'item7' (MD&A — results of operations, liquidity, "
                    "management's explanation of changes). Omit (null) to "
                    "search all three. Set this whenever the question's intent "
                    "clearly maps to one section (e.g. 'risk' -> item1a) — it "
                    "meaningfully improves precision."
                ),
            },
        },
        "required": ["query"],
    },
    # Return type: list[src.schemas.Chunk]. Empty list means no chunk cleared
    # the similarity threshold under the given filters — not an error.
}

# --- lookup_financial ---------------------------------------------------------

LOOKUP_FINANCIAL_SCHEMA: dict[str, Any] = {
    "name": "lookup_financial",
    "description": (
        "Exact lookup of a single reported financial metric from the "
        "structured XBRL fact table — the authoritative source for any "
        "dollar figure, not search_filings. Use this whenever the question "
        "asks 'what was X' for a specific company and fiscal year (revenue, "
        "net income, R&D expense, operating income, gross profit, total "
        "assets, operating cash flow, or SG&A). Returns exactly one fact per "
        "call — for a question spanning two fiscal years (e.g. 'how did "
        "revenue change from FY2023 to FY2024'), call this tool twice, once "
        "per year, then use calculate to find the difference; do not guess "
        "the delta from a single call or from prose. If the metric is not "
        "tagged for this filer in this fiscal year (some companies report a "
        "combined line item that others report separately, or vice versa), "
        "this returns a typed miss with a reason instead of raising an error "
        "— treat a miss as a real, reportable answer ('not reported'), not as "
        "a signal to fall back to search_filings for the same number."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Filer ticker, e.g. 'MSFT' or 'AAPL'.",
            },
            "metric": {
                "type": "string",
                "description": (
                    "One of the hardcoded metric keys: 'revenue', "
                    "'rnd_expense', 'gross_profit', 'operating_income', "
                    "'net_income', 'total_assets', 'operating_cash_flow', "
                    "'sga'. Use the filer's own fiscal-year label, not a "
                    "calendar year."
                ),
            },
            "fiscal_year": {
                "type": "integer",
                "description": "Fiscal year of the figure, e.g. 2024.",
            },
        },
        "required": ["ticker", "metric", "fiscal_year"],
    },
    # Return type: src.schemas.Fact | src.schemas.Miss.
}

# --- calculate ---------------------------------------------------------

CALCULATE_SCHEMA: dict[str, Any] = {
    "name": "calculate",
    "description": (
        "Evaluate a numeric arithmetic expression (+, -, *, /, parentheses) "
        "over literal numbers. Use this for any arithmetic on figures already "
        "obtained from lookup_financial — differences, percentage changes, "
        "sums, ratios — instead of computing it yourself in prose. Doing "
        "arithmetic outside this tool risks a transcription or rounding "
        "error that this tool's exact evaluation avoids. Do not put a metric "
        "name or a question into this tool — it only accepts a self-contained "
        "numeric expression, e.g. '245122000000 - 211915000000', never "
        "'Microsoft revenue 2024 minus 2023'. This tool does not have access "
        "to any figures itself — obtain the numbers from lookup_financial "
        "first, then pass them here literally."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "A self-contained arithmetic expression using only numeric "
                    "literals, +, -, *, /, and parentheses, e.g. "
                    "'(245122000000 - 211915000000) / 211915000000'. Evaluated "
                    "against a restricted AST node whitelist, never Python's "
                    "built-in eval (FR4.3) — no names, calls, or attribute "
                    "access are permitted."
                ),
            },
        },
        "required": ["expression"],
    },
    # Return type: float.
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    SEARCH_FILINGS_SCHEMA,
    LOOKUP_FINANCIAL_SCHEMA,
    CALCULATE_SCHEMA,
]
