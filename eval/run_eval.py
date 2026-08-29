"""Eval harness entrypoint (Lane F — PLAN.md Wave 1).

Owns: this file, eval/judge.py, and eval/metrics.py, exclusively.

Contract
--------
Runs any/all of the four arms (`baseline_rag`, `baseline_tools`,
`agent_custom`, `agent_langgraph`) against `data/golden.jsonl` and emits a
tier-broken-out markdown comparison table (FR8.9). Two modes:

- `make eval` — replays recorded judge fixtures from
  eval/fixtures/judgments/ (keyed by content hash of question+answer+
  context, FR8.5). No live API calls. This is what CI runs — deterministic,
  under 3 minutes (FR9.5, FR9.6).
- `make eval-live` — runs the full pipeline live, including live judge
  calls, and records new fixtures. This is the Wave 3 measurement run, not
  part of CI.

**Multi-hop `expected_numeric` is a delta, not an endpoint** (PLAN.md
Contract decision #2). For `tier == "multi_hop"` golden items, this module
must compare the arm's numeric answer against the *computed change between
the two fiscal years being compared*, not against the later year's raw
figure. Getting this backwards silently fails every multi-hop numeric
assertion.

Per-tier scoring:
- `numeric` and `multi_hop` numeric assertions: deterministic exact-match
  within `numeric_tolerance` of `expected_numeric` (never judged — FR8.4).
- `single_hop` / prose content: `faithfulness` via eval/judge.py only.
- `unanswerable`: deterministic refusal check (`response.refused`), scored
  as `refusal_accuracy`.

Output: `results/eval_<timestamp>.json` plus the markdown table (FR8.9); the
latest table is pasted into the README by Lane K in Wave 4.

Comparison: paired per-item results across arms, McNemar's test for arm-vs
-arm significance (not independent-sample stats — the arms answer identical
questions), per-tier confidence intervals (FR8.6). Report `n=25` honestly;
several metrics move in large discrete increments at this sample size and
the README must say so (FR8.7) — this module should make that easy to state
correctly (e.g. always report raw counts alongside any rate/percentage).
"""

from __future__ import annotations


def run_eval(mode: str, live: bool = False) -> dict:
    """Run one or all arms against data/golden.jsonl and return the full
    per-item, per-tier, and paired-comparison results. See module
    docstring for the full contract, especially the multi-hop delta
    convention and the recall@5 exclusion documented in
    eval/metrics.py.
    """
    raise NotImplementedError


def main() -> None:
    """CLI entrypoint for `make eval` / `make eval-live`."""
    raise NotImplementedError


if __name__ == "__main__":
    main()
