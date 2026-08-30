# FilingAgent

Does an agent that can take several tool-calling turns actually beat one that
gets exactly one call? This is a controlled four-arm comparison over SEC 10-K
filings, built to answer that honestly rather than to demo that it works.

**Runs entirely on your own machine.** No API keys, no accounts, no data
leaving the box — inference happens on your GPU through Ollama.

> **Note:** this README is provisional. The full write-up — results table,
> diagram, and limitations — lands in Wave 4 (see `PLAN.md`).

## Quickstart

```bash
# 1. Install Ollama (https://ollama.com), then build the model this app expects
make model    # == ollama pull qwen3:8b && ollama create filingagent-qwen3 -f Modelfile

# 2. Build and run
docker compose up --build

# 3. Open http://localhost:8000
```

That's it. The image bakes the vector index (469 chunks), the XBRL fact table
(32 rows), and the embedding model in at build time, so there's no ingestion
step and the container needs no network access at all.

> No `make` on Windows? Run the two commands in the comment above directly.

### Step 1 is not optional

`make model` doesn't just pull a model — it builds a variant with the context
window capped at 12288 tokens (see `Modelfile`). Stock `qwen3:8b` defaults to a
**40960**-token window and sizes its KV cache from that, turning a 5.2GB model
into an **11GB** reservation. On a 6GB card that isn't "slower", it's a hard
failure partway through a run:

```
HTTP 500: model requires more system memory (5.6 GiB) than is available (5.3 GiB)
```

Measured on an RTX 3050 6GB / 15.4GB RAM laptop:

| Context | Reservation | On CPU | Result |
|---|---|---|---|
| 40960 (stock) | 11.0 GB | 54% | 500 error under load |
| **12288 (`make model`)** | **7.2 GB** | **26%** | **13.5s per tool turn** |

The app also sets `LLM_REASONING_EFFORT=none`. qwen3 is a reasoning model: left
alone it emits a hidden thinking block before every answer, measured here at
**65.1s vs 5.0s** on an identical question — ~13x, on *every turn of every
arm*. It's applied identically to all four arms, so the comparison between them
is unaffected; only absolute wall-clock changes.

**Model choice matters more than it looks.** Three of the four arms are
tool-calling loops, and small models get tool *arguments* wrong even when they
correctly decide to call something. On the same prompt:

| Model | Tool call produced |
|---|---|
| `llama3.2:3b` | `{"ticker":"Microsoft","fiscal_year":"2024"}` — wrong symbol, year as string; every lookup returns a `Miss` |
| `qwen2.5-coder:7b` | emitted the call as *prose text* rather than a tool call — scores zero on every tool arm |
| `qwen3:8b` | `{"ticker":"MSFT","metric":"revenue","fiscal_year":2024}` — correct |

Use an 8B-class model with native tool calling, or the tool arms fail for
reasons that have nothing to do with their design.

If you want to change `num_ctx`, size it from the real workload: a retrieval
prompt is ~2,010 tokens (5 chunks at `baseline._TOP_K`), and the worst
realistic case is an agent spending all five turns on `search_filings` —
~11,000 tokens including the ~1,500-token tool schemas. 12288 clears that.
8192 would silently truncate it, which is worse than failing loudly: the arm
still answers, just from a context with the evidence cut off.

## The four arms

All four share one provider, one model, and one prompt. The only variable is
tool access and turn budget.

| Arm | Tools | Turns | What it isolates |
|---|---|---|---|
| `baseline_rag` | none | 1 | retrieval alone |
| `baseline_tools` | all 3 | **exactly 1** | tool *access*, without agency |
| `agent_custom` | all 3 | ≤ 5 | a hand-rolled tool loop |
| `agent_langgraph` | all 3 | ≤ 5 | the same loop, on LangGraph |

`baseline_tools` is the arm that matters. Without it, "the agent beat plain
RAG" would only show that tools help — true by construction, and not
interesting. The real claim is `agent_custom` > `baseline_tools` on multi-hop
questions.

The UI runs one question against several arms at once and puts their tool
traces side by side, so the difference between one call and a multi-turn loop
is visible rather than asserted.

## Evaluation

25 questions across four tiers (single-hop, numeric, multi-hop, unanswerable),
**hand-written by a human before any code existed** — which is what makes the
numbers mean anything. Numeric answers are asserted deterministically against
XBRL facts, never judged by a model. Scoring uses McNemar's paired test with
Wilson confidence intervals.

Two scoring rules that are easy to get wrong, and were:

- Multi-hop expected values are **deltas**, not endpoints.
- `recall@5` **excludes** items whose sources aren't in the indexed sections,
  rather than counting them as misses.

## Corpus

Apple and Microsoft 10-Ks, FY2023 and FY2024 — Items 1, 1A, and 7 only.
Filings are committed under `data/raw/`; nothing is scraped at build time.

## Development

```bash
pip install -r requirements.txt
cp .env.example .env      # defaults to local Ollama; no key needed
make ingest               # populate the vector store and fact table
make serve                # http://localhost:8000
make test                 # 429 tests
make lint
```

On Windows `make` may not be on PATH — use `mingw32-make`, or run the commands
from the `Makefile` directly.

To use a hosted provider instead of local inference, set `LLM_PROVIDER` and
that provider's key in `.env`. `src/llm.py` supports Ollama, Groq, Gemini,
Cerebras, Z.AI, and Anthropic; adding another is a table entry. See
`.env.example` for the free-tier limits measured on each — none of the hosted
free tiers can serve this workload, which is why local is the default.

## License

MIT
