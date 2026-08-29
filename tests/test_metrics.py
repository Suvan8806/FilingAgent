"""Unit tests for eval/metrics.py (Lane F — PLAN.md Wave 1).

Covers the three requirements PLAN.md/PRD.md flag as silent-corruption
risks if missed:

1. recall@5 excludes expected_sources == [] items from its denominator
   (PLAN.md Contract decision #3) — tested explicitly below, not just
   incidentally covered by another test.
2. numeric_accuracy tolerance boundaries, including the multi_hop
   convention that numeric_tolerance is a fraction of |expected_numeric|.
3. mcnemar_test against a known contingency table with a hand-computed
   exact p-value.

Also covers refusal_accuracy and wilson_confidence_interval sanity.
"""

from __future__ import annotations

from math import isclose

import pytest
from pydantic import ValidationError

from eval.metrics import (
    mcnemar_test,
    numeric_accuracy,
    numeric_accuracy_detail,
    recall_at_5,
    recall_at_5_detail,
    refusal_accuracy,
    refusal_accuracy_detail,
    wilson_confidence_interval,
)
from src.schemas import GoldenItem

# --- helpers ----------------------------------------------------------------


def _numeric_item(question_id: str, expected_numeric: float, tolerance: float, tier: str = "numeric", sources=None) -> GoldenItem:
    return GoldenItem.model_validate(
        {
            "question_id": question_id,
            "tier": tier,
            "answerable": True,
            "question": "x",
            "expected_answer": "x",
            "expected_numeric": expected_numeric,
            "numeric_tolerance": tolerance,
            "expected_sources": sources or [],
            "expected_tools": ["lookup_financial"],
        }
    )


def _prose_item(question_id: str, sources) -> GoldenItem:
    return GoldenItem.model_validate(
        {
            "question_id": question_id,
            "tier": "single_hop",
            "answerable": True,
            "question": "x",
            "expected_answer": "x",
            "expected_numeric": None,
            "numeric_tolerance": None,
            "expected_sources": sources,
            "expected_tools": ["search_filings"],
        }
    )


def _unanswerable_item(question_id: str, kind: str = "future") -> GoldenItem:
    return GoldenItem.model_validate(
        {
            "question_id": question_id,
            "tier": "unanswerable",
            "answerable": False,
            "question": "x",
            "expected_answer": "UNANSWERABLE",
            "expected_numeric": None,
            "numeric_tolerance": None,
            "expected_sources": [],
            "expected_tools": ["search_filings"],
            "kind": kind,
        }
    )


MSFT_2024_ITEM7 = {"ticker": "MSFT", "fiscal_year": 2024, "section": "item7"}
AAPL_2024_ITEM1 = {"ticker": "AAPL", "fiscal_year": 2024, "section": "item1"}


# --- 1. recall@5 exclusion (PLAN.md Contract decision #3) -------------------------


def test_recall_at_5_excludes_empty_expected_sources_from_denominator():
    """Items with expected_sources == [] (like q009/q010/q011 in the real
    golden set) must not appear in the denominator at all — not be counted
    as hits, and not be counted as misses. This is the exact scenario the
    real q009-q011 items hit: the figure lives only in the unindexed Item 8,
    so no retrieved chunk will ever match, and that must not look like a
    retrieval bug.
    """
    golden = [
        _numeric_item("q100", 100.0, 0, tier="numeric", sources=[MSFT_2024_ITEM7]),  # retrievable, will hit
        _numeric_item("q101", 200.0, 0, tier="numeric", sources=[]),  # NOT retrievable — must be excluded
        _numeric_item("q102", 300.0, 0, tier="numeric", sources=[]),  # NOT retrievable — must be excluded
    ]
    results = [
        {"question_id": "q100", "citations": [MSFT_2024_ITEM7]},
        # q101/q102 deliberately have NO citations at all (nothing retrieved,
        # nothing could ever be retrieved for them) — if the exclusion were
        # missing, these would count as misses and recall would be 1/3.
        {"question_id": "q101", "citations": []},
        {"question_id": "q102", "citations": []},
    ]

    detail = recall_at_5_detail(results, golden)

    assert detail["denominator"] == 1, "expected_sources==[] items must not enter the denominator"
    assert detail["excluded"] == 2
    assert detail["correct"] == 1
    assert detail["rate"] == 1.0  # not 1/3 — that would mean the exclusion was skipped
    assert recall_at_5(results, golden) == 1.0


