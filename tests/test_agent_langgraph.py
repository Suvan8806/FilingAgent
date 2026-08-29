"""Tests for the `agent_langgraph` arm (src/agent_langgraph.py).

No live API calls. Every test injects a stubbed LLM client and
fixture-backed fake tool functions, and an autouse guard makes a live call
*impossible*: `src.llm.default_client` is monkeypatched to raise, so an
arm that fell back to the real provider would fail loudly instead of
quietly spending the real `GROQ_API_KEY` that `src/__init__.py` now loads
from `.env`. Each test additionally asserts the stub was actually invoked
(`client.calls`), so a test that silently made zero LLM calls cannot pass
as a green equivalence check.

The stubs are imported from `tests.test_arms` rather than re-declared:
this file's central claim is that `agent_langgraph` and `agent_custom`
behave identically, and that claim is only worth anything if both arms are
driven by the *same* fake client and the *same* fake tools.

What is covered
---------------
- **Equivalence with `agent_custom`** across the paths that matter —
  happy path, search-with-citations, FR4.1 turn-cap overrun, FR4.5
  refusal, and a no-tool-call turn. Answer, citations, trace, `incomplete`
  and `refused` must match, as must the *shape of the LLM requests*
  (turn count, and which turns carried tool schemas).
- The arm is genuinely running LangGraph — a compiled `StateGraph` with
  the three expected nodes — not a hand-rolled loop wearing the name.
- The one-tool-call-per-turn cap holds in code, not just via the
  provider's `disable_parallel_tool_use` hint.
- Both provider wire formats (Anthropic, Groq/OpenAI-compatible) produce
  identical observable behavior, as for `agent_custom`.
"""

from __future__ import annotations

from typing import Any

import pytest

from src import agent_langgraph as arm
from src import llm
from src.agent import MAX_TURNS as CUSTOM_MAX_TURNS
from src.agent import SYSTEM_PROMPT_TOOLS, run_agent_custom
from src.agent_langgraph import run_agent_langgraph
from src.schemas import QueryResponse
from tests.test_arms import (
    FakeClient,
    FakeGroqClient,
    FakeMessage,
    FakeToolUseBlock,
    fixture_chunk,
    fixture_miss,
    groq_text_message,
    groq_tool_call_message,
    make_tools,
    text_message,
    tool_use_message,
)


