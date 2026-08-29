"""Unit + end-to-end tests for eval/run_eval.py (Agent M — eval-harness lane).

Why this file exists
--------------------
`eval/run_eval.py` is the code that produces the four-arm results table the
whole project rests on, and it had **zero** tests before this lane. A bug
here does not crash — it emits a plausible-looking number that nobody can
catch by reading the table.

The five places a bug would silently corrupt a published result, all pinned
below:

1. **`recall@5` denominator** (PLAN.md Contract decision #3). Items with
   `expected_sources: []` mean "not retrievable from the indexed corpus,"
   and must be excluded from the denominator, not counted as misses.
   `eval/metrics.py` implements the exclusion and `tests/test_metrics.py`
   asserts it in isolation; what is asserted here is that `run_eval()`
   **honors it end to end against the real 25-item golden set** — the
   composed path, not just the unit.
2. **The numeric tier is asserted deterministically, never judged.**
   `expected_numeric` +/- `numeric_tolerance`, with `tolerance == 0`
   meaning exact (not "any"). The judge must never be consulted for a
   numeric, multi_hop, or unanswerable item — asserted by call recording,
   not by inspection.
3. **Multi-hop `expected_numeric` is a DELTA**, never the later-year
   endpoint. An arm that reports the endpoint must score wrong.
4. **Unanswerable items** (q023-q025) are scored purely on
   `QueryResponse.refused`, and `refusal_accuracy` is computed over exactly
   those three items.
5. **Judge outcomes flow through untouched** — a missing replay fixture
   must not be laundered into a scored result.

Hermeticity and no live calls
-----------------------------
Nothing in this file constructs an arm, an LLM client, a Chroma store, or a
facts DB. `_arm_dispatch` and `judge_faithfulness` are replaced at the
`eval.run_eval` module boundary, and `socket.socket.connect` is patched to
raise for the whole module so an inadequately scoped stub fails loudly
instead of spending real quota (`.env` auto-loads a working key at
`import eval` time). `test_the_no_live_call_guard_is_actually_in_force`
verifies the guard rather than assuming it. `main()`'s output directory is
redirected to `tmp_path`, so no test writes into the repo.

`data/golden.jsonl` is read (never written) by the end-to-end tests on
purpose: the exclusion rule is only meaningful against the real data, and
reproducing golden content in a local fixture would be exactly the
divergence PLAN.md's Wave 0 step 3 exists to prevent.

Bugs found and NOT fixed (reported per this lane's contract)
------------------------------------------------------------
Two `xfail(strict=True)` tests below document real defects in code this
lane does not own. They are marked strict so that fixing the defect turns
the suite red and forces the marker to be removed rather than the bug being
quietly re-introduced:

- `test_multi_hop_extraction_takes_the_delta_from_the_golden_phrasing`
- `test_faithfulness_denominator_agrees_with_the_tier_breakdown`
- `test_markdown_faithfulness_percentage_matches_its_own_counts`
"""

from __future__ import annotations

import json
import socket
import sys
from math import isnan
from typing import Any, Callable

import pytest

from eval import run_eval as harness
from eval.judge import JudgeFixtureMissingError
from src.schemas import Citation, GoldenItem, QueryResponse, ToolCall

# --- no-live-call guard -------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError(
            "tests/test_run_eval.py attempted a real network connection — "
            "a stub is mis-scoped and would have spent live quota"
        )

    monkeypatch.setattr(socket.socket, "connect", _boom, raising=False)
    monkeypatch.setattr(socket, "create_connection", _boom, raising=False)


def test_the_no_live_call_guard_is_actually_in_force():
    with pytest.raises(AssertionError, match="real network connection"):
        socket.create_connection(("example.invalid", 443))


# --- builders ------------------------------------------------------------------

MSFT_2024_ITEM7 = {"ticker": "MSFT", "fiscal_year": 2024, "section": "item7"}
AAPL_2024_ITEM1 = {"ticker": "AAPL", "fiscal_year": 2024, "section": "item1"}


def _item(
    question_id: str,
    tier: str,
    *,
    expected_numeric: float | None = None,
    tolerance: float | None = None,
    sources: list[dict] | None = None,
    kind: str | None = None,
    question: str | None = None,
) -> GoldenItem:
    return GoldenItem.model_validate(
        {
            "question_id": question_id,
            "tier": tier,
            "answerable": tier != "unanswerable",
            "question": question or f"question for {question_id}",
            "expected_answer": "x",
            "expected_numeric": expected_numeric,
            "numeric_tolerance": tolerance,
            "expected_sources": sources or [],
            "expected_tools": ["search_filings"],
            **({"kind": kind} if tier == "unanswerable" else {}),
        }
    )


def _citation(source: dict) -> Citation:
    return Citation(
        chunk_id=f"{source['ticker']}_{source['fiscal_year']}_{source['section']}_001",
        ticker=source["ticker"],
        fiscal_year=source["fiscal_year"],
        section=source["section"],
        source_url="https://www.sec.gov/example",
    )


def _tool_call(name: str = "search_filings", summary: str = "3 chunks") -> ToolCall:
    return ToolCall(name=name, arguments={}, result_summary=summary, latency_ms=1.0)


