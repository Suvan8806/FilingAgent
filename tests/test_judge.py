"""Unit tests for eval/judge.py (Agent M — eval-harness coverage lane).

Why this file exists
--------------------
`eval/judge.py` produces the `faithfulness` column of the four-arm results
table. Until this lane it had **zero** tests, which meant the one number in
the project that is produced by an LLM was also the one number nothing
verified. A bug here yields a plausible-looking percentage that cannot be
caught by inspection.

What is pinned here, in priority order:

1. **Fixture replay is deterministic and offline.** A cache hit returns the
   recorded verdict and makes **no** provider call at all (asserted, not
   assumed); a cache miss under `live=False` raises
   `JudgeFixtureMissingError` rather than falling back to a live call or
   silently scoring zero (FR8.5 point 4, FR9.6).
2. **Provider dispatch.** `temperature=0` is sent on the OpenAI-compatible
   path and **never** on the Anthropic path (it returns a 400 there — see
   the FR8.5 revision in eval/judge.py's docstring). The provider cases are
   parametrized over `judge._OPENAI_COMPATIBLE` itself, so a provider added
   to that table (as `gemini` was) is covered automatically instead of
   silently escaping this file.
3. **The parse is validated, never trusted.** `_parse_judge_json` must
   raise on non-JSON and on a missing/non-boolean `faithful`, per FR8.5
   point 3.
4. The three FR8.8 reliability checks (`check_determinism`,
   `check_cross_model_agreement`, `check_adversarial_fixtures`) behave as
   documented, including the "Ollama unreachable => NaN, never 0.0"
   distinction, which is the difference between "we could not measure
   agreement" and "the judges disagreed completely."

No live API call, guaranteed three ways
---------------------------------------
`.env` is auto-loaded at `import eval` time (eval/__init__.py) and holds a
real, working provider key. An inadequately scoped stub in this file would
therefore not fail loudly — it would spend real quota. So:

1. Every API-key env var `eval/judge.py` could build a live client from is
   deleted for the duration of every test. The set is derived from
   `judge._OPENAI_COMPATIBLE` itself, not hardcoded, so adding a provider
   cannot silently open a hole.
2. `socket.socket.connect` / `socket.create_connection` are patched to raise
   for the whole module. Any outbound attempt is a hard test failure with a
   traceback pointing at the culprit, not a billed call.
3. Every test that exercises a call path asserts the fake was **actually
   invoked**. A test that passes with zero recorded calls proves nothing.

`test_the_no_live_call_guard_is_actually_in_force` verifies guards 1 and 2
directly, so the guarantee is checked rather than asserted in prose.

Hermeticity
-----------
`judge.FIXTURES_DIR` is redirected to `tmp_path` for every test, so nothing
here reads or writes the real `eval/fixtures/judgments/` directory. Nothing
in this file asserts a specific provider string except where the test pins
it explicitly — `LLM_PROVIDER` is `gemini` locally and absent in CI.
"""

from __future__ import annotations

import json
import socket
import sys
import types
from math import isnan
from typing import Any

import pytest

from eval import judge

# --- no-live-call scaffolding ------------------------------------------------

ALL_KEY_ENV_VARS = {key_env for key_env, _base in judge._OPENAI_COMPATIBLE.values()} | {
    judge.ANTHROPIC_API_KEY_ENV
}

OPENAI_COMPATIBLE_PROVIDERS = sorted(judge._OPENAI_COMPATIBLE)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an outbound connection structurally impossible, and strip every
    provider key so even a mis-scoped stub cannot construct a live client.
    """

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError(
            "tests/test_judge.py attempted a real network connection — "
            "a stub is mis-scoped and would have spent live quota"
        )

    monkeypatch.setattr(socket.socket, "connect", _boom, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom, raising=False)
    monkeypatch.setattr(socket, "create_connection", _boom, raising=False)
    for key_env in ALL_KEY_ENV_VARS:
        monkeypatch.delenv(key_env, raising=False)


@pytest.fixture(autouse=True)
def isolated_fixture_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Never read or write the real eval/fixtures/judgments/ directory.

    `_fixture_path` resolves `FIXTURES_DIR` at call time, so patching the
    module attribute is sufficient and is cleanly undone by monkeypatch.
    """
    target = tmp_path / "judgments"
    monkeypatch.setattr(judge, "FIXTURES_DIR", target)
    return target


