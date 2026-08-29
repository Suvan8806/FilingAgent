# Golden Set — Authoring Instructions

**Owner:** Suvan Kasina (human). This task is not delegable. See "The hard rule" below.
**Output:** `data/golden.jsonl` — 25 questions
**Time:** ~2h45 of your time, plus ~55 min of agent prep that runs in parallel
**Referenced by:** PRD.md §7 FR8.2 · PLAN.md Wave 1, Lane H

---

## The hard rule

**No LLM writes or verifies a golden question or its answer.**

This isn't purity. The project's entire claim is *"I measured this."* If an LLM writes the questions and an LLM judges the answers, the eval measures agreement between two LLM passes over the same corpus — not system quality. A reviewer who runs evals will ask *"who wrote the golden set?"*, and "a human did" is the only answer that survives. Expect that question in an interview.

There is a sharper failure mode. An agent that writes `"expected_answer": "$29.5B"` produces a *plausible* number. If it's wrong, `numeric_accuracy` **silently inverts** — the system returns the correct answer and the eval scores it a miss. You won't catch it, because you'll be debugging retrieval when the bug is in the answer key.

An answer key you didn't verify is worse than no answer key.

---

## The split

There are two jobs here. One is delegable, one isn't.

### Delegate to an agent — mechanical, real time savings

- [ ] Fetch and extract the 4 filings (MSFT FY2023/FY2024, AAPL FY2023/FY2024) into `data/raw/`
- [ ] Pull XBRL `companyfacts` for both tickers; dump the 8 tracked metrics × 2 years into a flat table you can read at a glance
- [ ] Surface source material as a menu — e.g. *"here are the 12 risk-factor subsections in AAPL's Item 1A with their headings and character offsets"*
- [ ] Extract the MD&A (Item 7) subsection headings for each filing
- [ ] **After** you've written the questions: format your notes into valid JSONL, validate against the schema, check tier counts

### Keep for yourself — non-negotiable

- [ ] Which questions to ask
- [ ] Every `expected_answer`
- [ ] Verifying every number against the filing or the XBRL payload **with your own eyes**
- [ ] `expected_sources` and `expected_tools`

The delegated half is genuine savings — roughly 45 minutes of fetching and parsing. The kept half is the project.

---

## Before you start

The agent prep is **done**. Everything below is on disk:

| What | Where |
|---|---|
| The 4 filings (raw 10-K HTML) | `data/raw/{msft,aapl}_fy{2023,2024}_10k.htm` |
| Provenance — accession, period end, filing date, source URL | `data/raw/manifest.json` |
| Flat 8-metric fact table, rendered | `data/reference/XBRL_FACTS.md` |
| Same table, machine-readable (copy `expected_numeric` from here) | `data/reference/xbrl_facts.csv` |
| Raw SEC payloads | `data/reference/companyfacts_{msft,aapl}.json` |
| Section outlines, all four filings | `data/reference/OUTLINES.md` |
| Section outlines, one file per filing | `data/reference/OUTLINE_{TICKER}_fy{YYYY}.md` |

Have these open in tabs:

1. MSFT 10-K FY2024 and FY2023
2. AAPL 10-K FY2024 and FY2023
3. `XBRL_FACTS.md` (read) and `xbrl_facts.csv` (copy raw values from)
4. `OUTLINES.md` — the topic menu you pick `single_hop` questions from
5. A scratch file — write questions in plain text first, convert to JSONL last

Do **not** author directly into JSONL. You'll spend your attention on comma placement instead of on the questions.

### Fiscal year reference

Confirmed from EDGAR's `reportDate` for each filing (recorded in `data/raw/manifest.json`):

| Ticker | FY | Period end | Window | Filed |
|---|---|---|---|---|
| MSFT | 2023 | 2023-06-30 | Jul 2022 – Jun 2023 | 2023-07-27 |
| MSFT | 2024 | 2024-06-30 | Jul 2023 – Jun 2024 | 2024-07-30 |
| AAPL | 2023 | 2023-09-30 | Oct 2022 – Sep 2023 | 2023-11-03 |
| AAPL | 2024 | 2024-09-28 | Oct 2023 – Sep 2024 | 2024-11-01 |

