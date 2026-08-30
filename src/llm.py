"""src/llm.py — provider-agnostic chat + tool-calling adapter (Lane E).

Owns: this file, exclusively.

Two wire formats — the Anthropic Messages API and the OpenAI-compatible
Chat Completions API Groq serves — are normalized behind one interface so
`src.agent._run_tool_arm` and `src.baseline.run_baseline_rag` never branch
on provider. Selected once, at import time, via `LLM_PROVIDER` (`groq` |
`anthropic`, default `groq`). **All four arms share this one pinned
provider/model** (`PROVIDER`, `MODEL` below) — a difference between arms
would confound the entire four-arm comparison the project measures.

Groq model default correction
------------------------------
The requested default, `llama-3.3-70b-versatile`, is **not used**. Verified
against Groq's live docs on 2026-08-29:

- https://console.groq.com/docs/deprecations — llama-3.3-70b-versatile's
  shutdown date (08/16/26) has already passed as of this check; Groq's
  documented replacement is `openai/gpt-oss-120b` (or `qwen/qwen3.6-27b`).
- https://console.groq.com/docs/models — `openai/gpt-oss-120b` is a
  current production model (500 tok/s, $0.15/$0.60 per MTok on paid
  plans).
- https://console.groq.com/docs/rate-limits — `openai/gpt-oss-120b` is
  available on the **Free** plan (30 RPM / 8K TPM / 200K TPD as of this
  check).

Default is therefore `openai/gpt-oss-120b`. Override via `LLM_MODEL` if
Groq's lineup changes again — do not hardcode trust in either string.

Wire-format differences this module hides (see class `LLMSession`):

| | Anthropic | OpenAI-compatible (Groq) |
|---|---|---|
| Tool requested | `stop_reason == "tool_use"` | `finish_reason == "tool_calls"` |
| Where the call lives | `content` blocks, `type: "tool_use"` (`.id`, `.name`, `.input`) | `message.tool_calls[]` (`.id`, `.function.name`, `.function.arguments` as a JSON string) |
| Sending results back | one `user` message with all `tool_result` blocks, keyed by `tool_use_id` | one `role: "tool"` message per call, keyed by `tool_call_id` |
| Tool schema shape | `{name, description, input_schema}` | `{type: "function", function: {name, description, parameters}}` |
| One-call-only request hint | `tool_choice: {type: "auto", disable_parallel_tool_use: true}` | `parallel_tool_calls: false` |

`src/tool_schemas.py` is frozen (Wave 0) and is never edited here — see
`tool_specs()`, which translates it into whichever shape the active
provider needs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

_DEFAULT_MODELS: dict[str, str] = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-opus-5",
}

# Providers reachable through the OpenAI SDK by overriding `base_url`. Every
# entry shares one wire format and one code path (`_send_openai_compatible`),
# so adding a provider is a table entry rather than a new branch.
#
# Anthropic is deliberately absent: it has a genuinely different wire format
# (`tool_use` content blocks rather than `tool_calls`) and keeps its own path.
_OPENAI_COMPATIBLE: dict[str, tuple[str, str]] = {
    # provider: (api-key env var, default base URL)
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
}

PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
if PROVIDER not in _DEFAULT_MODELS:
    raise ValueError(f"Unsupported LLM_PROVIDER {PROVIDER!r}; expected one of {sorted(_DEFAULT_MODELS)}")

# Pinned once, here, and shared by every arm (baseline_rag, baseline_tools,
# agent_custom) — see module docstring. No temperature/top_p/top_k on
# either path: Anthropic 400s on non-default sampling params, and Groq's
# OpenAI-compatible endpoint accepts them — but arms must be identical
# across providers, so neither path sets them.
MODEL = os.environ.get("LLM_MODEL") or _DEFAULT_MODELS[PROVIDER]

MAX_OUTPUT_TOKENS = 8192

# Resolved from the provider table, with `LLM_BASE_URL` as an escape hatch for
# any other OpenAI-compatible endpoint (a self-hosted vLLM, Ollama's /v1, a
# proxy) without touching this file. Empty for Anthropic, which ignores it.
BASE_URL = os.environ.get("LLM_BASE_URL") or (_OPENAI_COMPATIBLE.get(PROVIDER, ("", ""))[1])


# --- Normalized shapes -------------------------------------------------------


@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class NormalizedResponse:
    """The one internal shape `_run_tool_arm` / `run_baseline_rag` see —
    they never branch on provider or inspect a raw SDK response.
    """

    text: str
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


# --- Client construction -----------------------------------------------------


def default_client(provider: str | None = None) -> Any:
    """The real SDK client for `provider` (default: the pinned PROVIDER),
    constructed lazily so tests that always inject a fake client never
    need a live API key and never touch the network.
    """
    provider = provider or PROVIDER
    if provider == "anthropic":
        import anthropic

        return anthropic.Anthropic()
    if provider in _OPENAI_COMPATIBLE:
        import openai

        key_env, default_base = _OPENAI_COMPATIBLE[provider]
        api_key = os.environ.get(key_env)
        if not api_key:
            raise RuntimeError(
                f"{key_env} is not set, which {provider!r} requires. Copy .env.example "
                f"to .env and fill it in — see the provider section there."
            )
        base_url = BASE_URL if provider == PROVIDER else default_base
        return openai.OpenAI(base_url=base_url, api_key=api_key)
    raise ValueError(f"Unsupported provider {provider!r}")


# --- Tool schema translation --------------------------------------------------


def tool_specs(schemas: list[dict[str, Any]], provider: str | None = None) -> list[dict[str, Any]]:
    """Translate `src.tool_schemas.TOOL_SCHEMAS` (Anthropic shape:
    `{name, description, input_schema}`) into whichever wire shape the
    active provider needs. `tool_schemas.py` itself is never edited
    (frozen, Wave 0 owns it) — this is a pure read-only translation.
    """
    provider = provider or PROVIDER
    if provider == "anthropic":
        return list(schemas)
    if provider in _OPENAI_COMPATIBLE:
        return [
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["input_schema"],
                },
            }
            for schema in schemas
        ]
    raise ValueError(f"Unsupported provider {provider!r}")


# --- Conversation session -----------------------------------------------------


class LLMSession:
    """Owns one conversation's message history in whichever provider
    -native format `provider` requires, and exposes only normalized
    responses (`NormalizedResponse`). This is what makes the arms'
    tool-calling loop provider-agnostic: `_run_tool_arm` calls `.send()`
    and `.add_tool_result()` and never touches `content` blocks, `.input`
    JSON strings, or role="tool" vs. tool_result blocks directly.
    """

    def __init__(
        self,
        client: Any,
        *,
        system: str,
        question: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._provider = provider or PROVIDER
        self._model = model or MODEL
        self._system = system
        if self._provider == "anthropic":
            self._messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        elif self._provider in _OPENAI_COMPATIBLE:
            self._messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ]
        else:
            raise ValueError(f"Unsupported provider {self._provider!r}")

    def send(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        disable_parallel_tool_use: bool = False,
    ) -> NormalizedResponse:
        """Send the current conversation (+ optional tool schemas),
        append the raw assistant turn to history in the provider's native
        shape, and return a normalized response.
        """
        if self._provider == "anthropic":
            return self._send_anthropic(tools, disable_parallel_tool_use)
        return self._send_openai_compatible(tools, disable_parallel_tool_use)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Append one tool's result to history, in whichever shape the
        provider expects (one `user` message with a `tool_result` block
        for Anthropic; one `role: "tool"` message for Groq/OpenAI).
        """
        if self._provider == "anthropic":
            self._messages.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": content}],
                }
            )
        else:
            self._messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    # --- Anthropic wire format ---

    def _send_anthropic(
        self, tools: list[dict[str, Any]] | None, disable_parallel_tool_use: bool
    ) -> NormalizedResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": self._system,
            "messages": self._messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": disable_parallel_tool_use}
        response = self._client.messages.create(**kwargs)
        # Preserve the full content (tool_use blocks included), not just
        # the text, when threading the turn back into history.
        self._messages.append({"role": "assistant", "content": response.content})

        text_parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        tool_calls = [
            NormalizedToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if getattr(b, "type", None) == "tool_use"
        ]
        return NormalizedResponse(text="\n".join(p for p in text_parts if p).strip(), tool_calls=tool_calls)

    # --- OpenAI-compatible (Groq) wire format ---

    def _send_openai_compatible(self, tools: list[dict[str, Any]] | None, disable_parallel_tool_use: bool) -> NormalizedResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": self._messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            if disable_parallel_tool_use:
                kwargs["parallel_tool_calls"] = False
        response = self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls = [
            NormalizedToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments) if tc.function.arguments else {},
            )
            for tc in raw_tool_calls
        ]

        # Append the provider's OWN message, not a hand-rebuilt copy.
        #
        # Rebuilding it keeps only `content` and `tool_calls`, silently
        # dropping provider-specific fields. Gemini 3.x attaches a
        # `thought_signature` to each tool call, at
        # `tool_calls[].extra_content.google.thought_signature`, and it MUST
        # be echoed back on the following turn — otherwise the API rejects
        # the request outright:
        #
        #   400 Function call is missing a thought_signature in functionCall
        #   parts. This is required for tools to work correctly.
        #
        # The failure is invisible in single-shot tool calling, because
        # nothing is ever sent back. It only appears on the second request of
        # a conversation, which is why `baseline_rag` worked in production
        # while all three tool-using arms returned 500.
        #
        # `exclude_none=True` drops the nulls providers pad responses with
        # (`refusal`, `audio`, ...) that some reject on echo-back, while
        # preserving anything they actually populated.
        #
        # The fallback reconstructs the message for test doubles that are
        # plain objects rather than SDK models. It is deliberately the
        # fallback and not the default: a hand-built dict is what caused the
        # production failure above, and it passed every unit test, because a
        # fake cannot drop a field it never had.
        dump = getattr(message, "model_dump", None)
        if callable(dump):
            self._messages.append(dump(exclude_none=True))
        else:
            entry: dict[str, Any] = {"role": "assistant", "content": message.content}
            if raw_tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in raw_tool_calls
                ]
            self._messages.append(entry)

        return NormalizedResponse(text=(message.content or "").strip(), tool_calls=tool_calls)
