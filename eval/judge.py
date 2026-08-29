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

**FR8.5 — judge determinism (revised: `temperature: 0` is not a global
guarantee).** `temperature`/`top_p`/`top_k` were removed on Claude Opus
4.7+ and return a 400; Sonnet 5 rejects any non-default value. Sending
`temperature: 0` unconditionally would hard-fail on the Anthropic path. The
determinism stack, per the revised FR8.5:

1. **Pinned model ID**, read from env (`JUDGE_MODEL`). Any OpenAI-compatible
   provider works — `_OPENAI_COMPATIBLE` below maps the provider to its key
   variable and base URL, and `LLM_BASE_URL` overrides the URL for endpoints
   not in that table. Anthropic (`ANTHROPIC_MODEL`, e.g. `claude-opus-5`) is
   the alternate, and is separate because its wire format genuinely differs.
   **Re-verify the pinned model against the provider's current list before
   any live run** — free-tier availability shifts, and a previously pinned
   Groq model (`llama-3.3-70b-versatile`) was retired mid-build. See
   `.env.example` for the full set and `LLM_PROVIDER` for the switch.
2. **`temperature=0` only on the OpenAI-compatible path** (verified accepted
   there, including the local Ollama cross-model check). It is *never* sent
   on the Anthropic path, where non-default sampling params return a 400;
   the provider dispatch below is what keeps this from being a hardcoded
   global that hard-fails whenever `LLM_PROVIDER=anthropic`.
3. **JSON-only output**, requested via `response_format={"type":
   "json_schema", ...}` on providers that support structured outputs, with
   a fallback to `{"type": "json_object"}` plus an explicit schema spelled
   out in the system prompt for providers that only implement the latter.
   Either way, **the parsed verdict is validated here** (`_parse_judge_json`)
   — a provider's "guaranteed JSON" is never trusted blindly, and a
   malformed parse raises instead of being silently dropped.
4. **Content-hash fixture replay** — every judgment written to
   `eval/fixtures/judgments/`, keyed by a hash of (question, answer,
   context). **CI replays these fixtures and never calls the API** — this,
   not any sampling parameter, is what makes a red build mean a real
   regression (FR9.6). Live judging only happens via `make eval-live`.

**FR8.8 — judge-reliability check (replaces the old hand-agreement
check).** With the build fully agent-executed, an agent grading the judge
is the same model class judging itself and is not evidence. Three
automated checks instead, all implemented against this module:

1. **Determinism replay** — re-run the judge 3x at temperature 0 (on the
   OpenAI-compatible path; see point 2 above) on the same 5 items; assert
   byte-identical verdicts.
2. **Cross-model agreement** — a *different* model, running locally via
   Ollama (`OLLAMA_BASE_URL` / `OLLAMA_JUDGE_MODEL`), applies the same
   rubric to the same 5 items; report the inter-judge agreement rate. A
   different lab's weights is a stronger independence claim than two
   models from one vendor, and it is free, offline, and reproducible.
   **Optional** — skipped with a clear message (never a hard failure) when
   Ollama is unreachable, so CI and a fresh clone never break on it.
3. **Adversarial fixtures** — hand-built cases from tests/fixtures/ (never
   from the golden set) containing a known-unsupported claim; the judge
   must mark them unfaithful.

All three checks call live models directly (there is nothing to replay —
they are testing the judge itself, not being tested by it) and are meant
to be run on demand during Wave 3.3, not as part of the CI replay path.

**The README must describe check #2 as "cross-model judge agreement,"
never as "hand-verified"** — see PLAN.md "Status" and PRD FR8.8.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "eval" / "fixtures" / "judgments"

# --- provider config (PRD FR8.5, .env.example) ------------------------------

# "groq" (default, OpenAI-compatible endpoint) | "anthropic" (alternate).
# All four arms and the primary judge share one provider (PRD FR3 — a model
# difference between arms would confound the four-arm comparison); this
# module only cares about the judge's own provider choice.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq")

# OpenAI-compatible providers, mirroring `src/llm.py`'s `_OPENAI_COMPATIBLE`
# table. Kept as its own table rather than imported because FR8.8 allows the
# judge to run on a different provider than the arms — importing the arms'
# already-resolved constants would silently couple the two choices together.
_OPENAI_COMPATIBLE: dict[str, tuple[str, str]] = {
    # provider: (api-key env var, default base URL)
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
}