Apple's fiscal year ends on the last Saturday of September, so the date moves — FY2023 ended the 30th, FY2024 the 28th.

**And the years are not the same length.** Both filings state it under "Fiscal Period" in Item 7:

> "The Company's fiscal years 2024 and 2022 spanned 52 weeks each, whereas fiscal year 2023 spanned 53 weeks."
> — AAPL FY2024 10-K, Item 7

So AAPL FY2023 has **an extra week of sales in it**. Its reported +2.02% FY2023→FY2024 revenue growth compares a 53-week year against a 52-week one, which understates the per-week change. Microsoft has no such effect — its fiscal year is a fixed June 30 year end.

This is the strongest fiscal-period material in the corpus. It is explicitly disclosed in prose, it is invisible in the XBRL numbers alone, and answering it correctly requires retrieving the disclosure *and* doing arithmetic. Use it.

**The point:** MSFT "FY2024" and AAPL "FY2024" describe windows offset by roughly a quarter. Any system that treats `2024` as an interchangeable filter token is silently comparing different periods.

### The 8 tracked metrics

revenue · R&D expense · operating income · net income · gross profit · total assets · operating cash flow · SG&A

Questions must stay inside this set — the XBRL tag map is hardcoded to exactly these (PRD FR1.7). The tags these two filers actually use are recorded in the `us_gaap_tag` column of `xbrl_facts.csv`.

> **SG&A is the exception — read this before writing an SG&A question.**
> AAPL tags `SellingGeneralAndAdministrativeExpense` directly. **MSFT does not tag a combined SG&A at all** — it reports `SellingAndMarketingExpense` and `GeneralAndAdministrativeExpense` as two separate line items. A single-tag SG&A lookup succeeds for AAPL and returns nothing for MSFT.
>
> That makes "compare MSFT and AAPL SG&A" a genuinely good `unanswerable` or refusal-shaped question. If instead you want a numeric answer for MSFT, you must sum the two components — and that sum is a *derivation*, not a reported fact. Say so in `expected_answer`.

---

## Schema

```json
{
  "id": "q014",
  "question": "How did Microsoft's R&D expense change from FY2023 to FY2024?",
  "tier": "multi_hop",
  "answerable": true,
  "expected_answer": "Increased from $27.2B to $29.5B, about 8.4%",
  "expected_numeric": 29510000000,
  "numeric_tolerance": 0.01,
  "expected_sources": [{"ticker": "MSFT", "fiscal_year": 2024, "section": "item7"}],
  "expected_tools": ["lookup_financial", "calculate"]
}
```

*(Values above are placeholders. Replace every one with a figure you verified.)*

| Field | Notes |
|---|---|
| `id` | `q001`–`q025`, zero-padded |
| `tier` | `single_hop` · `numeric` · `multi_hop` · `unanswerable` |
| `answerable` | `false` only for the `unanswerable` tier |
| `expected_answer` | Prose. What a correct answer says. Used by the judge for prose tiers. |
| `expected_numeric` | Numeric tiers only. **Raw units** (dollars, not billions). Deterministic assertion — no judge. |
| `numeric_tolerance` | Fractional. `0.01` = 1%. Use `0` for figures reported exactly. |
| `expected_sources` | Sections that *must* be retrievable for the answer. Drives `recall@5`. |
| `expected_tools` | Which tools a correct route calls. Drives routing analysis. |
| `section` | `item1` · `item1a` · `item7` |

---

## Tier recipes

### `numeric` — 8 questions, ~25 min

The cheap tier. Work **from the XBRL fact table, not from the prose** — it's the authoritative source and it's already structured.

- One metric, one company, one fiscal year. "What was AAPL's FY2024 total net sales?"
- Copy `expected_numeric` straight out of the fact table. Full precision, raw units.
- Spread across both companies, both years, and at least 5 distinct metrics.
- `expected_tools: ["lookup_financial"]`

> **Watch:** if the XBRL tag map and your answer key disagree, the map is what the system will return. Resolve the discrepancy now, not during Wave 3.

### `single_hop` — 10 questions, ~60 min

This is where the time goes, and it's the tier that determines whether the eval is meaningful.