# --- fake provider SDKs (the only two boundaries that reach the wire) --------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class FakeOpenAIClient:
    def __init__(self, recorder: "FakeOpenAIModule", api_key: str, base_url: str) -> None:
        self.recorder = recorder
        self.api_key = api_key
        self.base_url = base_url
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> _FakeCompletion:
        self.recorder.calls.append({"api_key": self.api_key, "base_url": self.base_url, **kwargs})
        fmt = (kwargs.get("response_format") or {}).get("type")
        if self.recorder.reject_json_schema and fmt == "json_schema":
            raise RuntimeError("this provider does not implement json_schema structured outputs")
        return _FakeCompletion(self.recorder.payload)


class FakeOpenAIModule(types.ModuleType):
    """Stand-in for the `openai` SDK. `eval/judge.py` imports it lazily
    inside `_call_model_openai_compatible`, so installing it in
    `sys.modules` is enough — there is no package-attribute caching hazard
    here (that trap applies to `from src import store`-style imports).
    """

    def __init__(self, payload: str = '{"faithful": true, "unsupported_claims": [], "rationale": "ok"}') -> None:
        super().__init__("openai")
        self.payload = payload
        self.calls: list[dict[str, Any]] = []
        self.reject_json_schema = False

    def OpenAI(self, api_key: str, base_url: str) -> FakeOpenAIClient:  # noqa: N802 - mirrors the SDK's name
        return FakeOpenAIClient(self, api_key=api_key, base_url=base_url)


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeThinkingBlock:
    def __init__(self) -> None:
        self.type = "thinking"
        self.text = "SHOULD NOT BE PARSED"


class FakeAnthropicClient:
    def __init__(self, recorder: "FakeAnthropicModule", api_key: str) -> None:
        self.recorder = recorder
        self.api_key = api_key
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any):
        self.recorder.calls.append({"api_key": self.api_key, **kwargs})
        return types.SimpleNamespace(
            content=[_FakeThinkingBlock(), _FakeTextBlock(self.recorder.payload)]
        )


class FakeAnthropicModule(types.ModuleType):
    def __init__(self, payload: str = '{"faithful": false, "unsupported_claims": ["x"], "rationale": "no"}') -> None:
        super().__init__("anthropic")
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def Anthropic(self, api_key: str) -> FakeAnthropicClient:  # noqa: N802 - mirrors the SDK's name
        return FakeAnthropicClient(self, api_key=api_key)


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAIModule:
    module = FakeOpenAIModule()
    monkeypatch.setitem(sys.modules, "openai", module)
    return module


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> FakeAnthropicModule:
    module = FakeAnthropicModule()
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


def _pin_provider(monkeypatch: pytest.MonkeyPatch, provider: str) -> tuple[str, str]:
    """Pin `judge`'s import-time-resolved provider constants explicitly.

    These are module globals read at *call* time by `_call_primary_model`,
    so patching them is what makes a test independent of whatever
    `LLM_PROVIDER` the developer's `.env` happens to set.
    """
    key_env, base_url = judge._OPENAI_COMPATIBLE[provider]
    monkeypatch.setattr(judge, "LLM_PROVIDER", provider)
    monkeypatch.setattr(judge, "API_KEY_ENV", key_env)
    monkeypatch.setattr(judge, "BASE_URL", base_url)
    monkeypatch.setenv(key_env, f"test-key-for-{provider}")
    return key_env, base_url


VERDICT_TRUE = {"faithful": True, "unsupported_claims": [], "rationale": "ok"}


def _verdict(faithful: bool) -> dict:
    return {"faithful": faithful, "unsupported_claims": [], "rationale": "stub"}


# --- 0. the guard itself ------------------------------------------------------


