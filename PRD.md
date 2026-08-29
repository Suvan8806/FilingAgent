# FilingAgent — Product Requirements Document

**Author:** Suvan Kasina
**Status:** Draft v2 (revised post-council)
**Target build time:** ~20 focused hours across 2 days
**Companion:** PLAN.md (parallel execution plan)

---

## 1. Summary

FilingAgent is a REST service that answers natural-language questions about public company SEC filings. It combines semantic retrieval over filing prose with structured lookup of reported financial figures, and uses an LLM tool-calling agent to decide which data source (or sequence of sources) a given question requires.

The deliverable is a containerized FastAPI service, a **four-arm controlled evaluation** with a deliberately un-confounded experimental design, and a CI pipeline that blocks regressions deterministically.

### What changed in v2

Six changes, each traceable to a specific failure the design review caught:

| # | Change | Why |
|---|---|---|
| 1 | Four eval arms, not two. Added `baseline_tools` — same tools, one call, no iteration | The two-arm design was **confounded**: the agent had XBRL access and the baseline didn't, so the numeric tier was unwinnable by construction. That measures tool access, not agency. |
| 2 | Numeric tier asserted deterministically, not judged by an LLM | Exact match against XBRL is free, unflaky, and correct. Judge prose only. |
| 3 | Judge outputs recorded to fixtures; CI replays them | Gating a public repo on live nondeterministic judge calls reds the build at random — an anti-signal on the most important artifact. |
| 4 | Corpus cut to 2 companies (MSFT, AAPL) | Funds the additions. The fiscal-period misalignment demo survives — MSFT's FY ends in June, Apple's in late September. |
| 5 | Added a LangGraph arm, public deploy, and a monitoring surface | Directly addresses named JD qualifications. See §5. |
| 6 | Added an explicit "what if the agent loses" clause | The v1 PRD committed publicly to a result it had not measured. |

---

## 2. Problem statement

A 10-K annual report contains two fundamentally different kinds of information:

- **Unstructured prose** — risk factors, management's discussion and analysis, business description. Answers questions like *"what does the company consider its biggest supply chain risk?"*
- **Structured numeric facts** — revenue, R&D expense, operating income, tagged in XBRL. Answers questions like *"what was FY2024 revenue?"*

A single-shot RAG pipeline handles the first category acceptably and the second category badly. It cannot do arithmetic, it cannot reliably filter to a specific fiscal year, and it will hallucinate numbers that appear near-but-not-exactly in retrieved chunks.

Many real questions need both, plus a computation joining them:

> *"How did Microsoft's R&D spending change from FY2023 to FY2024, and how did management explain the change?"*

This requires two numeric lookups, a subtraction, and a prose retrieval — in that order. The sequence cannot be hardcoded because it depends on the question.

### The fiscal-period trap

There is a second, subtler failure mode this corpus is chosen to expose. **Fiscal years are not calendar years, and they do not align across companies.** Microsoft's FY2024 covers July 2023 – June 2024. Apple's FY2024 covers October 2023 – September 2024. A naive retrieval system that treats "2024" as a filter token will silently return figures from a nine-month-offset window and present them as comparable.

This is a real production failure mode in financial data systems, it is invisible unless you have handled it, and the golden set contains questions specifically designed to punish it.

---

## 3. Goals

**G1.** Answer questions spanning prose, numeric, and mixed categories over a fixed corpus of SEC filings.

**G2.** Ground every answer in retrieved evidence, with citations identifying source document and section.

**G3.** Refuse to answer when the corpus does not contain the answer, rather than fabricating one.

**G4.** Quantify system quality against a hand-authored golden dataset using an **un-confounded four-arm design**, isolating the contribution of tool access from the contribution of iterative agency.

**G5.** Ship as a reproducible container with automated tests, a deterministic eval-gated CI pipeline, and a publicly reachable live instance.

### Non-goals

- Real-time filing ingestion or scheduled updates
- Multi-tenancy, user accounts, or persistent chat history
- A frontend beyond the auto-generated OpenAPI docs page
- Coverage beyond the fixed 2-company / 2-year corpus
- Financial advice of any kind — the service reports what filings say, nothing more

---

## 4. Users

**Primary:** the hiring manager reviewing the repo. **They will not clone it.** They will open the GitHub link, read the top third of the README, possibly click a live link, and close the tab. Roughly 90 seconds. Everything invisible in the top third of the README is worth approximately zero.

Design consequence: the README *is* the deliverable. The code is the evidence backing it.

