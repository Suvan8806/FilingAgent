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
  `expected_sources == []`) — the exclusion is computed dynamically from
  each item's `expected_sources`, never a hardcoded id list, so it cannot
  silently go stale if the golden set is ever regenerated from
  data/questions and answers.txt. A dedicated unit test in
  tests/test_metrics.py asserts this exclusion directly (PLAN.md "Risks
  and guards": "recall@5 misreported... asserted in a Lane F unit test").

  **This same exclusion also, correctly, drops q023/q024/q025 (the
  `unanswerable` tier) from the denominator** — they carry
  `expected_sources == []` too, for the same reason: there is nothing to
  retrieve for a question the corpus cannot answer. This is not a gap to
  "fix" by special-casing the unanswerable tier back in; `recall@5` is
  scoped to items where a correct retrieval is possible by construction,
  and `refusal_accuracy` is the metric that scores the unanswerable tier.

- `numeric_accuracy` — exact match within `numeric_tolerance` of
  `expected_numeric`, deterministic assertion against the XBRL-backed
  answer. For `tier == "multi_hop"` items, the comparison target is the
  **delta** between the two fiscal years (PLAN.md Contract decision #2),
  not the later-year endpoint — `data/golden.jsonl`'s `expected_numeric`
  already *is* that delta for every multi_hop row (verified against
  `data/reference/xbrl_facts.csv` per PLAN.md "Status"). This module
  receives the arm's already-extracted numeric answer from
  eval/run_eval.py and compares it against `expected_numeric` uniformly
  regardless of tier — it is run_eval.py's job (see its docstring) to
  make sure that extracted value represents a computed difference for
  multi_hop items, never a bare fiscal-year endpoint.

  `numeric_tolerance` is interpreted as a **fraction of
  `abs(expected_numeric)`** (e.g. `0` => exact match, required for the
  numeric tier's raw XBRL lookups; `0.01` => 1%, used only on multi_hop
  deltas to tolerate minor arithmetic rounding by the answering arm). This
  matches the golden set's own usage: every `numeric` tier row uses
  `numeric_tolerance: 0`, every `multi_hop` row uses `0.01`.

- `refusal_accuracy` — fraction of `tier == "unanswerable"` items where
  `QueryResponse.refused` is True.

- `p50` / `p95` latency, `avg_tool_calls` — straightforward aggregates over
  `QueryResponse.latency_ms` / `len(trace)`. Computed in eval/run_eval.py
  directly (they need no golden-set cross-reference), not here.

Paired comparison (FR8.6): all arms answer identical questions, so
arm-vs-arm significance uses McNemar's test on paired pass/fail outcomes,
not an independent-samples test. `mcnemar_test` uses the **exact** (binomial)
form rather than the chi-square approximation — at n=25 (and far fewer
discordant pairs once split by tier), the chi-square approximation is
unreliable for small counts; the exact test is the honest choice here
(FR8.7). `wilson_confidence_interval` supports the "per-tier confidence
intervals" requirement in FR8.6 — at n=25 (4 multi_hop, 3 unanswerable)
several metrics move in large discrete increments, and the honest-n
requirement (FR8.7) means raw counts must always be reported next to any
rate. All `*_detail` functions below return `{"correct", "denominator",
"rate", ...}` dicts precisely so eval/run_eval.py can report counts, not
just percentages.

Result item schema (the `results: list[dict]` argument throughout this
module) — one dict per golden item actually scored, built by
eval/run_eval.py from a `QueryResponse`:

    {
        "question_id": str,               # matches GoldenItem.question_id
        "citations": list[dict],          # [{"ticker", "fiscal_year", "section"}, ...]
        "numeric_value": float | None,     # extracted numeric answer (delta, for multi_hop)
        "refused": bool,
        "faithful": bool | None,          # judge verdict, single_hop only
        "tool_calls": int,
        "latency_ms": float,
        "answer": str,
    }

Functions here look items up by `question_id`; a golden item with no
matching result (arm not yet run, or a lookup failure) is treated as not
scored — excluded from the numerator, still counted in the denominator
where applicable, matching "a missing answer is a wrong answer," not "a
missing answer is ignored."
"""

from __future__ import annotations

from math import comb, sqrt

from src.schemas import GoldenItem

# --- recall@5 ----------------------------------------------------------------


def recall_at_5(results: list[dict], golden: list[GoldenItem]) -> float:
    """Fraction of items (excluding any with expected_sources == []) where
    at least one retrieved chunk matches an expected source. See module
    docstring for why the exclusion is required, not optional.
    """
    return recall_at_5_detail(results, golden)["rate"]


def recall_at_5_detail(results: list[dict], golden: list[GoldenItem]) -> dict:
    """Same as `recall_at_5` but returns raw counts alongside the rate
    (FR8.7 — never report a bare percentage at this sample size).
    """
    results_by_id = {r["question_id"]: r for r in results}

    # CRITICAL (PLAN.md Contract decision #3 / PRD FR8.1, FR8.4): items with
    # expected_sources == [] are "not retrievable from the indexed corpus"
    # (e.g. Item 8 figures — PRD §6 does not index Item 8), not retrieval
    # misses. Exclude them from the denominator entirely rather than
    # counting them as failures.
    eligible = [item for item in golden if item.expected_sources]

    correct = 0
    for item in eligible:
        result = results_by_id.get(item.question_id)
        if result is None:
            continue
        expected_keys = {(s.ticker, s.fiscal_year, s.section) for s in item.expected_sources}
        retrieved_keys = {
            (c.get("ticker"), c.get("fiscal_year"), c.get("section"))
            for c in result.get("citations", [])
        }
        if expected_keys & retrieved_keys:
            correct += 1

    denominator = len(eligible)
    return {
        "correct": correct,
        "denominator": denominator,
        "excluded": len(golden) - denominator,
        "rate": correct / denominator if denominator else float("nan"),
    }


# --- numeric_accuracy ----------------------------------------------------------------


def numeric_accuracy(results: list[dict], golden: list[GoldenItem]) -> float:
    """Deterministic exact-match-within-tolerance on the numeric and
    multi_hop tiers. Multi-hop comparisons are against a delta, not an
    endpoint — see eval/run_eval.py.
    """
    return numeric_accuracy_detail(results, golden)["rate"]


def numeric_accuracy_detail(results: list[dict], golden: list[GoldenItem]) -> dict:
    """Same as `numeric_accuracy` but returns raw counts alongside the rate."""
    results_by_id = {r["question_id"]: r for r in results}

    eligible = [
        item
        for item in golden
        if item.tier in ("numeric", "multi_hop") and item.expected_numeric is not None
    ]

    correct = 0
    scored = 0
    for item in eligible:
        result = results_by_id.get(item.question_id)
        if result is None or result.get("numeric_value") is None:
            continue
        scored += 1
        tolerance = item.numeric_tolerance or 0.0
        allowed = tolerance * abs(item.expected_numeric)
        if abs(result["numeric_value"] - item.expected_numeric) <= allowed:
            correct += 1

    denominator = len(eligible)
    return {
        "correct": correct,
        "scored": scored,
        "denominator": denominator,
        "rate": correct / denominator if denominator else float("nan"),
    }


# --- refusal_accuracy ----------------------------------------------------------------


def refusal_accuracy(results: list[dict], golden: list[GoldenItem]) -> float:
    """Fraction of unanswerable items correctly refused."""
    return refusal_accuracy_detail(results, golden)["rate"]


def refusal_accuracy_detail(results: list[dict], golden: list[GoldenItem]) -> dict:
    """Same as `refusal_accuracy` but returns raw counts alongside the rate."""
    results_by_id = {r["question_id"]: r for r in results}

    eligible = [item for item in golden if item.tier == "unanswerable"]
    correct = sum(
        1 for item in eligible if results_by_id.get(item.question_id, {}).get("refused") is True
    )

    denominator = len(eligible)
    return {
        "correct": correct,
        "denominator": denominator,
        "rate": correct / denominator if denominator else float("nan"),
    }


# --- paired comparison (FR8.6) ----------------------------------------------------------------


def mcnemar_test(arm_a_results: list[bool], arm_b_results: list[bool]) -> dict:
    """Paired significance test between two arms' per-item pass/fail
    outcomes (FR8.6). Both lists must be the same length and in the same
    question order — the arms answer identical questions, so this is a
    paired, not independent-samples, comparison.

    Uses the **exact** (binomial) McNemar test rather than the chi-square
    approximation: at n=25 overall, and far fewer discordant pairs once
    split by tier (e.g. n=4 on multi_hop), the chi-square approximation is
    not reliable. The exact test is appropriate for small, discrete
    samples and is the honest choice for this sample size (FR8.7).
    """
    if len(arm_a_results) != len(arm_b_results):
        raise ValueError(
            "mcnemar_test requires paired results of equal length "
            f"(got {len(arm_a_results)} vs {len(arm_b_results)}); arms must answer "
            "identical questions in identical order (FR8.6)."
        )

    n = len(arm_a_results)
    b = sum(1 for a, bb in zip(arm_a_results, arm_b_results) if a and not bb)
    c = sum(1 for a, bb in zip(arm_a_results, arm_b_results) if (not a) and bb)
    discordant = b + c
    concordant = n - discordant

    if discordant == 0:
        p_value = 1.0
    else:
        k = min(b, c)
        tail = sum(comb(discordant, i) for i in range(0, k + 1))
        p_value = min(1.0, 2 * tail / (2**discordant))

    return {
        "n": n,
        "b": b,
        "c": c,
        "concordant": concordant,
        "discordant": discordant,
        "p_value": p_value,
        "significant_at_0.05": discordant > 0 and p_value < 0.05,
    }


def wilson_confidence_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95%-by-default Wilson score confidence interval for a binomial rate.

    Preferred over the normal approximation at small n (FR8.6 "per-tier
    confidence intervals") because it stays within [0, 1] and behaves
    sanely at the tier sizes here (as small as n=3 for `unanswerable`).
    """
    if n <= 0:
        return (float("nan"), float("nan"))

    phat = successes / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    margin = z * sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    lower = (center - margin) / denom
    upper = (center + margin) / denom
    return (max(0.0, lower), min(1.0, upper))
