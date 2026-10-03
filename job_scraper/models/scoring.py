"""Pydantic contracts for AI-generated job fit assessments."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_scraper.models.profiles import ProfileId


class FitAssessment(BaseModel):
    """Validated dimension scores and explanation returned by an AI assistant."""

    model_config = ConfigDict(extra="forbid")

    profile_id: ProfileId
    profile_version: int = Field(ge=1)
    rubric_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    skill_stack_fit: float = Field(ge=0, le=100)
    semantic_experience_fit: float = Field(ge=0, le=100)
    fit_category: Literal["Strong", "Good", "Stretch", "Low"]
    fit_explanation: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def category_matches_scores(self) -> FitAssessment:
        overall = round((self.skill_stack_fit + self.semantic_experience_fit) / 2, 2)
        expected = (
            "Strong" if overall >= 85 else
            "Good" if overall >= 70 else
            "Stretch" if overall >= 50 else
            "Low"
        )
        if self.fit_category != expected:
            raise ValueError(f"fit_category must be {expected} for a {overall:g} overall fit")
        return self


class NeedsReviewAssessment(BaseModel):
    """A JD cannot be scored reliably from the available role information."""

    model_config = ConfigDict(extra="forbid")

    profile_id: ProfileId
    profile_version: int = Field(ge=1)
    rubric_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    review_reason: str = Field(min_length=1, max_length=4000)
