"""Every provider in `src.llm._OPENAI_COMPATIBLE` must be supported at every
dispatch point -- not just at the ones someone remembered to update.

Why this file exists
--------------------
`src/llm.py` was refactored from a hardcoded Groq client into a provider
table so a second OpenAI-compatible provider could be added by config alone.
Two dispatch points were missed and left comparing `== "groq"`:
`tool_specs()` and `LLMSession.__init__()`. With `LLM_PROVIDER=gemini` the
app could not serve a single request -- both raised
`ValueError: Unsupported provider 'gemini'`.

The whole suite still reported `261 passed`.

It passed because every other test names its provider explicitly
(`provider="groq"` / `"anthropic"`), so nothing anywhere exercised the
*configured* provider. Green suite, broken application -- the worst failure
mode a test suite has, because it actively reassures you.

These tests are parametrized over the table itself rather than over a literal
list of provider names. Adding a provider therefore adds its own coverage,
and the next missed dispatch point fails here instead of in production.
"""

from __future__ import annotations

import pytest

from src import llm
from src.tool_schemas import TOOL_SCHEMAS

OPENAI_COMPATIBLE_PROVIDERS = sorted(llm._OPENAI_COMPATIBLE)
ALL_PROVIDERS = sorted({*llm._OPENAI_COMPATIBLE, "anthropic"})


class _UnusedClient:
    """`LLMSession.__init__` must not call the provider. If it ever does,
    this raises instead of quietly opening a socket.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"LLMSession touched the client during construction: {name!r}")


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_tool_specs_supports_every_declared_provider(provider: str) -> None:
    specs = llm.tool_specs(TOOL_SCHEMAS, provider=provider)
    assert len(specs) == len(TOOL_SCHEMAS)


@pytest.mark.parametrize("provider", OPENAI_COMPATIBLE_PROVIDERS)
def test_openai_compatible_tool_specs_share_one_wire_shape(provider: str) -> None:
    """The point of the table is that these providers are interchangeable.
    If one ever needs a different tool payload it needs its own branch, and
    this assertion is where that should surface.
    """
    specs = llm.tool_specs(TOOL_SCHEMAS, provider=provider)
    for spec, schema in zip(specs, TOOL_SCHEMAS, strict=True):
        assert spec["type"] == "function"
        assert spec["function"]["name"] == schema["name"]
        assert spec["function"]["parameters"] == schema["input_schema"]


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_llm_session_constructs_for_every_declared_provider(provider: str) -> None:
    session = llm.LLMSession(
        _UnusedClient(),
        system="system prompt",
        question="question text",
        provider=provider,
        model="test-model",
    )
    assert session._messages, f"{provider} produced no initial messages"


@pytest.mark.parametrize("provider", OPENAI_COMPATIBLE_PROVIDERS)
def test_openai_compatible_sessions_open_with_a_system_message(provider: str) -> None:
    """Anthropic carries the system prompt as a top-level `system` argument;
    the OpenAI-compatible providers carry it as the first message. Getting
    this wrong silently drops the system prompt -- the arms would still
    answer, just without their instructions, which no smoke test would catch.
    """
    session = llm.LLMSession(
        _UnusedClient(),
        system="system prompt",
        question="question text",
        provider=provider,
        model="test-model",
    )
    assert session._messages[0] == {"role": "system", "content": "system prompt"}
    assert session._messages[1] == {"role": "user", "content": "question text"}


def test_the_configured_provider_is_itself_fully_supported() -> None:
    """The regression test proper.

    Everything above pins named providers. This one pins whatever
    `LLM_PROVIDER` actually resolved to at import, which is the case that
    was broken while the suite was green.
    """
    llm.tool_specs(TOOL_SCHEMAS)
    session = llm.LLMSession(_UnusedClient(), system="s", question="q")
    assert session._messages
    assert llm.MODEL, "a model must resolve for the configured provider"


@pytest.mark.parametrize("provider", OPENAI_COMPATIBLE_PROVIDERS)
def test_every_openai_compatible_provider_declares_a_key_var_and_base_url(provider: str) -> None:
    key_env, base_url = llm._OPENAI_COMPATIBLE[provider]
    assert key_env.endswith("_API_KEY"), f"{provider} key var {key_env!r} breaks the convention"

    # Plain http is allowed only for loopback: a local runtime like Ollama has
    # no TLS and needs none, since the traffic never leaves the machine. Any
    # provider reached over the network must be https, or a key would cross
    # the wire in the clear.
    if base_url.startswith("http://"):
        host = base_url.removeprefix("http://").split("/")[0].split(":")[0]
        assert host in {"localhost", "127.0.0.1", "host.docker.internal"}, (
            f"{provider} uses plain http against non-loopback host {host!r}; "
            "remote providers must use https"
        )
    else:
        assert base_url.startswith("https://"), f"{provider} base URL must be https, got {base_url!r}"


def test_an_unknown_provider_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="not-a-provider"):
        llm.tool_specs(TOOL_SCHEMAS, provider="not-a-provider")
    with pytest.raises(ValueError, match="not-a-provider"):
        llm.LLMSession(_UnusedClient(), system="s", question="q", provider="not-a-provider")
