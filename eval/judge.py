"""LLM judge — faithfulness only (Lane F — PLAN.md Wave 1).

Owns: this file, jointly with eval/run_eval.py and eval/metrics.py (Lane
F's exclusive write scope).

Contract
--------
The judge scores exactly one metric: `faithfulness` — is every claim in
the answer supported by the retrieved context — on prose tiers only
(`single_hop`; unanswerable is scored deterministically by refusal, numeric
tiers are scored deterministically by exact match). Everything else in
FR8.4 is computed without an LLM in the loop.

**FR8.5 — judge determinism, non-negotiable:**
- Pinned model, temperature 0, JSON-only output.
- Every judgment written to `eval/fixtures/judgments/`, keyed by a content
  hash of (question, answer, context).
- CI **replays** these fixtures rather than calling the API live — a red
  build must always mean a real regression, never a flaky judge call
  (FR9.6). Live judging only happens via `make eval-live`.

**FR8.8 — judge-reliability check (replaces the old hand-agreement
check).** With the build fully agent-executed, an agent grading the judge
is the same model class judging itself and is not evidence. Three
automated checks instead, all implemented against this module:

1. Determinism replay — re-run the judge 3x at temperature 0 on the same 5
   items; assert byte-identical verdicts.
2. Cross-model agreement — a *different* model applies the same rubric to
   the same 5 items; report the inter-judge agreement rate.
3. Adversarial fixtures — hand-built cases from tests/fixtures/ (never
   from the golden set) containing a known-unsupported claim; the judge
   must mark them unfaithful.

**The README must describe this as "cross-model judge agreement," never as
"hand-verified"** — see PLAN.md "Status" and PRD FR8.8. Getting this
wording right in Lane K (Wave 4) depends on this module actually
implementing check #2 as a real cross-model call, not a stub that always
reports 100% agreement.
"""

from __future__ import annotations


def judge_faithfulness(question: str, answer: str, context: list[str]) -> dict:
    """Score one (question, answer, context) triple for faithfulness.
    Returns a JSON-shaped dict (verdict + rationale). Looks up
    eval/fixtures/judgments/ first when running in replay mode; only calls
    the live API under `make eval-live`.
    """
    raise NotImplementedError


def check_determinism(items: list[dict], n_runs: int = 3) -> bool:
    """FR8.8 check #1: re-run the judge n_runs times at temperature 0 on
    the same items; True iff all runs produce byte-identical verdicts.
    """
    raise NotImplementedError


def check_cross_model_agreement(items: list[dict]) -> float:
    """FR8.8 check #2: apply the same rubric with a different model;
    return the inter-judge agreement rate.
    """
    raise NotImplementedError


def check_adversarial_fixtures(items: list[dict]) -> bool:
    """FR8.8 check #3: hand-built known-unsupported-claim cases from
    tests/fixtures/; True iff the judge marks all of them unfaithful.
    """
    raise NotImplementedError