def _response(
    answer: str = "an answer",
    *,
    citations: list[dict] | None = None,
    trace: list[ToolCall] | None = None,
    refused: bool = False,
    incomplete: bool = False,
    latency_ms: float = 5.0,
    mode: str = "agent_custom",
) -> QueryResponse:
    return QueryResponse(
        answer=answer,
        citations=[_citation(c) for c in (citations or [])],
        trace=trace if trace is not None else [_tool_call()],
        latency_ms=latency_ms,
        mode=mode,
        incomplete=incomplete,
        refused=refused,
    )


# --- 1. golden loading against the real, human-authored data -------------------


def test_load_golden_reads_all_twenty_five_items_through_the_frozen_contract():
    golden = harness._load_golden()
    assert len(golden) == 25
    assert all(isinstance(item, GoldenItem) for item in golden)
    assert len({item.question_id for item in golden}) == 25


def test_tier_counts_match_the_fr8_3_distribution():
    counts = harness._tier_counts(harness._load_golden())
    assert counts == {"single_hop": 10, "numeric": 8, "multi_hop": 4, "unanswerable": 3}
    assert set(counts) == set(harness.TIERS)


def test_tier_counts_reports_zero_for_absent_tiers():
    counts = harness._tier_counts([_item("q001", "numeric", expected_numeric=1.0, tolerance=0.0)])
    assert counts == {"single_hop": 0, "numeric": 1, "multi_hop": 0, "unanswerable": 0}


def test_load_golden_skips_blank_lines(tmp_path):
    item = _item("q001", "single_hop", sources=[MSFT_2024_ITEM7])
    path = tmp_path / "golden.jsonl"
    path.write_text("\n" + item.model_dump_json() + "\n\n", encoding="utf-8")
    assert [i.question_id for i in harness._load_golden(path)] == ["q001"]


# --- 2. numeric extraction -----------------------------------------------------


def test_find_dollar_figures_handles_both_golden_set_number_shapes():
    assert harness._find_dollar_figures("$211,915,000,000") == [211915000000.0]
    assert harness._find_dollar_figures("$33.2 billion") == pytest.approx([33200000000.0])
    assert harness._find_dollar_figures("$1.5 million") == [1500000.0]
    assert harness._find_dollar_figures("$4 thousand") == [4000.0]


def test_find_dollar_figures_returns_largest_first():
    figures = harness._find_dollar_figures("$1,000,000 and $5,000,000 and $3,000,000")
    assert figures == [5000000.0, 3000000.0, 1000000.0]


def test_find_dollar_figures_drops_small_bare_numbers_like_percentages():
    """"or 16%" and "24%" appear in the real multi_hop golden answers; they
    must never be mistaken for filing figures.
    """
    assert harness._find_dollar_figures("revenue grew 16% to $245,122,000,000") == [245122000000.0]
    assert harness._find_dollar_figures("margin was 73.9%") == []


def test_find_dollar_figures_returns_empty_for_prose_without_numbers():
    assert harness._find_dollar_figures("Apple describes supply chain concentration risk.") == []


def test_numeric_tier_takes_the_filing_figure_not_an_incidental_number():
    answer = "In fiscal 2024, total net sales were $391,035,000,000."
    assert harness._extract_numeric_value(answer, "numeric") == 391035000000.0


def test_extract_numeric_value_returns_none_when_no_figure_is_present():
    assert harness._extract_numeric_value("I cannot answer that question.", "numeric") is None
    assert harness._extract_numeric_value("I cannot answer that question.", "multi_hop") is None


def test_multi_hop_falls_back_to_a_computed_difference_never_a_bare_endpoint():
    """PLAN.md Contract decision #2. Two endpoints stated with no change
    language must yield the computed difference, not the later year.
    """
    answer = "FY2023 was $211,915,000,000. FY2024 was $245,122,000,000."
    assert harness._extract_numeric_value(answer, "multi_hop") == pytest.approx(33207000000.0)


def test_multi_hop_uses_a_stated_delta_when_it_is_the_only_figure_nearby():
    answer = "R&D expense increased by $1,455,000,000 year over year."
    assert harness._extract_numeric_value(answer, "multi_hop") == 1455000000.0


def test_multi_hop_with_a_single_figure_returns_that_figure():
    assert harness._extract_numeric_value("$294,000,000", "multi_hop") == 294000000.0


def test_multi_hop_extraction_matches_the_q004_golden_phrasing():
    """q004 is the one multi_hop golden answer whose phrasing the current
    extractor handles correctly ("...was higher by $294,000,000"), which is
    what makes the failure on q001/q002/q003 below a real defect rather
    than a mismatched expectation.
    """
    golden = {i.question_id: i for i in harness._load_golden()}
    q004 = golden["q004"]
    extracted = harness._extract_numeric_value(q004.expected_answer, "multi_hop")
    assert extracted == pytest.approx(q004.expected_numeric)


