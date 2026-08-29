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

How equivalence with `agent_custom` is enforced (not merely intended)
---------------------------------------------------------------------
Every behavior-bearing ingredient is **imported from `src.agent`**, never
re-declared here:

- `agent.SYSTEM_PROMPT_TOOLS`  — same system prompt, byte for byte
- `TOOL_SCHEMAS` + `llm.tool_specs` — same three tools, same descriptions
- `agent._execute_tool`        — same dispatch, same `ToolCall` record,
                                 same deterministic FR4.5 evidence signal
- `agent._citations_from_chunks` — same citation extraction
- `agent._REFUSAL_MESSAGE`     — same refusal text
- `agent._default_tool_functions` — same real tool bindings
- `MAX_TURNS = agent.MAX_TURNS` — the cap cannot drift between the arms
- `src.llm.LLMSession`         — same provider/model, pinned once in
                                 `src.llm` and shared by all four arms

What is genuinely different (and therefore what ADR 002 measures)
------------------------------------------------------------------
Only the orchestration mechanism: `agent_custom` runs a `while` loop with
`break`s; this arm runs a compiled `StateGraph` whose nodes are the same
three steps (call the model / execute one tool / force a final synthesis)
and whose `break`s are conditional edges. The observable
`QueryResponse` — answer, citations, trace, `incomplete`, `refused` — is
produced by the same code in both arms.

Three framework-specific facts that have no counterpart in the raw loop,
recorded here because ADR 002 has to state them rather than gloss them:

1. **LangGraph imposes its own `recursion_limit`** (default 25 supersteps)
   and raises `GraphRecursionError` when it trips. That is a second,
   framework-level cap layered on top of FR4.1's turn cap. It is set
   explicitly below, comfortably above the maximum number of supersteps
   this graph can reach, so the FR4.1 cap is always the one that fires —
   but the raw loop has no such concept at all.
2. **The graph is compiled once** (module-level cache) rather than rebuilt
   per query, so the per-query framework cost measured by the eval harness
   is `CompiledStateGraph.invoke()` overhead, not graph construction.
   Compilation cost is paid once, at first call.
3. **Importing `langgraph` emits a `LangChainPendingDeprecationWarning`**
   from `langgraph.checkpoint.base` at 0.2.45. The raw loop imports
   nothing that warns. Cosmetic, but it is an observable difference in
   what the framework arm drags in.

No behavior `agent_custom` performs turned out to be inexpressible as a
StateGraph — the raw loop's control flow maps onto nodes and conditional
edges one-to-one.
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src import agent, llm
from src.schemas import Citation, QueryResponse, ToolCall
from src.tool_schemas import TOOL_SCHEMAS

# Bound to src.agent's cap rather than re-typed as a literal 5: the whole
# point of this arm is that the two agent arms cannot silently diverge.
MAX_TURNS = agent.MAX_TURNS

# LangGraph counts supersteps, not turns. The longest path through this
# graph is START -> (call_model -> execute_tool) x MAX_TURNS -> synthesize,
# i.e. 2 * MAX_TURNS + 1 supersteps. The margin keeps the framework's cap
# from ever pre-empting FR4.1's cap (see module docstring, note 1).
RECURSION_LIMIT = 2 * MAX_TURNS + 5

_NODE_MODEL = "call_model"
_NODE_TOOL = "execute_tool"
_NODE_SYNTHESIZE = "synthesize"


class AgentState(TypedDict):
    """The graph's channel schema.

    Every field uses LangGraph's default last-value-wins channel, so a node
    returning a key simply replaces it — nodes build and return whole new
    lists rather than mutating the ones they were handed.

    `session` is the one deliberately stateful channel: an `LLMSession`
    carries the provider-native message history, and threading a tool
    result into that history is the conversational state the graph is
    orchestrating. It is passed by reference and never replaced.
    """

    session: llm.LLMSession
    tool_specs: list[dict[str, Any]]
    tool_functions: agent.ToolFunctions
    turn: int
    response: llm.NormalizedResponse | None
    trace: list[ToolCall]
    citations: list[Citation]
    found_evidence: bool
    incomplete: bool
    final_text: str


# --- Nodes -------------------------------------------------------------------


def _call_model(state: AgentState) -> dict[str, Any]:
    """One tool-enabled turn. Mirrors the top of `_run_tool_arm`'s loop
    body: increment the turn counter, send, and — if the model stopped
    asking for tools — treat that response's text as final.
    """
    response = state["session"].send(tools=state["tool_specs"], disable_parallel_tool_use=True)
    update: dict[str, Any] = {"turn": state["turn"] + 1, "response": response}
    if not response.has_tool_calls:
        update["final_text"] = response.text
    return update