**Secondary:** a senior engineer who does read `agent.py`, in a later interview round. Keep it legible.

**Tertiary:** you, in an interview, needing to explain a design decision and its measured consequence.

---

## 5. JD alignment

This project targets a specific posting (Python Developer Intern – RAG/Agentic AI). Mapping, so nothing named in the JD is left unaddressed:

| JD item | Where it's covered |
|---|---|
| RAG frameworks — LangChain/**LangGraph**/LlamaIndex/Haystack | `src/agent_langgraph.py` — fourth eval arm (§8) |
| LLMs, prompt engineering | Tool descriptions, judge rubric, system prompts; ADR 004 |
| Agentic architectures, tool calling, orchestration | `src/agent.py` — raw tool loop |
| Vector databases | ChromaDB with metadata filtering |
| FastAPI | `src/api.py` |
| Cloud deployment | Public free-tier deploy, live `/docs` link in README |
| Docker | Dockerfile + compose |
| Data ingestion/cleansing/transformation/indexing/retrieval | `src/ingest.py`, `src/chunking.py`, XBRL normalization |
| Evaluating model responses | Four-arm eval harness, LLM judge, hand-agreement check |
| **Monitoring** AI system performance | `/stats` rolling metrics over persisted traces (FR7) |
| Debugging/improving AI performance | `docs/adr/`, limitations section, retro |
| Testing, documentation | `tests/`, ADRs, README |
| Agile ceremonies, code review | Issue board, PR-per-lane, `docs/retro.md` |

---

## 6. Scope

### Corpus

| Item | Value |
|---|---|
| Companies | **2 — MSFT, AAPL** |
| Fiscal years | 2 per company (FY2023, FY2024) |
| Documents | 4 10-K filings |
| Source | SEC EDGAR (filings) + SEC XBRL `companyfacts` API (numeric) |

Raw filing text is committed to the repo under `data/raw/` so the build is reproducible without re-scraping.

> **EDGAR access note:** EDGAR requires a `User-Agent` header containing a real contact email or it returns 403, and enforces a 10 req/sec rate limit. Set this before writing any fetch code; it is the most common thirty-minute stall in this kind of project.

### Sections extracted

- Item 1 — Business
- Item 1A — Risk Factors
- Item 7 — Management's Discussion and Analysis

Other sections are skipped. They are mostly boilerplate and exhibits, and they bloat the index.

---

## 7. Functional requirements

### FR1 — Ingestion

- **FR1.1** Fetch 10-K documents for the configured tickers and years from EDGAR, with a compliant `User-Agent` and rate limiting.
- **FR1.2** Split each filing on section headers into the three sections above; fall back to fixed-size character chunking with overlap within a section that exceeds the chunk budget.
- **FR1.3** Attach metadata to every chunk: `ticker`, `fiscal_year`, `fiscal_period_end`, `section`, `filing_date`, `chunk_id`, `source_url`.
- **FR1.4** Embed chunks and persist to a local vector store.
- **FR1.5** Fetch XBRL company facts and load a normalized numeric table with columns `ticker`, `metric`, `fiscal_year`, `fiscal_period_end`, `value`, `unit`.
- **FR1.6** Ingestion is idempotent — re-running does not duplicate records.
- **FR1.7** The XBRL tag map is **hardcoded** for the ~8 metrics the golden set asks about. Do not build a general tag resolver. Tag selection (`Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax` vs `RevenueFromContractWithCustomerIncludingAssessedTax`) is a known multi-hour sink; resolve it by inspecting the actual `companyfacts` payload for these two companies and committing the mapping, not by generalizing.

### FR2 — Retrieval

- **FR2.1** Given a query, return top-k chunks ranked by embedding similarity.
- **FR2.2** Support optional hard filters on `ticker`, `fiscal_year`, and `section`.
- **FR2.3** Return each chunk with its metadata so the answer can cite it.

### FR3 — The four arms

All four arms answer the same questions through the same eval harness. This is the core of the experiment.

| Arm | Retrieval | Tools | Iteration | Isolates |
|---|---|---|---|---|
| `baseline_rag` | top-5 prose, single shot | none | none | naive RAG floor |
| `baseline_tools` | via tools | all 3 | **exactly 1 call** | effect of *tool access* |
| `agent_custom` | via tools | all 3 | up to 5 turns | effect of *iterative agency* |
| `agent_langgraph` | via tools | all 3 | up to 5 turns | effect of *framework* |

