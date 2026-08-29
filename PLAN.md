# FilingAgent — Parallel Build Plan (v3, all-agent)

**Companion to:** PRD.md v2
**Structure:** 5 waves. Waves 1 and 4 are wide-parallel. Waves 0, 2, and 3 are narrow or serial.
**Staffing:** every lane below is a Claude agent lane. The only remaining human step is Wave 5.

---

## Status

**Lane H (golden set) is COMPLETE and is the one artifact that was human-authored.** That
ordering is now permanent and is the project's central credibility claim — it happened before
any code existed, so nothing downstream could have contaminated it.

| Artifact | State |
|---|---|
| 4 filings + provenance | `data/raw/` + `manifest.json` ✅ |
| XBRL fact table | `data/reference/xbrl_facts.csv`, `XBRL_FACTS.md` ✅ |
| Section outlines | `data/reference/OUTLINES.md` + per-filing ✅ |
| Hand-authored Q&A source | `data/questions and answers.txt` ✅ **human-written** |
| **Golden set** | **`data/golden.jsonl` — 25 items ✅ verified** |

Verification on record: tier counts exactly 10 / 8 / 4 / 3 · 2 fiscal-period probes (q004
multi-hop, q022 single-hop) · all 8 numeric answers reconcile digit-for-digit against
`xbrl_facts.csv` · all 4 multi-hop deltas reconcile · q018 corrected to 73.9% against the
filing's Gross Margin table.

**Not started:** everything else. No `src/`, `eval/`, `tests/`. `README.md` is a stub.

### What changed in v3

The golden set landed early, so the human critical path is gone. Every remaining lane is an
agent lane. Two consequences that are *not* cosmetic:

1. **FR8.8 changes meaning.** The old "hand-agreement check — 5 judge outputs verified by
   hand" cannot be delegated without making the README claim false. Replaced by an automated
   **judge-reliability check** (§Wave 3.3). The README must describe it accurately and must
   **not** say "hand-verified."
2. **Wave 5 stays human.** An agent cannot apply to jobs or message a hiring manager on your
   behalf. That step is yours.

---

## Contract decisions — RESOLVED, apply verbatim in Wave 0

These were open. `data/golden.jsonl` already commits to all three, so the contract follows the
data, not the other way round. **PRD.md FR8.1's sample must be updated to match.**

| # | Decision | Consequence |
|---|---|---|
| 1 | Field is **`question_id`**, not `id` | `GoldenItem`, `eval/run_eval.py`, `eval/metrics.py` all use `question_id` |
| 2 | Multi-hop `expected_numeric` = **the delta**, `numeric_tolerance: 0.01` | Not the later-year endpoint. All 4 multi-hop answers are phrased as changes. |
| 3 | `expected_sources: []` means **"not retrievable"**, not "no match" | `recall@5` **excludes** these items from its denominator. q009/q010/q011 are affected. |

Decision 3 is the one that silently corrupts a metric if missed: those three figures exist
only in Item 8, which is not indexed (PRD §6). Counting them as misses would report a
retrieval bug that does not exist.

**Unanswerable items** additionally carry a `kind` field (`future` · `out_of_corpus` ·
`never_tagged`) not present in the PRD sample. `GoldenItem` must allow it.

---

## The one hard chain

```
ingest → populated store → real eval numbers → results table → README
```

Everything else is decoupled by the two Wave 0 artifacts: **frozen contracts** (code against
an interface, not an implementation) and a **frozen fixture** (real data at every boundary
before ingestion exists). The fixture is what unlocks parallelism — not the agent count.

### What still cannot be parallelized

- **The measurement run.** You cannot measure arms against an index that doesn't exist.
- **The README results table.** Downstream of measurement.
- **Wave 5.** Human.

---

## Wave 0 — Contract freeze (1 agent, serial, ~60 min)

**Nothing else starts until this lands.** One writer, one commit, no concurrency.

**Step by step:**

1. Repo skeleton: `.gitignore`, `pyproject.toml` (or `requirements.txt`), `.env.example`.
2. `src/schemas.py` — the frozen contract:
   - `Chunk(text, ticker, fiscal_year, fiscal_period_end, section, chunk_id, source_url, filing_date)`
   - `Fact(ticker, metric, fiscal_year, fiscal_period_end, value, unit)` · `Miss(ticker, metric, fiscal_year, reason)`
   - `ToolCall(name, arguments, result_summary, latency_ms)`
   - `Citation(chunk_id, ticker, fiscal_year, section, source_url)`
   - `QueryRequest(question, mode)`, mode ∈ `{baseline_rag, baseline_tools, agent_custom, agent_langgraph}`
   - `QueryResponse(answer, citations, trace, latency_ms, mode, incomplete, refused)`
   - `GoldenItem` — **must match `data/golden.jsonl` exactly**, including `question_id` and optional `kind`