def test_multi_hop_extraction_takes_the_delta_from_the_golden_phrasing():
    """REGRESSION. Was xfail(strict=True) when this lane reported the bug.

    `_extract_numeric_value`'s delta-keyword branch used
    `_find_dollar_figures(window)[0]` -- the LARGEST figure in the 60-char
    window after the keyword, not the NEAREST one its docstring promised.
    In the golden set's own phrasing ("increased $33.2 billion, or 16%,
    from $211,915,000,000 in FY2023 to ...") the window holds the delta and
    both endpoints, and the endpoint is larger -- so q001/q002/q003 all
    extracted an endpoint. That is exactly the failure PLAN.md Contract
    decision #2 exists to prevent: it would have reported near-0% multi_hop
    accuracy for every arm and flattened the project's headline
    agent_custom vs baseline_tools claim to a tie, for reasons having
    nothing to do with either arm.

    Fixed by splitting `_find_dollar_figures_in_order` (positional) from
    `_find_dollar_figures` (largest-first) and using the former here.
    """
    golden = {i.question_id: i for i in harness._load_golden()}
    for question_id in ("q001", "q002", "q003"):
        item = golden[question_id]
        extracted = harness._extract_numeric_value(item.expected_answer, "multi_hop")
        allowed = (item.numeric_tolerance or 0.0) * abs(item.expected_numeric)
        assert extracted is not None
        assert abs(extracted - item.expected_numeric) <= allowed, (
            f"{question_id}: extracted {extracted!r} but expected the delta {item.expected_numeric!r}"
        )


def test_multi_hop_delta_keyword_window_prefers_the_nearest_figure_not_the_largest():
    """Pins the *mechanism* of the fix, so a future refactor that reaches
    for the convenient sorted helper fails here with the reason attached.

    The window after "increased" holds both 33.2B (the delta) and 211.915B
    (an endpoint). Nearest-by-position is the delta; largest is the
    endpoint. This tier must take the delta.
    """
    answer = "Total revenue increased $33.2 billion, from $211,915,000,000 in FY2023."
    assert harness._extract_numeric_value(answer, "multi_hop") == pytest.approx(33_200_000_000)

    # The sorted helper still exists and is still largest-first; the point is
    # that the multi-hop branch must not be the caller that uses it.
    assert harness._find_dollar_figures(answer)[0] == 211915000000.0
    assert harness._find_dollar_figures_in_order(answer)[0] == pytest.approx(33_200_000_000)


# --- 3. per-item pass/fail: tolerance, delta, refusal --------------------------


def test_numeric_tolerance_zero_means_exact_not_any():
    item = _item("q005", "numeric", expected_numeric=211915000000, tolerance=0)
    assert harness._item_passed(item, {"numeric_value": 211915000000}) is True
    assert harness._item_passed(item, {"numeric_value": 211915000001}) is False
    assert harness._item_passed(item, {"numeric_value": 211914999999}) is False


def test_multi_hop_tolerance_is_a_fraction_of_the_expected_delta_at_both_boundaries():
    expected = 33207000000
    item = _item("q001", "multi_hop", expected_numeric=expected, tolerance=0.01)
    allowed = 0.01 * expected  # 332,070,000

    assert harness._item_passed(item, {"numeric_value": expected + allowed}) is True
    assert harness._item_passed(item, {"numeric_value": expected - allowed}) is True
    assert harness._item_passed(item, {"numeric_value": expected + allowed + 1}) is False
    assert harness._item_passed(item, {"numeric_value": expected - allowed - 1}) is False


def test_multi_hop_endpoint_is_scored_wrong_even_though_it_is_a_real_filing_figure():
    item = _item("q001", "multi_hop", expected_numeric=33207000000, tolerance=0.01)
    assert harness._item_passed(item, {"numeric_value": 245122000000}) is False


def test_missing_numeric_answer_is_a_failure_not_a_skip():
    item = _item("q005", "numeric", expected_numeric=100.0, tolerance=0)
    assert harness._item_passed(item, {"numeric_value": None}) is False
    assert harness._item_passed(item, {}) is False
    assert harness._item_passed(item, None) is False


def test_numeric_item_without_an_expected_value_cannot_pass():
    item = _item("q005", "numeric")
    assert harness._item_passed(item, {"numeric_value": 1.0}) is False


@pytest.mark.parametrize("kind", ["future", "out_of_corpus", "never_tagged"])
def test_unanswerable_items_pass_iff_the_arm_refused(kind):
    item = _item("q023", "unanswerable", kind=kind)
    assert harness._item_passed(item, {"refused": True}) is True
    assert harness._item_passed(item, {"refused": False}) is False
    assert harness._item_passed(item, {"refused": None}) is False
    assert harness._item_passed(item, {}) is False


def test_single_hop_passes_only_on_an_affirmative_judge_verdict():
    item = _item("q013", "single_hop", sources=[MSFT_2024_ITEM7])
    assert harness._item_passed(item, {"faithful": True}) is True
    assert harness._item_passed(item, {"faithful": False}) is False
    # None, not False: an unjudged item is unscorable and must be EXCLUDED
    # from the tier denominator and the McNemar pairing, not counted as a
    # failure. Scoring it False publishes "single_hop 0/10" whenever the
    # replay fixtures are missing, which reads as a faithfulness collapse.
    assert harness._item_passed(item, {"faithful": None}) is None


# --- 4. response -> result dict ------------------------------------------------


def test_citation_dicts_carry_exactly_the_recall_join_key():
    response = _response(citations=[MSFT_2024_ITEM7, AAPL_2024_ITEM1])
    assert harness._citation_dicts(response) == [MSFT_2024_ITEM7, AAPL_2024_ITEM1]


def test_judge_context_uses_only_search_filings_summaries():
    response = _response(
        trace=[
            _tool_call("search_filings", "chunk A"),
            _tool_call("lookup_financial", "MSFT revenue 2024"),
            _tool_call("calculate", "245122 - 211915"),
            _tool_call("search_filings", "chunk B"),
        ]
    )
    assert harness._judge_context(response) == ["chunk A", "chunk B"]


