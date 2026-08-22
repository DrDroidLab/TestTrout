"""Credentials and configuration the *application* needs, read from its source.

Not to be confused with what TestTrout needs from a person — that is
:mod:`testtrout.domain.fact`. This is the other direction: the variables the
application itself reaches for, discovered by reading the code, so the setup
form can show them with the line they appear on rather than asking someone to
remember.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.location import SourceLocation


class RequirementKind(StrEnum):
    """What a discovered requirement is for."""

    DEPLOYMENT_URL = "deployment_url"
    SUPABASE_URL = "supabase_url"
    SUPABASE_ANON_KEY = "supabase_anon_key"
    SUPABASE_SERVICE_KEY = "supabase_service_key"
    TEST_USER = "test_user"
    THIRD_PARTY_KEY = "third_party_key"
    MODEL_KEY = "model_key"
    OTHER = "other"

    @property
    def secret(self) -> bool:
        """Whether the value must never be written to a committed file."""
        return self not in {
            RequirementKind.DEPLOYMENT_URL,
            RequirementKind.SUPABASE_URL,
            RequirementKind.OTHER,
        }


class Requirement(BaseModel):
    """One thing the deployment needs, discovered from the code."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(description="Environment variable name, or a logical name.")
    kind: RequirementKind = RequirementKind.OTHER
    purpose: str = ""
    locations: list[SourceLocation] = Field(default_factory=list)
    detected_from: str = Field(default="", description="How this was found, for auditability.")
    optional: bool = False

    @property
    def secret(self) -> bool:
        """Whether this value belongs in ``.env`` rather than committed config."""
        return self.kind.secret


__all__ = ["Requirement", "RequirementKind"]