- **FR3.1** `baseline_rag` — single retrieval call, top-5 chunks, stuffed into a prompt with the question. System prompt instructs: answer only from context; if the context does not contain the answer, say so.
- **FR3.2** `baseline_tools` — identical tool schemas to the agent, but the loop is capped at one tool call and one synthesis step. **This is the confound control.** Without it, the numeric tier is unwinnable by the prose-only baseline and any agent "win" on that tier is a statement about tool access, not about agency.
- **FR3.3** All arms remain live and reachable via the `mode` parameter after ship. They are the control arms, not throwaway code.

### FR4 — Agent

The agent is an LLM tool-calling loop. It receives the question and a tool schema, and iterates until it produces a final answer or hits the turn cap.

**Tools:**

| Tool | Signature | Purpose |
|---|---|---|
| `search_filings` | `(query: str, ticker: str \| None, fiscal_year: int \| None, section: str \| None) -> list[Chunk]` | Semantic search with metadata filters |
| `lookup_financial` | `(ticker: str, metric: str, fiscal_year: int) -> Fact \| Miss` | Exact numeric fact from the XBRL table |
| `calculate` | `(expression: str) -> float` | Arithmetic on a restricted grammar |

- **FR4.1** Maximum 5 tool-calling turns per request; on exceeding, return a partial answer flagged as incomplete.
- **FR4.2** Every tool invocation is recorded in the response trace: tool name, arguments, result summary, latency.
- **FR4.3** `calculate` must not use raw `eval`. Restrict to a parsed arithmetic grammar over numeric literals via an AST node whitelist.
- **FR4.4** `lookup_financial` returns a **typed miss**, not an exception, when a fact is absent. The agent must be able to reason about absence.
- **FR4.5** If the tools return no supporting evidence, the agent must return an explicit "not found in corpus" response rather than answering from parametric knowledge.
- **FR4.6** `agent_langgraph` implements identical behavior over identical tools using LangGraph's `StateGraph`, so the arms differ only in orchestration mechanism.

### FR5 — API

| Endpoint | Method | Behavior |
|---|---|---|
| `/query` | POST | Body: `{question: str, mode: "baseline_rag" \| "baseline_tools" \| "agent_custom" \| "agent_langgraph"}`. Returns answer, citations, trace, latency. |
| `/healthz` | GET | Liveness + vector store reachability |
| `/stats` | GET | Corpus stats **and** rolling operational metrics (FR7) |
| `/docs` | GET | Auto-generated OpenAPI |

- **FR5.1** All request and response bodies are Pydantic models.
- **FR5.2** Structured JSON logging with a per-request trace ID.
- **FR5.3** Errors return typed responses, never raw stack traces.

### FR6 — Public deployment

- **FR6.1** A publicly reachable instance with a live `/docs` link at the top of the README.
- **FR6.2** The index is **baked into the image** at build time. Do not ingest at boot — free-tier instances will time out on a multi-minute ingest.
- **FR6.3 (required before the link is public)** The endpoint is unauthenticated and spends a real API key. It **must** ship with: a per-IP rate limit, a hard global daily request cap that degrades to a canned response, and a provider-side spend limit on the key. A public demo without a spend cap is a liability, not a portfolio piece.

### FR7 — Monitoring

- **FR7.1** Every `/query` invocation persists its trace to SQLite: question, mode, latency, tool calls, citation count, refusal flag, trace ID.
- **FR7.2** `/stats` reports, over recent traffic: request count by mode, rolling p50/p95 latency, tool-call distribution, refusal rate.
- **FR7.3** Persisted traces double as a golden-set feeder — low-confidence or refused live queries are candidates for future eval cases. Document this loop in the README; it is the "monitoring and improving AI system performance" objective made concrete.

### FR8 — Evaluation

- **FR8.1** A golden dataset of 25 hand-authored questions stored as JSONL:

```json
{
  "question_id": "q014",
  "question": "How did Microsoft's R&D expense change from FY2023 to FY2024?",
  "tier": "multi_hop",
  "answerable": true,
  "expected_answer": "Increased from $27.2B to $29.5B, about 8.5%",
  "expected_numeric": 2315000000,
  "numeric_tolerance": 0.01,
  "expected_sources": [{"ticker": "MSFT", "fiscal_year": 2024, "section": "item7"}],
  "expected_tools": ["lookup_financial", "calculate", "search_filings"]
}
```

