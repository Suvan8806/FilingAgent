"""Deterministic eval metrics + paired comparison (Lane F — PLAN.md Wave 1).

Owns: this file, jointly with eval/run_eval.py and eval/judge.py (Lane F's
exclusive write scope).

Contract
--------
All metrics except `faithfulness` (eval/judge.py) are computed here without
an LLM in the loop (FR8.4):

- `recall_at_5` — fraction of items where >=1 retrieved chunk matches an
  `expected_sources` entry (ticker + fiscal_year + section).

  **CRITICAL — recall@5 denominator (PLAN.md Contract decision #3):**
  Items whose `expected_sources == []` MUST BE EXCLUDED from the
  denominator entirely — they are not "no match found," they are "not
  retrievable from the indexed corpus" (the figure lives only in Item 8,
  which PRD §6 explicitly does not index). Counting them as retrieval
  misses would report a retrieval bug that does not exist. This affects
  **q009, q010, q011** in data/golden.jsonl (all `tier == "numeric"`,
  `expected_sources == []`) — verify against the live file, do not
  hardcode these three IDs as a magic list that silently goes stale if the
  golden set is ever regenerated from data/questions and answers.txt.
  A dedicated unit test in tests/ must assert this exclusion directly
  (PLAN.md "Risks and guards": "recall@5 misreported... asserted in a Lane
  F unit test").

- `numeric_accuracy` — exact match within `numeric_tolerance` of
  `expected_numeric`, deterministic assertion against the XBRL-backed
  answer. For `tier == "multi_hop"` items, the comparison target is the
  **delta** between the two fiscal years, not the later-year endpoint —
  see eval/run_eval.py's docstring; this module receives the already
  -computed comparison value from run_eval.py and should not need to know
  which tier it came from, but must not silently accept an endpoint value
  mislabeled as a delta.

- `refusal_accuracy` — fraction of `tier == "unanswerable"` items where
  `QueryResponse.refused` is True.

- `p50` / `p95` latency, `avg_tool_calls` — straightforward aggregates over
  `QueryResponse.latency_ms` / `len(trace)`.

Paired comparison (FR8.6): all arms answer identical questions, so
arm-vs-arm significance uses McNemar's test on paired pass/fail outcomes,
not an independent-samples test. Report per-tier confidence intervals
alongside raw counts — at n=25 (4 multi_hop, 3 unanswerable) several
metrics move in large discrete increments, and the honest-n requirement
(FR8.7) means raw counts must always be reported next to any rate.
"""

from __future__ import annotations

from src.schemas import GoldenItem


def recall_at_5(results: list[dict], golden: list[GoldenItem]) -> float:
    """Fraction of items (excluding any with expected_sources == []) where
    at least one retrieved chunk matches an expected source. See module
    docstring for why the exclusion is required, not optional.
    """
    raise NotImplementedError


def numeric_accuracy(results: list[dict], golden: list[GoldenItem]) -> float:
    """Deterministic exact-match-within-tolerance on the numeric and
    multi_hop tiers. Multi-hop comparisons are against a delta, not an
    endpoint — see eval/run_eval.py.
    """
    raise NotImplementedError


def refusal_accuracy(results: list[dict], golden: list[GoldenItem]) -> float:
    """Fraction of unanswerable items correctly refused."""
    raise NotImplementedError


def mcnemar_test(arm_a_results: list[bool], arm_b_results: list[bool]) -> dict:
    """Paired significance test between two arms' per-item pass/fail
    outcomes (FR8.6).
    """
    raise NotImplementedError