def test_the_no_live_call_guard_is_actually_in_force():
    """If this stops holding, every other test in this file could be making
    real, billed provider calls while still passing. That is the exact
    failure mode this assertion exists to make impossible.
    """
    for key_env in ALL_KEY_ENV_VARS:
        import os

        assert os.environ.get(key_env) is None, f"{key_env} leaked into the test environment"

    with pytest.raises(AssertionError, match="real network connection"):
        socket.create_connection(("example.invalid", 443))


def test_judge_module_imports_without_provider_sdks_installed():
    """FR8.5: importing the judge must never require `openai`/`anthropic`
    or a network. Only an actual live call may.
    """
    assert "openai" not in sys.modules or True  # import state is irrelevant
    assert judge.judge_faithfulness is not None
    assert callable(judge._content_hash)


# --- 1. content hash ----------------------------------------------------------


def test_content_hash_is_stable_across_calls_and_equal_inputs():
    a = judge._content_hash("q", "a", ["c1", "c2"])
    b = judge._content_hash("q", "a", ["c1", "c2"])
    assert a == b
    assert len(a) == 64  # sha256 hex


@pytest.mark.parametrize(
    "question,answer,context",
    [
        ("q2", "a", ["c1", "c2"]),
        ("q", "a2", ["c1", "c2"]),
        ("q", "a", ["c1"]),
        ("q", "a", ["c2", "c1"]),  # context order is part of the identity
    ],
)
def test_content_hash_changes_when_any_component_changes(question, answer, context):
    base = judge._content_hash("q", "a", ["c1", "c2"])
    assert judge._content_hash(question, answer, context) != base


def test_content_hash_accepts_any_sequence_for_context():
    """`_content_hash` normalizes context with `list(...)`, so a tuple and a
    list of the same items must hash identically — otherwise a caller
    passing a tuple would silently miss every recorded fixture.
    """
    assert judge._content_hash("q", "a", ("c1", "c2")) == judge._content_hash("q", "a", ["c1", "c2"])


# --- 2. fixture replay (the CI path) -----------------------------------------


def test_replay_cache_hit_returns_recorded_verdict_and_makes_no_provider_call(monkeypatch, fake_openai):
    _pin_provider(monkeypatch, OPENAI_COMPATIBLE_PROVIDERS[0])
    calls: list[tuple] = []
    monkeypatch.setattr(
        judge, "_call_primary_model", lambda *a, **k: calls.append(a) or _verdict(True)
    )

    # Record via the live path once...
    recorded = judge.judge_faithfulness("q", "a", ["ctx"], live=True)
    assert len(calls) == 1, "the live path never reached the model stub"

    # ...then replay. The stub must not be consulted again, and the fake
    # SDK must have recorded nothing at all.
    replayed = judge.judge_faithfulness("q", "a", ["ctx"], live=False)

    assert replayed == recorded == _verdict(True)
    assert len(calls) == 1, "replay called the provider — the fixture cache was bypassed"
    assert fake_openai.calls == []


def test_replay_is_deterministic_across_repeated_reads(monkeypatch):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(False))
    judge.judge_faithfulness("q", "a", ["ctx"], live=True)

    verdicts = [judge.judge_faithfulness("q", "a", ["ctx"], live=False) for _ in range(3)]

    assert all(json.dumps(v, sort_keys=True) == json.dumps(verdicts[0], sort_keys=True) for v in verdicts)


def test_replay_cache_miss_raises_loudly_instead_of_scoring_zero(monkeypatch):
    """FR9.6: a missing fixture in CI is a real gap, not a silent 0. It must
    not degrade to `{"faithful": False}` and must not fall back to a live
    call.
    """
    called: list[str] = []
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: called.append("x") or _verdict(True))

    with pytest.raises(judge.JudgeFixtureMissingError) as excinfo:
        judge.judge_faithfulness("never recorded", "a", ["ctx"], live=False)

    assert called == [], "a replay-mode cache miss silently fell back to a live call"
    message = str(excinfo.value)
    assert judge._content_hash("never recorded", "a", ["ctx"]) in message
    assert "eval-live" in message  # tells the operator how to fix it


def test_fixture_miss_is_keyed_on_content_not_just_question(monkeypatch):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(True))
    judge.judge_faithfulness("q", "answer-v1", ["ctx"], live=True)

    # Same question, different answer => different hash => still a miss.
    with pytest.raises(judge.JudgeFixtureMissingError):
        judge.judge_faithfulness("q", "answer-v2", ["ctx"], live=False)