**Field conventions — these bind `GoldenItem`, `run_eval.py`, and `metrics.py`.** The shipped
`data/golden.jsonl` already commits to all three; the contract follows the data.

| Convention | Rule |
|---|---|
| Key name | **`question_id`**, not `id` |
| Multi-hop `expected_numeric` | **the delta**, not the later-year endpoint (above: $29.51B − $27.195B) |
| `expected_sources: []` | means **"not retrievable from the indexed corpus"** — such items are **excluded from the `recall@5` denominator**, not counted as misses |

`unanswerable` items additionally carry `kind` ∈ `future` · `out_of_corpus` · `never_tagged`.

- **FR8.2** **The golden set is authored by hand, by a human, with every numeric answer verified against the actual filing page.** No LLM may generate or verify golden questions or answers. An LLM-authored set judged by an LLM is circular and renders every downstream number meaningless. This is a hard constraint, not a preference. Authoring procedure, tier recipes, verification discipline, and the delegable/non-delegable split are specified in [GOLDEN_SET.md](GOLDEN_SET.md).

  > **✅ SATISFIED.** `data/golden.jsonl` was authored by hand from `data/questions and answers.txt`, **before any source code existed** — nothing downstream could have contaminated it. Only the delegable half was automated: JSONL formatting, schema validation, tier-count checks, and a mechanical cross-check of every numeric answer against `data/reference/xbrl_facts.csv` (all 8 reconcile digit-for-digit; all 4 multi-hop deltas reconcile). One transcription error was caught this way and corrected against the filing (q018, Apple FY2024 Services gross margin 74.0% → 73.9%).
  >
  > This requirement is now **historical fact, not a pending task.** Agents may regenerate `golden.jsonl` from the human-authored source file; agents may **not** author new questions or edit any `expected_answer`.

- **FR8.3** Tier distribution:

| Tier | Count | Example |
|---|---|---|
| `single_hop` | 10 | "What does Apple list as a principal supply chain risk?" |
| `numeric` | 8 | "What was AAPL's FY2024 total net sales?" |
| `multi_hop` | 4 | The MSFT R&D question above |
| `unanswerable` | 3 | "What is MSFT's FY2027 revenue guidance?" |

At least two questions must specifically probe fiscal-period misalignment (e.g. comparing MSFT and AAPL "FY2024" figures, where the underlying windows differ by a quarter).

- **FR8.4** Metrics:

| Metric | Definition | Method |
|---|---|---|
| `recall@5` | Fraction where ≥1 retrieved chunk matches an expected source. **Items with `expected_sources: []` are excluded from the denominator** — see FR8.1 conventions | Deterministic |
| `numeric_accuracy` | Exact match within tolerance on the numeric tier | **Deterministic assertion against XBRL — not judged** |
| `faithfulness` | Every claim supported by retrieved context | LLM judge, prose tiers only |
| `refusal_accuracy` | Fraction of unanswerable questions correctly refused | Deterministic |
| `p50 / p95 latency` | Wall-clock per request | Deterministic |
| `avg_tool_calls` | Mean tool invocations | Deterministic |

Only `faithfulness` uses the judge. Everything else is computed without an LLM in the loop.

- **FR8.5** **Judge determinism.** The judge model is pinned, temperature 0, JSON-only output. Every judgment is written to `eval/fixtures/judgments/` keyed by a content hash of (question, answer, context). CI **replays** these fixtures rather than calling the API. Live judging runs on demand via `make eval-live`.

- **FR8.6** **Paired comparison.** All arms answer identical questions. Report per-item paired results and use McNemar's test for arm-vs-arm significance rather than treating arms as independent samples. Report per-tier confidence intervals.

- **FR8.7** **Honest reporting of n.** The README states the sample size plainly and does not claim significance the sample cannot support. At n=25 with 4 multi-hop and 3 unanswerable items, several metrics move in large discrete increments. Say so. A stated limitation is stronger than an unstated one a reviewer discovers.

- **FR8.8** **Judge-reliability check.** *(Revised — was "hand-agreement check.")* The original requirement had a human verify five judge outputs against the rubric. With the build fully agent-executed, an agent grading the judge is the same model class judging itself and is not evidence. Three automated checks replace it:

  1. **Determinism replay** — re-run the judge 3× at temperature 0 on the same 5 items; assert byte-identical verdicts. Catches a judge that is nondeterministic despite the pin.
  2. **Cross-model agreement** — a *different* model applies the same rubric to the same 5 items; report the inter-judge agreement rate.
  3. **Adversarial fixtures** — hand-built cases drawn from `tests/fixtures/` (never from the golden set) containing a known-unsupported claim. The judge must mark them unfaithful.

  **The README must report this as "cross-model judge agreement," never as "hand-verified."** Describing an automated check as a human one is exactly the overclaim this PRD's four-arm design exists to avoid, and it would undermine the FR8.2 claim that carries the project.