# Falls back to the groq entry for an unrecognized provider, so a typo surfaces
# as a clear missing-key error naming the expected variable rather than a
# KeyError on this table.
API_KEY_ENV, _DEFAULT_BASE_URL = _OPENAI_COMPATIBLE.get(LLM_PROVIDER, _OPENAI_COMPATIBLE["groq"])
BASE_URL = os.environ.get("LLM_BASE_URL") or _DEFAULT_BASE_URL

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# Provider-specific judge model env vars — the two providers' default models
# are unrelated strings, so one shared "JUDGE_MODEL" can't cover both.
# "llama-3.3-70b-versatile" is retired (shutdown date passed); the current
# documented Groq free-tier replacement is "openai/gpt-oss-120b" — verify
# against Groq's current model list before a live run regardless, since
# free-tier availability shifts (PRD FR8.5, .env.example).
JUDGE_MODEL_GROQ = os.environ.get("JUDGE_MODEL", "openai/gpt-oss-120b")
JUDGE_MODEL_ANTHROPIC = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

# temperature=0 is valid on the OpenAI-compatible path only (point 2 above) —
# never passed on the Anthropic path, where it would return a 400.
JUDGE_TEMPERATURE = 0

# Cross-model judge (FR8.8 #2): local, optional, skipped (not failed) if
# unreachable.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_JUDGE_MODEL = os.environ.get("OLLAMA_JUDGE_MODEL", "llama3.1:8b")


def _judge_model() -> str:
    return JUDGE_MODEL_GROQ if LLM_PROVIDER != "anthropic" else JUDGE_MODEL_ANTHROPIC


FAITHFULNESS_SYSTEM_PROMPT = """You are a strict faithfulness judge for a 10-K filing Q&A \
system. You will be given a question, an answer produced by the system, and the retrieved \
context the system had access to. Judge ONLY whether every factual claim in the answer is \
directly supported by the retrieved context — not whether the answer is well-written, \
complete, or independently true. If the context does not contain a claim the answer makes, \
that claim is unsupported, even if it happens to be true.

Respond with JSON only. No prose, no markdown code fences, no explanation outside the JSON \
object. The JSON object must have exactly these keys and types, matching this schema:
{"type": "object", "properties": {"faithful": {"type": "boolean"}, "unsupported_claims": \
{"type": "array", "items": {"type": "string"}}, "rationale": {"type": "string"}}, \
"required": ["faithful", "unsupported_claims", "rationale"], "additionalProperties": false}"""

