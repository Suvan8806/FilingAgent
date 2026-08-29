# Golden Set — Blank Authoring Worksheet

**Owner:** Suvan Kasina (human). **No LLM writes or verifies any field below.**
**Target output:** `data/golden.jsonl` — 25 items
**Governed by:** PRD.md FR8.1–FR8.3 · GOLDEN_SET.md
**This file is scratch.** It is not in the PRD §12 deliverable tree. Delete or gitignore before ship.

Fill this worksheet in plain text. Convert to JSONL **last** (§4). Do not author directly into JSONL.

---

## 1. Allocation

| Tier | Count | ID range | `answerable` | Fiscal-period probes |
|---|---|---|---|---|
| `multi_hop` | 4 | q001–q004 | `true` | 1 (mark it) |
| `numeric` | 8 | q005–q012 | `true` | 0 |
| `single_hop` | 10 | q013–q022 | `true` | 1 (mark it) |
| `unanswerable` | 3 | q023–q025 | `false` | 0 |
| **Total** | **25** | | | **2** ✓ FR8.3 |

IDs are grouped by tier here so tier counts are verifiable at a glance. They do not have to
be authored in this order — `multi_hop` first is recommended (hardest, carries the claim).

---

## 2. Retrieval checklist — what to pull, per tier

### 2.1 Every tier — open these first

| Source | Use |
|---|---|
| `data/reference/xbrl_facts.csv` | the **only** source for `expected_numeric`. Raw whole dollars. |
| `data/reference/XBRL_FACTS.md` | orientation only — **never copy from it**, its figures are rounded to billions |
| `data/reference/OUTLINES.md` | heading menu; `@n` = char offset into that section's plain text |
| `data/raw/manifest.json` | authoritative `period_of_report` per filing |
| `data/raw/*.htm` | the filing itself — where every prose answer gets verified |

---

### 2.2 `numeric` × 8 — pull from `xbrl_facts.csv`

For each of the 8 items, record these columns off **one CSV row**:

- [ ] `ticker`
- [ ] `fiscal_year`
- [ ] `period_end`
- [ ] `metric` — must be one of the 8 tracked
- [ ] `us_gaap_tag` — confirms the system will resolve the same tag you did
- [ ] `value_usd` — → `expected_numeric`, **copied digit-for-digit, raw units**
- [ ] `accn` — provenance if you need to re-check

**Rows to skip:** any row where `value_usd` is blank. The two MSFT `sga` rows are blank
(`(not reported)`) — MSFT does not tag a combined SG&A. Those belong in `unanswerable`, not here.

**Coverage constraints to track as you go — tick when satisfied across the 8:**

- [ ] both tickers appear
- [ ] both fiscal years appear
- [ ] ≥ 5 distinct `metric` values appear

**⚠ Trap to check before assigning `expected_sources` on this tier.**
The indexed corpus is only Item 1 / Item 1A / Item 7 (PRD §6). Not every XBRL metric appears
in those three sections — some live only in the financial statements (Item 8), which is **not
indexed**. If you assign `expected_sources` to a section that does not actually contain the
figure, `recall@5` scores 0 for that item by construction while `numeric_accuracy` passes, and
the table will look like a retrieval bug that isn't one.

For each numeric item, before filling `expected_sources`:

- [ ] Ctrl+F the figure in the filing `.htm`
- [ ] Confirm it appears inside Item 1, Item 1A, or Item 7
- [ ] If it does **not** → either choose a different metric, or leave `expected_sources: []`
      and note in this worksheet that recall is not meaningful for that item

---

### 2.3 `single_hop` × 10 — pull from `OUTLINES.md` + the filing

Per item:

- [ ] `ticker`, `fiscal_year`
- [ ] `section` — one of `item1` · `item1a` · `item7`
- [ ] exact heading text from `OUTLINES.md` (this is your Ctrl+F anchor)
- [ ] `@n` char offset (locates it if the heading text repeats)
- [ ] the specific paragraph you can point at as the answer — read it in the `.htm`

**Confusability check — do this per item, it is what makes the tier discriminating:**