- **FR8.9** `run_eval.py` runs all arms and emits a markdown comparison table broken out by tier. Results are written to `results/eval_<timestamp>.json`; the latest table is pasted into the README.

### FR9 — Testing and CI

- **FR9.1** Unit tests for each tool, including failure paths (unknown ticker, missing year, malformed expression, division by zero).
- **FR9.2** Unit tests for section chunking against a fixture filing, covering both the header-split path and the character-chunk fallback.
- **FR9.3** Integration test hitting `/query` end-to-end in every mode with a stubbed LLM.
- **FR9.4** **CI fails if any `NotImplementedError` remains in `src/` or `eval/`.** This is the anti-zombie-stub guard that makes parallel construction safe.
- **FR9.5** GitHub Actions on push and PR: lint → pytest → replayed eval.
- **FR9.6** CI fails if `faithfulness` or `recall@5` drops below thresholds committed in `eval/thresholds.json`. Because CI replays recorded judgments, this gate is deterministic — a red build means a real regression, never a flaky judge call.

### FR10 — Packaging

- **FR10.1** Dockerfile for the API — slim base, cached dependency layer, non-root user, index baked in.
- **FR10.2** `docker-compose.yml` bringing up API + persisted Chroma volume.
- **FR10.3** `make ingest`, `make serve`, `make eval`, `make eval-live`, `make test`, `make lint`.
- **FR10.4** `.env.example` documenting every required variable. No secrets committed.

---

## 8. Non-functional requirements

| Requirement | Target |
|---|---|
| `baseline_rag` p95 latency | < 4s |
| `agent_custom` p95 latency | < 15s |
| Cold start to serving | < 30s given a pre-built index |
| Full ingestion runtime | < 10 min |
| Repo clone → passing tests | < 5 min, documented in README |
| CI wall-clock | < 3 min (replayed eval, no live API calls) |

---

## 9. Architecture

```
                    ┌─────────────────┐
   HTTP  ──────────▶│  FastAPI        │──────▶ trace store (SQLite)
                    │  /query         │           │
                    └────────┬────────┘           ▼
                             │                 /stats
        ┌────────────┬───────┴───────┬────────────────┐
        ▼            ▼               ▼                ▼
 ┌────────────┐ ┌──────────┐  ┌────────────┐  ┌──────────────┐
 │baseline_rag│ │baseline_ │  │agent_custom│  │agent_langgraph│
 │ retrieve→  │ │  tools   │  │ raw loop   │  │  StateGraph   │
 │    LLM     │ │ 1 call   │  │ ≤5 turns   │  │  ≤5 turns     │
 └─────┬──────┘ └────┬─────┘  └─────┬──────┘  └───────┬───────┘
       │             └──────┬───────┴─────────────────┘
       │                    ▼
       │        ┌───────────────────────┐
       └───────▶│ search_filings        │──▶ ChromaDB (chunks + metadata)
                │ lookup_financial      │──▶ SQLite facts (XBRL)
                │ calculate             │──▶ restricted AST grammar
                └───────────────────────┘
```

### Technology choices

| Layer | Choice | Rationale |
|---|---|---|
| API | FastAPI | Named in the target JD; Pydantic validation and free OpenAPI docs |
| Vector store | ChromaDB, local persistence | Zero-ops, metadata filtering, adequate at ~1.5k chunks |
| Numeric store | SQLite | Ships with Python, real SQL, no container needed |
| Embeddings | Chroma default ONNX MiniLM (local) | No second API key, no per-call cost, ~80MB, fits a free-tier instance. Keeps `docker compose up` genuinely one-command. |
| Agent (primary) | Direct tool-calling loop against the provider SDK | ~80 lines and fully legible |
| Agent (comparison) | LangGraph `StateGraph` | Same behavior, different orchestration — makes ADR 002 a measurement rather than an assertion |
| Eval | Custom harness | Full control over tier breakdown and paired comparison |
| CI | GitHub Actions | Free, visible on the repo |

For provider tool-calling semantics, consult the vendor's current documentation rather than memory — e.g. https://docs.claude.com/en/api/overview for the Claude API.

