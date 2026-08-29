"""Tests for the three eval arms (Lane E — PLAN.md Wave 1).

No live API calls: every test injects a `FakeClient` standing in for
`anthropic.Anthropic` and fixture-backed fake tool functions (built from
tests/fixtures/mini_corpus.json and tests/fixtures/mini_facts.csv) instead
of `src.tools`, which is Lane D's concurrently-written module.

Coverage required by PLAN.md Wave 1 Lane E:
- `baseline_tools` makes AT MOST ONE tool call — the confound control,
  tested hard (including a response that *tries* to request two tools in
  parallel).
- `agent_custom` stops at 5 turns and flags `incomplete`.
- The refusal path fires when tools return no supporting evidence
  (FR4.5).
- Every arm returns a valid `QueryResponse` with a populated trace.
- `src.llm`'s Anthropic and Groq (OpenAI-compatible) wire formats
  normalize to the same internal shape, and the arms produce identical
  behavior under stubs regardless of which provider is selected.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from src import llm
from src.agent import run_agent_custom
from src.baseline import run_baseline_rag, run_baseline_tools
from src.schemas import Chunk, Fact, Miss, QueryResponse
from src.tool_schemas import TOOL_SCHEMAS

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# --- Fixture loading -------------------------------------------------------


def _load_corpus() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "mini_corpus.json").read_text(encoding="utf-8"))


def _load_facts() -> list[dict[str, str]]:
    with open(FIXTURES_DIR / "mini_facts.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fixture_chunk(index: int = 0) -> Chunk:
    """One real `Chunk` built from the frozen mini corpus fixture."""
    return Chunk.model_validate(_load_corpus()[index])


def fixture_fact(ticker: str = "MSFT", fiscal_year: int = 2024, metric: str = "revenue") -> Fact:
    """One real `Fact` built from the frozen mini facts fixture."""
    for row in _load_facts():
        if row["ticker"] == ticker and int(row["fiscal_year"]) == fiscal_year and row["metric"] == metric:
            return Fact(
                ticker=row["ticker"],
                metric=row["metric"],
                fiscal_year=int(row["fiscal_year"]),
                fiscal_period_end=row["period_end"],
                value=float(row["value_usd"]),
                unit="USD",
            )
    raise KeyError((ticker, fiscal_year, metric))


def fixture_miss(ticker: str = "MSFT", fiscal_year: int = 2024, metric: str = "sga") -> Miss:
    return Miss(ticker=ticker, metric=metric, fiscal_year=fiscal_year, reason="not tagged for this filer")


# --- Fake tool functions -----------------------------------------------------


def make_tools(
    *,
    search: Any = None,
    lookup: Any = None,
    calculate: Any = None,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build a `tool_functions` dict to inject into an arm.

    `search` / `lookup` / `calculate` are canned return values (or
    callables taking the same kwargs the real tool would, for tests that
    need per-call behavior). Defaults return realistic fixture-backed
    results. `calls`, if given, records every invocation as
    `(tool_name, kwargs)` for assertions.
    """
    record = calls if calls is not None else []

    def _search_filings(query: str, ticker: str | None = None, fiscal_year: int | None = None, section: str | None = None):
        record.append(("search_filings", {"query": query, "ticker": ticker, "fiscal_year": fiscal_year, "section": section}))
        if callable(search):
            return search(query=query, ticker=ticker, fiscal_year=fiscal_year, section=section)
        if search is not None:
            return search
        return [fixture_chunk(0)]

    def _lookup_financial(ticker: str, metric: str, fiscal_year: int):
        record.append(("lookup_financial", {"ticker": ticker, "metric": metric, "fiscal_year": fiscal_year}))
        if callable(lookup):
            return lookup(ticker=ticker, metric=metric, fiscal_year=fiscal_year)
        if lookup is not None:
            return lookup
        return fixture_fact(ticker, fiscal_year, metric)

    def _calculate(expression: str):
        record.append(("calculate", {"expression": expression}))
        if callable(calculate):
            return calculate(expression=expression)
        if calculate is not None:
            return calculate
        return 1.0

    return {
        "search_filings": _search_filings,
        "lookup_financial": _lookup_financial,
        "calculate": _calculate,
    }


# --- Fake Anthropic client ---------------------------------------------------


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, tool_id: str, name: str, tool_input: dict[str, Any]) -> None:
        self.id = tool_id
        self.name = name
        self.input = tool_input


