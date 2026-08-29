"""FilingAgent — frozen Pydantic contract (Wave 0).

Every model in this file is a load-bearing interface shared by all seven
Wave 1 lanes. **Contracts here are immutable after Wave 0** (PLAN.md, "Rules
for the rest of the project" #1). A lane that needs a shape change stops the
world and re-opens Wave 0 — it does not quietly diverge.

`GoldenItem` is validated against the real `data/golden.jsonl` (25 hand
-authored, human-verified items — see PLAN.md "Status" and PRD.md FR8.2) as
part of this same wave. The data is the source of truth: if a golden item
fails to parse, this file is wrong, not the data.

Field-convention decisions baked in here (PLAN.md "Contract decisions —
RESOLVED"):

1. The golden-item primary key is ``question_id`` (not ``id``).
2. For ``tier == "multi_hop"``, ``expected_numeric`` is the *delta* between
   the two fiscal years being compared, not the later-year endpoint.
3. ``expected_sources == []`` means "this fact is not retrievable from the
   indexed corpus" (e.g. it lives only in Item 8, which is not indexed —
   see PRD §6), not "no matching source exists to check against." Downstream,
   ``eval/metrics.py`` must exclude these items from the ``recall@5``
   denominator rather than counting them as retrieval misses.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- Shared literals ---------------------------------------------------------

Section = Literal["item1", "item1a", "item7"]
Mode = Literal["baseline_rag", "baseline_tools", "agent_custom", "agent_langgraph"]
ToolName = Literal["search_filings", "lookup_financial", "calculate"]
Tier = Literal["single_hop", "numeric", "multi_hop", "unanswerable"]
UnanswerableKind = Literal["future", "out_of_corpus", "never_tagged"]


# --- Corpus / retrieval -------------------------------------------------------


class Chunk(BaseModel):
    """One indexed unit of filing prose, as produced by src/chunking.py and
    persisted by src/store.py. `ticker` and `fiscal_year` are left as plain
    str/int (not Literal) rather than constrained to the current 2-company,
    2-year corpus, because `search_filings` must be able to construct and
    return a `Chunk` (or an empty result) without a Pydantic validation error
    when it is asked about tickers/years outside that scope — the *tool*
    handles that as a typed miss, not the schema.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    ticker: str
    fiscal_year: int
    fiscal_period_end: date
    section: Section
    chunk_id: str = Field(min_length=1, description="Deterministic, stable across re-ingestion (FR1.6 idempotency).")
    source_url: str
    filing_date: date


class Citation(BaseModel):
    """A pointer back to a `Chunk` that grounded part of an answer. Distinct
    from `Chunk` because a response should be able to cite evidence without
    re-serializing the full chunk text over the wire.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1)
    ticker: str
    fiscal_year: int
    section: Section
    source_url: str


# --- Structured facts ---------------------------------------------------------


class Fact(BaseModel):
    """A single normalized XBRL numeric fact, as produced by src/xbrl.py and
    served by `lookup_financial` via src/facts.py.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    metric: str
    fiscal_year: int
    fiscal_period_end: date
    value: float
    unit: str = Field(description='e.g. "USD".')


class Miss(BaseModel):
    """A typed absence for `lookup_financial`, returned instead of raising
    (FR4.4) so the agent can reason about absence rather than catching an
    exception. Covers: unknown ticker, unsupported fiscal year, and metrics
    that exist on one filer but are never XBRL-tagged on another (see
    data/reference/xbrl_facts.csv, MSFT `sga` rows — q025 in the golden set).
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    metric: str
    fiscal_year: int
    reason: str = Field(min_length=1, description="Human-readable cause, e.g. 'unknown ticker' or 'metric not tagged for this filer'.")


# --- Agent trace ---------------------------------------------------------


class ToolCall(BaseModel):
    """One recorded invocation of a tool during an agent/baseline run
    (FR4.2). Populates `QueryResponse.trace` and is persisted verbatim by
    src/traces.py for the `/stats` monitoring surface (FR7).
    """

    model_config = ConfigDict(extra="forbid")

    name: ToolName
    arguments: dict[str, Any]
    result_summary: str = Field(description="Short, human-readable summary of the result — not the full payload.")
    latency_ms: float = Field(ge=0)


# --- API contract ---------------------------------------------------------


class QueryRequest(BaseModel):
    """Body of POST /query (FR5)."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    mode: Mode


class QueryResponse(BaseModel):
    """Response body of POST /query (FR5)."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[Citation]
    trace: list[ToolCall]
    latency_ms: float = Field(ge=0)
    mode: Mode
    incomplete: bool = Field(description="True if the agent hit the turn cap (FR4.1) and the answer is partial.")
    refused: bool = Field(description="True if the system explicitly declined to answer rather than fabricate (G3, FR4.5).")


# --- Golden set ---------------------------------------------------------


class ExpectedSource(BaseModel):
    """One expected retrieval source for a golden item. Deliberately
    constrained to the fixed corpus (Literal ticker/fiscal_year/section)
    because this model only ever describes *golden-set expectations*, never
    live tool I/O — unlike `Chunk`/`Citation`, over-constraining it is safe
    and catches golden-set typos early.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: Literal["MSFT", "AAPL"]
    fiscal_year: Literal[2023, 2024]
    section: Section


class GoldenItem(BaseModel):
    """One row of data/golden.jsonl. **Must match the shipped file exactly**
    — see the validation script this schema was built against
    (tests/test_schemas.py, and the one-off check run in Wave 0). The data
    is authoritative; this schema follows it, never the other way round.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(pattern=r"^q\d{3}$")
    tier: Tier
    answerable: bool
    question: str = Field(min_length=1)
    expected_answer: str = Field(min_length=1)
    expected_numeric: int | float | None = None
    numeric_tolerance: float | None = None
    expected_sources: list[ExpectedSource] = Field(default_factory=list)
    expected_tools: list[ToolName]
    kind: UnanswerableKind | None = Field(
        default=None,
        description='Present only when tier == "unanswerable": future / out_of_corpus / never_tagged.',
    )

    @model_validator(mode="after")
    def _check_conventions(self) -> "GoldenItem":
        if self.answerable != (self.tier != "unanswerable"):
            raise ValueError(
                f"{self.question_id}: answerable must be False iff tier == 'unanswerable' "
                f"(got answerable={self.answerable}, tier={self.tier!r})"
            )
        if (self.expected_numeric is None) != (self.numeric_tolerance is None):
            raise ValueError(
                f"{self.question_id}: numeric_tolerance must be None iff expected_numeric is None "
                f"(got expected_numeric={self.expected_numeric!r}, numeric_tolerance={self.numeric_tolerance!r})"
            )
        if self.tier == "unanswerable" and self.kind is None:
            raise ValueError(f"{self.question_id}: unanswerable items must carry a 'kind'")
        if self.tier != "unanswerable" and self.kind is not None:
            raise ValueError(f"{self.question_id}: 'kind' is only valid on unanswerable items (tier={self.tier!r})")
        return self