---

## 10. Success criteria

1. `docker compose up` serves working `/query` in all four modes.
2. A public URL serves `/docs` and answers a real question, with rate limiting and a spend cap in place.
3. `make eval` produces a four-arm, tier-broken-out comparison table with paired significance.
4. **`agent_custom` beats `baseline_tools` on the multi-hop tier** — this, not the gap against `baseline_rag`, is the claim about agency.
5. `refusal_accuracy` is at least 2/3 on the unanswerable tier.
6. CI is green, runs in under 3 minutes, and visibly gates on eval thresholds deterministically.
7. README opens with a plain-English result sentence, the architecture diagram, one concrete before/after example, and the results table.

### If the agent loses

The v1 PRD committed publicly to a result it had not measured. That is a real risk: on four clean filings with well-formed XBRL, `baseline_tools` may match `agent_custom` on everything except multi-hop, at a quarter of the latency.

**Pre-committed response:** report it. The README states what the agent did and did not improve, and the limitations section explains why — small corpus, well-structured source data, a question distribution that rarely requires more than two hops. A measured null result honestly reported is a stronger interview artifact than a win engineered by a rigged control arm, and it is the only version of this project that survives an informed reviewer asking "how do you know?"

Do not respond to a disappointing table by weakening `baseline_tools`.

### Anti-criteria — signs it went wrong

- The agent calls exactly one tool on every question. It is a RAG pipeline wearing a costume; the tool descriptions need work.
- `agent_custom` and `baseline_tools` score identically on multi-hop. Either the golden set's multi-hop questions are secretly single-hop, or the turn cap is never being reached.
- Faithfulness is 1.0. The judge prompt is broken.
- The agent beats `baseline_rag` on numeric by a huge margin and you report that as the headline. That gap is tool access, not agency, and it was guaranteed by construction.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| XBRL tag selection eats hours | Inspect the actual `companyfacts` payload for two companies, commit a hardcoded 8-metric map. Timebox 60 min. Do not generalize. |
| EDGAR HTML parsing eats hours | Compliant `User-Agent` set up front. Timebox to 90 min. Fall back to manually-downloaded filing text committed to `data/raw/`. |
| Section headers vary across filers | Regex with several variants plus a character-chunk fallback. Log which strategy fired per document. |
| LLM judge is noisy | Temperature 0, pinned model, rubric prompt, recorded fixtures, hand-check 5 judgments and report agreement. |
| Public demo drains the API key | Rate limit + daily cap + provider spend limit before the link goes public. Non-negotiable (FR6.3). |
| Parallel agents produce zombie stubs | CI fails on any remaining `NotImplementedError` (FR9.4). |
| Parallel agents cause interface drift | Contracts frozen before any implementation; one writer per file. See PLAN.md. |
| Commit history looks machine-generated | Squash each lane to one clean, well-messaged commit. |
| Day runs long | See cut order in PLAN.md. Never cut the golden set or the eval. |

---

## 12. Deliverables

```
filingagent/
├── README.md                       # live link, plain-English result, diagram, table
├── PRD.md
├── PLAN.md
├── docker-compose.yml
├── Dockerfile
├── Makefile
├── .env.example
├── .github/workflows/ci.yml
├── data/
│   ├── raw/                        # committed filing text
│   └── golden.jsonl                # 25 hand-authored questions — HUMAN ONLY
├── src/
│   ├── schemas.py                  # frozen contracts
│   ├── chunking.py
│   ├── ingest.py
│   ├── xbrl.py
│   ├── store.py
│   ├── facts.py
│   ├── tools.py
│   ├── baseline.py                 # baseline_rag + baseline_tools
│   ├── agent.py                    # raw tool loop
│   ├── agent_langgraph.py          # framework arm
│   ├── traces.py                   # monitoring persistence
│   └── api.py
├── eval/
│   ├── run_eval.py
│   ├── judge.py
│   ├── metrics.py                  # paired comparison, McNemar, CIs
│   ├── thresholds.json
│   └── fixtures/judgments/         # recorded judge outputs for CI replay
├── tests/
│   └── fixtures/                   # frozen 20-chunk corpus + 30-row fact table
└── docs/
    ├── adr/
    │   ├── 001-section-aware-chunking.md
    │   ├── 002-tool-loop-vs-langgraph.md
    │   ├── 003-eval-before-agent.md
    │   └── 004-four-arm-design.md   # why baseline_tools exists
    ├── examples/
    └── retro.md
```