def test_recall_at_5_still_scores_retrievable_items_as_misses_when_no_match():
    golden = [_numeric_item("q100", 100.0, 0, tier="numeric", sources=[MSFT_2024_ITEM7])]
    results = [{"question_id": "q100", "citations": [AAPL_2024_ITEM1]}]  # wrong source retrieved

    detail = recall_at_5_detail(results, golden)
    assert detail["denominator"] == 1
    assert detail["correct"] == 0
    assert detail["rate"] == 0.0


def test_recall_at_5_excludes_unanswerable_tier_too_not_just_numeric():
    """expected_sources == [] excludes q009-q011 (numeric) AND q023-q025
    (unanswerable) for the identical reason: nothing is retrievable for
    either. This is correct, not a gap — see eval/metrics.py's module
    docstring. Pin it here so it isn't "fixed" by someone special-casing
    the unanswerable tier back into the denominator.
    """
    golden = [
        _numeric_item("q100", 100.0, 0, tier="numeric", sources=[MSFT_2024_ITEM7]),
        _unanswerable_item("q023", kind="future"),
    ]
    results = [
        {"question_id": "q100", "citations": [MSFT_2024_ITEM7]},
        {"question_id": "q023", "citations": []},
    ]

    detail = recall_at_5_detail(results, golden)

    assert detail["denominator"] == 1  # only q100; q023 excluded despite refused=True elsewhere
    assert detail["excluded"] == 1
    assert detail["rate"] == 1.0


def test_recall_at_5_all_items_excluded_reports_nan_not_zero():
    golden = [_numeric_item("q101", 200.0, 0, sources=[]), _numeric_item("q102", 300.0, 0, sources=[])]
    detail = recall_at_5_detail([], golden)
    assert detail["denominator"] == 0
    assert detail["rate"] != detail["rate"]  # NaN


# --- 2. numeric_accuracy tolerance boundaries ----------------------------------------------------------------


def test_numeric_accuracy_exact_tolerance_zero_requires_exact_match():
    golden = [_numeric_item("q005", 211915000000, 0, tier="numeric")]

    exact = [{"question_id": "q005", "numeric_value": 211915000000}]
    assert numeric_accuracy(exact, golden) == 1.0

    off_by_one = [{"question_id": "q005", "numeric_value": 211915000001}]
    assert numeric_accuracy(off_by_one, golden) == 0.0


def test_numeric_accuracy_multi_hop_relative_tolerance_boundary():
    """PLAN.md Contract decision #2: multi_hop expected_numeric is the
    delta. numeric_tolerance (0.01 in the real golden set) is a fraction
    of |expected_numeric| — verify both sides of that boundary exactly.
    """
    expected_delta = 33207000000
    tolerance = 0.01
    golden = [_numeric_item("q001", expected_delta, tolerance, tier="multi_hop")]

    allowed = tolerance * expected_delta  # 332,070,000
    at_boundary = [{"question_id": "q001", "numeric_value": expected_delta + allowed}]
    assert numeric_accuracy(at_boundary, golden) == 1.0

    just_over = [{"question_id": "q001", "numeric_value": expected_delta + allowed + 1}]
    assert numeric_accuracy(just_over, golden) == 0.0


def test_numeric_accuracy_rejects_endpoint_mislabeled_as_delta():
    """The corruption scenario the task explicitly calls out: comparing a
    multi_hop answer against the later-year endpoint instead of the delta
    must fail, even though the endpoint is a "real" number from the filing.
    """
    # q001: MSFT revenue delta FY2023->FY2024 is $33.207B; the FY2024
    # endpoint is $245.122B. An arm that reports the endpoint (not the
    # change) must NOT be scored correct.
    golden = [_numeric_item("q001", 33207000000, 0.01, tier="multi_hop")]
    endpoint_only = [{"question_id": "q001", "numeric_value": 245122000000}]
    assert numeric_accuracy(endpoint_only, golden) == 0.0


def test_numeric_accuracy_missing_answer_is_not_scored_correct():
    golden = [_numeric_item("q005", 100.0, 0, tier="numeric")]
    detail = numeric_accuracy_detail([{"question_id": "q005", "numeric_value": None}], golden)
    assert detail["denominator"] == 1
    assert detail["scored"] == 0
    assert detail["correct"] == 0
    assert detail["rate"] == 0.0


def test_numeric_accuracy_ignores_single_hop_and_unanswerable_items():
    golden = [
        _numeric_item("q005", 100.0, 0, tier="numeric"),
        _prose_item("q013", sources=[MSFT_2024_ITEM7]),
        _unanswerable_item("q023"),
    ]
    results = [{"question_id": "q005", "numeric_value": 100.0}]
    detail = numeric_accuracy_detail(results, golden)
    assert detail["denominator"] == 1  # only the numeric-tier item is eligible