@pytest.fixture(autouse=True)
def _forbid_live_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guard: no test in this module may reach a real provider."""

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to construct a real LLM client — stub was not used")

    monkeypatch.setattr(llm, "default_client", _explode)


# --- Scenarios shared by both arms -------------------------------------------
#
# Each entry is (question, canned-response factory, tool-function kwargs).
# The factory is called once per arm so the two arms consume structurally
# identical — but independent — response queues.

_SEARCH_ARGS = {"query": "segments", "ticker": "MSFT", "fiscal_year": None, "section": None}
_LOOKUP_ARGS = {"ticker": "AAPL", "metric": "net_income", "fiscal_year": 2024}


def _happy_path() -> list[FakeMessage]:
    return [
        tool_use_message("lookup_financial", _LOOKUP_ARGS),
        text_message("AAPL FY2024 net income was $93,736 million."),
    ]


def _with_citations() -> list[FakeMessage]:
    return [
        tool_use_message("search_filings", _SEARCH_ARGS),
        text_message("MSFT reports three segments."),
    ]


def _turn_cap_overrun() -> list[FakeMessage]:
    return [
        *(
            tool_use_message("lookup_financial", _LOOKUP_ARGS, tool_id=f"toolu_{i}")
            for i in range(CUSTOM_MAX_TURNS)
        ),
        text_message("Partial answer based on what was found so far."),
    ]


def _no_evidence() -> list[FakeMessage]:
    return [
        tool_use_message("search_filings", {"query": "revenue in 2030", "ticker": None, "fiscal_year": None, "section": None}),
        tool_use_message("lookup_financial", {"ticker": "MSFT", "metric": "sga", "fiscal_year": 2024}),
        text_message("I believe MSFT's future revenue will be strong."),
    ]


def _immediate_answer() -> list[FakeMessage]:
    return [text_message("MSFT's fiscal year ends in June.")]


SCENARIOS: dict[str, tuple[str, Any, dict[str, Any]]] = {
    "happy_path": ("What was AAPL's FY2024 net income?", _happy_path, {}),
    "with_citations": ("What segments does MSFT report?", _with_citations, {}),
    "turn_cap_overrun": ("Compare every metric across both years.", _turn_cap_overrun, {}),
    "no_evidence": ("What will MSFT's revenue be in 2030?", _no_evidence, {"search": [], "lookup": fixture_miss()}),
    "immediate_answer": ("When does MSFT's fiscal year end?", _immediate_answer, {}),
}


def _trace_signature(response: QueryResponse) -> list[tuple[str, dict[str, Any], str]]:
    """Trace comparison excluding `latency_ms`, which is wall-clock."""
    return [(c.name, c.arguments, c.result_summary) for c in response.trace]


def _request_signature(client: FakeClient) -> list[bool]:
    """Which LLM turns carried tool schemas — the request-shape fingerprint."""
    return ["tools" in call for call in client.calls]


# --- The central claim: behavioral equivalence with agent_custom --------------


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_langgraph_arm_matches_agent_custom_exactly(scenario: str):
    question, responses, tool_kwargs = SCENARIOS[scenario]

    custom_client = FakeClient(responses())
    custom = run_agent_custom(
        question, client=custom_client, tool_functions=make_tools(**tool_kwargs), provider="anthropic"
    )

    graph_client = FakeClient(responses())
    graph = run_agent_langgraph(
        question, client=graph_client, tool_functions=make_tools(**tool_kwargs), provider="anthropic"
    )

    # Both stubs were genuinely exercised — no live call, and no vacuous pass.
    assert custom_client.calls and graph_client.calls

    assert graph.answer == custom.answer
    assert graph.citations == custom.citations
    assert _trace_signature(graph) == _trace_signature(custom)
    assert graph.incomplete == custom.incomplete
    assert graph.refused == custom.refused

    # ...and the two arms issued the same LLM requests, in the same order.
    assert _request_signature(graph_client) == _request_signature(custom_client)

    # The only intended difference.
    assert custom.mode == "agent_custom"
    assert graph.mode == "agent_langgraph"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_langgraph_arm_sends_the_same_prompt_and_tools_as_agent_custom(scenario: str):
    question, responses, tool_kwargs = SCENARIOS[scenario]

    custom_client = FakeClient(responses())
    run_agent_custom(question, client=custom_client, tool_functions=make_tools(**tool_kwargs), provider="anthropic")
    graph_client = FakeClient(responses())
    run_agent_langgraph(question, client=graph_client, tool_functions=make_tools(**tool_kwargs), provider="anthropic")

    assert graph_client.calls[0]["system"] == custom_client.calls[0]["system"] == SYSTEM_PROMPT_TOOLS
    assert graph_client.calls[0]["tools"] == custom_client.calls[0]["tools"]
    assert graph_client.calls[0]["tool_choice"] == custom_client.calls[0]["tool_choice"]


def test_langgraph_arm_and_agent_custom_share_one_turn_cap():
    """The caps cannot drift: this arm binds to `src.agent.MAX_TURNS`."""
    assert arm.MAX_TURNS == CUSTOM_MAX_TURNS == 5


# --- FR4.1 — turn cap and incompleteness -------------------------------------


def test_stops_at_five_turns_and_flags_incomplete():
    client = FakeClient(_turn_cap_overrun())

    response = run_agent_langgraph(
        "Compare every metric across both years.", client=client, tool_functions=make_tools(), provider="anthropic"
    )

    assert len(response.trace) == 5, "agent_langgraph must stop at MAX_TURNS=5 tool calls"
    assert response.incomplete is True
    assert response.refused is False
    assert response.answer == "Partial answer based on what was found so far."
    # 5 tool-enabled turns + 1 forced tools-free synthesis call.
    assert len(client.calls) == 6
    assert "tools" not in client.calls[-1]


def test_completing_before_the_cap_is_not_incomplete():
    client = FakeClient(_happy_path())

    response = run_agent_langgraph(
        "What was AAPL's FY2024 net income?", client=client, tool_functions=make_tools(), provider="anthropic"
    )

    assert len(response.trace) == 1
    assert response.incomplete is False
    assert response.refused is False
    assert len(client.calls) == 2


# --- FR4.5 — explicit refusal, never parametric knowledge --------------------


def test_refuses_when_tools_return_no_evidence():
    client = FakeClient(_no_evidence())

    response = run_agent_langgraph(
        "What will MSFT's revenue be in 2030?",
        client=client,
        tool_functions=make_tools(search=[], lookup=fixture_miss()),
        provider="anthropic",
    )

    assert response.refused is True
    assert response.citations == []
    assert len(response.trace) == 2
    # The model's guess must be overridden, not returned verbatim.
    assert "2030" not in response.answer
    assert response.answer


def test_refuses_when_the_model_never_calls_a_tool():
    client = FakeClient(_immediate_answer())

    response = run_agent_langgraph(
        "When does MSFT's fiscal year end?", client=client, tool_functions=make_tools(), provider="anthropic"
    )

    assert len(client.calls) == 1
    assert response.trace == []
    assert response.refused is True


# --- FR4.2 — trace and citations ---------------------------------------------


def test_search_results_become_citations_and_a_valid_query_response():
    client = FakeClient(_with_citations())

    response = run_agent_langgraph(
        "What segments does MSFT report?", client=client, tool_functions=make_tools(), provider="anthropic"
    )

    assert response.mode == "agent_langgraph"
    assert len(response.citations) == 1
    assert response.citations[0].chunk_id == fixture_chunk(0).chunk_id
    assert len(response.trace) == 1
    assert response.trace[0].name == "search_filings"
    assert response.trace[0].latency_ms >= 0

    # Round-trips cleanly through the frozen Wave 0 contract.
    revalidated = QueryResponse.model_validate(response.model_dump(mode="json"))
    assert revalidated == response


def test_a_failing_tool_is_recorded_as_a_miss_and_does_not_crash_the_graph():
    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("index unavailable")

    client = FakeClient(
        [tool_use_message("search_filings", _SEARCH_ARGS), text_message("Answered anyway.")]
    )

    response = run_agent_langgraph(
        "What segments does MSFT report?",
        client=client,
        tool_functions=make_tools(search=_boom),
        provider="anthropic",
    )

    assert len(response.trace) == 1
    assert response.trace[0].result_summary.startswith("error:")
    assert response.refused is True


# --- One tool call per turn, enforced in code --------------------------------


def test_executes_at_most_one_tool_call_per_turn_even_if_two_are_returned():
    first = FakeToolUseBlock("toolu_1", "lookup_financial", _LOOKUP_ARGS)
    smuggled = FakeToolUseBlock("toolu_2", "calculate", {"expression": "1+1"})
    client = FakeClient(
        [FakeMessage([first, smuggled], stop_reason="tool_use"), text_message("AAPL FY2024 net income was $93,736 million.")]
    )
    calls_seen: list[tuple[str, dict[str, Any]]] = []

    response = run_agent_langgraph(
        "What was AAPL's FY2024 net income?",
        client=client,
        tool_functions=make_tools(calls=calls_seen),
        provider="anthropic",
    )

    assert len(response.trace) == 1
    assert len(calls_seen) == 1
    assert calls_seen[0][0] == "lookup_financial"


# --- Provider parity ----------------------------------------------------------


def test_equivalent_behavior_under_both_provider_wire_formats(monkeypatch: pytest.MonkeyPatch):
    final_answer = "AAPL FY2024 net income was $93,736 million."

    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "MODEL", "claude-opus-5")
    anthropic_result = run_agent_langgraph(
        "What was AAPL's FY2024 net income?",
        client=FakeClient(_happy_path()),
        tool_functions=make_tools(),
    )

    monkeypatch.setattr(llm, "PROVIDER", "groq")
    monkeypatch.setattr(llm, "MODEL", "openai/gpt-oss-120b")
    groq_client = FakeGroqClient(
        [groq_tool_call_message("lookup_financial", _LOOKUP_ARGS), groq_text_message(final_answer)]
    )
    groq_result = run_agent_langgraph(
        "What was AAPL's FY2024 net income?", client=groq_client, tool_functions=make_tools()
    )

    assert groq_client.calls, "the Groq stub was never called"
    assert anthropic_result.answer == groq_result.answer == final_answer
    assert anthropic_result.incomplete == groq_result.incomplete is False
    assert anthropic_result.refused == groq_result.refused is False
    assert _trace_signature(anthropic_result) == _trace_signature(groq_result)


# --- It really is LangGraph ---------------------------------------------------


def test_the_arm_is_actually_orchestrated_by_a_compiled_langgraph_stategraph():
    from langgraph.graph.state import CompiledStateGraph, StateGraph

    assert isinstance(arm.build_graph(), StateGraph)

    compiled = arm.compiled_graph()
    assert isinstance(compiled, CompiledStateGraph)
    assert set(compiled.nodes) >= {"call_model", "execute_tool", "synthesize"}
    # Compiled once and reused — the per-query cost measured by ADR 002 is
    # invocation overhead, not graph construction.
    assert arm.compiled_graph() is compiled


def test_the_frameworks_recursion_limit_can_never_pre_empt_the_fr41_turn_cap():
    """LangGraph's own cap must sit strictly above the longest reachable
    path (START -> (model, tool) x MAX_TURNS -> synthesize), or the
    framework would truncate the run before FR4.1 does — which would be a
    behavioral difference from `agent_custom`, not just an orchestration
    one.
    """
    longest_path_supersteps = 2 * arm.MAX_TURNS + 1
    assert arm.RECURSION_LIMIT > longest_path_supersteps
