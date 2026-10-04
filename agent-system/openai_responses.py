"""Minimal OpenAI Responses API boundary for the orchestrator."""

from __future__ import annotations

import json
import os
from typing import Any


DEFAULT_MODEL = "gpt-6-luna"


class MissingOpenAIKeyError(RuntimeError):
    """Raised when the OpenAI API key is not present in the environment."""


def require_openai_api_key() -> str:
    """Return the configured API key without logging or storing it."""

    value = os.environ.get("OPENAI_API_KEY")
    if not value:
        raise MissingOpenAIKeyError("OPENAI_API_KEY is required but was not found in the environment")
    return value


def _extract_text(response: Any) -> str:
    if hasattr(response, "output_text") and isinstance(response.output_text, str):
        return response.output_text.strip()

    output = getattr(response, "output", None)
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
        if chunks:
            return "\n".join(chunks).strip()

    raise ValueError("OpenAI response did not contain text output")


def call_openai_text(
    *,
    instructions: str,
    user_input: str,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = 700,
    text_format: dict[str, Any] | None = None,
) -> str:
    """Call the Responses API and return output text."""

    require_openai_api_key()
    from openai import OpenAI  # Imported only after the key check.

    client = OpenAI()
    request: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": user_input,
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_output_tokens,
    }
    if text_format is not None:
        request["text"] = {"format": text_format}

    response = client.responses.create(**request)
    return _extract_text(response)


def call_openai_json(
    *,
    instructions: str,
    user_input: str,
    schema: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = 700,
) -> dict[str, Any]:
    """Call the Responses API and parse a JSON object from the model output."""

    text = call_openai_text(
        instructions=instructions,
        user_input=user_input,
        model=model,
        max_output_tokens=max_output_tokens,
        text_format={
            "type": "json_schema",
            "name": "test_001_boss_task",
            "schema": schema,
            "strict": True,
        },
    )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenAI response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("OpenAI response JSON must be an object")
    return value