- [ ] Does a near-identical heading exist in the **same company's other fiscal year**?
      (Compare the two `OUTLINE_{TICKER}_fy{YYYY}.md` files.) If yes, decide deliberately:
      either the question pins the year explicitly, or it is intentionally testing the
      year filter. Record which.
- [ ] Does a near-identical heading exist in the **other company's** filing?
      If yes, same decision for the ticker filter.

**Coverage constraints across the 10:**

- [ ] ≈ 5 per company
- [ ] all three sections represented
- [ ] both fiscal years represented
- [ ] exactly 1 item marked as the `single_hop` fiscal-period probe (§2.5)

---

### 2.4 `multi_hop` × 4 — both legs must exist before the question is valid

Per item, you need a **numeric leg** and a **prose leg**. If the prose leg does not exist,
it is arithmetic, not multi-hop — discard it.

Numeric leg — from `xbrl_facts.csv`, **two rows**:

- [ ] row A: ticker / fiscal_year / metric / `value_usd`
- [ ] row B: ticker / fiscal_year / metric / `value_usd`
- [ ] the delta or ratio you computed **by hand** from those two values

Prose leg — from the filing:

- [ ] `section` (Item 7 MD&A is where explanatory prose usually lives)
- [ ] heading text + `@n` from `OUTLINES.md`
- [ ] the paragraph that actually explains the change

**Convention decision — settle it once, apply to all 4, and write it down:**
`expected_numeric` holds a single value. For a change-over-time question, is that the
**later-year endpoint** or the **computed delta**? The PRD FR8.1 sample uses the endpoint.
Pick one; an eval harness cannot disambiguate this at runtime.

- [ ] Convention chosen and recorded here: ______________________

Also:

- [ ] exactly 1 of the 4 marked as the `multi_hop` fiscal-period probe (§2.5)
- [ ] each of the 4 genuinely requires >1 hop — re-read them cold and confirm a single
      tool call could not produce a complete answer (PRD §10 anti-criteria)

---

### 2.5 The 2 fiscal-period probes — extra pulls

These count toward their tiers' totals. Required by FR8.3. Pull all of this before writing:

- [ ] `period_of_report` for every filing involved, from `data/raw/manifest.json`
- [ ] the fiscal window each of those dates implies
- [ ] the **prose disclosure** that makes the misalignment or the week-count visible —
      locate it via the `Fiscal Period` heading in `OUTLINES.md` under the relevant Item 7,
      and read it in the `.htm`
- [ ] confirm the disclosure is inside an **indexed section** (Item 1 / 1A / 7) — if it is
      not retrievable, the probe cannot be answered from the corpus and becomes an
      accidental `unanswerable`

Record for each probe:

- [ ] which filings/windows are being compared
- [ ] what a **correct** answer must acknowledge about the period mismatch
- [ ] what a **naive** answer would report instead (this is the failure you are measuring)

---

### 2.6 `unanswerable` × 3 — three distinct kinds

- [ ] item 1 — **future / forward-looking**: information a 10-K does not contain by nature
- [ ] item 2 — **out of corpus**: an entity or fiscal year outside the 2-company × 2-year scope
- [ ] item 3 — **never tagged**: a figure the filer does not report as a single fact.
      Confirm the gap in `xbrl_facts.csv` — a blank `value_usd` is your evidence.

Per item:

- [ ] `answerable: false`
- [ ] `expected_numeric: null`
- [ ] `expected_answer` = what a correct **refusal** says, including *why* it cannot be answered
- [ ] `expected_sources: []`

**Convention decision — settle once, apply to all 3, write it down:**
Does `expected_tools` list the tool a correct route **attempts** before refusing (routing
analysis stays meaningful), or `[]` (nothing should be called)? Attempt-then-refuse is the
more realistic trace, but pick one and be consistent.

- [ ] Convention chosen and recorded here: ______________________

---

## 3. The 25 blank slots

Fill in plain text. Leave a field blank if it does not apply to the tier.

### `multi_hop` — q001–q004

```
id:                 q001
tier:               multi_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes (not exported to JSONL) ---
numeric leg A:      csv row ____
numeric leg B:      csv row ____
prose leg:          heading "____"  @____
hand-computed delta:
where I verified it:
```