def test_judge_context_is_empty_when_nothing_was_retrieved():
    assert harness._judge_context(_response(trace=[_tool_call("calculate", "1+1")])) == []


def test_to_result_dict_extracts_a_number_only_for_numeric_tiers():
    numeric_item = _item("q005", "numeric", expected_numeric=1.0, tolerance=0)
    prose_item = _item("q013", "single_hop", sources=[MSFT_2024_ITEM7])
    response = _response("The figure was $211,915,000,000.", citations=[MSFT_2024_ITEM7])

    assert harness._to_result_dict(numeric_item, response)["numeric_value"] == 211915000000.0
    assert harness._to_result_dict(prose_item, response)["numeric_value"] is None


def test_to_result_dict_records_the_full_scoring_surface():
    item = _item("q013", "single_hop", sources=[MSFT_2024_ITEM7])
    response = _response(
        "text", citations=[MSFT_2024_ITEM7], trace=[_tool_call(), _tool_call("calculate", "1")], incomplete=True
    )

    result = harness._to_result_dict(item, response)

    assert result["question_id"] == "q013"
    assert result["tool_calls"] == 2
    assert result["latency_ms"] == 5.0
    assert result["refused"] is False
    assert result["incomplete"] is True
    assert result["faithful"] is None  # filled in later, only for single_hop
    assert result["answer"] == "text"


# --- 5. aggregates -------------------------------------------------------------


def test_percentile_on_a_known_series():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert harness._percentile(values, 50) == 30.0
    assert harness._percentile(values, 95) == 50.0
    assert harness._percentile(values, 0) == 10.0


def test_percentile_of_an_empty_series_is_nan():
    assert isnan(harness._percentile([], 50))


def test_percentile_of_a_single_value():
    assert harness._percentile([7.0], 95) == 7.0


def test_tier_breakdown_counts_and_bounds_each_tier_independently():
    golden = [
        _item("q001", "multi_hop", expected_numeric=100, tolerance=0.01),
        _item("q005", "numeric", expected_numeric=100, tolerance=0),
        _item("q013", "single_hop", sources=[MSFT_2024_ITEM7]),
        _item("q023", "unanswerable", kind="future"),
    ]
    results = [
        {"question_id": "q001", "numeric_value": 100},
        {"question_id": "q005", "numeric_value": 999},
        {"question_id": "q013", "faithful": True},
        {"question_id": "q023", "refused": True},
    ]

    breakdown = harness._tier_breakdown(results, golden)

    assert breakdown["multi_hop"] == {
        "n": 1,
        "passed": 1,
        "excluded": 0,
        "rate": 1.0,
        "ci_95": pytest.approx(breakdown["multi_hop"]["ci_95"]),
    }
    assert breakdown["numeric"]["passed"] == 0
    assert breakdown["numeric"]["rate"] == 0.0
    assert breakdown["single_hop"]["passed"] == 1
    assert breakdown["unanswerable"]["passed"] == 1
    for tier in harness.TIERS:
        lower, upper = breakdown[tier]["ci_95"]
        assert 0.0 <= lower <= upper <= 1.0


def test_tier_breakdown_reports_nan_rate_for_an_absent_tier():
    golden = [_item("q005", "numeric", expected_numeric=1, tolerance=0)]
    breakdown = harness._tier_breakdown([{"question_id": "q005", "numeric_value": 1}], golden)
    assert breakdown["multi_hop"]["n"] == 0
    assert isnan(breakdown["multi_hop"]["rate"])


def test_score_arm_faithfulness_is_scoped_to_single_hop_and_excludes_unjudged_items():
    golden = [
        _item("q013", "single_hop", sources=[MSFT_2024_ITEM7]),
        _item("q014", "single_hop", sources=[MSFT_2024_ITEM7]),
        _item("q005", "numeric", expected_numeric=1, tolerance=0),
    ]
    results = [
        {"question_id": "q013", "faithful": True, "latency_ms": 1.0, "tool_calls": 1, "numeric_value": None, "refused": False, "citations": [MSFT_2024_ITEM7]},
        {"question_id": "q014", "faithful": None, "latency_ms": 1.0, "tool_calls": 1, "numeric_value": None, "refused": False, "citations": []},
        # A numeric item carrying a stray `faithful` must not enter the metric.
        {"question_id": "q005", "faithful": True, "latency_ms": 1.0, "tool_calls": 3, "numeric_value": 1, "refused": False, "citations": []},
    ]

    metrics = harness._score_arm(results, golden)

    assert metrics["faithfulness"]["scored"] == 1
    assert metrics["faithfulness"]["correct"] == 1
    # `denominator` is the JUDGED count, not the tier size, so that the rate
    # and the counts printed beside it in the markdown table divide by the
    # same number. `excluded` keeps the ungraded item visible.
    assert metrics["faithfulness"]["denominator"] == 1
    assert metrics["faithfulness"]["excluded"] == 1
    assert metrics["avg_tool_calls"] == pytest.approx(5 / 3)


def test_score_arm_faithfulness_rate_is_nan_when_nothing_was_judged():
    golden = [_item("q013", "single_hop", sources=[MSFT_2024_ITEM7])]
    results = [{"question_id": "q013", "faithful": None, "latency_ms": 1.0, "tool_calls": 1, "numeric_value": None, "refused": False, "citations": []}]
    metrics = harness._score_arm(results, golden)
    assert isnan(metrics["faithfulness"]["rate"])