FAITHFULNESS_JSON_SCHEMA = {
    "name": "faithfulness_verdict",
    "schema": {
        "type": "object",
        "properties": {
            "faithful": {"type": "boolean"},
            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": ["faithful", "unsupported_claims", "rationale"],
        "additionalProperties": False,
    },
}


class JudgeFixtureMissingError(RuntimeError):
    """Raised when replaying (live=False) and no recorded fixture exists
    for the given (question, answer, context) triple. This is meant to
    fail loudly — a missing fixture in CI is a real gap (a case that was
    never judged live and recorded), not something to silently skip or
    treat as passing (FR9.6: a red build must mean a real regression).
    """


# --- content-hash fixture cache (FR8.5) --------------------------------------


def _content_hash(question: str, answer: str, context: list[str]) -> str:
    canonical = json.dumps(
        {"question": question, "answer": answer, "context": list(context)},
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fixture_path(content_hash: str) -> Path:
    return FIXTURES_DIR / f"{content_hash}.json"


def _load_fixture(content_hash: str) -> dict | None:
    path = _fixture_path(content_hash)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_fixture(
    content_hash: str, question: str, answer: str, context: list[str], verdict: dict
) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "content_hash": content_hash,
        "provider": LLM_PROVIDER,
        "model": _judge_model(),
        "question": question,
        "answer": answer,
        "context": list(context),
        "verdict": verdict,
    }
    _fixture_path(content_hash).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


# --- live model calls (only reached under live=True / make eval-live) -------


def _build_user_prompt(question: str, answer: str, context: list[str]) -> str:
    context_block = "\n\n---\n\n".join(context) if context else "(no context retrieved)"
    return (
        f"Question:\n{question}\n\n"
        f"Answer to evaluate:\n{answer}\n\n"
        "Retrieved context (the ONLY source of truth — ignore outside knowledge):\n"
        f"{context_block}"
    )


def _parse_judge_json(raw_text: str) -> dict:
    try:
        verdict = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Judge did not return JSON-only output: {raw_text!r}") from exc

    if "faithful" not in verdict or not isinstance(verdict["faithful"], bool):
        raise ValueError(f"Judge JSON missing boolean 'faithful' field: {verdict!r}")

    verdict.setdefault("unsupported_claims", [])
    verdict.setdefault("rationale", "")
    return verdict


def _call_model_openai_compatible(
    model: str, question: str, answer: str, context: list[str], *, base_url: str, api_key: str
) -> dict:
    """Call an OpenAI-compatible chat completions endpoint (Groq or local
    Ollama). Lazily imports `openai` so importing this module never
    requires the SDK or network — only an actual live call does.
    """
    import openai  # noqa: PLC0415 - intentionally lazy, see docstring above

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    messages = [
        {"role": "system", "content": FAITHFULNESS_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(question, answer, context)},
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=JUDGE_TEMPERATURE,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": FAITHFULNESS_JSON_SCHEMA},
        )
    except Exception:
        # Not every OpenAI-compatible provider implements json_schema
        # structured outputs — fall back to the more widely supported
        # json_object mode. The schema is already spelled out in the system
        # prompt for this case. Either way the parse is validated below,
        # never trusted blindly (PRD FR8.5 point 3).
        response = client.chat.completions.create(
            model=model,
            temperature=JUDGE_TEMPERATURE,
            messages=messages,
            response_format={"type": "json_object"},
        )

    return _parse_judge_json(response.choices[0].message.content)


def _call_model_anthropic(model: str, question: str, answer: str, context: list[str]) -> dict:
    """Call the Anthropic Messages API. Lazily imports `anthropic` for the
    same reason `_call_model_openai_compatible` lazily imports `openai`.

    Deliberately does NOT set temperature/top_p/top_k (PRD FR8.5 revision:
    removed on Claude Opus 4.7+, rejected with a 400 on current models).
    """
    import anthropic  # noqa: PLC0415 - intentionally lazy, see docstring above

    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{ANTHROPIC_API_KEY_ENV} is required for live judging via Anthropic "
            "(see .env.example). Fixture-replay mode (live=False) does not need it."
        )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=FAITHFULNESS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(question, answer, context)}],
    )
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    return _parse_judge_json(raw_text)


def _call_primary_model(question: str, answer: str, context: list[str]) -> dict:
    """Provider dispatch for the primary judge — any OpenAI-compatible
    provider (default) or Anthropic (alternate), per LLM_PROVIDER
    (PRD FR8.5, .env.example).
    """
    model = _judge_model()
    if LLM_PROVIDER == "anthropic":
        return _call_model_anthropic(model, question, answer, context)

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_ENV} is required for live judging via {LLM_PROVIDER!r} "
            "(see .env.example). Fixture-replay mode (live=False) does not need it."
        )
    return _call_model_openai_compatible(model, question, answer, context, base_url=BASE_URL, api_key=api_key)