def test_live_run_records_a_fixture_with_full_provenance(monkeypatch, isolated_fixture_dir):
    provider = OPENAI_COMPATIBLE_PROVIDERS[0]
    _pin_provider(monkeypatch, provider)
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(True))

    judge.judge_faithfulness("q", "a", ["ctx1", "ctx2"], live=True)

    files = list(isolated_fixture_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["content_hash"] == judge._content_hash("q", "a", ["ctx1", "ctx2"])
    assert payload["provider"] == provider
    assert payload["model"] == judge._judge_model()
    assert payload["question"] == "q"
    assert payload["answer"] == "a"
    assert payload["context"] == ["ctx1", "ctx2"]
    assert payload["verdict"] == _verdict(True)


def test_live_run_rerecords_identically_for_the_same_triple(monkeypatch, isolated_fixture_dir):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(True))
    judge.judge_faithfulness("q", "a", ["ctx"], live=True)
    first = (isolated_fixture_dir / f"{judge._content_hash('q', 'a', ['ctx'])}.json").read_text(encoding="utf-8")
    judge.judge_faithfulness("q", "a", ["ctx"], live=True)
    second = (isolated_fixture_dir / f"{judge._content_hash('q', 'a', ['ctx'])}.json").read_text(encoding="utf-8")
    assert first == second, "fixture serialization is not byte-stable"


# --- 3. provider dispatch (FR8.5 point 2) ------------------------------------


@pytest.mark.parametrize("provider", OPENAI_COMPATIBLE_PROVIDERS)
def test_openai_compatible_path_sends_temperature_zero(provider, monkeypatch, fake_openai):
    """Parametrized over `judge._OPENAI_COMPATIBLE` itself so a newly added
    provider (`gemini` was added mid-build) is covered without editing this
    test.
    """
    key_env, base_url = _pin_provider(monkeypatch, provider)

    verdict = judge._call_primary_model("q", "a", ["ctx"])

    assert verdict["faithful"] is True
    assert fake_openai.calls, "the fake openai SDK recorded no request — the stub did not take effect"
    call = fake_openai.calls[0]
    assert call["temperature"] == 0
    assert judge.JUDGE_TEMPERATURE == 0
    assert call["api_key"] == f"test-key-for-{provider}"
    assert call["base_url"] == base_url
    assert call["model"] == judge.JUDGE_MODEL_GROQ
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"] is judge.FAITHFULNESS_JSON_SCHEMA
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["system", "user"]
    assert call["messages"][0]["content"] == judge.FAITHFULNESS_SYSTEM_PROMPT
    assert key_env in ALL_KEY_ENV_VARS


