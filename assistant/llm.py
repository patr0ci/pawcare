"""Thin, provider-agnostic LLM client.

Anything that speaks the OpenAI chat-completions protocol works (OpenRouter, OpenAI, DeepSeek, vLLM...),
so switching models is a config change, not a rewrite. `FakeLLM` keeps tests and the no-key demo offline.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings


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
class Done:
    usage: Usage
    model: str


Event = Delta | Done


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=60, max_retries=2)
        self.model = model

    def stream(self, messages: list[dict]) -> Iterator[Event]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            temperature=0.2,
        )
        usage = Usage()
        model = self.model
        for chunk in response:
            if chunk.usage:
                usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
            model = chunk.model or model
            if chunk.choices and chunk.choices[0].delta.content:
                yield Delta(chunk.choices[0].delta.content)
        yield Done(usage=usage, model=model)


class FakeLLM:
    """Answers with the first sentence of the first source, or admits it doesn't know. Deterministic."""

    model = "fake"

    def stream(self, messages: list[dict]) -> Iterator[Event]:
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
    if settings.LLM_PROVIDER == "fake" or not settings.LLM_API_KEY:
        return FakeLLM()
    return OpenAICompatibleLLM(settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL)
