"""Thin, provider-agnostic LLM client.

Anything that speaks the OpenAI chat-completions protocol works (OpenRouter, OpenAI, DeepSeek, vLLM...),
so switching models is a config change, not a rewrite. `FakeLLM` keeps tests and the no-key demo offline.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self.prompt_tokens * settings.LLM_PRICE_INPUT_PER_M
            + self.completion_tokens * settings.LLM_PRICE_OUTPUT_PER_M
        ) / 1_000_000


@dataclass
class Delta:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, as the model produced it


@dataclass
class Done:
    usage: Usage
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)


Event = Delta | Done


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=45, max_retries=1)
        self.model = model

    def stream(self, messages: list[dict], tools: list[dict] | None = None) -> Iterator[Event]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            stream=True,
            stream_options={"include_usage": True},
            temperature=0.2,
        )
        usage = Usage()
        model = self.model
        calls: dict[int, ToolCall] = {}  # tool calls arrive in fragments, keyed by index
        for chunk in response:
            if chunk.usage:
                usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
            model = chunk.model or model
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield Delta(delta.content)
            for fragment in delta.tool_calls or []:
                call = calls.setdefault(fragment.index, ToolCall(id="", name="", arguments=""))
                call.id = fragment.id or call.id
                if fragment.function:
                    call.name += fragment.function.name or ""
                    call.arguments += fragment.function.arguments or ""
        yield Done(usage=usage, model=model, tool_calls=[calls[i] for i in sorted(calls)])


class FakeLLM:
    """Answers with the first sentence of the first source, or admits it doesn't know. Deterministic."""

    model = "fake"

    def stream(self, messages: list[dict], tools: list[dict] | None = None) -> Iterator[Event]:
        sources = messages[0]["content"].split("Sources:\n", 1)[-1]
        # Source block layout: "[1] Title (url)\nTitle\n\nFirst paragraph..."
        match = re.match(r"\[1\] [^\n]*\n[^\n]*\n\n([^\n]+)", sources)
        if match:
            sentence = re.split(r"(?<=[.!?])\s", match.group(1).strip())[0]
            answer = f"{sentence} [1]"
        else:
            answer = "I don't know based on our help center. Please call the clinic at (555) 014-7788."
        for word in answer.split(" "):
            yield Delta(word + " ")
        prompt_tokens = sum(len(m["content"].split()) for m in messages)
        yield Done(usage=Usage(prompt_tokens, len(answer.split())), model=self.model)


@lru_cache(maxsize=1)
def get_llm():
    if settings.LLM_PROVIDER == "fake":
        return FakeLLM()
    if not settings.LLM_API_KEY:
        if not settings.DEBUG:
            # In production a missing key would quietly serve canned answers that look like a broken assistant.
            raise ImproperlyConfigured("Set LLM_API_KEY, or LLM_PROVIDER=fake to run without a model.")
        return FakeLLM()  # local development without a key
    return OpenAICompatibleLLM(settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL)