# --- refusal_accuracy ----------------------------------------------------------------


def test_refusal_accuracy_counts_only_unanswerable_tier():
    golden = [
        _unanswerable_item("q023", kind="future"),
        _unanswerable_item("q024", kind="out_of_corpus"),
        _unanswerable_item("q025", kind="never_tagged"),
        _numeric_item("q005", 100.0, 0, tier="numeric"),
    ]
    results = [
        {"question_id": "q023", "refused": True},
        {"question_id": "q024", "refused": True},
        {"question_id": "q025", "refused": False},
        {"question_id": "q005", "refused": False},  # answerable item, must not affect this metric
    ]
    detail = refusal_accuracy_detail(results, golden)
    assert detail["denominator"] == 3
    assert detail["correct"] == 2
    assert isclose(refusal_accuracy(results, golden), 2 / 3)


def test_refusal_accuracy_missing_result_counts_as_not_refused():
    golden = [_unanswerable_item("q023")]
    detail = refusal_accuracy_detail([], golden)
    assert detail["correct"] == 0
    assert detail["denominator"] == 1


# --- 3. mcnemar_test on a known contingency table -------------------------------------------------


def test_mcnemar_known_contingency_table_exact_p_value():
    """b=2 (A-only-pass), c=8 (B-only-pass), n=10 discordant.
    Exact two-sided p = 2 * sum_{i=0}^{2} C(10, i) * 0.5^10
                       = 2 * (1 + 10 + 45) / 1024 = 112 / 1024 = 0.109375.
    Hand-verifiable, independent of any stats library.
    """
    # 2 pairs where A passes and B fails
    a_only = [(True, False)] * 2
    # 8 pairs where B passes and A fails
    b_only = [(False, True)] * 8
    # 15 concordant pairs (irrelevant to b/c, included to prove they're ignored)
    concordant = [(True, True)] * 10 + [(False, False)] * 5

    pairs = a_only + b_only + concordant
    arm_a = [p[0] for p in pairs]
    arm_b = [p[1] for p in pairs]

    result = mcnemar_test(arm_a, arm_b)

    assert result["b"] == 2
    assert result["c"] == 8
    assert result["discordant"] == 10
    assert result["concordant"] == 15
    assert result["n"] == 25
    assert isclose(result["p_value"], 0.109375, rel_tol=1e-9)
    assert result["significant_at_0.05"] is False


def test_mcnemar_no_discordant_pairs_is_not_significant():
    arm_a = [True, True, False, False]
    arm_b = [True, True, False, False]
    result = mcnemar_test(arm_a, arm_b)
    assert result["discordant"] == 0
    assert result["p_value"] == 1.0
    assert result["significant_at_0.05"] is False


def test_mcnemar_fully_one_sided_discordance_is_significant():
    # b=0, c=10: exact p = 2 * C(10,0) / 1024 = 2/1024 ~= 0.001953125
    arm_a = [False] * 10
    arm_b = [True] * 10
    result = mcnemar_test(arm_a, arm_b)
    assert result["b"] == 0
    assert result["c"] == 10
    assert isclose(result["p_value"], 2 / 1024, rel_tol=1e-9)
    assert result["significant_at_0.05"] is True


def test_mcnemar_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        mcnemar_test([True, False], [True])


# --- wilson_confidence_interval sanity ----------------------------------------------------------------


def test_wilson_confidence_interval_bounds_stay_within_unit_interval():
    lower, upper = wilson_confidence_interval(3, 3)  # 100% on n=3, like the unanswerable tier
    assert 0.0 <= lower <= upper <= 1.0


def test_wilson_confidence_interval_zero_n_is_nan():
    lower, upper = wilson_confidence_interval(0, 0)
    assert lower != lower  # NaN
    assert upper != upper


# --- GoldenItem fixture sanity (guards the helpers above against schema drift) ------------------


def test_golden_item_helpers_produce_valid_items():
    # If src/schemas.py's GoldenItem contract ever changes, these helper
    # constructors should fail loudly here rather than the tests above
    # silently testing nothing.
    _numeric_item("q900", 1.0, 0.0)
    _prose_item("q901", [MSFT_2024_ITEM7])
    _unanswerable_item("q902")
    with pytest.raises(ValidationError):
        GoldenItem.model_validate({"question_id": "bad", "tier": "numeric"})