class FakeMessage:
    def __init__(self, content: list[Any], stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, outer: "FakeClient") -> None:
        self._outer = outer

    def create(self, **kwargs: Any) -> FakeMessage:
        return self._outer._create(**kwargs)


class FakeClient:
    """Stand-in for `anthropic.Anthropic` — no network calls.

    `responses` is consumed in order, one per `messages.create` call.
    Every call's kwargs are recorded in `.calls` so tests can assert on
    request shape (e.g. whether `tools` was passed on a given turn).
    """

    def __init__(self, responses: list[FakeMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = _FakeMessages(self)

    def _create(self, **kwargs: Any) -> FakeMessage:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeClient ran out of canned responses")
        return self._responses.pop(0)


def tool_use_message(name: str, tool_input: dict[str, Any], tool_id: str = "toolu_1") -> FakeMessage:
    return FakeMessage([FakeToolUseBlock(tool_id, name, tool_input)], stop_reason="tool_use")


def text_message(text: str) -> FakeMessage:
    return FakeMessage([FakeTextBlock(text)], stop_reason="end_turn")


# --- Fake Groq (OpenAI-compatible) client -----------------------------------


class FakeGroqFunctionCall:
    def __init__(self, name: str, arguments_json: str) -> None:
        self.name = name
        self.arguments = arguments_json


class FakeGroqToolCall:
    def __init__(self, tool_call_id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = tool_call_id
        self.function = FakeGroqFunctionCall(name, json.dumps(arguments))


class FakeGroqMessage:
    def __init__(self, content: str | None = None, tool_calls: list[FakeGroqToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class FakeGroqChoice:
    def __init__(self, message: FakeGroqMessage) -> None:
        self.message = message


class FakeGroqCompletion:
    def __init__(self, message: FakeGroqMessage) -> None:
        self.choices = [FakeGroqChoice(message)]


class _FakeChatCompletions:
    def __init__(self, outer: "FakeGroqClient") -> None:
        self._outer = outer

    def create(self, **kwargs: Any) -> FakeGroqCompletion:
        return self._outer._create(**kwargs)


class _FakeChat:
    def __init__(self, outer: "FakeGroqClient") -> None:
        self.completions = _FakeChatCompletions(outer)


class FakeGroqClient:
    """Stand-in for `openai.OpenAI` (Groq's OpenAI-compatible endpoint) —
    no network calls. Same `.calls` recording contract as `FakeClient`.
    """

    def __init__(self, responses: list[FakeGroqCompletion]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = _FakeChat(self)

    def _create(self, **kwargs: Any) -> FakeGroqCompletion:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeGroqClient ran out of canned responses")
        return self._responses.pop(0)


def groq_tool_call_message(name: str, arguments: dict[str, Any], tool_call_id: str = "call_1") -> FakeGroqCompletion:
    return FakeGroqCompletion(FakeGroqMessage(content=None, tool_calls=[FakeGroqToolCall(tool_call_id, name, arguments)]))


def groq_text_message(text: str) -> FakeGroqCompletion:
    return FakeGroqCompletion(FakeGroqMessage(content=text))


# --- baseline_rag -------------------------------------------------------


def test_baseline_rag_answers_from_forced_single_retrieval():
    client = FakeClient([text_message("Revenue grew because of Azure.")])
    tools = make_tools()

    response = run_baseline_rag("Why did revenue grow?", client=client, tool_functions=tools, provider="anthropic")

    assert isinstance(response, QueryResponse)
    assert response.mode == "baseline_rag"
    assert response.answer == "Revenue grew because of Azure."
    assert response.refused is False
    assert response.incomplete is False
    assert len(response.trace) == 1
    assert response.trace[0].name == "search_filings"
    assert len(response.citations) == 1
    # No tool schemas are ever exposed to the model in this arm.
    assert len(client.calls) == 1
    assert "tools" not in client.calls[0]


def test_baseline_rag_refuses_when_retrieval_is_empty():
    client = FakeClient([])  # no LLM call should happen at all
    tools = make_tools(search=[])

    response = run_baseline_rag("What will next year's revenue be?", client=client, tool_functions=tools, provider="anthropic")

    assert response.refused is True
    assert response.citations == []
    assert len(response.trace) == 1
    assert client.calls == []


# --- baseline_tools — the confound control ----------------------------------


def test_baseline_tools_makes_at_most_one_tool_call():
    client = FakeClient(
        [
            tool_use_message("lookup_financial", {"ticker": "MSFT", "metric": "revenue", "fiscal_year": 2024}),
            text_message("MSFT FY2024 revenue was $245,122 million."),
        ]
    )
    tools = make_tools()

    response = run_baseline_tools("What was MSFT's FY2024 revenue?", client=client, tool_functions=tools, provider="anthropic")

    assert response.mode == "baseline_tools"
    assert len(response.trace) == 1, "baseline_tools must make AT MOST ONE tool call"
    assert response.trace[0].name == "lookup_financial"
    assert response.incomplete is False
    assert response.refused is False
    assert response.answer == "MSFT FY2024 revenue was $245,122 million."
    # Exactly one tool-enabled call, exactly one tools-free synthesis call.
    assert len(client.calls) == 2
    assert "tools" in client.calls[0]
    assert "tools" not in client.calls[1]


def test_baseline_tools_enforces_the_cap_even_if_a_response_requests_two_tools():
    """The one-call guarantee must hold at the code level, not merely by
    asking the API for `disable_parallel_tool_use` — a (misbehaving)
    response with two tool_use blocks must still result in exactly one
    executed tool call.
    """
    smuggled_second_call = FakeToolUseBlock("toolu_2", "calculate", {"expression": "1+1"})
    first_call = FakeToolUseBlock("toolu_1", "lookup_financial", {"ticker": "AAPL", "metric": "revenue", "fiscal_year": 2024})
    client = FakeClient(
        [
            FakeMessage([first_call, smuggled_second_call], stop_reason="tool_use"),
            text_message("AAPL FY2024 revenue was $391,035 million."),
        ]
    )
    calls_seen: list[tuple[str, dict[str, Any]]] = []
    tools = make_tools(calls=calls_seen)

    response = run_baseline_tools("What was AAPL's FY2024 revenue?", client=client, tool_functions=tools, provider="anthropic")

    assert len(response.trace) == 1
    assert len(calls_seen) == 1
    assert calls_seen[0][0] == "lookup_financial"


def test_baseline_tools_no_tool_call_needed_makes_a_single_llm_call():
    client = FakeClient([text_message("MSFT's fiscal year ends in June.")])
    tools = make_tools()

    response = run_baseline_tools("When does MSFT's fiscal year end?", client=client, tool_functions=tools, provider="anthropic")

    # Zero tool calls means zero evidence gathered under FR4.5's
    # deterministic check — this is also exercised more directly below,
    # but a real single-turn response with no tool_use is a legitimate
    # path through baseline_tools and must not require a synthesis call.
    assert len(client.calls) == 1
    assert response.refused is True
    assert len(response.trace) == 0


# --- agent_custom — turn cap and incompleteness ------------------------------


def test_agent_custom_stops_at_five_turns_and_flags_incomplete():
    tool_turns = [
        tool_use_message("lookup_financial", {"ticker": "MSFT", "metric": "revenue", "fiscal_year": 2023}, tool_id=f"toolu_{i}")
        for i in range(5)
    ]
    client = FakeClient([*tool_turns, text_message("Partial answer based on what was found so far.")])
    tools = make_tools()

    response = run_agent_custom("Compare every metric across both years.", client=client, tool_functions=tools, provider="anthropic")

    assert response.mode == "agent_custom"
    assert len(response.trace) == 5, "agent_custom must stop at MAX_TURNS=5 tool calls"
    assert response.incomplete is True
    assert response.refused is False
    assert response.answer == "Partial answer based on what was found so far."
    # 5 tool-enabled turns + 1 forced tools-free synthesis call.
    assert len(client.calls) == 6
    assert "tools" not in client.calls[-1]


def test_agent_custom_completes_before_the_cap_is_not_incomplete():
    client = FakeClient(
        [
            tool_use_message("lookup_financial", {"ticker": "AAPL", "metric": "net_income", "fiscal_year": 2024}),
            text_message("AAPL FY2024 net income was $93,736 million."),
        ]
    )
    tools = make_tools()

    response = run_agent_custom("What was AAPL's FY2024 net income?", client=client, tool_functions=tools, provider="anthropic")

    assert len(response.trace) == 1
    assert response.incomplete is False
    assert response.refused is False
    assert len(client.calls) == 2


# --- FR4.5 — explicit refusal when tools return no evidence -----------------


def test_agent_custom_refuses_when_tools_return_no_evidence():
    client = FakeClient(
        [
            tool_use_message("search_filings", {"query": "revenue in 2030", "ticker": None, "fiscal_year": None, "section": None}),
            tool_use_message("lookup_financial", {"ticker": "MSFT", "metric": "sga", "fiscal_year": 2024}),
            text_message("I believe MSFT's future revenue will be strong."),
        ]
    )
    tools = make_tools(search=[], lookup=fixture_miss())

    response = run_agent_custom("What will MSFT's revenue be in 2030?", client=client, tool_functions=tools, provider="anthropic")

    assert response.refused is True
    assert response.citations == []
    assert len(response.trace) == 2
    # The model's parametric-knowledge guess must be overridden, not
    # returned verbatim (FR4.5).
    assert "2030" not in response.answer
    assert response.answer


def test_baseline_tools_refuses_when_the_single_tool_call_finds_nothing():
    client = FakeClient(
        [
            tool_use_message("lookup_financial", {"ticker": "MSFT", "metric": "sga", "fiscal_year": 2024}),
            text_message("MSFT does not separately report SG&A, but I estimate it anyway."),
        ]
    )
    tools = make_tools(lookup=fixture_miss())

    response = run_baseline_tools("What was MSFT's FY2024 SG&A?", client=client, tool_functions=tools, provider="anthropic")

    assert response.refused is True
    assert len(response.trace) == 1
    assert response.trace[0].name == "lookup_financial"


# --- Every arm returns a valid, fully-populated QueryResponse ---------------


@pytest.mark.parametrize("mode", ["baseline_rag", "baseline_tools", "agent_custom"])
def test_every_arm_returns_a_valid_query_response_with_populated_trace(mode: str):
    if mode == "baseline_rag":
        client = FakeClient([text_message("Answer grounded in context.")])
        tools = make_tools()
        response = run_baseline_rag("What segments does MSFT report?", client=client, tool_functions=tools, provider="anthropic")
    elif mode == "baseline_tools":
        client = FakeClient(
            [
                tool_use_message("search_filings", {"query": "segments", "ticker": "MSFT", "fiscal_year": None, "section": None}),
                text_message("MSFT reports three segments."),
            ]
        )
        tools = make_tools()
        response = run_baseline_tools("What segments does MSFT report?", client=client, tool_functions=tools, provider="anthropic")
    else:
        client = FakeClient(
            [
                tool_use_message("search_filings", {"query": "segments", "ticker": "MSFT", "fiscal_year": None, "section": None}),
                text_message("MSFT reports three segments."),
            ]
        )
        tools = make_tools()
        response = run_agent_custom("What segments does MSFT report?", client=client, tool_functions=tools, provider="anthropic")

    # Round-trips cleanly through the frozen Wave 0 contract.
    revalidated = QueryResponse.model_validate(response.model_dump(mode="json"))
    assert revalidated == response
    assert response.mode == mode
    assert len(response.trace) >= 1
    assert response.latency_ms >= 0
    assert isinstance(response.answer, str) and response.answer


# --- src.llm — provider adapter normalization --------------------------------


def test_tool_specs_translates_schema_shape_per_provider():
    anthropic_specs = llm.tool_specs(TOOL_SCHEMAS, provider="anthropic")
    groq_specs = llm.tool_specs(TOOL_SCHEMAS, provider="groq")

    # Anthropic shape is TOOL_SCHEMAS verbatim — tool_schemas.py is never
    # edited or copied-with-modifications by this module.
    assert anthropic_specs == TOOL_SCHEMAS

    assert len(groq_specs) == len(TOOL_SCHEMAS)
    for original, translated in zip(TOOL_SCHEMAS, groq_specs, strict=True):
        assert translated["type"] == "function"
        assert translated["function"]["name"] == original["name"]
        assert translated["function"]["description"] == original["description"]
        assert translated["function"]["parameters"] == original["input_schema"]


def test_llm_session_normalizes_text_only_response_identically_across_providers():
    anthropic_client = FakeClient([text_message("hello")])
    groq_client = FakeGroqClient([groq_text_message("hello")])

    anthropic_response = llm.LLMSession(
        anthropic_client, system="sys", question="q", provider="anthropic", model="claude-opus-5"
    ).send(tools=None)
    groq_response = llm.LLMSession(
        groq_client, system="sys", question="q", provider="groq", model="openai/gpt-oss-120b"
    ).send(tools=None)

    assert anthropic_response.text == groq_response.text == "hello"
    assert anthropic_response.has_tool_calls is False
    assert groq_response.has_tool_calls is False


def test_llm_session_normalizes_tool_call_identically_across_providers():
    """The abstraction's actual claim: two structurally different wire
    responses (Anthropic content blocks vs. OpenAI message.tool_calls with
    a JSON-string `arguments` field) must normalize to the same
    NormalizedToolCall shape.
    """
    arguments = {"ticker": "MSFT", "metric": "revenue", "fiscal_year": 2024}

    anthropic_client = FakeClient([tool_use_message("lookup_financial", arguments, tool_id="toolu_abc")])
    groq_client = FakeGroqClient([groq_tool_call_message("lookup_financial", arguments, tool_call_id="call_abc")])

    anthropic_specs = llm.tool_specs(TOOL_SCHEMAS, provider="anthropic")
    groq_specs = llm.tool_specs(TOOL_SCHEMAS, provider="groq")

    anthropic_response = llm.LLMSession(
        anthropic_client, system="sys", question="q", provider="anthropic", model="claude-opus-5"
    ).send(tools=anthropic_specs, disable_parallel_tool_use=True)
    groq_response = llm.LLMSession(
        groq_client, system="sys", question="q", provider="groq", model="openai/gpt-oss-120b"
    ).send(tools=groq_specs, disable_parallel_tool_use=True)

    assert anthropic_response.has_tool_calls and groq_response.has_tool_calls
    assert len(anthropic_response.tool_calls) == len(groq_response.tool_calls) == 1

    a_call, g_call = anthropic_response.tool_calls[0], groq_response.tool_calls[0]
    assert a_call.name == g_call.name == "lookup_financial"
    assert a_call.arguments == g_call.arguments == arguments
    assert isinstance(a_call.id, str) and a_call.id
    assert isinstance(g_call.id, str) and g_call.id

    # Request-level one-call hints differ by wire format but both fire.
    assert anthropic_client.calls[0]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert groq_client.calls[0]["parallel_tool_calls"] is False


def test_llm_session_add_tool_result_uses_provider_native_shape():
    anthropic_session = llm.LLMSession(
        FakeClient([]), system="sys", question="q", provider="anthropic", model="claude-opus-5"
    )
    anthropic_session.add_tool_result("toolu_1", '{"result": 42}')
    last = anthropic_session._messages[-1]
    assert last["role"] == "user"
    assert last["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"result": 42}'}

    groq_session = llm.LLMSession(FakeGroqClient([]), system="sys", question="q", provider="groq", model="openai/gpt-oss-120b")
    groq_session.add_tool_result("call_1", '{"result": 42}')
    last = groq_session._messages[-1]
    assert last == {"role": "tool", "tool_call_id": "call_1", "content": '{"result": 42}'}


def test_agent_custom_produces_equivalent_behavior_under_both_providers(monkeypatch: pytest.MonkeyPatch):
    """Same question, same fixture-backed tool result, two different wire
    formats — the arm's observable behavior (trace shape, refusal,
    incompleteness, final answer) must be identical either way.
    """
    arguments = {"ticker": "AAPL", "metric": "net_income", "fiscal_year": 2024}
    final_answer = "AAPL FY2024 net income was $93,736 million."

    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "MODEL", "claude-opus-5")
    anthropic_client = FakeClient([tool_use_message("lookup_financial", arguments), text_message(final_answer)])
    anthropic_result = run_agent_custom(
        "What was AAPL's FY2024 net income?", client=anthropic_client, tool_functions=make_tools()
    )

    monkeypatch.setattr(llm, "PROVIDER", "groq")
    monkeypatch.setattr(llm, "MODEL", "openai/gpt-oss-120b")
    groq_client = FakeGroqClient([groq_tool_call_message("lookup_financial", arguments), groq_text_message(final_answer)])
    groq_result = run_agent_custom(
        "What was AAPL's FY2024 net income?", client=groq_client, tool_functions=make_tools()
    )

    assert anthropic_result.answer == groq_result.answer == final_answer
    assert anthropic_result.incomplete == groq_result.incomplete is False
    assert anthropic_result.refused == groq_result.refused is False
    assert len(anthropic_result.trace) == len(groq_result.trace) == 1
    assert anthropic_result.trace[0].name == groq_result.trace[0].name == "lookup_financial"
    assert anthropic_result.trace[0].arguments == groq_result.trace[0].arguments == arguments
