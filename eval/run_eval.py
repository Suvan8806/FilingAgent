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
Contract decision #2). For `tier == "multi_hop"` golden items,
`data/golden.jsonl`'s `expected_numeric` already *is* that delta (verified
against `data/reference/xbrl_facts.csv` per PLAN.md "Status"), so scoring
never needs to re-derive it from two separate facts. The part this module
owns is `_extract_numeric_value`: when a multi_hop answer states the change
directly (e.g. "increased $33.2 billion"), take that figure; when an answer
instead states two endpoint figures without stating a delta, fall back to
the **computed absolute difference** between them — never the bare
later-year endpoint. Getting this backwards silently fails every multi-hop
numeric assertion.

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
the README must say so (FR8.7) — this module states the sample size and
tier breakdown directly in the emitted table so that requirement is
satisfied by construction, not left to whoever pastes the table into the
README.

Arm dispatch — Wave 1 concurrency note
---------------------------------------
`src/baseline.py`, `src/agent.py`, and `src/agent_langgraph.py` (Lanes E and
the langgraph arm) are being written concurrently with this file and are
`NotImplementedError` stubs as of Wave 1. `_arm_dispatch()` imports them
lazily so that this module — and its pure-logic helpers — can be imported
and unit tested (see tests/test_metrics.py) without those implementations
existing yet. Actually invoking an arm before Wave 2 integration will raise
`NotImplementedError` from the arm itself, which is expected and correct
(PLAN.md's anti-zombie guard), not something this module should catch or
paper over.

Known interface gap (not owned by this file, cannot be fixed here): the
frozen `QueryResponse`/`Citation` schema (src/schemas.py) does not carry
raw chunk text on a citation, only `chunk_id` + metadata. The best
available proxy for "what the arm actually saw" for judge context is each
`search_filings` ToolCall's `result_summary` (see `_judge_context` below).
This is a real limitation worth naming in the README's limitations section
(PLAN.md Wave 4, Lane K), not something to silently work around by
reaching into src/store.py from here (out of this lane's write scope, and
would recouple the harness to retrieval internals).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from eval.judge import JudgeFixtureMissingError, judge_faithfulness
from eval.metrics import (
    mcnemar_test,
    numeric_accuracy_detail,
    recall_at_5_detail,
    refusal_accuracy_detail,
    wilson_confidence_interval,
)
from src.schemas import GoldenItem, QueryResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "data" / "golden.jsonl"
TIERS = ("single_hop", "numeric", "multi_hop", "unanswerable")

# Dollar figures in golden `expected_answer` prose look like "$211,915,000,000"
# or "$33.2 billion" / "$1,455,000,000". Match both forms.
_RAW_DOLLAR_RE = re.compile(r"\$?\s*([\d][\d,]*(?:\.\d+)?)\s*(billion|million|thousand)?", re.IGNORECASE)
_SCALE = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_DELTA_KEYWORDS = re.compile(
    r"(?:increase[ds]?|decrease[ds]?|change[ds]?|difference|grew|declined|higher|lower|by)",
    re.IGNORECASE,
)


# --- golden set loading ----------------------------------------------------------------


def _load_golden(path: Path = GOLDEN_PATH) -> list[GoldenItem]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(GoldenItem.model_validate(json.loads(line)))
    return items


def _tier_counts(golden: list[GoldenItem]) -> dict[str, int]:
    counts: dict[str, int] = {tier: 0 for tier in TIERS}
    for item in golden:
        counts[item.tier] = counts.get(item.tier, 0) + 1
    return counts


# --- numeric extraction ----------------------------------------------------------------


def _find_dollar_figures(text: str) -> list[float]:
    """Best-effort extraction of raw dollar amounts from free text, largest
    first. Not a general NLP number parser — deliberately narrow to the
    "$X" / "$X billion" / "X,XXX,XXX" shapes the golden answers and (we
    expect) the arms' answers actually use.
    """
    figures = []
    for match in _RAW_DOLLAR_RE.finditer(text):
        raw, scale = match.groups()
        if raw is None:
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if scale:
            value *= _SCALE[scale.lower()]
        # Skip small bare numbers unlikely to be a filing figure (percentages,
        # fiscal years, etc.) unless they were qualified with a scale word.
        if scale is None and value < 1000:
            continue
        figures.append(value)
    figures.sort(reverse=True)
    return figures


def _extract_numeric_value(answer: str, tier: str) -> float | None:
    """Deterministic, tier-aware extraction of the arm's numeric answer.

    - `numeric` tier: `expected_numeric` is a single raw fact — take the
      largest dollar figure mentioned (the filing figure, not an
      incidental number like a fiscal year).
    - `multi_hop` tier: `expected_numeric` is the DELTA between two fiscal
      years (PLAN.md Contract decision #2), never a bare endpoint. If the
      answer states the change using change-language ("increased $X",
      "a difference of $X"), take the figure nearest that keyword. If the
      answer instead states two endpoint figures without stating a delta,
      fall back to the **computed absolute difference** between the two
      largest figures mentioned — a computed difference, never a bare
      endpoint.
    """
    figures = _find_dollar_figures(answer)
    if not figures:
        return None

    if tier != "multi_hop":
        return figures[0]

    for keyword_match in _DELTA_KEYWORDS.finditer(answer):
        window = answer[keyword_match.end() : keyword_match.end() + 60]
        nearby = _find_dollar_figures(window)
        if nearby:
            return nearby[0]

    if len(figures) >= 2:
        return abs(figures[0] - figures[1])

    return figures[0]


# --- arm dispatch ----------------------------------------------------------------


def _arm_dispatch() -> dict[str, Callable[[str], QueryResponse]]:
    """Lazily import Lane E / langgraph arm implementations — see module
    docstring's "Arm dispatch" section for why this must stay lazy during
    Wave 1.
    """
    from src.agent import run_agent_custom
    from src.agent_langgraph import run_agent_langgraph
    from src.baseline import run_baseline_rag, run_baseline_tools

    return {
        "baseline_rag": run_baseline_rag,
        "baseline_tools": run_baseline_tools,
        "agent_custom": run_agent_custom,
        "agent_langgraph": run_agent_langgraph,
    }


# --- response -> result dict ----------------------------------------------------------------


def _citation_dicts(response: QueryResponse) -> list[dict]:
    return [
        {"ticker": c.ticker, "fiscal_year": c.fiscal_year, "section": c.section}
        for c in response.citations
    ]


def _judge_context(response: QueryResponse) -> list[str]:
    """QueryResponse carries no raw chunk text on a citation (see module
    docstring's "Known interface gap"). Use each search_filings ToolCall's
    result_summary as the best available proxy for retrieved context.
    """
    return [call.result_summary for call in response.trace if call.name == "search_filings"]


def _to_result_dict(item: GoldenItem, response: QueryResponse) -> dict:
    numeric_value = None
    if item.tier in ("numeric", "multi_hop"):
        numeric_value = _extract_numeric_value(response.answer, item.tier)

    return {
        "question_id": item.question_id,
        "citations": _citation_dicts(response),
        "numeric_value": numeric_value,
        "refused": response.refused,
        "faithful": None,
        "tool_calls": len(response.trace),
        "latency_ms": response.latency_ms,
        "answer": response.answer,
        "incomplete": response.incomplete,
    }


def _item_passed(item: GoldenItem, result: dict) -> bool:
    """Tier-appropriate pass/fail for a single scored item — the paired
    boolean McNemar's test operates on (FR8.6).
    """
    if not result:
        return False
    if item.tier == "unanswerable":
        return result.get("refused") is True
    if item.tier in ("numeric", "multi_hop"):
        if item.expected_numeric is None or result.get("numeric_value") is None:
            return False
        tolerance = item.numeric_tolerance or 0.0
        allowed = tolerance * abs(item.expected_numeric)
        return abs(result["numeric_value"] - item.expected_numeric) <= allowed
    # single_hop
    return result.get("faithful") is True


# --- scoring ----------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _tier_breakdown(results: list[dict], golden: list[GoldenItem]) -> dict:
    results_by_id = {r["question_id"]: r for r in results}
    breakdown = {}
    for tier in TIERS:
        items = [g for g in golden if g.tier == tier]
        n = len(items)
        passed = sum(1 for g in items if _item_passed(g, results_by_id.get(g.question_id)))
        lower, upper = wilson_confidence_interval(passed, n)
        breakdown[tier] = {"n": n, "passed": passed, "rate": passed / n if n else float("nan"), "ci_95": [lower, upper]}
    return breakdown


def _score_arm(results: list[dict], golden: list[GoldenItem]) -> dict:
    latencies = [r["latency_ms"] for r in results]
    tool_calls = [r["tool_calls"] for r in results]

    single_hop_ids = {g.question_id for g in golden if g.tier == "single_hop"}
    faithful_scored = [
        r for r in results if r["question_id"] in single_hop_ids and r.get("faithful") is not None
    ]
    faithful_correct = sum(1 for r in faithful_scored if r["faithful"])

    return {
        "recall_at_5": recall_at_5_detail(results, golden),
        "numeric_accuracy": numeric_accuracy_detail(results, golden),
        "refusal_accuracy": refusal_accuracy_detail(results, golden),
        "faithfulness": {
            "correct": faithful_correct,
            "scored": len(faithful_scored),
            "denominator": len(single_hop_ids),
            "rate": faithful_correct / len(faithful_scored) if faithful_scored else float("nan"),
        },
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "avg_tool_calls": statistics.fmean(tool_calls) if tool_calls else float("nan"),
        "tier_breakdown": _tier_breakdown(results, golden),
    }


def _pairwise_comparisons(
    arms: list[str], per_arm_pass: dict[str, dict[str, bool]], golden: list[GoldenItem]
) -> dict:
    comparisons = {}
    for i, arm_a in enumerate(arms):
        for arm_b in arms[i + 1 :]:
            key = f"{arm_a}_vs_{arm_b}"
            entry = {
                "overall": mcnemar_test(
                    [per_arm_pass[arm_a][g.question_id] for g in golden],
                    [per_arm_pass[arm_b][g.question_id] for g in golden],
                )
            }
            for tier in TIERS:
                tier_items = [g for g in golden if g.tier == tier]
                if not tier_items:
                    entry[tier] = None
                    continue
                entry[tier] = mcnemar_test(
                    [per_arm_pass[arm_a][g.question_id] for g in tier_items],
                    [per_arm_pass[arm_b][g.question_id] for g in tier_items],
                )
            comparisons[key] = entry
    return comparisons


# --- entrypoint ----------------------------------------------------------------


def run_eval(mode: str = "all", live: bool = False) -> dict:
    """Run one or all arms against data/golden.jsonl and return the full
    per-item, per-tier, and paired-comparison results. See module
    docstring for the full contract, especially the multi-hop delta
    convention and the recall@5 exclusion documented in
    eval/metrics.py.
    """
    golden = _load_golden()
    dispatch = _arm_dispatch()

    if mode == "all":
        arms = list(dispatch.keys())
    elif mode in dispatch:
        arms = [mode]
    else:
        raise ValueError(f"Unknown mode {mode!r}; expected one of {sorted(dispatch)} or 'all'")

    per_arm_results: dict[str, list[dict]] = {}
    per_arm_pass: dict[str, dict[str, bool]] = {}

    for arm in arms:
        handler = dispatch[arm]
        arm_results = []
        arm_pass = {}

        for item in golden:
            start = time.perf_counter()
            response = handler(item.question)
            if not response.latency_ms:
                response = response.model_copy(update={"latency_ms": (time.perf_counter() - start) * 1000})

            result = _to_result_dict(item, response)

            if item.tier == "single_hop":
                try:
                    verdict = judge_faithfulness(
                        item.question, response.answer, _judge_context(response), live=live
                    )
                    result["faithful"] = bool(verdict.get("faithful"))
                except JudgeFixtureMissingError:
                    result["faithful"] = None

            arm_results.append(result)
            arm_pass[item.question_id] = _item_passed(item, result)

        per_arm_results[arm] = arm_results
        per_arm_pass[arm] = arm_pass

    metrics = {arm: _score_arm(results, golden) for arm, results in per_arm_results.items()}
    comparisons = _pairwise_comparisons(arms, per_arm_pass, golden) if len(arms) > 1 else {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_items": len(golden),
        "tier_counts": _tier_counts(golden),
        "arms": arms,
        "metrics": metrics,
        "comparisons": comparisons,
        "per_item": per_arm_results,
    }


def _fmt_rate(detail: dict) -> str:
    if not detail or not detail.get("denominator"):
        return "n/a"
    rate = detail["rate"]
    if rate != rate:  # NaN
        return "n/a"
    return f"{rate:.0%} ({detail['correct']}/{detail['denominator']})"


def render_markdown_table(payload: dict) -> str:
    """Emit the FR8.9 markdown comparison table, broken out by tier, with
    the honest-n statement (FR8.7) stated directly in the output.
    """
    n = payload["n_items"]
    tc = payload["tier_counts"]
    lines = [
        f"# FilingAgent eval results (n={n})",
        "",
        (
            f"**Sample size: n={n}** "
            f"({tc.get('single_hop', 0)} single_hop, {tc.get('numeric', 0)} numeric, "
            f"{tc.get('multi_hop', 0)} multi_hop, {tc.get('unanswerable', 0)} unanswerable). "
            "At this size several metrics move in large discrete increments — e.g. one "
            "multi_hop item is 25 percentage points on that tier alone. Treat tier-level "
            "rates as directional; only the paired McNemar results below (with their own "
            "n and discordant-pair counts) support a significance claim, and only where "
            "n is large enough to say anything (FR8.7)."
        ),
        "",
        "| Arm | recall@5 | numeric_accuracy | faithfulness | refusal_accuracy | "
        "p50 (ms) | p95 (ms) | avg_tool_calls |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for arm, m in payload["metrics"].items():
        p50 = m["p50_latency_ms"]
        p95 = m["p95_latency_ms"]
        tools = m["avg_tool_calls"]
        lines.append(
            f"| {arm} | {_fmt_rate(m['recall_at_5'])} | {_fmt_rate(m['numeric_accuracy'])} | "
            f"{_fmt_rate(m['faithfulness'])} | {_fmt_rate(m['refusal_accuracy'])} | "
            f"{p50:.0f} | {p95:.0f} | {tools:.2f} |"
        )

    lines += ["", "## Per-tier breakdown", "", "| Arm | Tier | n | passed | rate | 95% CI |", "|---|---|---|---|---|---|"]
    for arm, m in payload["metrics"].items():
        for tier, tb in m["tier_breakdown"].items():
            if tb["n"]:
                ci = f"[{tb['ci_95'][0]:.2f}, {tb['ci_95'][1]:.2f}]"
                rate = f"{tb['rate']:.0%}"
            else:
                ci, rate = "n/a", "n/a"
            lines.append(f"| {arm} | {tier} | {tb['n']} | {tb['passed']} | {rate} | {ci} |")

    if payload["comparisons"]:
        lines += [
            "",
            "## Paired arm-vs-arm significance (exact McNemar)",
            "",
            "| Comparison | Tier | n | discordant (b/c) | p-value | sig @ .05 |",
            "|---|---|---|---|---|---|",
        ]
        for key, entry in payload["comparisons"].items():
            for tier, result in entry.items():
                if result is None:
                    continue
                lines.append(
                    f"| {key} | {tier} | {result['n']} | {result['b']}/{result['c']} | "
                    f"{result['p_value']:.4f} | {result['significant_at_0.05']} |"
                )

    return "\n".join(lines)


def main() -> None:
    """CLI entrypoint for `make eval` / `make eval-live`."""
    parser = argparse.ArgumentParser(description="FilingAgent four-arm eval harness (PRD FR8.9).")
    parser.add_argument(
        "--mode",
        default="all",
        help="Arm to run: baseline_rag | baseline_tools | agent_custom | agent_langgraph | all (default).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--replay",
        action="store_true",
        help="Replay recorded judge fixtures only (default, CI mode, FR9.5/FR9.6).",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Call the live LLM judge and record new fixtures (Wave 3 measurement run, not CI).",
    )
    args = parser.parse_args()

    payload = run_eval(mode=args.mode, live=args.live)

    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"eval_{timestamp}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(render_markdown_table(payload))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