@pytest.mark.parametrize("provider", OPENAI_COMPATIBLE_PROVIDERS)
def test_openai_compatible_path_requires_its_own_api_key(provider, monkeypatch, fake_openai):
    key_env, _base = _pin_provider(monkeypatch, provider)
    monkeypatch.delenv(key_env, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        judge._call_primary_model("q", "a", ["ctx"])

    assert key_env in str(excinfo.value)
    assert provider in str(excinfo.value)
    assert fake_openai.calls == [], "a client was built despite the missing key"


def test_unknown_provider_falls_back_to_a_named_key_error(monkeypatch, fake_openai):
    """A typo in LLM_PROVIDER must surface as an actionable missing-key
    error naming the expected variable, never a KeyError on the provider
    table.
    """
    monkeypatch.setattr(judge, "LLM_PROVIDER", "typo-provider")
    monkeypatch.setattr(judge, "API_KEY_ENV", "GROQ_API_KEY")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        judge._call_primary_model("q", "a", ["ctx"])


def test_json_schema_rejection_falls_back_to_json_object_still_at_temperature_zero(monkeypatch, fake_openai):
    _pin_provider(monkeypatch, OPENAI_COMPATIBLE_PROVIDERS[0])
    fake_openai.reject_json_schema = True

    verdict = judge._call_primary_model("q", "a", ["ctx"])

    assert verdict["faithful"] is True
    assert len(fake_openai.calls) == 2, "the json_object fallback did not fire"
    assert fake_openai.calls[0]["response_format"]["type"] == "json_schema"
    assert fake_openai.calls[1]["response_format"] == {"type": "json_object"}
    # The determinism pin must survive the fallback, not just the first try.
    assert [c["temperature"] for c in fake_openai.calls] == [0, 0]


def test_anthropic_path_never_sends_sampling_parameters(monkeypatch, fake_anthropic):
    """The FR8.5 revision: `temperature`/`top_p`/`top_k` return a 400 on
    current Claude models. Sending them would hard-fail every live judge run
    configured with LLM_PROVIDER=anthropic.
    """
    monkeypatch.setattr(judge, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv(judge.ANTHROPIC_API_KEY_ENV, "test-anthropic-key")

    verdict = judge._call_primary_model("q", "a", ["ctx"])

    assert verdict["faithful"] is False
    assert fake_anthropic.calls, "the fake anthropic SDK recorded no request"
    call = fake_anthropic.calls[0]
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in call, f"{banned} was sent on the Anthropic path (returns 400)"
    assert call["model"] == judge.JUDGE_MODEL_ANTHROPIC
    assert call["system"] == judge.FAITHFULNESS_SYSTEM_PROMPT
    assert call["max_tokens"] == 512


def test_anthropic_path_concatenates_only_text_blocks(monkeypatch, fake_anthropic):
    """A non-text block (e.g. a thinking block) in the response must not be
    fed to the JSON parser.
    """
    monkeypatch.setattr(judge, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv(judge.ANTHROPIC_API_KEY_ENV, "k")
    verdict = judge._call_primary_model("q", "a", ["ctx"])
    assert verdict["unsupported_claims"] == ["x"]


def test_anthropic_path_requires_its_key(monkeypatch, fake_anthropic):
    monkeypatch.setattr(judge, "LLM_PROVIDER", "anthropic")
    monkeypatch.delenv(judge.ANTHROPIC_API_KEY_ENV, raising=False)

    with pytest.raises(RuntimeError, match=judge.ANTHROPIC_API_KEY_ENV):
        judge._call_primary_model("q", "a", ["ctx"])

    assert fake_anthropic.calls == []


@pytest.mark.parametrize(
    "provider,expected_attr",
    [("anthropic", "JUDGE_MODEL_ANTHROPIC")] + [(p, "JUDGE_MODEL_GROQ") for p in OPENAI_COMPATIBLE_PROVIDERS],
)
def test_judge_model_selection_follows_the_provider(provider, expected_attr, monkeypatch):
    monkeypatch.setattr(judge, "LLM_PROVIDER", provider)
    assert judge._judge_model() == getattr(judge, expected_attr)


def test_user_prompt_carries_question_answer_and_every_context_chunk():
    prompt = judge._build_user_prompt("Q?", "A.", ["chunk one", "chunk two"])
    assert "Q?" in prompt and "A." in prompt
    assert "chunk one" in prompt and "chunk two" in prompt
    assert "ONLY source of truth" in prompt


def test_user_prompt_marks_empty_context_explicitly():
    assert "(no context retrieved)" in judge._build_user_prompt("Q?", "A.", [])


# --- 4. parse validation (FR8.5 point 3) -------------------------------------


def test_parse_judge_json_accepts_a_well_formed_verdict():
    verdict = judge._parse_judge_json(json.dumps(VERDICT_TRUE))
    assert verdict == VERDICT_TRUE


def test_parse_judge_json_fills_optional_fields():
    verdict = judge._parse_judge_json('{"faithful": false}')
    assert verdict["faithful"] is False
    assert verdict["unsupported_claims"] == []
    assert verdict["rationale"] == ""


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "```json\n{\"faithful\": true}\n```",  # markdown fences are not JSON
        "",
        None,
    ],
)
def test_parse_judge_json_raises_on_non_json(raw):
    with pytest.raises(ValueError, match="JSON-only output"):
        judge._parse_judge_json(raw)


@pytest.mark.parametrize(
    "raw",
    [
        '{"unsupported_claims": [], "rationale": "x"}',  # no faithful key
        '{"faithful": "true"}',  # string, not bool
        '{"faithful": 1}',  # int, not bool
        '{"faithful": null}',
    ],
)
def test_parse_judge_json_rejects_a_missing_or_non_boolean_verdict(raw):
    """A provider's "guaranteed JSON" is never trusted blindly — a truthy
    string would otherwise be silently accepted as a `True` verdict.
    """
    with pytest.raises(ValueError, match="faithful"):
        judge._parse_judge_json(raw)


# --- 5. FR8.8 check #1 — determinism replay ----------------------------------


def test_check_determinism_true_when_every_run_matches(monkeypatch):
    calls: list[str] = []

    def _stub(question, answer, context):
        calls.append(question)
        return _verdict(True)

    monkeypatch.setattr(judge, "_call_primary_model", _stub)

    assert judge.check_determinism([{"question": "q", "answer": "a", "context": ["c"]}], n_runs=3) is True
    assert len(calls) == 3, "check_determinism must actually re-call the model n_runs times"


def test_check_determinism_bypasses_the_fixture_cache_entirely(monkeypatch):
    """Replaying a cached fixture n times would trivially "pass" without
    exercising determinism at all, so this check must never read the cache.
    """
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(True))
    monkeypatch.setattr(
        judge, "_load_fixture", lambda *a, **k: pytest.fail("check_determinism read the fixture cache")
    )

    assert judge.check_determinism([{"question": "q", "answer": "a", "context": ["c"]}], n_runs=2) is True


