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

Shared dispatch path
---------------------
`run_agent_custom` and `src.baseline.run_baseline_tools` both call
`_run_tool_arm` below with the *same* system prompt, tool schemas, tool
-execution/trace/citation bookkeeping, and refusal logic — the two arms
differ **only** in `max_turns` (5 vs 1). `src.baseline.run_baseline_rag`
also reuses this module's helpers (`_default_tool_functions`,
`_citations_from_chunks`, `_REFUSAL_MESSAGE`) so that its retrieval
bookkeeping and response assembly are identical in shape to the tool
-using arms, even though it has no tool schemas exposed to the model
(PLAN.md Wave 1 Lane E: "All three of your arms share ONE dispatch path").

Provider and model
-------------------
Neither this module nor `src.baseline` talks to a wire format directly —
all LLM calls go through `src.llm.LLMSession` / `src.llm.default_client`
/ `src.llm.tool_specs`, which normalize Anthropic and Groq (OpenAI
-compatible) behind one interface (see `src/llm.py`'s docstring for the
exact format differences it hides and why the default Groq model string
was corrected). Provider and model are selected **once**, in `src.llm`,
via `LLM_PROVIDER`/`LLM_MODEL`, and every arm shares that single choice —
a difference between arms here would confound the whole four-arm
comparison the project measures.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from src import llm
from src import tools as _tools
from src.schemas import Citation, Fact, Miss, QueryResponse, ToolCall
from src.tool_schemas import TOOL_SCHEMAS

MAX_TURNS = 5

_REFUSAL_MESSAGE = (
    "This question could not be answered from the indexed corpus: the "
    "available tools returned no supporting evidence (no matching filing "
    "excerpts and no tagged financial fact), so no grounded answer can be "
    "given."
)

SYSTEM_PROMPT_TOOLS = (
    "You are a financial research assistant answering questions about SEC "
    "10-K filings for MSFT and AAPL. You have three tools: search_filings "
    "(semantic search over indexed filing prose), lookup_financial (exact "
    "XBRL financial facts), and calculate (arithmetic on numbers you have "
    "already retrieved). Answer ONLY using information returned by these "
    "tools during this conversation — never from your own prior knowledge "
    "of these companies' filings. If the tools do not return information "
    "that answers the question, say explicitly that the answer could not "
    "be found in the corpus rather than guessing or answering from "
    "memory. When you have enough information, give a direct final answer "
    "grounded in the figures or facts the tools returned."
)

ToolFunctions = Mapping[str, Callable[..., Any]]


def _default_tool_functions() -> ToolFunctions:
    """The real tool implementations (src.tools), used outside tests.

    Tests inject fixture-backed fakes instead (PLAN.md Wave 1 Lane E:
    "test with a stubbed LLM and fixture-backed fake tools") so arm
    behavior can be verified without depending on Lane D's concurrently
    -written src/tools.py implementation.
    """
    return {
        "search_filings": _tools.search_filings,
        "lookup_financial": _tools.lookup_financial,
        "calculate": _tools.calculate,
    }


def _citations_from_chunks(chunks: Any) -> list[Citation]:
    return [
        Citation(
            chunk_id=chunk.chunk_id,
            ticker=chunk.ticker,
            fiscal_year=chunk.fiscal_year,
            section=chunk.section,
            source_url=chunk.source_url,
        )
        for chunk in chunks or []
    ]


def _execute_tool(
    name: str, arguments: dict[str, Any], tool_functions: ToolFunctions
) -> tuple[Any, str, ToolCall, bool]:
    """Run one tool call and package it for both the trace and the
    `tool_result` sent back to the model.

    Returns `(raw_result, tool_result_content, ToolCall, found_evidence)`.
    `found_evidence` is the deterministic FR4.5 signal: True only when
    `search_filings` returned at least one chunk or `lookup_financial`
    returned a `Fact` (never for a `Miss`, an empty list, or a tool
    error) — `calculate` is treated as evidence-preserving since it only
    ever operates on numbers a prior evidence-bearing call produced.
    """
    func = tool_functions.get(name)
    start = time.monotonic()
    error_message: str | None = None
    raw_result: Any = None
    try:
        if func is None:
            raise KeyError(f"no implementation registered for tool {name!r}")
        raw_result = func(**arguments)
    except Exception as exc:  # tool failures become a typed, recorded miss — never crash the loop
        error_message = str(exc)
    latency_ms = (time.monotonic() - start) * 1000

    if error_message is not None:
        summary = f"error: {error_message}"
        content = json.dumps({"error": error_message})
        found_evidence = False
    elif name == "search_filings":
        chunks = list(raw_result or [])
        found_evidence = bool(chunks)
        summary = f"{len(chunks)} chunk(s) found" if chunks else "no matching chunks"
        content = json.dumps([c.model_dump(mode="json") for c in chunks])
    elif name == "lookup_financial":
        if isinstance(raw_result, Miss):
            found_evidence = False
            summary = f"miss: {raw_result.reason}"
        elif isinstance(raw_result, Fact):
            found_evidence = True
            summary = f"{raw_result.metric}={raw_result.value} {raw_result.unit} (FY{raw_result.fiscal_year})"
        else:
            found_evidence = False
            summary = "unrecognized lookup_financial result"
        content = json.dumps(raw_result.model_dump(mode="json") if hasattr(raw_result, "model_dump") else None)
    elif name == "calculate":
        found_evidence = True
        summary = f"= {raw_result}"
        content = json.dumps({"result": raw_result})
    else:
        found_evidence = False
        summary = f"unrecognized tool: {name}"
        content = json.dumps({"error": summary})

    call = ToolCall(name=name, arguments=dict(arguments), result_summary=summary, latency_ms=latency_ms)
    return raw_result, content, call, found_evidence


