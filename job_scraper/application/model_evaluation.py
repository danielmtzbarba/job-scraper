"""One audited Vertex call, shared by classification and fit scoring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel
import structlog

from job_scraper.integrations.vertex_ai import ModelResponse
from job_scraper.models.jobs import JobMirrorRecord
from job_scraper.models.profiles import ScoringProfile
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository

Output = TypeVar("Output", bound=BaseModel)
logger = structlog.get_logger(__name__)


class StructuredModel(Protocol):
    def generate(self, prompt: str, schema: type[Output]) -> ModelResponse: ...


@dataclass(frozen=True)
class ScoringSettings:
    project_id: str
    location: str
    model_id: str
    input_price_per_million: float
    output_price_per_million: float

    def __post_init__(self) -> None:
        if not all((self.project_id.strip(), self.location.strip(), self.model_id.strip())):
            raise ValueError("GCP project, location, and model are required")
        if any(
            not math.isfinite(price) or price <= 0
            for price in (self.input_price_per_million, self.output_price_per_million)
        ):
            raise ValueError("Positive input and output token prices are required")


@dataclass(frozen=True)
class EvaluationOutcome(Generic[Output]):
    result: Output
    evaluation_run_id: str
    response: ModelResponse
    estimated_cost_usd: float | None


class ModelEvaluator:
    def __init__(
        self, repository: SQLiteJobRepository, model: StructuredModel, settings: ScoringSettings
    ) -> None:
        self.repository = repository
        self.model = model
        self.settings = settings

    def call(
        self,
        job: JobMirrorRecord,
        *,
        batch_id: str,
        stage: str,
        prompt: str,
        schema: type[Output],
        classifier_prompt_version: str | None = None,
        profile: ScoringProfile | None = None,
        rubric_version: str | None = None,
        prompt_version: str | None = None,
    ) -> EvaluationOutcome[Output]:
        settings = self.settings
        run_id = self.repository.start_evaluation_run(
            batch_id=batch_id,
            source=job.source,
            deduplication_key=job.deduplication_key,
            stage=stage,
            model_id=settings.model_id,
            project_id=settings.project_id,
            location=settings.location,
            classifier_prompt_version=classifier_prompt_version,
            profile_id=str(profile.id) if profile else None,
            profile_version=profile.version if profile else None,
            rubric_version=rubric_version,
            prompt_version=prompt_version,
            input_price_per_million=settings.input_price_per_million,
            output_price_per_million=settings.output_price_per_million,
        )
        response: ModelResponse | None = None
        try:
            response = self.model.generate(prompt, schema)
            if not response.text:
                raise ValueError("Model returned no text")
            result = schema.model_validate_json(response.text)
        except Exception as exc:
            estimated_cost_usd = _estimated_cost(response, settings) if response else None
            self.repository.finish_evaluation_run(
                run_id,
                status="Failed",
                prompt_tokens=response.prompt_tokens if response else None,
                candidate_tokens=response.candidate_tokens if response else None,
                thought_tokens=response.thought_tokens if response else None,
                estimated_cost_usd=estimated_cost_usd,
                error_type=type(exc).__name__,
            )
            logger.warning(
                "model_evaluation_failed",
                stage=stage,
                source_job_id=job.source_job_id,
                evaluation_run_id=run_id,
                prompt_tokens=response.prompt_tokens if response else None,
                candidate_tokens=response.candidate_tokens if response else None,
                thought_tokens=response.thought_tokens if response else None,
                estimated_cost_usd=estimated_cost_usd,
                error_type=type(exc).__name__,
            )
            raise
        estimated_cost_usd = _estimated_cost(response, settings)
        self.repository.finish_evaluation_run(
            run_id,
            status="Completed",
            prompt_tokens=response.prompt_tokens,
            candidate_tokens=response.candidate_tokens,
            thought_tokens=response.thought_tokens,
            estimated_cost_usd=estimated_cost_usd,
            result=result.model_dump(mode="json"),
        )
        return EvaluationOutcome(result, run_id, response, estimated_cost_usd)


def _estimated_cost(response: ModelResponse, settings: ScoringSettings) -> float | None:
    if response.prompt_tokens is None or response.candidate_tokens is None:
        return None
    output_tokens = response.candidate_tokens + (response.thought_tokens or 0)
    return round(
        (response.prompt_tokens * settings.input_price_per_million
         + output_tokens * settings.output_price_per_million) / 1_000_000,
        8,
    )