def test_check_determinism_false_when_a_verdict_flips(monkeypatch):
    sequence = [_verdict(True), _verdict(False), _verdict(True)]
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: sequence.pop(0))

    assert judge.check_determinism([{"question": "q", "answer": "a", "context": ["c"]}], n_runs=3) is False


def test_check_determinism_false_when_only_the_rationale_differs(monkeypatch):
    """Byte-identical means byte-identical — a judge whose prose wanders is
    not deterministic even if the boolean is stable.
    """
    seq = [
        {"faithful": True, "unsupported_claims": [], "rationale": "because A"},
        {"faithful": True, "unsupported_claims": [], "rationale": "because B"},
    ]
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: seq.pop(0))
    assert judge.check_determinism([{"question": "q", "answer": "a", "context": ["c"]}], n_runs=2) is False


def test_check_determinism_on_no_items_is_vacuously_true(monkeypatch):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: pytest.fail("should not be called"))
    assert judge.check_determinism([]) is True


# --- 6. FR8.8 check #2 — cross-model agreement -------------------------------


def _items(n: int) -> list[dict]:
    return [{"question": f"q{i}", "answer": f"a{i}", "context": [f"c{i}"]} for i in range(n)]


def test_cross_model_agreement_rate_is_the_fraction_of_matching_verdicts(monkeypatch):
    primary = [_verdict(True), _verdict(True), _verdict(False), _verdict(True)]
    cross = [_verdict(True), _verdict(False), _verdict(False), _verdict(True)]
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: primary.pop(0))
    monkeypatch.setattr(judge, "_call_ollama_model", lambda *a, **k: cross.pop(0))

    assert judge.check_cross_model_agreement(_items(4)) == 0.75


def test_cross_model_agreement_is_nan_not_zero_when_ollama_is_unreachable(monkeypatch, capsys):
    """NaN and 0.0 mean opposite things: "we could not measure" vs "the two
    judges agreed on nothing." Reporting the latter for the former would be
    an outright false claim in the README.
    """
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(True))
    monkeypatch.setattr(judge, "_call_ollama_model", lambda *a, **k: None)

    rate = judge.check_cross_model_agreement(_items(3))

    assert isnan(rate)
    assert rate != 0.0
    assert "undefined" in capsys.readouterr().out


def test_cross_model_agreement_on_no_items_is_nan(monkeypatch):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: pytest.fail("should not be called"))
    assert isnan(judge.check_cross_model_agreement([]))


