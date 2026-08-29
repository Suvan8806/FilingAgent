"""baseline_rag + baseline_tools arms (Lane E — PLAN.md Wave 1).

Owns: this file and src/agent.py, jointly (Lane E's exclusive write scope).

Contract
--------
Two of the four eval arms (PRD FR3). Both are deliberately simple —
complexity belongs in `agent_custom` / `agent_langgraph`, not here.

`baseline_rag` (FR3.1):
  Single retrieval call (top-5 chunks via `src.tools.search_filings`, no
  ticker/fiscal_year/section filters unless trivially derivable from the
  question), stuffed into one prompt with the question. System prompt must
  instruct: answer only from the provided context; if the context does not
  contain the answer, say so explicitly rather than fabricating (G3,
  FR4.5). No tool access beyond that single retrieval — this arm has no
  numeric tool access by construction, which is the point: it is the naive
  -RAG floor the other arms are measured against.

`baseline_tools` (FR3.2) — **the confound control, not a throwaway arm**:
  Identical tool schemas to `agent_custom`/`agent_langgraph`
  (src.tool_schemas.TOOL_SCHEMAS), but the loop is hard-capped at exactly
  one tool call followed by one synthesis step. Without this arm, the
  numeric tier is unwinnable for baseline_rag by construction (it has no
  XBRL access), so any agent "win" on numeric would be a statement about
  tool access, not agency — this arm exists specifically so that
  agent_custom's win (if any) on the multi-hop tier is attributable to
  *iteration*, not merely to having tools at all. Do not remove or weaken
  this arm under time pressure (PLAN.md "Never cut").

Both functions share the same `QueryRequest -> QueryResponse` shape used
by src.agent and src.agent_langgraph, and are expected to share a dispatch
path with them wired in src.api (single `mode -> handler` dict, not four
divergent code paths).
"""

from __future__ import annotations

from src.schemas import QueryResponse


def run_baseline_rag(question: str) -> QueryResponse:
    """Single-shot top-5 retrieval, no tools, no iteration (FR3.1)."""
    raise NotImplementedError


def run_baseline_tools(question: str) -> QueryResponse:
    """Full tool access, capped at exactly one tool call + one synthesis
    step (FR3.2). The confound control for the four-arm design.
    """
    raise NotImplementedError