```
id:                 q002
tier:               multi_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
numeric leg A:      csv row ____
numeric leg B:      csv row ____
prose leg:          heading "____"  @____
hand-computed delta:
where I verified it:
```

```
id:                 q003
tier:               multi_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
numeric leg A:      csv row ____
numeric leg B:      csv row ____
prose leg:          heading "____"  @____
hand-computed delta:
where I verified it:
```

```
id:                 q004
tier:               multi_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
numeric leg A:      csv row ____
numeric leg B:      csv row ____
prose leg:          heading "____"  @____
hand-computed delta:
where I verified it:
```

### `numeric` — q005–q012

```
id:                 q005
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q006
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q007
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q008
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q009
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q010
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q011
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

```
id:                 q012
tier:               numeric
question:
expected_answer:
expected_numeric:
numeric_tolerance:
expected_sources:   ticker=____  fy=____  section=____   [ ] figure confirmed present in an indexed section
expected_tools:
--- verification notes ---
csv row:            ____   metric=____   us_gaap_tag=____
digit-for-digit re-read:  [ ] done
units are raw dollars:    [ ] confirmed
```

**Numeric-tier coverage tally — complete after all 8:**
tickers used: ____  ·  fiscal years used: ____  ·  distinct metrics used: ____ (need ≥5)

### `single_hop` — q013–q022

```
id:                 q013
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q014
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q015
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q016
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q017
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q018
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q019
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q020
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q021
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

```
id:                 q022
tier:               single_hop
fiscal_period_probe: [ ] yes  [ ] no
question:
expected_answer:
expected_sources:   ticker=____  fy=____  section=____
expected_tools:
--- verification notes ---
heading:            "____"  @____
paragraph I am pointing at:
same heading in other FY?      [ ] yes → deliberate? ____   [ ] no
same heading in other ticker?  [ ] yes → deliberate? ____   [ ] no
```

**Single-hop coverage tally — complete after all 10:**
per company: MSFT ____ / AAPL ____  ·  sections used: ____  ·  fiscal years used: ____

### `unanswerable` — q023–q025

```
id:                 q023
tier:               unanswerable
kind:               [ ] future  [ ] out of corpus  [ ] never tagged
question:
expected_answer:    (the correct refusal, and why)
expected_numeric:   null
expected_sources:   []
expected_tools:
--- verification notes ---
evidence the corpus cannot answer it:
```

```
id:                 q024
tier:               unanswerable
kind:               [ ] future  [ ] out of corpus  [ ] never tagged
question:
expected_answer:    (the correct refusal, and why)
expected_numeric:   null
expected_sources:   []
expected_tools:
--- verification notes ---
evidence the corpus cannot answer it:
```

```
id:                 q025
tier:               unanswerable
kind:               [ ] future  [ ] out of corpus  [ ] never tagged
question:
expected_answer:    (the correct refusal, and why)
expected_numeric:   null
expected_sources:   []
expected_tools:
--- verification notes ---
evidence the corpus cannot answer it:
```

**Unanswerable tally:** all three `kind` boxes used exactly once? [ ] yes

---

## 4. `data/golden.jsonl` — format contract

One JSON object per line. No wrapping array. No trailing commas. UTF-8, LF endings.
The `--- verification notes ---` blocks above are worksheet-only and are **not** exported.

### 4.1 Field contract

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | always | `q001`–`q025`, zero-padded, unique |
| `question` | string | always | non-empty |
| `tier` | string | always | `single_hop` · `numeric` · `multi_hop` · `unanswerable` |
| `answerable` | boolean | always | `false` iff `tier == "unanswerable"` |
| `expected_answer` | string | always | prose; for `unanswerable`, the correct refusal |
| `expected_numeric` | number \| null | always present | raw units (dollars, not billions). `null` unless the item asserts a figure |
| `numeric_tolerance` | number \| null | always present | fractional — `0.01` = 1%. `0` for exactly-reported figures. `null` iff `expected_numeric` is `null` |
| `expected_sources` | array | always present | may be `[]`. Objects: `{ticker, fiscal_year, section}` |
| `expected_tools` | array of string | always present | may be `[]`. Values from `search_filings` · `lookup_financial` · `calculate` |