def test_pairwise_comparisons_are_paired_per_tier_and_overall():
    golden = [
        _item("q001", "multi_hop", expected_numeric=1, tolerance=0.01),
        _item("q002", "multi_hop", expected_numeric=1, tolerance=0.01),
        _item("q013", "single_hop", sources=[MSFT_2024_ITEM7]),
    ]
    per_arm_pass = {
        "a": {"q001": True, "q002": True, "q013": False},
        "b": {"q001": False, "q002": False, "q013": False},
    }

    comparisons = harness._pairwise_comparisons(["a", "b"], per_arm_pass, golden)

    entry = comparisons["a_vs_b"]
    assert entry["overall"]["b"] == 2 and entry["overall"]["c"] == 0
    assert entry["multi_hop"]["n"] == 2
    assert entry["single_hop"]["discordant"] == 0
    assert entry["numeric"] is None  # tier absent from this golden slice
    assert entry["unanswerable"] is None


# --- 6. arm dispatch -----------------------------------------------------------


def test_arm_dispatch_wires_exactly_the_four_frozen_modes():
    from src.agent import run_agent_custom
    from src.baseline import run_baseline_rag, run_baseline_tools

    dispatch = harness._arm_dispatch()

    assert set(dispatch) == {"baseline_rag", "baseline_tools", "agent_custom", "agent_langgraph"}
    assert dispatch["baseline_rag"] is run_baseline_rag
    assert dispatch["baseline_tools"] is run_baseline_tools
    assert dispatch["agent_custom"] is run_agent_custom
    assert callable(dispatch["agent_langgraph"])


# --- 7. run_eval end to end, against the real golden set -----------------------


class JudgeSpy:
    """Records every judge invocation so "the numeric tier is never judged"
    can be asserted rather than assumed.
    """

    def __init__(self, verdict: dict | None = None, raises: Exception | None = None) -> None:
        self.verdict = verdict if verdict is not None else {"faithful": True}
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, question: str, answer: str, context: list[str], *, live: bool = False) -> dict:
        self.calls.append({"question": question, "answer": answer, "context": context, "live": live})
        if self.raises is not None:
            raise self.raises
        return self.verdict


def _perfect_arm(golden: list[GoldenItem]) -> Callable[[str], QueryResponse]:
    """An arm that answers every golden item correctly: cites exactly the
    expected sources, states `expected_numeric` verbatim, and refuses the
    unanswerable tier.
    """
    by_question = {item.question: item for item in golden}

    def _handler(question: str) -> QueryResponse:
        item = by_question[question]
        if item.tier == "unanswerable":
            return _response("I cannot answer that from this corpus.", citations=[], refused=True)
        answer = (
            f"${item.expected_numeric:,.0f}"
            if item.expected_numeric is not None
            else f"Prose answer for {item.question_id}."
        )
        return _response(
            answer,
            citations=[s.model_dump() for s in item.expected_sources],
            trace=[_tool_call("search_filings", f"context for {item.question_id}")],
        )

    return _handler


@pytest.fixture
def golden() -> list[GoldenItem]:
    return harness._load_golden()


@pytest.fixture
def stub_arm(monkeypatch: pytest.MonkeyPatch, golden: list[GoldenItem]):
    """Install a single fake arm in place of the real dispatch. Keeps every
    arm implementation, LLM client, Chroma store, and facts DB out of the
    loop.
    """
    handler = _perfect_arm(golden)
    seen: list[str] = []

    def _recording(question: str) -> QueryResponse:
        seen.append(question)
        return handler(question)

    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"stub_arm": _recording})
    return seen


def test_run_eval_excludes_unretrievable_items_from_the_recall_denominator(monkeypatch, stub_arm, golden):
    """PLAN.md Contract decision #3, end to end. The unit-level assertion
    lives in tests/test_metrics.py; this proves `run_eval()` actually
    composes it, which is where the corruption would show up in a published
    table.
    """
    judge = JudgeSpy()
    monkeypatch.setattr(harness, "judge_faithfulness", judge)

    payload = harness.run_eval(mode="stub_arm")
    recall = payload["metrics"]["stub_arm"]["recall_at_5"]

    assert recall["denominator"] == 19, "expected_sources==[] items leaked into the denominator"
    assert recall["excluded"] == 6
    assert recall["correct"] == 19
    assert recall["rate"] == 1.0, (
        "a perfect arm scored below 1.0 — the six unretrievable items were counted as misses, "
        "which reports a retrieval bug that does not exist (PLAN.md Contract decision #3)"
    )


def test_the_excluded_items_are_exactly_the_ones_plan_md_names(golden):
    """PLAN.md names q009/q010/q011 (Item 8 figures, unindexed) plus the
    three unanswerable items. Computed dynamically from the data, never a
    hardcoded list, so a regenerated golden set changes this assertion
    rather than silently invalidating the metric.
    """
    excluded = {item.question_id for item in golden if not item.expected_sources}
    assert excluded == {"q009", "q010", "q011", "q023", "q024", "q025"}


def test_run_eval_never_judges_a_numeric_multi_hop_or_unanswerable_item(monkeypatch, stub_arm, golden):
    """FR8.4: only `faithfulness` uses the judge. If a numeric item ever
    reached the judge, the deterministic XBRL assertion the project's
    credibility rests on would have an LLM in the loop.
    """
    judge = JudgeSpy()
    monkeypatch.setattr(harness, "judge_faithfulness", judge)

    harness.run_eval(mode="stub_arm")

    judged = {call["question"] for call in judge.calls}
    single_hop = {item.question for item in golden if item.tier == "single_hop"}
    assert judged == single_hop
    assert len(judge.calls) == 10, "the judge was called a number of times other than once per single_hop item"


