"""Small Vertex AI boundary for structured Gemini responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel


@dataclass(frozen=True)
class ModelResponse:
    text: str | None
    prompt_tokens: int | None
    candidate_tokens: int | None
    thought_tokens: int | None


ResponseSchema = TypeVar("ResponseSchema", bound=BaseModel)


class VertexModelClient:
    """Uses ADC and an explicitly selected GCP project, location, and model."""

    def __init__(self, *, project_id: str, location: str, model_id: str) -> None:
        self.model_id = model_id
        self._client = genai.Client(
            enterprise=True,
            project=project_id,
            location=location,
            http_options=types.HttpOptions(
                api_version="v1",
                timeout=60_000,
                retry_options=types.HttpRetryOptions(
                    attempts=3,
                    initial_delay=1,
                    max_delay=5,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
            ),
        )

    def generate(self, prompt: str, schema: type[ResponseSchema]) -> ModelResponse:
        response = self._client.models.generate_content(
            model=self.model_id,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0,
            ),
        )
        usage = response.usage_metadata
        return ModelResponse(
            text=response.text,
            prompt_tokens=usage.prompt_token_count if usage else None,
            candidate_tokens=usage.candidates_token_count if usage else None,
            thought_tokens=usage.thoughts_token_count if usage else None,
        )

    def close(self) -> None:
        self._client.close()
