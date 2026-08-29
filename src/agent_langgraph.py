"""agent_langgraph — StateGraph framework arm (PLAN.md Wave 1, cut-order
item 1 if the build runs long — keep the import and a stub, drop the eval
arm before dropping anything else).

Owns: this file, exclusively (not part of Lane E's src/agent.py write
scope — kept separate so a framework dependency never blocks the raw-loop
arm).

Contract
--------
Same tools, same behavior, same 5-turn cap as `agent_custom` (src/agent.py)
— implemented over LangGraph's `StateGraph` instead of a hand-rolled loop
(FR4.6). The two arms must differ **only** in orchestration mechanism, not
in capability, so that ADR 002 (docs/adr/002-tool-loop-vs-langgraph.md) is
a measurement of framework overhead at this scale, not a confounded
comparison.

Behavior — identical contract to src.agent.run_agent_custom:
- MAX_TURNS = 5 (FR4.1); exceeding it returns `incomplete=True`.
- Every tool call recorded as a `ToolCall` in the trace (FR4.2).
- No supporting evidence -> explicit refusal, not a parametric-knowledge
  answer (FR4.5).
- Uses `src.tool_schemas.TOOL_SCHEMAS` and dispatches to the same
  `src.tools` functions as `agent_custom` and `baseline_tools`.

If this arm is cut under time pressure, `src/api.py` must still import this
module and route `mode="agent_langgraph"` to a response that is honest
about being unimplemented (not silently aliased to `agent_custom`) — see
PLAN.md cut order.
"""

from __future__ import annotations

from src.schemas import QueryResponse

MAX_TURNS = 5


def run_agent_langgraph(question: str) -> QueryResponse:
    """Same behavior as src.agent.run_agent_custom, orchestrated via a
    LangGraph StateGraph instead of a hand-rolled loop. See module
    docstring.
    """
    raise NotImplementedError