def test_run_eval_passes_the_retrieved_context_and_replay_flag_to_the_judge(monkeypatch, stub_arm, golden):
    judge = JudgeSpy()
    monkeypatch.setattr(harness, "judge_faithfulness", judge)

    harness.run_eval(mode="stub_arm", live=False)

    assert all(call["live"] is False for call in judge.calls)
    assert all(call["context"] and call["context"][0].startswith("context for") for call in judge.calls)


def test_run_eval_scores_a_perfect_arm_at_one_hundred_percent_on_every_tier(monkeypatch, stub_arm, golden):
    monkeypatch.setattr(harness, "judge_faithfulness", JudgeSpy())

    payload = harness.run_eval(mode="stub_arm")
    metrics = payload["metrics"]["stub_arm"]

    assert payload["n_items"] == 25
    assert payload["tier_counts"] == {"single_hop": 10, "numeric": 8, "multi_hop": 4, "unanswerable": 3}
    assert metrics["numeric_accuracy"] == {"correct": 12, "scored": 12, "denominator": 12, "rate": 1.0}
    assert metrics["refusal_accuracy"] == {"correct": 3, "denominator": 3, "rate": 1.0}
    assert metrics["faithfulness"]["correct"] == 10
    for tier, breakdown in metrics["tier_breakdown"].items():
        assert breakdown["passed"] == breakdown["n"], f"{tier} did not score perfectly"


def test_refusal_accuracy_is_computed_over_exactly_the_unanswerable_items(monkeypatch, golden):
    """A refusal on an answerable question must not be rewarded, and an
    answer on an unanswerable one must not be excused.
    """
    unanswerable = {item.question for item in golden if item.tier == "unanswerable"}
    refuse_everything = sorted(unanswerable)[0]

    def _handler(question: str) -> QueryResponse:
        # Refuses one unanswerable item and, wrongly, every other question.
        refused = question not in unanswerable or question == refuse_everything
        return _response("...", citations=[], refused=refused)

    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"stub_arm": _handler})
    monkeypatch.setattr(harness, "judge_faithfulness", JudgeSpy({"faithful": False}))

    metrics = harness.run_eval(mode="stub_arm")["metrics"]["stub_arm"]

    assert metrics["refusal_accuracy"]["denominator"] == 3
    assert metrics["refusal_accuracy"]["correct"] == 1
    assert metrics["refusal_accuracy"]["rate"] == pytest.approx(1 / 3)


def test_run_eval_scores_a_multi_hop_endpoint_answer_as_wrong(monkeypatch, golden):
    """The corruption scenario PLAN.md Contract decision #2 names: an arm
    that reports the later-year endpoint instead of the change must lose the
    multi_hop tier, not win it.
    """
    by_question = {item.question: item for item in golden}

    def _handler(question: str) -> QueryResponse:
        item = by_question[question]
        if item.tier == "multi_hop":
            # A real filing figure, but an endpoint, not a delta.
            return _response("Revenue was $245,122,000,000 in FY2024.", citations=[])
        return _perfect_arm(golden)(question)

    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"stub_arm": _handler})
    monkeypatch.setattr(harness, "judge_faithfulness", JudgeSpy())

    metrics = harness.run_eval(mode="stub_arm")["metrics"]["stub_arm"]

    assert metrics["tier_breakdown"]["multi_hop"]["passed"] == 0
    assert metrics["numeric_accuracy"]["correct"] == 8  # the numeric tier still passes


def test_run_eval_backfills_latency_when_an_arm_reports_zero(monkeypatch, golden):
    monkeypatch.setattr(
        harness, "_arm_dispatch", lambda: {"stub_arm": lambda q: _response("x", latency_ms=0.0)}
    )
    monkeypatch.setattr(harness, "judge_faithfulness", JudgeSpy())

    metrics = harness.run_eval(mode="stub_arm")["metrics"]["stub_arm"]

    assert metrics["p50_latency_ms"] > 0
    assert metrics["p95_latency_ms"] >= metrics["p50_latency_ms"]


def test_run_eval_rejects_an_unknown_mode(monkeypatch, stub_arm):
    with pytest.raises(ValueError, match="Unknown mode"):
        harness.run_eval(mode="not_an_arm")


def test_run_eval_emits_no_comparisons_for_a_single_arm(monkeypatch, stub_arm):
    monkeypatch.setattr(harness, "judge_faithfulness", JudgeSpy())
    payload = harness.run_eval(mode="stub_arm")
    assert payload["arms"] == ["stub_arm"]
    assert payload["comparisons"] == {}