3. **Validate the contract against real data immediately:** load all 25 lines of
   `data/golden.jsonl` through `GoldenItem`. If any line fails, the model is wrong — the data
   is the source of truth and is already verified.
4. `src/tool_schemas.py` — three tool JSON schemas, exact parameter names and types.
5. `eval/thresholds.json` — keys present, values `null` until measured.
6. `tests/fixtures/` — hand-built, committed, frozen:
   - `mini_corpus.json` — 20 chunks spanning both tickers, both years, all three sections
   - `mini_facts.csv` — 30 fact rows
   - `sample_10k_excerpt.txt` — one real filing excerpt for chunking tests
7. Stub every file in PRD §12's tree with a docstring and `raise NotImplementedError`.
8. `Makefile` with all targets declared (bodies may be TODO).
9. `.github/workflows/ci.yml` — lint + pytest + the `NotImplementedError` guard.
10. Update **PRD.md FR8.1's sample** to match the three resolved decisions above.

### The anti-zombie guard

```python
# tests/test_no_stubs.py
def test_no_unimplemented_stubs_remain():
    """Fails CI if any lane left a NotImplementedError behind."""
    hits = grep_for("NotImplementedError", paths=["src/", "eval/"])
    assert not hits, f"Unimplemented stubs: {hits}"
```

Mark `xfail` during Wave 1; flip to hard failure at the start of Wave 2.

### Rules for the rest of the project

1. **Contracts are immutable after Wave 0.** A lane needing a change stops the world.
2. **One writer per file. Ever.**
3. **No agent rewrites `data/golden.jsonl` content.** Regeneration from
   `data/questions and answers.txt` is fine; authoring new questions or editing answers is not.
4. **Each lane squashes to one clean commit.**

---

## Wave 1 — Wide parallel (7 agent lanes, ~4h wall-clock)

All seven run **concurrently**, every lane against `tests/fixtures/`, every lane ships its own
unit tests.

| Lane | Owns (exclusive write) | Deliverable |
|---|---|---|
| **A — Ingestion** | `src/chunking.py`, `src/ingest.py` | Section-header regex variants + char-chunk fallback; EDGAR fetch with compliant `User-Agent` + rate limit; emits `Chunk` |
| **B — Data layer** | `src/store.py`, `src/facts.py` | Chroma wrapper (`add_chunks`, `search(query,k,filters)`, deterministic `chunk_id` upsert); SQLite facts table returning `Fact \| Miss` |
| **C — XBRL** | `src/xbrl.py` | `companyfacts` fetch + **hardcoded 8-metric tag map**. The map is already derivable from `data/reference/xbrl_facts.csv` — read it, don't re-derive. Timebox 30 min. |
| **D — Tools** | `src/tools.py` | Three tools; `calculate` as AST-whitelist grammar; typed misses; **tool descriptions** (highest-leverage prose in the repo) |
| **E — Arms** | `src/baseline.py`, `src/agent.py` | `baseline_rag`, `baseline_tools` (MAX_TURNS=1), `agent_custom` (MAX_TURNS=5), one shared dispatch path |
| **F — Eval harness** | `eval/run_eval.py`, `eval/judge.py`, `eval/metrics.py` | Runs any arm; deterministic numeric assertion; pinned temp-0 judge with fixture recording; paired McNemar + per-tier CIs; markdown table emitter |
| **G — API + infra** | `src/api.py`, `src/traces.py`, `Dockerfile`, `docker-compose.yml`, deploy config | FastAPI, four modes via dispatch dict; JSON logging + trace IDs; SQLite trace persistence; `/stats`; rate limit + daily cap |

**Lane A note — filings are already committed.** `data/raw/` holds all four 10-Ks with
provenance in `manifest.json`. Lane A must parse from disk and treat live EDGAR fetch as a
refresh path only. Do not re-scrape during the build.

**Lane F carries three specific requirements** from the resolved decisions:
- `recall@5` denominator excludes items with `expected_sources: []`
- numeric tier asserted deterministically against `expected_numeric` ± `numeric_tolerance`, never judged
- multi-hop `expected_numeric` is a delta — the harness compares against a computed change, not an endpoint

### Wave 1 exit criteria

- Every lane's unit tests pass against fixtures
- No lane modified a file it doesn't own
- All 25 `golden.jsonl` items load cleanly through `GoldenItem`
- Each lane squashed and merged behind a green PR

---

## Wave 2 — Integration (3 agents → 1, ~2h) — IN PROGRESS