`expected_sources[]` object:

| Field | Type | Rule |
|---|---|---|
| `ticker` | string | `MSFT` \| `AAPL` |
| `fiscal_year` | integer | `2023` \| `2024` |
| `section` | string | `item1` \| `item1a` \| `item7` |

### 4.2 JSON Schema (draft 2020-12) — validate each line against this

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "GoldenItem",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "id", "question", "tier", "answerable", "expected_answer",
    "expected_numeric", "numeric_tolerance", "expected_sources", "expected_tools"
  ],
  "properties": {
    "id": { "type": "string", "pattern": "^q0(0[1-9]|[1-9][0-9])$" },
    "question": { "type": "string", "minLength": 1 },
    "tier": { "enum": ["single_hop", "numeric", "multi_hop", "unanswerable"] },
    "answerable": { "type": "boolean" },
    "expected_answer": { "type": "string", "minLength": 1 },
    "expected_numeric": { "type": ["number", "null"] },
    "numeric_tolerance": { "type": ["number", "null"], "minimum": 0 },
    "expected_sources": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["ticker", "fiscal_year", "section"],
        "properties": {
          "ticker": { "enum": ["MSFT", "AAPL"] },
          "fiscal_year": { "enum": [2023, 2024] },
          "section": { "enum": ["item1", "item1a", "item7"] }
        }
      }
    },
    "expected_tools": {
      "type": "array",
      "items": { "enum": ["search_filings", "lookup_financial", "calculate"] },
      "uniqueItems": true
    }
  },
  "allOf": [
    {
      "if": { "properties": { "tier": { "const": "unanswerable" } } },
      "then": {
        "properties": {
          "answerable": { "const": false },
          "expected_numeric": { "type": "null" }
        }
      },
      "else": { "properties": { "answerable": { "const": true } } }
    },
    {
      "if": { "properties": { "expected_numeric": { "type": "null" } } },
      "then": { "properties": { "numeric_tolerance": { "type": "null" } } },
      "else": { "properties": { "numeric_tolerance": { "type": "number" } } }
    }
  ]
}
```

### 4.3 Field shape by tier

| Tier | `expected_numeric` | `numeric_tolerance` | `expected_sources` | `answerable` |
|---|---|---|---|---|
| `single_hop` | `null` | `null` | ≥1 entry | `true` |
| `numeric` | number | number | ≥1 entry, or `[]` per §2.2 trap | `true` |
| `multi_hop` | number | number | ≥1 entry | `true` |
| `unanswerable` | `null` | `null` | `[]` | `false` |

### 4.4 File-level assertions (not expressible in per-line JSON Schema — check separately)

- [ ] exactly 25 lines
- [ ] `id` values are exactly `q001`–`q025`, no gaps, no duplicates
- [ ] tier counts: `single_hop` 10 · `numeric` 8 · `multi_hop` 4 · `unanswerable` 3
- [ ] exactly 3 items with `answerable: false`
- [ ] ≥2 fiscal-period probes (tracked in this worksheet — the JSONL has no field for it)
- [ ] every line parses as standalone JSON
- [ ] no line contains more than one object; no blank lines

---

## 5. Final gate — GOLDEN_SET.md checklist

- [ ] 25 entries, ids `q001`–`q025`
- [ ] tier counts exactly 10 / 8 / 4 / 3
- [ ] ≥2 questions probe fiscal-period misalignment
- [ ] every `expected_numeric` verified by eye against `xbrl_facts.csv` or the filing page
- [ ] all numerics in raw units, not billions
- [ ] every metric is one of the 8 tracked
- [ ] both companies and both fiscal years represented across tiers
- [ ] every `expected_sources` entry names a section that actually contains the answer
- [ ] `answerable: false` on exactly the 3 unanswerable items
- [ ] valid JSONL — one object per line, parses clean
- [ ] **you can say, for every single question, where the answer came from**
- [ ] no field in `golden.jsonl` was written or verified by an LLM (PRD FR8.2)
