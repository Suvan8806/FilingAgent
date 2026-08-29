# FilingAgent — Parallel Build Plan

**Companion to:** PRD.md v2
**Budget:** ~20 focused hours across 2 days
**Structure:** 5 waves. Waves 1 and 4 are wide-parallel. Waves 0, 2, and 3 are narrow or serial.

---

## The organizing idea

The v1 plan was six sequential blocks because it assumed one human doing one thing at a time. Most of those dependencies were false.

The real dependency graph has exactly **one hard chain**:

```
ingest → populated store → real eval numbers → results table → README
```

Everything else is decoupled by two artifacts, both created in Wave 0:

1. **Frozen contracts** — the Pydantic models, tool JSON schemas, and response envelope. Once these exist, every lane codes against an *interface* instead of waiting on an *implementation*.
2. **A frozen fixture** — a committed 20-chunk mini-corpus and 30-row fact table in `tests/fixtures/`. Every lane develops and tests against it without waiting on ingestion to finish.

The fixture is what unlocks parallelism. Not the agent count.

### What can never be parallelized

- **Golden-set authoring.** A human, in the filings, verifying numbers by eye. 2–3 hours. No agent may touch it (PRD FR8.2). It runs *concurrently* with Wave 1 but it is human-serial work. **Procedure: see [GOLDEN_SET.md](GOLDEN_SET.md).**
- **The measurement run.** You cannot measure arms against an index that doesn't exist yet.
- **The README results table.** Downstream of the measurement.

Everything else is a lane.

---

## Wave 0 — Contract freeze (serial, 1 agent + you, ~60 min)

**Nothing else starts until this lands.** One writer, one commit, no concurrency.

### Deliverables

- [ ] Repo skeleton, `.gitignore`, `pyproject.toml` / `requirements.txt`, `.env.example`
- [ ] `src/schemas.py` — **the frozen contract.** Pydantic models:
  - `Chunk(text, ticker, fiscal_year, fiscal_period_end, section, chunk_id, source_url, filing_date)`
  - `Fact(ticker, metric, fiscal_year, fiscal_period_end, value, unit)` and `Miss(ticker, metric, fiscal_year, reason)`
  - `ToolCall(name, arguments, result_summary, latency_ms)`
  - `Citation(chunk_id, ticker, fiscal_year, section, source_url)`
  - `QueryRequest(question, mode)` where mode ∈ `{baseline_rag, baseline_tools, agent_custom, agent_langgraph}`
  - `QueryResponse(answer, citations, trace, latency_ms, mode, incomplete, refused)`
  - `GoldenItem(...)` per PRD FR8.1
- [ ] `src/tool_schemas.py` — the three tool JSON schemas, exact parameter names and types
- [ ] `eval/thresholds.json` — keys present, values `null` until measured
- [ ] **`tests/fixtures/`** — hand-built, committed, frozen:
  - `mini_corpus.json` — 20 chunks spanning both tickers, both years, all three sections
  - `mini_facts.csv` — 30 fact rows (`ticker`, `metric`, `fiscal_year`, `fiscal_period_end`, `value`, `unit`)
  - `sample_10k_excerpt.txt` — one real filing excerpt for chunking tests
- [ ] Stub every file in the PRD's deliverable tree with a docstring and `raise NotImplementedError`
- [ ] `Makefile` with all targets declared (bodies may be TODO)
- [ ] `.github/workflows/ci.yml` — minimal: lint + pytest + **the `NotImplementedError` guard**
- [ ] GitHub Issues opened, one per lane, labeled as a sprint board

### The anti-zombie guard

Add this test in Wave 0. It is what makes parallel stub-first construction safe:

```python
# tests/test_no_stubs.py
def test_no_unimplemented_stubs_remain():
    """Fails CI if any lane left a NotImplementedError behind."""
    hits = grep_for("NotImplementedError", paths=["src/", "eval/"])
    assert not hits, f"Unimplemented stubs: {hits}"
```

Mark it `xfail` during Wave 1, flip it to a hard failure at the start of Wave 2.

### Rules that hold for the rest of the project

1. **Contracts are immutable after Wave 0.** A lane that needs a contract change stops the world: raise it, change it once, notify every lane. Do not let two lanes diverge on a shape.
2. **One writer per file. Ever.** No file appears in two lanes' ownership lists.
3. **No agent writes `data/golden.jsonl`.** Hard rule.
4. **Each lane squashes to one clean commit.** Forty machine commits in an hour tells a reviewer the AI built it.

---

## Wave 1 — Wide parallel (7 agent lanes + 1 human lane, ~4h wall-clock)

All eight run **concurrently**. Every lane develops against `tests/fixtures/`, not against real data. Every lane ships its own unit tests.