Widened from 2 lanes to 3. `agent_langgraph` was the only genuine stub left, and the cut-order
rule (§Cut order, item 1) permits dropping it *only if the build runs long* — it has not, and
`langgraph==0.2.45` was already pinned in Wave 0. Implementing it is what makes the Wave 2 exit
criterion "zero `NotImplementedError`" true without carving out an exemption.

| Step | Agent | Owns (exclusive write) | Work |
|---|---|---|---|
| 2.1 | Agent I | `Makefile`, `tests/test_no_stubs.py`, `src/tools.py` | Flip the guard to hard-fail. Wire real `store`/`facts` into `tools`. Run `make ingest` against `data/raw/`. Resolve interface drift. |
| 2.2 | Agent J *(concurrent)* | `src/api.py`, `src/traces.py`, `tests/test_api.py` | Wire all four modes into `/query`. Integration test every mode with a stubbed LLM. Verify traces persist, `/stats` populates, and the FR6.3 rails engage. |
| 2.3 | Agent K *(concurrent)* | `src/agent_langgraph.py` | Implement the fourth arm on LangGraph, behaviorally equivalent to `agent_custom` except for orchestration. |

Then **serially**: build the image with the index baked in, deploy to free tier, confirm public
`/docs` responds, and verify the rate limit and spend cap engage **before** the URL goes anywhere.

### Wave 2 pre-work, already landed

- **`.env` loading.** `src/llm.py` and `eval/judge.py` resolve provider, model, and API key at
  *module import time*, so a config module they import would run too late. `load_dotenv(...,
  override=False)` now lives in `src/__init__.py` and `eval/__init__.py`; `python-dotenv` is
  pinned. `override=False` keeps real env vars and `monkeypatch.setenv` authoritative, and makes
  the call a silent no-op in CI where no `.env` exists.
- **Groq verified live** — auth, tool calling, and strict-schema structured output all confirmed
  against `openai/gpt-oss-120b`. `temperature=0` is accepted on this path (it 400s on Anthropic;
  the provider split in `eval/judge.py` is correct).

### The guard was measuring the wrong thing

`tests/test_no_stubs.py` scanned for the *substring* `NotImplementedError`, which matches
`except NotImplementedError:` in `src/api.py` and plain docstring prose in `eval/run_eval.py`.
Flipping it to hard-fail unchanged would have produced three false failures and invited exactly
the wrong fix — an exemption list. It is being rewritten to detect genuine `raise` statements
via `ast`, so the guard fails only on real stubs.

### Exit criteria

- `make ingest` completes; **actual** chunk count reported, plus ~32 fact rows
- All four modes answer a real question end-to-end
- Public URL live, rate-limited, spend-capped
- Zero `NotImplementedError` in `src/` or `eval/`

> **On the chunk-count estimate.** The "~1000–1600 chunks" figure was an estimate made before
> ingestion existed; an exploratory run produced 469. The number is an observation, not a target
> — chunking must not be tuned to hit it. If 469 is right for four filings under section-aware
> chunking, the estimate was simply wrong and gets corrected here.

---

## Wave 3 — Measurement (serial, 1 agent + your review, ~1.5h)

**This is the experiment.** An agent executes it; the interpretation is reported, not spun.

**3.1 Run.** `make eval-live` across all four arms on the real index. Judge outputs recorded to
`eval/fixtures/judgments/`.

**3.2 Thresholds.** Set `eval/thresholds.json` floors slightly below measured scores. Confirm
CI replays fixtures and passes in under 3 minutes.

**3.3 Judge-reliability check — replaces the old hand-agreement check.**
The original FR8.8 required a human to verify 5 judge outputs. An agent grading the judge is
the same model class judging itself, so it is not evidence. Substitute three automated checks
that are real signal:

- **Determinism replay** — re-run the judge 3× at temperature 0 on the same 5 items; assert
  byte-identical verdicts. Catches a judge that is nondeterministic despite the pin.
- **Cross-model agreement** — have a *different* model apply the same rubric to the same 5
  items; report the agreement rate between the two judges.
- **Adversarial fixtures** — hand-built cases (from the frozen fixture, not the golden set)
  with a known-unsupported claim. The judge must mark them unfaithful. A judge that passes
  these cannot be trivially broken.

**The README must call this "cross-model judge agreement," not "hand-verified."** Reporting an
automated check as a human one is the kind of thing an informed reviewer catches, and it would
undermine the one claim the project is actually built on.

**3.4 Red-CI proof.** Push a deliberately broken retrieval filter to a branch, confirm CI goes
red, screenshot, revert.

### Read the table honestly

