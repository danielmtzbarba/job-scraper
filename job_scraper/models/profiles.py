"""Validated, self-contained career profiles used for JD fit scoring."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfileId(StrEnum):
    SWE = "swe"
    APPLIED_AI_FDE = "applied_ai_fde"
    AI_ENGINEER = "ai_engineer"
    BACKEND = "backend"
    PLATFORM_DEVOPS = "platform_devops"


class ProfileEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    setting: Literal["professional", "research", "personal_project"]
    context: str = Field(min_length=1)
    timeframe: str | None = None
    contribution: str = Field(min_length=1)
    outcome: str | None = None
    proof: Literal["corroborated", "documented", "self_reported"]
    source_refs: list[str] = Field(min_length=1)


class ProfileSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    level: Literal["demonstrated", "working", "familiar"]
    evidence_ids: list[str] = Field(min_length=1)


class ScoringProfile(BaseModel):
    """One complete profile; scoring never needs to load its source dossier."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: ProfileId
    version: int = Field(ge=1)
    status: Literal["draft", "reviewed", "active"] = "draft"
    headline: str = Field(min_length=1)
    focus: list[str] = Field(min_length=1)
    skills: list[ProfileSkill] = Field(min_length=1)
    evidence: list[ProfileEvidence] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_links(self) -> ScoringProfile:
        ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("profile evidence IDs must be unique")
        known_ids = set(ids)
        skill_names = [item.name.casefold() for item in self.skills]
        if len(skill_names) != len(set(skill_names)):
            raise ValueError("profile skill names must be unique")
        for skill in self.skills:
            missing = set(skill.evidence_ids) - known_ids
            if missing:
                raise ValueError(f"{skill.name} references missing evidence: {sorted(missing)}")
        return self