- Open Item 1A (and some Item 1) for each company. Read the actual risk-factor headings.
- Write questions whose answer you can point at a specific paragraph.
- **Write from the page in front of you, never from memory of what 10-Ks generally say.** Specificity is what makes a question hard for a naive retriever. "What supply chain risk does Apple identify?" is weak. "What does Apple say about single-source component suppliers?" is a real question with a real answer paragraph.
- Roughly 5 per company. Vary sections.
- `expected_tools: ["search_filings"]`

### `multi_hop` — 4 questions, ~35 min

- Pick a year-over-year delta from your fact table that's actually interesting.
- Then **find the MD&A paragraph that explains it.** Both halves must exist before the question is valid — if there's no explanatory prose, it isn't multi-hop, it's arithmetic.
- Shape: "How did X change from FY2023 to FY2024, and how did management explain it?"
- `expected_tools: ["lookup_financial", "calculate", "search_filings"]`

> These 4 questions carry the project's central claim — `agent_custom` beating `baseline_tools` here is the evidence for agency. Make them genuinely require more than one hop.

### `unanswerable` — 3 questions, ~10 min

Fastest tier. Three distinct *kinds* of unanswerable:

1. **Future** — "What is MSFT's FY2027 revenue guidance?"
2. **Out of corpus** — a company you didn't index
3. **Never tagged** — a metric outside the 8, or a breakdown the filing doesn't give

Set `answerable: false`, leave `expected_numeric` null, and put the correct refusal in `expected_answer`.

### The 2 fiscal-period questions — ~15 min

Required by PRD FR8.3. Draw from the tiers above (they count toward those totals), but write them **deliberately**, not as an afterthought.

Shape: force a comparison between MSFT and AAPL "FY2024" where the underlying windows differ by a quarter. A correct answer notes the misalignment. A naive one silently compares Jul–Jun against Oct–Sep and reports a clean delta.

This pair is the highest-value content in the set. It's the thing that proves you touched real filings rather than a tutorial.

---

## Verification discipline

For every number you write down:

1. Find it in the XBRL payload **or** on the filing page
2. Read it once more, digit by digit, against what you typed
3. Confirm units — XBRL is in raw dollars; the filing narrative is often in millions
4. Confirm the fiscal year matches the window in your reference table

If you cannot point at where a number came from, delete the question. A question you're unsure about costs more than a question you don't have.

---

## The one legitimate shortcut

Have an agent produce candidate questions **without answers** — a menu you edit down.

Rejecting a bad prompt is cheap. Verifying a hallucinated number is not.

Even then: **discard anything you wouldn't have thought to ask.** An agent's questions cluster on what's textually prominent, which is exactly the distribution a retriever finds easy. Your hand-written questions will be harder, and the gap between arms is the thing you're measuring. A golden set that's easy for the baseline produces a table with nothing in it.

---

## Final checklist

- [ ] 25 entries, ids `q001`–`q025`
- [ ] Tier counts exactly: 10 `single_hop` / 8 `numeric` / 4 `multi_hop` / 3 `unanswerable`
- [ ] ≥2 questions probe fiscal-period misalignment
- [ ] Every `expected_numeric` verified by eye against XBRL or the filing page
- [ ] All numerics in raw units, not billions
- [ ] Every metric is one of the 8 tracked
- [ ] Both companies and both fiscal years represented across tiers
- [ ] Every `expected_sources` entry names a section that actually contains the answer
- [ ] `answerable: false` on exactly the 3 unanswerable items
- [ ] Valid JSONL — one object per line, no trailing comma, parses clean
- [ ] You can say, for every single question, where the answer came from

---

## Time budget

| Step | Owner | Time |
|---|---|---|
| Fetch filings + XBRL table + heading menus | agent | 45 min (parallel) |
| `numeric` × 8 | you | 25 min |
| `single_hop` × 10 | you | 60 min |
| `multi_hop` × 4 | you | 35 min |
| `unanswerable` × 3 | you | 10 min |
| Fiscal-period pair | you | 15 min |
| Verification pass | you | 20 min |
| Format to JSONL + validate | agent | 10 min |
| | **your total** | **~2h45** |

Start this before the Wave 1 agent lanes. Everything in Wave 3 blocks on it, and it's the only artifact in the project that no amount of parallelism speeds up.
