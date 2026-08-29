"""agent_custom — raw tool-calling loop (Lane E — PLAN.md Wave 1).

Owns: this file, jointly with src/baseline.py (Lane E's exclusive write
scope).

Contract
--------
The `agent_custom` arm (PRD FR4): a direct tool-calling loop against the
provider SDK (~80 lines, fully legible — PRD §9 Technology choices), no
framework. This is the "iterative agency" arm, measured against
`baseline_tools` (identical tools, one call) to isolate the effect of
iteration itself (PRD FR3, §10 success criterion 4).

Behavior:
- **FR4.1** Hard cap of 5 tool-calling turns. On exceeding the cap without
  a final answer, return a response with `incomplete=True` and whatever
  partial answer is available — do not silently truncate or raise.
- **FR4.2** Every tool call, in order, is recorded as a `ToolCall` (name,
  arguments, result_summary, latency_ms) in `QueryResponse.trace`. This
  trace is what src.traces persists for the `/stats` monitoring surface
  and what eval/metrics.py uses to compute `avg_tool_calls`.
- **FR4.5** If no tool call returns supporting evidence, the final answer
  must explicitly state the question could not be answered from the
  corpus (`refused=True`), not answer from the model's parametric
  knowledge. This is what the unanswerable tier (q023–q025) measures.
- Uses `src.tool_schemas.TOOL_SCHEMAS` verbatim and dispatches accepted
  tool calls to `src.tools.search_filings` / `lookup_financial` /
  `calculate`.
- For provider-specific tool-calling semantics (message format, stop
  reasons, multi-turn tool result threading), consult current vendor docs
  at implementation time rather than memory (PRD §9) — e.g.
  https://docs.claude.com/en/api/overview for the Claude API.
"""

from __future__ import annotations

from src.schemas import QueryResponse

MAX_TURNS = 5


def run_agent_custom(question: str) -> QueryResponse:
    """Iterate tool calls up to MAX_TURNS, producing a grounded answer or
    an explicit incomplete/refused response. See module docstring.
    """
    raise NotImplementedError