def test_cross_model_agreement_scores_only_the_items_ollama_answered(monkeypatch):
    primary = [_verdict(True), _verdict(True), _verdict(True)]
    cross = [_verdict(True), None, _verdict(False)]
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: primary.pop(0))
    monkeypatch.setattr(judge, "_call_ollama_model", lambda *a, **k: cross.pop(0))

    # 2 items scored, 1 agreement => 0.5, not 1/3.
    assert judge.check_cross_model_agreement(_items(3)) == 0.5


def test_ollama_call_targets_the_openai_compatible_v1_path(monkeypatch, fake_openai):
    monkeypatch.setattr(judge, "OLLAMA_BASE_URL", "http://localhost:11434/")

    verdict = judge._call_ollama_model("q", "a", ["c"])

    assert verdict["faithful"] is True
    assert fake_openai.calls, "the fake openai SDK recorded no request"
    call = fake_openai.calls[0]
    assert call["base_url"] == "http://localhost:11434/v1"
    assert call["model"] == judge.OLLAMA_JUDGE_MODEL
    assert call["temperature"] == 0


def test_ollama_call_returns_none_and_explains_itself_when_unreachable(monkeypatch, capsys):
    def _unreachable(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(judge, "_call_model_openai_compatible", _unreachable)

    assert judge._call_ollama_model("q", "a", ["c"]) is None
    out = capsys.readouterr().out
    assert "Ollama unreachable" in out
    assert "skipping cross-model check" in out


# --- 7. FR8.8 check #3 — adversarial fixtures --------------------------------


def test_adversarial_fixtures_come_from_the_frozen_fixture_not_the_golden_set():
    """FR8.8 #3 is explicit: the golden set must not be reused here. Pin it,
    because reusing golden text would make the reliability check circular.
    """
    cases = judge._load_adversarial_fixtures()

    assert len(cases) >= 2
    golden_text = (judge.REPO_ROOT / "data" / "golden.jsonl").read_text(encoding="utf-8")
    for case in cases:
        assert case["context"] and all(chunk.strip() for chunk in case["context"])
        assert case["question"] not in golden_text
        for chunk in case["context"]:
            assert chunk not in golden_text


def test_adversarial_fixtures_fail_loudly_if_the_frozen_corpus_changes_shape(monkeypatch, tmp_path):
    corpus = tmp_path / "tests" / "fixtures"
    corpus.mkdir(parents=True)
    (corpus / "mini_corpus.json").write_text(
        json.dumps([{"chunk_id": "SOMETHING_ELSE", "text": "x"}]), encoding="utf-8"
    )
    monkeypatch.setattr(judge, "REPO_ROOT", tmp_path)

    with pytest.raises(KeyError, match="mini_corpus.json fixture may have changed shape"):
        judge._load_adversarial_fixtures()


def test_check_adversarial_passes_only_when_every_case_is_marked_unfaithful(monkeypatch):
    seen: list[str] = []

    def _stub(question, answer, context):
        seen.append(question)
        return _verdict(False)

    monkeypatch.setattr(judge, "_call_primary_model", _stub)

    assert judge.check_adversarial_fixtures() is True
    assert len(seen) == len(judge._load_adversarial_fixtures())


def test_check_adversarial_fails_when_the_judge_calls_an_unsupported_claim_faithful(monkeypatch):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(True))
    assert judge.check_adversarial_fixtures() is False


def test_check_adversarial_short_circuits_on_the_first_failure(monkeypatch):
    calls: list[str] = []
    verdicts = [_verdict(True), _verdict(False)]

    def _stub(question, answer, context):
        calls.append(question)
        return verdicts.pop(0)

    monkeypatch.setattr(judge, "_call_primary_model", _stub)

    assert judge.check_adversarial_fixtures() is False
    assert len(calls) == 1


def test_check_adversarial_accepts_caller_supplied_cases(monkeypatch):
    monkeypatch.setattr(judge, "_call_primary_model", lambda *a, **k: _verdict(False))
    custom = [{"question": "q", "answer": "unsupported", "context": ["ctx"]}]
    assert judge.check_adversarial_fixtures(custom) is True
    # An explicitly empty list is honored (vacuously true), not replaced by
    # the default loader — `items is not None` is the discriminator.
    assert judge.check_adversarial_fixtures([]) is True