class ContentJudge:
    """A judge whose verdict follows the answer text, so a weak arm is
    actually scored weak. A constant-verdict spy would hand every arm the
    whole single_hop tier for free and quietly hide a real difference.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, question: str, answer: str, context: list[str], *, live: bool = False) -> dict:
        self.calls.append(question)
        return {"faithful": answer.startswith("Prose answer")}


def test_run_eval_all_mode_runs_every_arm_and_pairs_them(monkeypatch, golden):
    strong = _perfect_arm(golden)

    def _weak(question: str) -> QueryResponse:
        return _response("no idea", citations=[], refused=False)

    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"strong": strong, "weak": _weak})
    monkeypatch.setattr(harness, "judge_faithfulness", ContentJudge())

    payload = harness.run_eval(mode="all")

    assert payload["arms"] == ["strong", "weak"]
    assert set(payload["per_item"]) == {"strong", "weak"}
    assert len(payload["per_item"]["strong"]) == 25
    comparison = payload["comparisons"]["strong_vs_weak"]
    assert comparison["overall"]["b"] == 25  # strong passes everything the weak arm fails
    assert comparison["overall"]["c"] == 0
    assert comparison["overall"]["significant_at_0.05"] is True


# --- 8. missing judge fixtures (the CI replay gap) -----------------------------


def test_a_missing_judge_fixture_leaves_the_item_unscored_for_faithfulness(monkeypatch, stub_arm):
    """`eval/fixtures/judgments/` is currently empty, so this is the state
    `make eval` is in today, not a hypothetical.
    """
    monkeypatch.setattr(
        harness, "judge_faithfulness", JudgeSpy(raises=JudgeFixtureMissingError("no fixture"))
    )

    metrics = harness.run_eval(mode="stub_arm")["metrics"]["stub_arm"]

    assert metrics["faithfulness"]["scored"] == 0
    assert isnan(metrics["faithfulness"]["rate"])


def test_faithfulness_denominator_agrees_with_the_tier_breakdown(monkeypatch, stub_arm):
    """REGRESSION. Was xfail(strict=True) when this lane reported the bug.

    `eval/judge.py` raises `JudgeFixtureMissingError` so a replay gap fails
    loudly (FR9.6). `run_eval()` catches it and sets `faithful=None`, but
    the two consumers then disagreed: `faithfulness` correctly dropped the
    item from its denominator while `_item_passed` scored that same item as
    a FAILURE in the tier breakdown and in every McNemar comparison. With
    `eval/fixtures/judgments/` empty -- today's state -- `make eval` would
    have published "single_hop 0/10" for every arm, indistinguishable from
    a genuine faithfulness collapse.

    Fixed by making `_item_passed` return None for an unscorable item and
    excluding None from both the tier denominator and the McNemar pairing,
    matching the contract `expected_sources: []` already has for recall@5.
    """
    monkeypatch.setattr(
        harness, "judge_faithfulness", JudgeSpy(raises=JudgeFixtureMissingError("no fixture"))
    )

    metrics = harness.run_eval(mode="stub_arm")["metrics"]["stub_arm"]

    assert metrics["tier_breakdown"]["single_hop"]["n"] == metrics["faithfulness"]["scored"]


def test_run_eval_does_not_swallow_other_judge_errors(monkeypatch, stub_arm):
    """Only the fixture-miss case is tolerated. A malformed judge response
    (`ValueError` from `_parse_judge_json`) must propagate, not be silently
    scored as unfaithful.
    """
    monkeypatch.setattr(
        harness, "judge_faithfulness", JudgeSpy(raises=ValueError("Judge did not return JSON-only output"))
    )
    with pytest.raises(ValueError):
        harness.run_eval(mode="stub_arm")


# --- 9. markdown table (FR8.9) and the CLI -------------------------------------


@pytest.fixture
def payload(monkeypatch, golden):
    strong = _perfect_arm(golden)

    def _weak(question: str) -> QueryResponse:
        return _response("no idea", citations=[], refused=False)

    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"strong": strong, "weak": _weak})
    monkeypatch.setattr(harness, "judge_faithfulness", ContentJudge())
    return harness.run_eval(mode="all")


def test_markdown_table_states_the_sample_size_and_tier_split_by_construction(payload):
    """FR8.7's honest-n requirement is satisfied in the emitted table itself,
    not left to whoever pastes it into the README.
    """
    table = harness.render_markdown_table(payload)

    assert "n=25" in table
    assert "10 single_hop, 8 numeric, 4 multi_hop, 3 unanswerable" in table
    assert "directional" in table


def test_markdown_table_carries_every_arm_metric_and_tier_row(payload):
    table = harness.render_markdown_table(payload)

    for arm in ("strong", "weak"):
        assert f"| {arm} |" in table
        for tier in harness.TIERS:
            assert f"| {arm} | {tier} |" in table
    assert "recall@5" in table and "numeric_accuracy" in table and "refusal_accuracy" in table
    assert "## Paired arm-vs-arm significance (exact McNemar)" in table
    assert "| strong_vs_weak | overall |" in table


def test_markdown_table_renders_counts_next_to_every_rate(payload):
    table = harness.render_markdown_table(payload)
    assert "100% (19/19)" in table  # recall for the perfect arm — 19, not 25
    assert "100% (3/3)" in table  # refusal


def test_fmt_rate_reports_n_a_rather_than_a_misleading_zero():
    assert harness._fmt_rate({}) == "n/a"
    assert harness._fmt_rate({"correct": 0, "denominator": 0, "rate": float("nan")}) == "n/a"
    assert harness._fmt_rate({"correct": 0, "denominator": 5, "rate": float("nan")}) == "n/a"
    assert harness._fmt_rate({"correct": 3, "denominator": 4, "rate": 0.75}) == "75% (3/4)"


def test_markdown_faithfulness_percentage_matches_its_own_counts(monkeypatch, golden):
    """REGRESSION. Was xfail(strict=True) when this lane reported the bug.

    `faithfulness` computed its rate over `scored` (judged items) while
    `_fmt_rate` printed the parenthetical over `denominator` (all
    single_hop items), so with 4 of 10 judged and 2 faithful the published
    table read "50% (2/10)" -- inviting the reader to conclude 8 items
    failed when 6 were never graded.

    Fixed by setting `denominator` to the judged count so rate and counts
    divide by the same number, with `excluded` recording the omission.
    """
    verdicts = {}

    def _judge(question, answer, context, *, live=False):
        # Judge only 4 of the 10 single_hop items; 2 of those are faithful.
        index = len(verdicts)
        verdicts[question] = index
        if index >= 4:
            raise JudgeFixtureMissingError("no fixture")
        return {"faithful": index < 2}

    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"stub_arm": _perfect_arm(golden)})
    monkeypatch.setattr(harness, "judge_faithfulness", _judge)

    payload = harness.run_eval(mode="stub_arm")
    faithfulness = payload["metrics"]["stub_arm"]["faithfulness"]

    assert faithfulness["correct"] == 2
    assert faithfulness["scored"] == 4
    rendered = harness._fmt_rate(faithfulness)
    assert rendered == "50% (2/4)", f"table would print {rendered!r}"


def test_markdown_table_renders_absent_tiers_as_n_a_and_skips_their_comparisons(monkeypatch):
    """A golden slice missing a whole tier must not divide by zero in the
    per-tier table or emit a McNemar row for a tier with no items.
    """
    reduced = [
        _item("q005", "numeric", expected_numeric=100, tolerance=0),
        _item("q013", "single_hop", sources=[MSFT_2024_ITEM7]),
    ]
    monkeypatch.setattr(harness, "_load_golden", lambda: reduced)
    monkeypatch.setattr(
        harness,
        "_arm_dispatch",
        lambda: {
            "a": lambda q: _response("$100", citations=[MSFT_2024_ITEM7]),
            "b": lambda q: _response("$999", citations=[]),
        },
    )
    monkeypatch.setattr(harness, "judge_faithfulness", ContentJudge())

    payload = harness.run_eval(mode="all")
    table = harness.render_markdown_table(payload)

    assert payload["comparisons"]["a_vs_b"]["multi_hop"] is None
    assert "| a | multi_hop | 0 | 0 | n/a | n/a |" in table
    assert "| a_vs_b | multi_hop |" not in table
    assert "| a_vs_b | numeric |" in table


def test_markdown_table_handles_an_arm_that_scored_nothing(monkeypatch, golden):
    """A table emitter that crashes on NaN would take down the whole Wave 3
    measurement run at the last step.
    """
    monkeypatch.setattr(
        harness, "_arm_dispatch", lambda: {"stub_arm": lambda q: _response("nothing", citations=[])}
    )
    monkeypatch.setattr(
        harness, "judge_faithfulness", JudgeSpy(raises=JudgeFixtureMissingError("no fixture"))
    )

    table = harness.render_markdown_table(harness.run_eval(mode="stub_arm"))

    assert "| stub_arm |" in table
    assert "n/a" in table


def test_main_writes_a_timestamped_results_file_and_prints_the_table(monkeypatch, tmp_path, capsys, golden):
    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(harness, "_arm_dispatch", lambda: {"stub_arm": _perfect_arm(golden)})
    monkeypatch.setattr(harness, "judge_faithfulness", JudgeSpy())
    monkeypatch.setattr(sys, "argv", ["run_eval", "--mode", "stub_arm", "--replay"])

    harness.main()

    written = list((tmp_path / "results").glob("eval_*.json"))
    assert len(written) == 1
    saved = json.loads(written[0].read_text(encoding="utf-8"))
    assert saved["n_items"] == 25
    assert saved["arms"] == ["stub_arm"]

    out = capsys.readouterr().out
    assert "# FilingAgent eval results (n=25)" in out
    assert str(written[0]) in out


def test_main_defaults_to_replay_mode_not_live(monkeypatch, tmp_path, golden):
    """The default must never be the billed path — `make eval` and a bare
    `python -m eval.run_eval` both have to stay offline (FR9.5).
    """
    seen: list[bool] = []

    def _fake_run_eval(mode: str = "all", live: bool = False) -> dict:
        seen.append(live)
        return {
            "generated_at": "now",
            "n_items": 0,
            "tier_counts": {},
            "arms": [],
            "metrics": {},
            "comparisons": {},
            "per_item": {},
        }

    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(harness, "run_eval", _fake_run_eval)
    monkeypatch.setattr(sys, "argv", ["run_eval"])

    harness.main()

    assert seen == [False]


def test_main_live_flag_reaches_run_eval(monkeypatch, tmp_path):
    seen: list[bool] = []

    def _fake_run_eval(mode: str = "all", live: bool = False) -> dict:
        seen.append(live)
        return {
            "generated_at": "now",
            "n_items": 0,
            "tier_counts": {},
            "arms": [],
            "metrics": {},
            "comparisons": {},
            "per_item": {},
        }

    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(harness, "run_eval", _fake_run_eval)
    monkeypatch.setattr(sys, "argv", ["run_eval", "--live"])

    harness.main()

    assert seen == [True]


def test_main_rejects_replay_and_live_together(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_eval", "--replay", "--live"])
    with pytest.raises(SystemExit):
        harness.main()