The claim is `agent_custom` > `baseline_tools` **on multi-hop**. The gap against `baseline_rag`
on numeric is tool access, guaranteed by construction, and is **not** evidence of agency.

If the agent doesn't win, execute PRD §10 "If the agent loses" — report it, explain it, keep
the control arm honest. **Do not weaken `baseline_tools`.**

---

## Wave 4 — Write-up and ship (4 agent lanes, ~2h)

| Lane | Owns | Deliverable |
|---|---|---|
| **K — README** | `README.md` | Live link at the very top. One plain-English result sentence above the table. One concrete before/after example. Diagram. Four-arm table. Quickstart. Honest `n=25` caveat. Limitations. |
| **L — ADRs** | `docs/adr/` | 001 section-aware chunking · 002 tool loop vs LangGraph, **framed as "at this scale, measured," never a verdict on frameworks** · 003 eval before agent · 004 why `baseline_tools` exists |
| **M — Examples** | `docs/examples/` | Committed request/response payloads showing full traces across modes |
| **N — Retro** | `docs/retro.md` | Estimated vs actual per wave; what parallel structure bought and cost |

**Lane K must state two things plainly:**
1. The golden set was hand-authored by a human before any code existed. This is the strongest
   sentence in the README — it is what makes every other number mean something.
2. Judge reliability was checked by cross-model agreement, not by hand.

### The two things reviewers actually notice

**The limitations section.** Everyone claims success; almost nobody states what their system
gets wrong. Name the real ones — including that `recall@5` is undefined for three numeric items
whose figures live in an unindexed section.

**Tone on ADR 002.** If the reviewing team runs LangChain daily, "frameworks are overhead"
reads as someone who doesn't know what he doesn't know. Measurement at a stated scale, with an
explicit "when I'd switch."

---

## Wave 5 — Distribution (HUMAN — 30 min, do not skip)

**No agent can do this step.** Applying and messaging as you would be impersonation.

- [ ] Apply, with the repo link in the application
- [ ] Message the hiring manager / recruiter directly with the live link
- [ ] Add to resume and portfolio site
- [ ] Apply to 10+ other listings with the same artifact — reusable for 18 months, not a single-shot bet
- [ ] Optional, high leverage: short write-up of the four-arm comparison

---

## Wall-clock summary

| Wave | Width | Wall-clock | Cumulative |
|---|---|---|---|
| ~~H — Golden set~~ | ✅ done | — | — |
| 0 — Contract freeze | 1 agent | 1.0h | 1.0h |
| 1 — Build | 7 agents | 4.0h | 5.0h |
| 2 — Integration | 2 → 1 | 2.0h | 7.0h |
| 3 — Measurement | 1 agent | 1.5h | 8.5h |
| 4 — Write-up | 4 agents | 2.0h | 10.5h |
| 5 — Distribution | you | 0.5h | **11.0h** |

---

## Risks and guards

| Risk | Guard |
|---|---|
| Zombie stubs | CI fails on any `NotImplementedError` (hard-fail from Wave 2) |
| Interface drift | Contracts frozen Wave 0; changes stop the world |
| Contract/data divergence | Wave 0 step 3 loads all 25 golden items through `GoldenItem` before anything else runs |
| `recall@5` misreported | Decision 3 — empty-source items excluded from the denominator, asserted in a Lane F unit test |
| Merge conflicts | One writer per file |
| Judge reliability overclaimed | Wave 3.3 automated checks; README wording audited in Lane K |
| Public demo drains the key | Rate limit + daily cap + provider spend limit before the link goes public |
| Commit history looks machine-authored | Squash each lane to one commit |

---

## Cut order, if it runs long

1. `agent_langgraph` arm — keep the import and a stub, drop the eval arm
2. `/stats` rolling metrics — keep trace persistence, drop aggregation
3. ADRs — a README design section covers it
4. `docs/examples/`

**Never cut:** the eval harness, `baseline_tools`, or the public link. Dropping `baseline_tools`
re-confounds the entire experiment and is the worst trade available here. The golden set is
already done and cannot be cut.

---

## Definition of done

- [ ] `docker compose up` → working `/query` in all four modes
- [ ] Public URL live, rate-limited, spend-capped, linked at the top of the README
- [ ] `make eval` → four-arm, tier-broken-out table with paired significance
- [ ] `agent_custom` vs `baseline_tools` on multi-hop reported honestly, win or lose
- [ ] `refusal_accuracy` ≥ 2/3
- [ ] CI green, deterministic, under 3 minutes, gating on thresholds
- [ ] Broken-filter red-CI screenshot captured
- [ ] README opens with live link, plain-English result, diagram, table
- [ ] README states the golden set was human-authored, and that judge agreement is cross-model
- [ ] Applied, and link sent directly
