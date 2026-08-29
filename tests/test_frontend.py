"""Tests for the served front end (Lane L).

What is under test
------------------
1. `GET /` serves the page, and every pre-existing route keeps its behavior.
2. The page is genuinely self-contained — no CDN, no external stylesheet or
   script, and the only host the browser talks to is this service itself.
   That is what makes "one container, one URL" true rather than aspirational.
3. The page references **exactly** the four frozen `Mode` strings from
   `src/schemas.py`. A typo'd mode in the UI would surface to a reviewer as
   a 422 on click, and nothing else in the build would catch it — the modes
   live in an HTML file that neither Pydantic nor mypy reads.
4. The shape `/query` actually returns is the shape the page renders.

No live API call
----------------
`.env` is auto-loaded at `import src` time (src/__init__.py) and holds a
real, working provider key, so an inadequate stub here would spend real
quota instead of failing loudly. The guard is the one already proven in
`tests/test_api_integration.py`, reused rather than re-invented:

- every provider API-key env var `src.llm` could build a client from is
  deleted for the duration of the test, so `src.llm.default_client()` raises
  instead of returning a live client;
- `src.llm.default_client` is replaced with a factory handing out a fake;
- every request-level test asserts the fake actually recorded calls, so a
  stub that failed to take effect fails the test instead of quietly hitting
  the network.

Those helpers are imported from `tests.test_api_integration` on purpose.
Copying them would mean two divergent definitions of "no live call," and the
weaker copy would be the one that silently starts charging the key. In
particular `_install_fake_module` handles the `from src import x` hazard:
that form caches the module as an attribute on the `src` package and
short-circuits `sys.modules`, so `monkeypatch.setitem` alone is ignored.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from src import llm
from src.api import CANNED_CAPACITY_MESSAGE, INDEX_HTML, create_app
from src.schemas import Citation, Mode, QueryResponse, ToolCall
from tests.test_api_integration import (
    FakeLLMClient,
    _all_provider_key_env_vars,
    _fake_facts_lookup,
    _fake_store_search,
    _install_fake_module,
    _stub_provider,
)

STATIC_INDEX = Path(INDEX_HTML)

FROZEN_MODES: set[str] = set(get_args(Mode))

# Any identifier shaped like an arm name. Deliberately broader than the four
# real values so that a typo (`agent_custum`, `baseline_tool`) is *caught* by
# the set comparison rather than skipped by an over-precise pattern.
MODE_TOKEN_RE = re.compile(r"\b(?:baseline|agent)_[a-z0-9_]+\b")

URL_ATTR_RE = re.compile(r"""(?:src|href)\s*=\s*"([^"]*)\"""", re.IGNORECASE)
FETCH_TARGET_RE = re.compile(r"""fetch\(\s*"([^"]+)\"""")


@pytest.fixture(scope="module")
def page_source() -> str:
    return STATIC_INDEX.read_text(encoding="utf-8")


@pytest.fixture
def trace_db(tmp_path) -> str:
    return str(tmp_path / "traces.sqlite")


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLMClient:
    """Replace the provider at the `src.llm` boundary and make a live call
    structurally impossible while the fixture is active.
    """
    for key_env in _all_provider_key_env_vars():
        monkeypatch.delenv(key_env, raising=False)

    provider = _stub_provider()
    fake = FakeLLMClient(provider=provider)

    def _default_client(requested: str | None = None) -> FakeLLMClient:
        fake.client_handouts += 1
        return fake

    monkeypatch.setattr(llm, "PROVIDER", provider)
    monkeypatch.setattr(llm, "MODEL", "stub-model-for-tests")
    monkeypatch.setattr(llm, "default_client", _default_client)
    return fake


@pytest.fixture
def fake_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "src.store", search=_fake_store_search)
    _install_fake_module(monkeypatch, "src.facts", lookup=_fake_facts_lookup)


def _client(**overrides: Any) -> TestClient:
    return TestClient(create_app(**overrides), raise_server_exceptions=False)


# --- GET / serves the page ---------------------------------------------------


def test_root_serves_html(trace_db):
    client = _client(trace_db=trace_db)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert body.lstrip().lower().startswith("<!doctype html>")
    assert "<title>" in body
    assert "</html>" in body.strip()[-16:]


def test_root_body_is_byte_for_byte_the_committed_asset(trace_db, page_source):
    """No templating, no server-side interpolation — what ships is the file
    that was reviewed. That is what makes the static analysis below (mode
    strings, self-containment) a statement about production, not about a
    file that happens to sit on disk.
    """
    assert _client(trace_db=trace_db).get("/").text == page_source


def test_root_is_not_in_the_openapi_contract(trace_db):
    """`/openapi.json` describes the JSON API. The page is a human surface,
    not part of the machine contract.
    """
    schema = _client(trace_db=trace_db).get("/openapi.json").json()

    assert "/" not in schema["paths"]


# --- Existing routes keep working -------------------------------------------


def test_existing_routes_are_unaffected(fake_corpus, trace_db):
    client = _client(trace_db=trace_db)

    assert client.get("/docs").status_code == 200

    schema = client.get("/openapi.json").json()
    assert {"/query", "/healthz", "/stats"} <= set(schema["paths"])

    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["vector_store"] == "reachable"

    stats = client.get("/stats").json()
    assert "corpus" in stats and "operational" in stats


# --- Only the four frozen modes may ship ------------------------------------


def test_page_references_exactly_the_four_frozen_modes(page_source):
    found = set(MODE_TOKEN_RE.findall(page_source))

    assert found == FROZEN_MODES, (
        "the page's arm identifiers must match src.schemas.Mode exactly; "
        f"unknown={sorted(found - FROZEN_MODES)} missing={sorted(FROZEN_MODES - found)}"
    )


@pytest.mark.parametrize("mode", sorted(FROZEN_MODES))
def test_every_frozen_mode_is_runnable_from_the_page(mode, page_source, stub_llm, fake_corpus, trace_db):
    """Each mode string the page offers must be accepted by `/query` — the
    end the reviewer actually experiences when they click an arm.
    """
    assert mode in page_source

    client = _client(trace_db=trace_db, rate_limit_per_min=1000, daily_cap=1000)
    response = client.post("/query", json={"question": "How did revenue change?", "mode": mode})

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == mode
    assert stub_llm.calls, "the fake LLM recorded no requests — the stub did not take effect"


# --- Self-contained: no CDN, no external host -------------------------------


def test_page_loads_nothing_from_an_external_host(page_source):
    external = [
        url
        for url in URL_ATTR_RE.findall(page_source)
        if url.startswith(("http://", "https://", "//"))
    ]

    assert not external, f"page must be self-contained; external references: {external}"


def test_page_has_no_external_script_or_stylesheet_tags(page_source):
    assert not re.search(r"<script[^>]*\ssrc\s*=", page_source, re.IGNORECASE)
    assert not re.search(r'<link[^>]*rel\s*=\s*"stylesheet"', page_source, re.IGNORECASE)


def test_browser_talks_only_to_our_own_query_endpoint(page_source):
    """No key can leak from a page that only ever calls its own origin."""
    assert FETCH_TARGET_RE.findall(page_source) == ["/query"]
    assert not re.search(r"XMLHttpRequest|WebSocket|EventSource", page_source)


def test_page_contains_no_credentials(page_source):
    lowered = page_source.lower()
    for needle in ("api_key", "apikey", "authorization", "sk-", "gsk_", "bearer "):
        assert needle not in lowered, f"credential-shaped string {needle!r} in a client-side asset"


# --- Untrusted text is never interpolated into markup -----------------------


def test_page_never_assigns_untrusted_text_to_markup(page_source):
    """Filing prose and LLM output are untrusted input. The page builds every
    node with createElement/textContent; if `innerHTML` (or a sink like
    `document.write` / `insertAdjacentHTML`) ever appears, this fails.
    """
    for sink in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML", "eval("):
        assert sink not in page_source, f"unsafe DOM sink {sink!r} in the page"

    # And it does use the safe sink, so the check above cannot pass vacuously
    # on a page that renders nothing.
    assert "textContent" in page_source


# --- Responsive + theme-aware ------------------------------------------------


def test_page_is_responsive_and_theme_aware(page_source):
    assert 'name="viewport"' in page_source
    assert "prefers-color-scheme: dark" in page_source
    assert "overflow-x: hidden" in page_source  # body never scrolls sideways
    assert "overflow-x: auto" in page_source  # wide code blocks scroll inside


# --- The page renders exactly what /query returns ---------------------------


def test_query_returns_every_field_the_page_renders(stub_llm, fake_corpus, trace_db):
    """The page reads answer / trace[] / citations[] / latency_ms /
    incomplete / refused. If any of those stopped being populated the UI
    would render blank sections and nothing else in the suite would notice.
    """
    client = _client(trace_db=trace_db, rate_limit_per_min=1000, daily_cap=1000)

    body = client.post(
        "/query", json={"question": "MSFT FY2024 revenue?", "mode": "agent_custom"}
    ).json()

    QueryResponse.model_validate(body)
    assert body["answer"].strip()
    assert body["latency_ms"] > 0
    assert body["incomplete"] is False and body["refused"] is False

    assert body["trace"], "the tool-call trace is the whole point of the comparison view"
    for call in body["trace"]:
        recorded = ToolCall.model_validate(call)
        # Each of the three fields the trace panel prints.
        assert recorded.name and isinstance(recorded.arguments, dict)
        assert recorded.result_summary
        assert recorded.latency_ms >= 0

    assert body["citations"]
    for citation in body["citations"]:
        cited = Citation.model_validate(citation)
        assert cited.ticker and cited.fiscal_year and cited.section and cited.source_url

    assert stub_llm.calls, "the fake LLM recorded no requests — the stub did not take effect"


# --- The two states the page must not render as a stack trace ---------------


def test_rate_limit_surfaces_a_human_message_not_a_stack_trace(trace_db):
    """A 429 is what a reviewer hits by clicking too fast. It must arrive as
    a typed body the page can turn into a sentence.
    """

    def _never_called(question: str) -> QueryResponse:  # pragma: no cover - must not run
        raise AssertionError("a rate-limited request reached an arm")

    client = _client(
        dispatch=dict.fromkeys(FROZEN_MODES, _never_called),
        rate_limit_per_min=0,
        daily_cap=1000,
        trace_db=trace_db,
    )

    response = client.post("/query", json={"question": "q", "mode": "agent_custom"})

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "http_error"
    assert "rate limit" in body["detail"].lower()
    assert "Traceback" not in body["detail"]


def test_daily_cap_canned_response_renders_as_a_refusal(monkeypatch, trace_db):
    """Over the cap, `/query` degrades to a canned 200. The page shows it via
    the same refusal path as a genuine refusal, so it must carry a
    human-readable `answer` and `refused: true` rather than an empty body.
    """
    monkeypatch.setenv("DAILY_REQUEST_CAP", "1")

    def _canned(question: str) -> QueryResponse:
        return QueryResponse(
            answer="ok",
            citations=[],
            trace=[],
            latency_ms=1.0,
            mode="agent_custom",
            incomplete=False,
            refused=False,
        )

    client = _client(
        dispatch=dict.fromkeys(FROZEN_MODES, _canned),
        rate_limit_per_min=1000,
        trace_db=trace_db,
    )

    assert client.post("/query", json={"question": "q1", "mode": "agent_custom"}).status_code == 200
    over = client.post("/query", json={"question": "q2", "mode": "agent_custom"})

    assert over.status_code == 200
    body = over.json()
    assert body["refused"] is True
    assert body["answer"] == CANNED_CAPACITY_MESSAGE
    assert body["answer"].strip()


# --- The asset ships in the image -------------------------------------------


def test_static_asset_lives_where_the_dockerfile_copies_it():
    """The Dockerfile does `COPY src/ ./src/` and nothing else for assets, so
    the page must live under src/ or `GET /` 404s in production while passing
    locally.
    """
    repo_root = Path(__file__).resolve().parents[1]

    assert STATIC_INDEX.is_file()
    assert STATIC_INDEX.parent.parent == repo_root / "src"

    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY src/ ./src/" in dockerfile
