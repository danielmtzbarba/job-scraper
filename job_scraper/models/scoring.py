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


class ProfileClassification(BaseModel):
    """Route a JD to one reviewed profile, or skip it as outside target roles."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["profile", "out_of_scope"]
    profile_id: ProfileId | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def profile_matches_decision(self) -> ProfileClassification:
        if not self.reason.strip():
            raise ValueError("classification requires a meaningful reason")
        if (self.decision == "profile") != (self.profile_id is not None):
            raise ValueError("profile decisions require one profile_id; out_of_scope requires none")
        return self


class ModelFitDecision(BaseModel):
    """Small structured response from Gemini before local assessment validation."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["scored", "needs_review"]
    skill_stack_fit: float | None = Field(default=None, ge=0, le=100)
    semantic_experience_fit: float | None = Field(default=None, ge=0, le=100)
    fit_explanation: str | None = Field(default=None, max_length=4000)
    review_reason: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def fields_match_decision(self) -> ModelFitDecision:
        if self.decision == "scored":
            if (self.skill_stack_fit is None or self.semantic_experience_fit is None
                    or not self.fit_explanation or not self.fit_explanation.strip()):
                raise ValueError("scored decisions require both scores and an explanation")
            if self.review_reason is not None:
                raise ValueError("scored decisions cannot include a review reason")
        else:
            if not self.review_reason or not self.review_reason.strip():
                raise ValueError("needs_review decisions require a reason")
            if any(value is not None for value in (self.skill_stack_fit, self.semantic_experience_fit, self.fit_explanation)):
                raise ValueError("needs_review decisions cannot include scores or an explanation")
        return self