def _execute_tool(state: AgentState) -> dict[str, Any]:
    """Execute AT MOST ONE tool call and thread its result back into the
    conversation.

    `disable_parallel_tool_use=True` asks the provider for a single call;
    as in `agent_custom`, the cap is also enforced here in code — only
    `tool_calls[0]` is ever executed, however many blocks a (stubbed or
    misbehaving) response actually contains.
    """
    response = state["response"]
    assert response is not None and response.tool_calls  # routed here only when true
    tool_call = response.tool_calls[0]

    raw_result, content, call_record, evidence = agent._execute_tool(
        tool_call.name, tool_call.arguments, state["tool_functions"]
    )

    citations = state["citations"]
    if tool_call.name == "search_filings" and raw_result:
        citations = [*citations, *agent._citations_from_chunks(raw_result)]

    state["session"].add_tool_result(tool_call.id, content)

    return {
        "trace": [*state["trace"], call_record],
        "citations": citations,
        "found_evidence": state["found_evidence"] or evidence,
    }


def _synthesize(state: AgentState) -> dict[str, Any]:
    """FR4.1 turn-cap fallback: the model was still requesting tools on the
    last permitted turn, so force one tools-free call for a final answer
    and mark the response `incomplete`.

    `agent_custom` computes this flag as `max_turns > 1` because it shares
    `_run_tool_arm` with `baseline_tools` (max_turns=1, where the synthesis
    step is mandatory by design rather than an overrun). This arm has no
    such sibling — MAX_TURNS is always 5 — so reaching this node is always
    a genuine overrun. Same value, fewer moving parts.
    """
    synthesis = state["session"].send(tools=None)
    return {"final_text": synthesis.text, "incomplete": True}


# --- Edges -------------------------------------------------------------------


def _route_after_model(state: AgentState) -> str:
    """`if not response.has_tool_calls: break` — as a conditional edge."""
    response = state["response"]
    return _NODE_TOOL if response is not None and response.has_tool_calls else END


def _route_after_tool(state: AgentState) -> str:
    """`if turn == max_turns: synthesis; break` — as a conditional edge."""
    return _NODE_SYNTHESIZE if state["turn"] >= MAX_TURNS else _NODE_MODEL


# --- Graph -------------------------------------------------------------------


def build_graph() -> StateGraph:
    """The uncompiled `StateGraph`. Exposed (rather than inlined into the
    cached compile) so tests and ADR 002 can inspect the topology without
    running the arm.
    """
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node(_NODE_MODEL, _call_model)
    graph.add_node(_NODE_TOOL, _execute_tool)
    graph.add_node(_NODE_SYNTHESIZE, _synthesize)

    graph.add_edge(START, _NODE_MODEL)
    graph.add_conditional_edges(_NODE_MODEL, _route_after_model, {_NODE_TOOL: _NODE_TOOL, END: END})
    graph.add_conditional_edges(
        _NODE_TOOL,
        _route_after_tool,
        {_NODE_SYNTHESIZE: _NODE_SYNTHESIZE, _NODE_MODEL: _NODE_MODEL},
    )
    graph.add_edge(_NODE_SYNTHESIZE, END)
    return graph


_COMPILED: Any | None = None


def compiled_graph() -> Any:
    """Compile once, reuse for every query — see module docstring, note 2.

    The graph is stateless between runs (no checkpointer; all per-query
    state lives in the invocation's `AgentState`), so a single compiled
    instance is safe to share.
    """
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph().compile()
    return _COMPILED


# --- Arm entry point ----------------------------------------------------------


def run_agent_langgraph(
    question: str,
    *,
    client: Any | None = None,
    tool_functions: agent.ToolFunctions | None = None,
    provider: str | None = None,
) -> QueryResponse:
    """Same behavior as src.agent.run_agent_custom, orchestrated via a
    LangGraph StateGraph instead of a hand-rolled loop. See module
    docstring.

    `client` / `tool_functions` / `provider` are the same optional
    dependency-injection points the other three arms expose (defaulting to
    `src.llm.PROVIDER`'s real client and `src.tools`), so tests can supply
    a stubbed LLM and fixture-backed fake tools without any live API call.
    `src/api.py` and `eval/run_eval.py` call this with the question alone.
    """
    start = time.monotonic()
    llm_client = client if client is not None else llm.default_client(provider)
    functions = tool_functions if tool_functions is not None else agent._default_tool_functions()

    initial: AgentState = {
        "session": llm.LLMSession(
            llm_client, system=agent.SYSTEM_PROMPT_TOOLS, question=question, provider=provider
        ),
        "tool_specs": llm.tool_specs(TOOL_SCHEMAS, provider=provider),
        "tool_functions": functions,
        "turn": 0,
        "response": None,
        "trace": [],
        "citations": [],
        "found_evidence": False,
        "incomplete": False,
        "final_text": "",
    }

    final = compiled_graph().invoke(initial, config={"recursion_limit": RECURSION_LIMIT})

    # FR4.5, applied exactly as `_run_tool_arm` applies it: the model's
    # text is discarded outright when no tool call produced evidence.
    if final["found_evidence"]:
        answer = final["final_text"]
        refused = False
    else:
        answer = agent._REFUSAL_MESSAGE
        refused = True

    return QueryResponse(
        answer=answer,
        citations=final["citations"],
        trace=final["trace"],
        latency_ms=(time.monotonic() - start) * 1000,
        mode="agent_langgraph",
        incomplete=final["incomplete"],
        refused=refused,
    )