def _run_tool_arm(
    question: str,
    mode: str,
    max_turns: int,
    *,
    client: Any | None = None,
    tool_functions: ToolFunctions | None = None,
    provider: str | None = None,
) -> QueryResponse:
    """Shared dispatch path for `baseline_tools` (max_turns=1) and
    `agent_custom` (max_turns=5) — see module docstring.

    Every turn is one `LLMSession.send()` call with `TOOL_SCHEMAS`
    (translated to the active provider's wire shape by `llm.tool_specs`)
    and `disable_parallel_tool_use=True`, so at most one tool call can be
    issued per turn, in addition to the loop-level `max_turns` cap. When
    the model stops requesting tools, that response's text is final. If
    it is still requesting a tool on the last permitted turn, one further
    tools-free "synthesis" call forces a final answer — for
    `baseline_tools` (max_turns=1) this IS the mandated "one tool call
    plus one synthesis step"; for `agent_custom` it is the FR4.1
    turn-cap fallback, and only there does it set `incomplete=True`.

    This loop is entirely provider-agnostic: it only ever touches
    `NormalizedResponse` / `NormalizedToolCall` from `src.llm`, never a
    raw Anthropic or Groq response.
    """
    start = time.monotonic()
    llm_client = client if client is not None else llm.default_client(provider)
    functions = tool_functions if tool_functions is not None else _default_tool_functions()

    session = llm.LLMSession(llm_client, system=SYSTEM_PROMPT_TOOLS, question=question, provider=provider)
    tools = llm.tool_specs(TOOL_SCHEMAS, provider=provider)

    trace: list[ToolCall] = []
    citations: list[Citation] = []
    found_evidence = False
    incomplete = False
    final_text = ""

    turn = 0
    while turn < max_turns:
        turn += 1
        response = session.send(tools=tools, disable_parallel_tool_use=True)

        if not response.has_tool_calls:
            final_text = response.text
            break

        # `disable_parallel_tool_use=True` asks the provider for at most
        # one tool call; execute at most one regardless of what a
        # (stubbed) response actually contains — the code enforces the
        # cap itself, not just the request parameter.
        tool_call = response.tool_calls[0]
        raw_result, content, call_record, evidence = _execute_tool(tool_call.name, tool_call.arguments, functions)
        trace.append(call_record)
        found_evidence = found_evidence or evidence
        if tool_call.name == "search_filings" and raw_result:
            citations.extend(_citations_from_chunks(raw_result))

        session.add_tool_result(tool_call.id, content)

        if turn == max_turns:
            synthesis = session.send(tools=None)
            final_text = synthesis.text
            # Only agent_custom (max_turns > 1) can genuinely "exceed the
            # cap" (FR4.1); baseline_tools' single synthesis step is
            # mandatory by design, not an overrun.
            incomplete = max_turns > 1
            break

    if not found_evidence:
        final_text = _REFUSAL_MESSAGE
        refused = True
    else:
        refused = False

    latency_ms = (time.monotonic() - start) * 1000
    return QueryResponse(
        answer=final_text,
        citations=citations,
        trace=trace,
        latency_ms=latency_ms,
        mode=mode,
        incomplete=incomplete,
        refused=refused,
    )


def run_agent_custom(
    question: str,
    *,
    client: Any | None = None,
    tool_functions: ToolFunctions | None = None,
    provider: str | None = None,
) -> QueryResponse:
    """Iterate tool calls up to MAX_TURNS, producing a grounded answer or
    an explicit incomplete/refused response. See module docstring.

    `client` / `tool_functions` / `provider` are optional dependency
    -injection points (default to `src.llm.PROVIDER`'s real client and
    `src.tools`) so tests can supply a stubbed LLM — for either wire
    format — and fixture-backed fake tools without any live API calls.
    """
    return _run_tool_arm(
        question,
        mode="agent_custom",
        max_turns=MAX_TURNS,
        client=client,
        tool_functions=tool_functions,
        provider=provider,
    )
