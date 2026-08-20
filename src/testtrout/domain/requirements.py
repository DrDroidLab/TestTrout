"""What a deployment needs before it can be tested, and what is possible without it.

Two ideas, and the second is the one that matters.

**Requirements** are discovered, not asked for. The scan already reads every
line of the application; the environment variables it reaches for are right
there in the source. Asking a developer to remember which keys their app needs
is asking them to re-derive something the code states plainly.

**Capabilities degrade.** A partial set of credentials should produce a partial
suite, not an error. Given only a URL you can still test public pages; add an
anon key and API tests work; add a second user and authorization tests become
possible. Each capability names exactly what it is missing, so the next step is
always one concrete thing rather than "configure it properly".
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


class Capability(StrEnum):
    """Something the tool can do, given enough configuration."""

    PROBE = "probe"
    """Load the deployment in a browser and record what it does."""
    BROWSER_TESTS = "browser_tests"
    """Drive the interface with Playwright."""
    API_TESTS = "api_tests"
    """Call endpoints and Supabase directly, without a browser."""
    AUTHORIZATION_TESTS = "authorization_tests"
    """Prove one user cannot reach another's data. Needs two accounts."""
    MODEL_FEATURES = "model_features"
    """Intent capture and scenario enrichment."""

    @property
    def label(self) -> str:
        """Human-readable name."""
        return self.value.replace("_", " ")


class Requirement(BaseModel):
    """One thing the deployment needs, discovered from the code."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(description="Environment variable name, or a logical name.")
    kind: RequirementKind = RequirementKind.OTHER
    purpose: str = ""
    enables: list[Capability] = Field(default_factory=list)
    locations: list[SourceLocation] = Field(default_factory=list)
    detected_from: str = Field(default="", description="How this was found, for auditability.")
    optional: bool = False

    @property
    def secret(self) -> bool:
        """Whether this value belongs in ``.env`` rather than committed config."""
        return self.kind.secret


class Readiness(BaseModel):
    """Whether one capability is usable, and what is missing if not."""

    model_config = ConfigDict(extra="forbid")

    capability: Capability
    ready: bool
    missing: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def next_step(self) -> str:
        """The single most useful thing to do next."""
        if self.ready:
            return ""
        return self.missing[0] if self.missing else self.detail


class Plan(BaseModel):
    """What is possible right now, and what each further step would unlock.

    The honest answer to "I have some of the credentials" — rather than
    refusing until everything is present.
    """

    model_config = ConfigDict(extra="forbid")

    requirements: list[Requirement] = Field(default_factory=list)
    readiness: list[Readiness] = Field(default_factory=list)

    def can(self, capability: Capability) -> bool:
        """Whether a capability is usable."""
        return any(r.capability is capability and r.ready for r in self.readiness)

    @property
    def available(self) -> list[Capability]:
        """Everything currently possible."""
        return [r.capability for r in self.readiness if r.ready]

    @property
    def blocked(self) -> list[Readiness]:
        """Everything not yet possible, with what it needs."""
        return [r for r in self.readiness if not r.ready]

    def required(self) -> list[Requirement]:
        """Requirements that are not optional.

        Optional ones — third-party keys, the model key — are real but do not
        stop anything important, and listing them alongside blockers buries the
        signal.
        """
        return [r for r in self.requirements if not r.optional]