| Lane | Owns (exclusive write) | Depends on | Deliverable |
|---|---|---|---|
| **A — Ingestion** | `src/chunking.py`, `src/ingest.py`, `data/raw/` | contracts | Section-header regex variants + char-chunk fallback; EDGAR fetch with compliant `User-Agent` + rate limit; emits `Chunk` objects |
| **B — Data layer** | `src/store.py`, `src/facts.py` | contracts | Chroma wrapper (`add_chunks`, `search(query,k,filters)`, deterministic `chunk_id` upsert); SQLite facts table + query API returning `Fact \| Miss` |
| **C — XBRL** | `src/xbrl.py` | contracts | `companyfacts` fetch + the **hardcoded 8-metric tag map** for MSFT/AAPL. Timeboxed 60 min. Inspect the real payload; do not generalize. |
| **D — Tools** | `src/tools.py` | contracts | Three tools against the fixture store; `calculate` as an AST-whitelist grammar; typed misses; **tool descriptions** (highest-leverage prose in the repo) |
| **E — Arms** | `src/baseline.py`, `src/agent.py` | contracts | `baseline_rag`, `baseline_tools` (MAX_TURNS=1), `agent_custom` (MAX_TURNS=5). All three share one dispatch path. |
| **F — Eval harness** | `eval/run_eval.py`, `eval/judge.py`, `eval/metrics.py` | contracts | Runs any arm; deterministic numeric assertion; pinned temp-0 judge with fixture recording; paired McNemar + per-tier CIs; markdown table emitter |
| **G — API + infra** | `src/api.py`, `src/traces.py`, `Dockerfile`, `docker-compose.yml`, deploy config | contracts | FastAPI with all four modes stubbed through a dispatch dict; JSON logging + trace IDs; SQLite trace persistence; `/stats` rolling metrics; rate limit + daily cap |
| **H — Golden set** | `data/golden.jsonl` | **nothing** | **YOU. By hand. In the filings.** 25 questions, every number eyeballed against the actual page. ≥2 fiscal-period-misalignment questions. **Full procedure, tier recipes, and checklist: [GOLDEN_SET.md](GOLDEN_SET.md).** The prep half (fetch filings, dump the XBRL fact table, extract section headings) *is* delegable and should run as an agent task alongside. |

### Why these can genuinely run at once

Lane E needs Lane D's tools; Lane D needs Lane B's store; Lane F needs Lane E's arms. Under the v1 plan that's a three-deep serial chain. It isn't, because the contract fixes the shape of every boundary and the fixture provides real data at every boundary. Lane E imports `tools.search_filings`, gets the frozen signature, and tests against fixture returns. When Lane D lands, the import resolves to the real thing with no change at the call site.

### Lane H is the critical path

It is human-serial, it takes 2–3 hours, and everything in Wave 3 is blocked on it. **Start it first and do not let it slip.** If Wave 1's agent lanes finish and Lane H isn't done, the agents wait. That is the correct outcome — the golden set is the project.

### Wave 1 exit criteria

- Every lane's unit tests pass against fixtures
- No lane has modified a file it doesn't own
- `golden.jsonl` has 25 verified entries
- Each lane squashed and merged behind a green PR

---

## Wave 2 — Integration (narrow, 2 agents, ~2h)

Parallelism collapses here. This is where stubs become real.

| Step | Agent | Work |
|---|---|---|
| 2.1 | Agent I | Flip the `NotImplementedError` guard to hard-fail. Wire real `store`/`facts` into `tools`. Run `make ingest` against real filings. Resolve interface drift between lanes. |
| 2.2 | Agent J *(concurrent)* | Wire all four modes into `/query` for real. Integration test hitting every mode with a stubbed LLM. Verify traces persist and `/stats` populates. |

Then, **serially**: build the image with the index baked in, deploy to free tier, confirm the public `/docs` responds, and verify the rate limit and spend cap actually engage before the URL goes anywhere.

### Wave 2 exit criteria

- `make ingest` produces ~1000–1600 chunks across 2 tickers × 2 years, plus ~32 fact rows
- All four modes answer a real question end-to-end
- Public URL live, rate-limited, spend-capped
- Zero `NotImplementedError` in `src/` or `eval/`

---

## Wave 3 — Measurement (SERIAL — this is the experiment, ~1.5h)

**No parallelism. No agent judgment substituted for yours.**

- [ ] Run `make eval-live` across all four arms on the real index
- [ ] Judge outputs recorded to `eval/fixtures/judgments/`
- [ ] **Hand-verify 5 judge outputs against the rubric.** If the judge marks something supported that clearly isn't, fix the rubric and rerun — every downstream number depends on this. Record the agreement rate.
- [ ] Set `eval/thresholds.json` floors slightly below measured scores
- [ ] Confirm CI replays fixtures and passes in under 3 minutes
- [ ] Push a deliberately broken retrieval filter to a branch, confirm CI goes red, screenshot it, revert

### Read the table honestly

The claim the project makes is `agent_custom` > `baseline_tools` **on multi-hop**. The gap against `baseline_rag` on the numeric tier is tool access, guaranteed by construction, and is not evidence of agency.

If the agent doesn't win, execute PRD §10 "If the agent loses" — report it, explain it, keep the control arm honest. Do not weaken `baseline_tools`.

---

## Wave 4 — Write-up and ship (wide parallel again, 4 lanes, ~2h)

