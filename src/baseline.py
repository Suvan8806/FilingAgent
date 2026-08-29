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

Shared dispatch path
---------------------
`run_baseline_tools` is a thin wrapper around `src.agent._run_tool_arm`
with `max_turns=1` — the exact same code `run_agent_custom` runs with
`max_turns=5`. `run_baseline_rag` cannot go through that same tool loop
(it has no tool schemas exposed to the model at all — that absence is the
arm's entire point), but it reuses `src.agent`'s tool-function factory
and citation builder, and — like every other arm — talks to the model
only through `src.llm.LLMSession` / `src.llm.default_client`, never a
raw provider SDK call, so its retrieval bookkeeping and response
assembly stay identical in shape to the tool-using arms (PLAN.md Wave 1
Lane E: "All three of your arms share ONE dispatch path so the only
differences between them are tool access and turn cap — nothing else").
Provider and model are pinned once in `src.llm` and shared by every arm.
"""

from __future__ import annotations

import time
from typing import Any

from src import agent, llm
from src.schemas import QueryResponse, ToolCall

SYSTEM_PROMPT_RAG = (
    "You are a financial research assistant answering questions about SEC "
    "10-K filings for MSFT and AAPL. Answer ONLY using the context excerpts "
    "provided below — never from your own prior knowledge of these "
    "companies' filings. If the context does not contain the answer, say "
    "explicitly that the answer could not be found in the provided context "
    "rather than guessing or answering from memory."
)

_TOP_K = 5


def run_baseline_rag(
    question: str,
    *,
    client: Any | None = None,
    tool_functions: agent.ToolFunctions | None = None,
    provider: str | None = None,
) -> QueryResponse:
    """Single-shot top-5 retrieval, no tools, no iteration (FR3.1).

    `client` / `tool_functions` / `provider` are optional dependency
    -injection points (default to `src.llm.PROVIDER`'s real client and
    `src.tools`) so tests can supply a stubbed LLM — for either wire
    format — and fixture-backed fake tools without any live API calls,
    matching `run_baseline_tools` / `run_agent_custom`.
    """
    start = time.monotonic()
    llm_client = client if client is not None else llm.default_client(provider)
    functions = tool_functions if tool_functions is not None else agent._default_tool_functions()

    search = functions["search_filings"]
    retrieval_args: dict[str, Any] = {
        "query": question,
        "ticker": None,
        "fiscal_year": None,
        "section": None,
    }
    t0 = time.monotonic()
    try:
        chunks = list(search(**retrieval_args) or [])
    except Exception as exc:  # a retrieval failure is a typed, recorded miss — never crash the arm
        chunks = []
        summary = f"error: {exc}"
    else:
        chunks = chunks[:_TOP_K]
        summary = f"{len(chunks)} chunk(s) found" if chunks else "no matching chunks"
    latency_ms = (time.monotonic() - t0) * 1000

    trace = [
        ToolCall(
            name="search_filings",
            arguments=retrieval_args,
            result_summary=summary,
            latency_ms=latency_ms,
        )
    ]
    citations = agent._citations_from_chunks(chunks)

    if not chunks:
        answer = agent._REFUSAL_MESSAGE
        refused = True
    else:
        context_block = "\n\n".join(
            f"[{chunk.ticker} FY{chunk.fiscal_year} {chunk.section}] {chunk.text}" for chunk in chunks
        )
        session = llm.LLMSession(
            llm_client,
            system=SYSTEM_PROMPT_RAG,
            question=f"Context:\n{context_block}\n\nQuestion: {question}",
            provider=provider,
        )
        answer = session.send(tools=None).text
        refused = False

    return QueryResponse(
        answer=answer,
        citations=citations,
        trace=trace,
        latency_ms=(time.monotonic() - start) * 1000,
        mode="baseline_rag",
        incomplete=False,
        refused=refused,
    )


def run_baseline_tools(
    question: str,
    *,
    client: Any | None = None,
    tool_functions: agent.ToolFunctions | None = None,
    provider: str | None = None,
) -> QueryResponse:
    """Full tool access, capped at exactly one tool call + one synthesis
    step (FR3.2). The confound control for the four-arm design.

    Delegates to `src.agent._run_tool_arm` with `max_turns=1` — see the
    module docstring's "Shared dispatch path" section. Do not special
    -case this function's prompt, retrieval, or synthesis logic
    separately from `agent_custom`; the one-call cap is enforced entirely
    inside the shared loop (turn cap + `disable_parallel_tool_use=True`
    + taking at most the first tool_use block per turn regardless of how
    many a response contains).
    """
    return agent._run_tool_arm(
        question,
        mode="baseline_tools",
        max_turns=1,
        client=client,
        tool_functions=tool_functions,
        provider=provider,
    )
