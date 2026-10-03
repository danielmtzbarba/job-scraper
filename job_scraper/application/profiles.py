"""Load private, versioned scoring profiles from local JSON files."""

from __future__ import annotations

import json
from pathlib import Path

from job_scraper.models.profiles import ProfileId, ScoringProfile


class ProfileStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, profile_id: ProfileId) -> ScoringProfile:
        current = json.loads((self.directory / "current.json").read_text(encoding="utf-8"))
        version = current[str(profile_id)]
        if type(version) is not int or version < 1:
            raise ValueError(f"invalid current version for {profile_id}")
        path = self.directory / str(profile_id) / f"v{version}.json"
        profile = ScoringProfile.model_validate_json(path.read_text(encoding="utf-8"))
        if profile.id != profile_id or profile.version != version:
            raise ValueError(f"profile ID/version in {path} does not match its path")
        return profile

    def list(self) -> list[ScoringProfile]:
        return [self.get(profile_id) for profile_id in ProfileId]