| Lane | Owns | Deliverable |
|---|---|---|
| **K — README** | `README.md` | Live link at the very top. **One plain-English sentence above the table** ("on 25 hand-written questions, the agent answered X where single-shot retrieval answered Y"). One concrete before/after example — a question the agent gets right and the baseline doesn't. Diagram. Four-arm table. Quickstart. Honest `n=25` caveat. Limitations. |
| **L — ADRs** | `docs/adr/` | 001 section-aware chunking; 002 tool loop vs LangGraph — **framed as "at this scale, measured," never as a verdict on frameworks**; 003 eval before agent; 004 why `baseline_tools` exists (the confound) |
| **M — Examples** | `docs/examples/` | Committed example request/response payloads showing full traces across modes |
| **N — Retro** | `docs/retro.md` | Estimated vs. actual per wave. What the parallel structure bought and where it cost. Honest. |

### The two things reviewers actually notice

**The limitations section.** Every applicant's README claims success; almost none states what the system gets wrong. "The agent still fails on questions requiring comparison across more than two fiscal years, because the tool loop caps at 5 turns" signals more than any green metric.

**Tone on ADR 002.** If the reviewing team runs LangChain daily, a repo concluding "frameworks are overhead" reads as someone who doesn't know what he doesn't know. Write it as a measurement at a stated scale, with an explicit "when I'd switch."

---

## Wave 5 — Distribution (30 min, and do not skip it)

The strongest argument raised in review was that hours spent polishing compete against hours spent distributing, and the plan never made that comparison.

- [ ] Apply, with the repo link in the application
- [ ] Message the hiring manager / recruiter directly with the live link
- [ ] Add to resume and portfolio site
- [ ] Apply to 10+ other listings with the same artifact — this project is reusable across every AI/ML application for the next 18 months, not a single-shot bet
- [ ] Optional, high leverage: a short write-up of the four-arm comparison. Almost no intern applicant has benchmarked their own agent loop against LangGraph on identical questions.

---

## Wall-clock summary

| Wave | Parallel width | Wall-clock | Cumulative |
|---|---|---|---|
| 0 — Contract freeze | 1 (serial) | 1.0h | 1.0h |
| 1 — Build | 7 agents + you | 4.0h | 5.0h |
| 2 — Integration | 2 → 1 | 2.0h | 7.0h |
| 3 — Measurement | 1 (serial) | 1.5h | 8.5h |
| 4 — Write-up | 4 agents | 2.0h | 10.5h |
| 5 — Distribution | 1 | 0.5h | **11.0h** |

~11 hours wall-clock against ~20 hours of work. The compression is real but it is **not** the 12-hour single-day plan from v1 — that estimate assumed serial execution *and* underestimated the work. Two sessions.

---

## Parallelization risks, and the guard for each

| Risk | Guard |
|---|---|
| Zombie stubs — a lane stubs something nobody unstubs | CI fails on any `NotImplementedError` (hard-fail from Wave 2) |
| Interface drift — two lanes assume different shapes | Contracts frozen in Wave 0; changes stop the world |
| Merge conflicts | One writer per file, exclusive ownership table above |
| Agent-generated golden set → circular eval | Hard rule: Lane H is human-only. This invalidates the project if violated. |
| Lanes finish but nothing integrates | Wave 2 exists specifically for this and is deliberately narrow |
| Commit history looks machine-authored | Squash each lane to one commit |
| Over-parallelizing the wrong thing | Wave 3 and Lane H are serial *by design*. Measurement and judgment don't parallelize. |

---

## Cut order, if it runs long

Cut from the bottom:

1. `agent_langgraph` arm — keep the LangGraph import and a stub so the dependency is real, drop the eval arm
2. `/stats` rolling metrics — keep trace persistence, drop the aggregation
3. ADRs — a design section in the README covers it
4. `docs/examples/`

**Never cut:** the golden set, the eval harness, `baseline_tools`, or the public link. Dropping `baseline_tools` to save twenty minutes re-confounds the entire experiment and is the single worst trade available in this plan.

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
- [ ] Judge hand-agreement rate stated
- [ ] Applied, and link sent directly

---

## Resume bullets to fill in afterward

> **FilingAgent — Agentic RAG over SEC Filings** | FastAPI, ChromaDB, SQLite, LangGraph, Docker, GitHub Actions
> - Designed a four-arm controlled evaluation isolating tool access from iterative agency, raising multi-hop faithfulness from 0.__ to 0.__ over a tool-equipped single-call control on a 25-question hand-authored golden set.
> - Benchmarked a hand-written tool-calling loop against an equivalent LangGraph implementation on identical questions, measuring __% latency difference at comparable answer quality.
> - Shipped a containerized FastAPI service with Pydantic-validated schemas, per-request trace IDs, persisted tool-call traces, and rolling p50/p95 latency and refusal-rate monitoring.
> - Made CI eval gating deterministic by recording and replaying pinned temperature-0 judge outputs, blocking retrieval regressions on every PR without nondeterministic API calls.

Fill the blanks from `results/`. Do not round generously — you will be asked about these numbers.