def _call_ollama_model(question: str, answer: str, context: list[str]) -> dict | None:
    """Cross-model judge call (FR8.8 #2) via local Ollama's OpenAI-compatible
    endpoint. Returns None — with a printed, explicit notice — if Ollama is
    unreachable, rather than raising: this check is optional and must never
    hard-fail CI or a fresh clone (PRD FR8.8, .env.example).
    """
    base_url = OLLAMA_BASE_URL.rstrip("/") + "/v1"
    try:
        return _call_model_openai_compatible(
            OLLAMA_JUDGE_MODEL,
            question,
            answer,
            context,
            base_url=base_url,
            api_key="ollama",  # Ollama's OpenAI-compatible endpoint ignores the key value
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure here means "skip", not "crash"
        print(f"[judge] Ollama unreachable at {base_url} ({exc!r}); skipping cross-model check for this item.")
        return None


# --- public API ----------------------------------------------------------------


def judge_faithfulness(
    question: str, answer: str, context: list[str], *, live: bool = False
) -> dict:
    """Score one (question, answer, context) triple for faithfulness.
    Returns a JSON-shaped dict (verdict + rationale). Looks up
    eval/fixtures/judgments/ first when running in replay mode (default);
    only calls the live provider (Groq by default, Anthropic as the
    alternate — see LLM_PROVIDER) under `make eval-live` (live=True), and
    records the result as a new fixture when it does.
    """
    content_hash = _content_hash(question, answer, context)

    if not live:
        cached = _load_fixture(content_hash)
        if cached is None:
            raise JudgeFixtureMissingError(
                f"No recorded judgment for content hash {content_hash} in {FIXTURES_DIR}. "
                "CI only replays recorded fixtures (FR8.5, FR9.6) — run `make eval-live` "
                "to record a live judgment for this (question, answer, context) triple."
            )
        return cached["verdict"]

    verdict = _call_primary_model(question, answer, context)
    _save_fixture(content_hash, question, answer, context, verdict)
    return verdict


def check_determinism(items: list[dict], n_runs: int = 3) -> bool:
    """FR8.8 check #1: re-run the judge n_runs times on the same items;
    True iff all runs produce byte-identical verdicts.

    Each item is a dict with "question", "answer", "context" keys. Always
    calls the live provider directly (never the fixture cache) — replaying
    a cached fixture n_runs times would trivially "pass" without
    exercising determinism at all.
    """
    for item in items:
        verdicts = []
        for _ in range(n_runs):
            verdict = _call_primary_model(item["question"], item["answer"], item["context"])
            verdicts.append(json.dumps(verdict, sort_keys=True))
        if len(set(verdicts)) != 1:
            return False
    return True


def check_cross_model_agreement(items: list[dict]) -> float:
    """FR8.8 check #2: apply the same rubric with a different model,
    running locally via Ollama; return the inter-judge agreement rate.

    Returns NaN (not 0.0) if Ollama was unreachable for every item — an
    undefined rate must never be reported as "0% agreement," which would
    misleadingly imply the judges actively disagreed.
    """
    if not items:
        return float("nan")

    agreements = 0
    scored = 0
    for item in items:
        primary = _call_primary_model(item["question"], item["answer"], item["context"])
        cross = _call_ollama_model(item["question"], item["answer"], item["context"])
        if cross is None:
            continue
        scored += 1
        if primary["faithful"] == cross["faithful"]:
            agreements += 1

    if scored == 0:
        print("[judge] Ollama unreachable for every item; cross-model agreement is undefined.")
        return float("nan")
    return agreements / scored


def _load_adversarial_fixtures() -> list[dict]:
    """Hand-built known-unsupported-claim cases, sourced from
    tests/fixtures/mini_corpus.json context (the frozen Wave 0 fixture) —
    **never from data/golden.jsonl** (FR8.8 #3 is explicit that the golden
    set must not be reused here, to avoid the judge being tuned to the
    same cases it is graded on). Each case pairs a real retrieved chunk
    with an answer asserting something that chunk does not say; a
    faithfulness judge that actually reads the context must mark all of
    these unfaithful.
    """
    corpus_path = REPO_ROOT / "tests" / "fixtures" / "mini_corpus.json"
    chunks = json.loads(corpus_path.read_text(encoding="utf-8"))
    chunks_by_id = {chunk["chunk_id"]: chunk["text"] for chunk in chunks}

    cases = [
        {
            "question": "What does Microsoft say about its operating segments?",
            "answer": (
                "Microsoft's Item 1 discloses that it operates a single unified reporting "
                "segment and does not break out Productivity and Business Processes, "
                "Intelligent Cloud, or More Personal Computing separately."
            ),
            "context_chunk_id": "MSFT_2023_item1_001",
        },
        {
            "question": "What quality or supply risks does Microsoft describe?",
            "answer": (
                "Microsoft states that it manufactures 100% of its hardware in-house and "
                "has zero reliance on third-party component suppliers, eliminating any "
                "quality or supply chain risk."
            ),
            "context_chunk_id": "MSFT_2023_item1a_001",
        },
    ]

    resolved = []
    for case in cases:
        chunk_id = case["context_chunk_id"]
        if chunk_id not in chunks_by_id:
            raise KeyError(
                f"Adversarial fixture references chunk_id {chunk_id!r}, not found in "
                f"{corpus_path} — the mini_corpus.json fixture may have changed shape."
            )
        resolved.append(
            {
                "question": case["question"],
                "answer": case["answer"],
                "context": [chunks_by_id[chunk_id]],
            }
        )
    return resolved


def check_adversarial_fixtures(items: list[dict] | None = None) -> bool:
    """FR8.8 check #3: hand-built known-unsupported-claim cases from
    tests/fixtures/; True iff the judge marks all of them unfaithful.
    Defaults to `_load_adversarial_fixtures()` when no items are supplied.
    """
    cases = items if items is not None else _load_adversarial_fixtures()
    for case in cases:
        verdict = _call_primary_model(case["question"], case["answer"], case["context"])
        if verdict["faithful"] is not False:
            return False
    return True
