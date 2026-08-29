"""Wave 0 contract validation against the real, human-authored golden set.

data/golden.jsonl is the source of truth (PLAN.md "Status": hand-authored
before any code existed). These tests assert that src.schemas.GoldenItem
matches it exactly, and that the golden set itself satisfies the tier/
answerable/kind/numeric-tolerance conventions the rest of the project
depends on (PRD FR8.1, FR8.3; PLAN.md "Contract decisions — RESOLVED").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.schemas import GoldenItem

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "data" / "golden.jsonl"


def _load_lines() -> list[str]:
    return [line for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_golden_items() -> list[GoldenItem]:
    return [GoldenItem.model_validate(json.loads(line)) for line in _load_lines()]


def test_all_25_golden_items_validate():
    lines = _load_lines()
    assert len(lines) == 25, f"expected 25 golden items, found {len(lines)}"

    failures = []
    for i, line in enumerate(lines, start=1):
        try:
            GoldenItem.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            failures.append(f"line {i}: {exc}")

    assert not failures, "golden items failed validation:\n" + "\n".join(failures)


def test_tier_counts_match_prd():
    """PRD FR8.3: 10 single_hop / 8 numeric / 4 multi_hop / 3 unanswerable."""
    items = _load_golden_items()
    counts: dict[str, int] = {}
    for item in items:
        counts[item.tier] = counts.get(item.tier, 0) + 1

    assert counts == {
        "single_hop": 10,
        "numeric": 8,
        "multi_hop": 4,
        "unanswerable": 3,
    }


def test_exactly_three_items_unanswerable():
    items = _load_golden_items()
    unanswerable = [item for item in items if not item.answerable]
    assert len(unanswerable) == 3
    assert all(item.tier == "unanswerable" for item in unanswerable)


def test_every_unanswerable_item_has_kind():
    items = _load_golden_items()
    for item in items:
        if item.tier == "unanswerable":
            assert item.kind is not None, f"{item.question_id}: unanswerable item missing 'kind'"
        else:
            assert item.kind is None, f"{item.question_id}: non-unanswerable item must not carry 'kind'"


def test_numeric_tolerance_none_iff_expected_numeric_none():
    items = _load_golden_items()
    for item in items:
        assert (item.expected_numeric is None) == (item.numeric_tolerance is None), (
            f"{item.question_id}: numeric_tolerance must be None iff expected_numeric is None "
            f"(expected_numeric={item.expected_numeric!r}, numeric_tolerance={item.numeric_tolerance!r})"
        )


def test_question_ids_are_q001_through_q025_unique():
    items = _load_golden_items()
    ids = [item.question_id for item in items]
    assert len(ids) == len(set(ids)), "duplicate question_id found"
    assert sorted(ids) == [f"q{i:03d}" for i in range(1, 26)]


@pytest.mark.parametrize(
    "bad_item",
    [
        # answerable/tier mismatch
        {
            "question_id": "q999",
            "tier": "unanswerable",
            "answerable": True,  # should be False
            "question": "x",
            "expected_answer": "x",
            "expected_numeric": None,
            "numeric_tolerance": None,
            "expected_sources": [],
            "expected_tools": ["search_filings"],
            "kind": "future",
        },
        # numeric_tolerance set without expected_numeric
        {
            "question_id": "q998",
            "tier": "single_hop",
            "answerable": True,
            "question": "x",
            "expected_answer": "x",
            "expected_numeric": None,
            "numeric_tolerance": 0.01,
            "expected_sources": [],
            "expected_tools": ["search_filings"],
        },
        # kind on a non-unanswerable item
        {
            "question_id": "q997",
            "tier": "single_hop",
            "answerable": True,
            "question": "x",
            "expected_answer": "x",
            "expected_numeric": None,
            "numeric_tolerance": None,
            "expected_sources": [],
            "expected_tools": ["search_filings"],
            "kind": "future",
        },
    ],
)
def test_convention_violations_are_rejected(bad_item):
    with pytest.raises(ValidationError):
        GoldenItem.model_validate(bad_item)
